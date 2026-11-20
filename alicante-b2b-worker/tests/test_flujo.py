import json
import os
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from openpyxl import Workbook, load_workbook  # noqa: E402

from b2bworker import cli, db, reglas as R  # noqa: E402
from b2bworker.red import ErrorConsulta  # noqa: E402

HOY = date(2026, 11, 12)
KIT_II = "Convocatoria de ayudas destinadas a la digitalización de empresas del Segmento II (entre 3 y menos de 10 empleados) (Programa Kit Digital)"
KIT_I = "Convocatoria de ayudas destinadas a la digitalización de empresas del Segmento I (entre 10 y menos de 50 empleados) (Programa Kit Digital)"


class ClienteFalso:
    def __init__(self, respuestas, fallar=()):
        self.respuestas, self.fallar, self.urls = respuestas, set(fallar), []

    def json(self, url, cabeceras=None):
        self.urls.append(url)
        cif = url.split("nifCif=")[1]
        if cif in self.fallar:
            raise ErrorConsulta("HTTP 503")
        return {"content": [dict(c, beneficiario=f"{cif} {c.get('nombre', 'EMPRESA')}") for c in self.respuestas.get(cif, [])]}


class Flujo(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        cli.DATOS = Path(self.tmp.name)
        for d in ("entrada", "salida", "logs"):
            (cli.DATOS / d).mkdir()
        self.con = db.abrir(cli.DATOS / "b2b.sqlite")
        wb = Workbook()
        ws = wb.active
        ws.append(["CLI_ID_FISCAL", "OTRA", "CLI_NOMBRE", "CLI_DIRECCION", "CLI_POBLACION", "CLI_CODIGO_POSTAL", "ING_TOTAL", "CLI_INTERLOCUTOR"])
        ws.append(["B12345674", "x", "KIT RECIENTE SL", "C/ A 1", "ELCHE", 3320, 999, "PERSONA SECRETA"])
        ws.append(["A28015865", "x", "CONSTRUCCIONES IVACE SA", "C/ B 2", "ALICANTE/ALACANT", "03001", 1, ""])
        ws.append(["12345678Z", "x", "AUTONOMO", "C/ C 3", "BENIDORM", "03500", 1, ""])
        ws.append(["B87654323", "x", "VIEJA EXTINGUIDA SL", "C/ D 4", "DENIA", "03700", 1, ""])
        ws.append(["J12345674", "x", "COMUNIDAD SIN AYUDAS", "C/ E 5", "VILLENA", "03400", 1, ""])
        self.entrada = cli.DATOS / "entrada" / "cartera.xlsx"
        wb.save(self.entrada)
        self.con.executemany("INSERT INTO borme_actos VALUES (?, ?, ?, ?)", [
            ("2020-01-10", "VIEJA EXTINGUIDA SL", "VIEJA EXTINGUIDA SL", "Disolución. Voluntaria. Extinción."),
            ("2021-03-02", "CONSTRUCCIONES IVACE SA", "CONSTRUCCIONES IVACE SA", "Nombramientos. Adm. Unico: PEREZ LOPEZ ANA."),
        ])
        self.con.execute("INSERT INTO borme_dias VALUES ('2020-01-10', 'ok', 1, 'x')")
        self.respuestas = {
            "B12345674": [{"fechaConcesion": "2026-08-01", "importe": 6000, "convocatoria": KIT_II, "nivel3": "RED.ES"}],
            "A28015865": [{"fechaConcesion": "2023-02-01", "importe": 12000, "convocatoria": KIT_I, "nivel3": "RED.ES"},
                          {"fechaConcesion": "2025-04-01", "importe": 3000, "convocatoria": "Digitalización", "nivel2": "INSTITUT VALENCIÀ DE COMPETITIVITAT EMPRESARIAL"}],
        }

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()

    def registros(self):
        from b2bworker.zonas import Zonas
        z = Zonas()
        return {f["cif"]: cli.clasificar(f, z, HOY) for f in self.con.execute("SELECT * FROM empresas WHERE lote='cartera'")}

    def test_flujo_completo_reanudable(self):
        self.assertTrue(cli.cargar_lote(self.con, "cartera", self.entrada))
        # Solo se guardan las columnas acordadas: nada de ingresos ni interlocutores.
        guardado = json.dumps([dict(r) for r in self.con.execute("SELECT * FROM empresas")])
        self.assertNotIn("PERSONA SECRETA", guardado)
        self.assertNotIn("999", guardado)
        self.assertNotIn("AUTONOMO", guardado, "de la persona física no se guarda el nombre")

        falso = ClienteFalso(self.respuestas, fallar={"J12345674"})
        cli.procesar(self.con, "cartera", cliente=falso, hoy=HOY)
        self.assertEqual(len(falso.urls), 4, "la persona física no se consulta")
        r = self.registros()
        self.assertEqual(r["12345678Z"]["prioridad"], "DESCARTADA")
        self.assertEqual(r["B87654323"]["descarte"], "Sociedad extinguida (BORME)")
        kit = r["B12345674"]
        self.assertEqual((kit["kit_urgente"], kit["segmento"], kit["prioridad"], kit["zona"][:6]), ("SÍ", R.S2, "A", "Zona 1"))
        iv = r["A28015865"]
        self.assertEqual((iv["segmento"], iv["sector_est"], iv["palanca"], iv["ivace"], iv["prioridad"]), (R.S1, R.INDUSTRIAL, R.P_IVACE, 1, "C"))
        self.assertIn("PEREZ LOPEZ ANA", iv["administradores"])
        self.assertIn("2025 · IVACE · 3.000 €", iv["detalle_ayudas"])
        self.assertIn("Red.es", iv["detalle_ayudas"])
        self.assertTrue(r["J12345674"]["consulta"].startswith("error BDNS"))

        # Reanudar: solo se repite lo que falló.
        falso2 = ClienteFalso(self.respuestas)
        cli.procesar(self.con, "cartera", cliente=falso2, hoy=HOY)
        self.assertEqual(len(falso2.urls), 1)
        self.assertFalse(cli.cargar_lote(self.con, "cartera", self.entrada), "el lote existente no se recarga")

        # Excel con las pestañas y fórmulas; el teléfono confirmado sube la prioridad.
        ruta = cli.generar_excel(self.con, "cartera", hoy=HOY)
        wb = load_workbook(ruta)
        self.assertEqual(wb.sheetnames, ["Resumen_Ejecutivo", "Listado_Puerta_Fria", "Prioridad_A_Urgentes", "Ruta_Por_Zonas_Alicante", "Por_Cualificar_Llamada",
                                         "Base_Completa_Enriquecida", "Descartadas_y_Errores"])
        base = wb["Base_Completa_Enriquecida"]
        cab = [c.value for c in base[1]]
        self.assertTrue(str(base.cell(2, cab.index("Prioridad") + 1).value).startswith("=IF("))
        self.assertEqual(base.freeze_panes, "A2")
        self.assertEqual(wb["Prioridad_A_Urgentes"].max_row, 2)
        self.assertIn("Detalle ayudas (últimas)", cab)
        pf = wb["Listado_Puerta_Fria"]
        self.assertEqual(pf.cell(1, 1).value, "Estado TES")
        filas_pf = {r[8]: r[1:] for r in pf.iter_rows(min_row=2, values_only=True)}
        self.assertEqual(set(filas_pf), {"B12345674", "A28015865", "J12345674"}, "solo las consultadas que encajan; nunca descartadas")
        self.assertEqual(filas_pf["J12345674"][0], "Base")
        self.assertEqual([r[1] for r in pf.iter_rows(min_row=2, values_only=True)], ["Alto", "Alto", "Base"], "primero las de mejor encaje")
        kit = filas_pf["B12345674"]
        self.assertEqual((kit[0], kit[5]), ("Alto", "Inmediata"))
        self.assertIn("caduca hacia el 01/02/2027", kit[1])
        self.assertIn("Kit Digital: bono vivo", kit[4])
        self.assertIn("a KIT RECIENTE SL le concedieron el Kit Digital el 01/08/2026", kit[2])
        self.assertTrue(kit[2].startswith("Buenos días, soy César Ruzafa, asesor comercial de empresas en Elche."))
        self.assertTrue(kit[2].endswith("no le llamo más."))
        self.assertIn("¿podría hablar con el responsable o la gerencia?", kit[3])
        iv = filas_pf["A28015865"]
        self.assertEqual(iv[0], "Alto")
        self.assertIn("DIGITALIZA-CV", iv[4], "industrial y 10-49 empleados (por su Kit Digital)")
        self.assertEqual(iv[13], "PEREZ LOPEZ ANA (Adm. Unico, 2021-03-02)")
        self.assertEqual(iv[9], "10-49 (segmento de su Kit Digital)")
        self.assertIn("ya ha trabajado con ayudas del IVACE.", iv[2])
        self.assertIn("¿podría hablar con Perez Lopez Ana?", iv[3])
        # Un Excel con columnas en otro orden (versión anterior) también se puede reimportar.
        vieja = Workbook()
        vs = vieja.active
        vs.title = "Base_Completa_Enriquecida"
        vs.append(["Teléfono ✎", "CIF", "Sector (confirmado) ✎", "Empleados (confirmado) ✎"])
        vs.append(["966 000 000", "B12345674", "Servicios", 7])
        ruta_vieja = cli.DATOS / "salida" / "vieja.xlsx"
        vieja.save(ruta_vieja)
        self.assertEqual(cli.actualizar(self.con, "cartera", ruta_vieja), 1)
        kit = self.registros()["B12345674"]
        self.assertEqual((kit["emp_conf"], kit["sector_conf"], kit["telefono"]), (7, "Servicios", "966 000 000"))
        fila_iv = next(i for i in range(2, base.max_row + 1) if base.cell(i, 1).value == "A28015865")
        base.cell(fila_iv, cab.index("Teléfono ✎") + 1, "965 123 456")
        base.cell(fila_iv, cab.index("Empleados (confirmado) ✎") + 1, 25)
        wb.save(ruta)
        self.assertGreaterEqual(cli.actualizar(self.con, "cartera", ruta), 1)
        iv = self.registros()["A28015865"]
        self.assertEqual((iv["emp_conf"], iv["prioridad"]), (25, "A"))

    def export_tes(self, filas, nombre="tes.xlsx"):
        """Export de campaña de TES con la estructura real: cabecera tras unas filas de filtro y pares título/valor."""
        wb = Workbook()
        ws = wb.active
        ws.append([])
        ws.append([None, "GEST_TES_CAMP_0000000999 PRUEBA"])
        ws.append([None, "Filtrado por"])
        cab = [None, "Nombre de la campaña", "Id Target Salesforce", "Estado", "Cliente: Nombre del cliente", "Cliente: Número de documento"]
        titulos = ["NUMERO_TELEFONO", "URL", "SECTOR ACTIVIDAD", "NÚMERO EMPLEADOS", "VALOR POTENCIAL CLIENTES", "Bono Concedido KD", "FECHA ULTIMA VISITA"]
        for n in range(1, len(titulos) + 1):
            cab += [f"Título columna {n}", f"Valor columna {n}"]
        ws.append(cab)
        for target, estado, cliente, doc, valores in filas:
            fila = [None, "CAPTAR_NO_CLIENTE alta cliente", target, estado, cliente, doc]
            for t, v in zip(titulos, valores):
                fila += [t, v]
            ws.append(fila)
        ws.append([None, "Total", "Recuento", len(filas)])
        ruta = cli.DATOS / "entrada" / nombre
        wb.save(ruta)
        return ruta

    def test_importar_tes(self):
        cli.cargar_lote(self.con, "cartera", self.entrada)
        cli.procesar(self.con, "cartera", cliente=ClienteFalso(self.respuestas), hoy=HOY)
        ruta = self.export_tes([
            ("TGT-1", "Pendiente", "Comunidad", "J12345674", [965111222.0, "WWW.COMUNIDAD.ES", "CONSTRUCCION DE EDIFICIOS RESIDENCIALES", "15", "PLATA", "", "30MAR2025"]),
            ("TGT-2", "Ganada", "Construcciones", "A28015865", ["965333444", "", "COMERCIO AL POR MAYOR DE MADERA, MATERIALES DE CONSTRUCCION", "30", "ORO", "Kit Digital Fase 1", "01JAN1900"]),
            ("TGT-3", "En Gestión", "Kit", "B12345674", ["965555666", "", "RESTAURANTES", "4", "BRONCE", "Kit Digital Fase 2", "01JAN1900"]),
            ("TGT-4", "Pendiente", "Otra", "B99999998", ["965777888", "", "", "", "", "", ""]),
        ])
        r = cli.importar_tes(self.con, "cartera", ruta)
        self.assertEqual((r["campana"], r["targets"], r["en_lote"], r["fuera_lote"], r["empleados"]),
                         ("GEST_TES_CAMP_0000000999 PRUEBA", 4, 3, 1, 3))
        reg = self.registros()
        j = reg["J12345674"]
        self.assertEqual((j["telefono"], j["tel_fuente"], j["tes_estado"], j["tes_target"]), ("965 11 12 22", "TES", "Pendiente", "TGT-1"))
        self.assertEqual((j["emp_est"], j["seg_est"], j["sector_est"], j["web"], j["tes_visita"]),
                         ("15 (TES)", R.S1, R.INDUSTRIAL, "www.comunidad.es", "2025-03-30"))
        self.assertEqual(j["palanca"], R.P_IVACE, "10-49 empleados e industrial según TES")
        self.assertEqual(reg["A28015865"]["sector_est"], R.SERVICIOS, "el comercio de materiales de construcción no es construcción")
        self.assertEqual(reg["A28015865"]["tes_visita"], "", "01JAN1900 es 'sin visita'")
        self.assertEqual(reg["B12345674"]["emp_est"], "4 (TES)", "la plantilla de TES manda sobre el segmento del Kit Digital")

        wb = load_workbook(cli.generar_excel(self.con, "cartera", hoy=HOY))
        pf = [r for r in wb["Listado_Puerta_Fria"].iter_rows(min_row=2, values_only=True)]
        self.assertEqual([(r[0], r[8]) for r in pf], [("En Gestión", "B12345674"), ("Pendiente", "J12345674")],
                         "orden por estado de TES; la ganada queda fuera de las pestañas de trabajo")
        self.assertEqual(pf[0][-1], "TGT-3")
        resumen = {r[0]: r[1] for r in wb["Resumen_Ejecutivo"].iter_rows(values_only=True) if r and r[0]}
        self.assertEqual(resumen["Campaña TES"], "GEST_TES_CAMP_0000000999 PRUEBA")
        self.assertEqual(resumen["  Fuera de la campaña"], 2)
        base = wb["Base_Completa_Enriquecida"]
        cab = [c.value for c in base[1]]
        estados = {base.cell(i, 1).value: base.cell(i, cab.index("Estado TES") + 1).value for i in range(2, base.max_row + 1)}
        self.assertEqual(estados["A28015865"], "Ganada", "en la base completa sigue estando")

        # Email y móvil publicados en su web (solo las que se van a trabajar y tienen web).
        class Web:
            urls = []

            def get(self, url, timeout=30, **_):
                self.urls.append(url)
                if url == "https://www.comunidad.es/":
                    return '<a href="mailto:info@comunidad.es">info</a> Móvil 612 345 678'
                raise ErrorConsulta("HTTP 404")
        web = Web()
        self.assertEqual(cli.buscar_contactos(self.con, "cartera", 50, cliente=web, hoy=HOY), 1)
        j = self.registros()["J12345674"]
        self.assertEqual((j["email_web"], j["movil_web"], j["contacto_fuente"]), ("info@comunidad.es", "612 34 56 78", "https://www.comunidad.es"))
        self.assertEqual(cli.buscar_contactos(self.con, "cartera", 50, cliente=web, hoy=HOY), 0, "no repite las ya revisadas")
        wb = load_workbook(cli.generar_excel(self.con, "cartera", hoy=HOY))
        hpf = [c.value for c in wb["Listado_Puerta_Fria"][1]]
        fila = next(r for r in wb["Listado_Puerta_Fria"].iter_rows(min_row=2, values_only=True) if r[8] == "J12345674")
        self.assertEqual(fila[hpf.index("Email publicado (web)")], "info@comunidad.es")

        # Un teléfono de tu archivo no se pisa; una nueva importación sustituye a la anterior.
        self.con.execute("UPDATE empresas SET telefono='966 00 00 00', tel_fuente='tu archivo' WHERE cif='B12345674'")
        cli.importar_tes(self.con, "cartera", self.export_tes([
            ("TGT-3", "Ofertado no interesa", "Kit", "B12345674", ["965555666", "", "RESTAURANTES", "4", "", "", ""])], "tes2.xlsx"))
        reg = self.registros()
        self.assertEqual((reg["B12345674"]["telefono"], reg["B12345674"]["tes_estado"]), ("966 00 00 00", "Ofertado no interesa"))
        self.assertEqual(reg["J12345674"]["tes_estado"], "", "ya no está en la campaña")

    def test_excluir(self):
        cli.cargar_lote(self.con, "cartera", self.entrada)
        cli.excluir(self.con, "a-28015865", "pidió no ser contactada")
        self.assertEqual(self.registros()["A28015865"]["prioridad"], "DESCARTADA")
        os.remove(self.entrada)


class Fichas(unittest.TestCase):
    def base(self, **kw):
        x = {"prioridad": "C", "segmento": "", "sector_conf": "", "sector_est": "", "kit_urgente": "", "kit_fecha": "", "kit_importe": None,
             "kit_segmento": "", "ivace": 0, "camara": 0, "diputacion": 0, "n_ayudas": 0, "ultima_organismo": None, "importe_3a": 0,
             "emp_conf": None, "seg_est": ""}
        x.update(kw)
        return x

    def test_encaje_y_exclusiones(self):
        from b2bworker.fichas import evaluar
        e = evaluar(self.base(), HOY)
        self.assertEqual((e["encaje"], e["ventana"]), ("Base", "Abril–Mayo 2027"))
        self.assertNotIn("DIGITALIZA", e["ayudas"], "sin indicios de industria o 10+ empleados no se propone")
        self.assertIn("nº de empleados", e["confirmar"])
        e = evaluar(self.base(n_ayudas=3, ultima_organismo="LABORA"), HOY)
        self.assertEqual(e["encaje"], "Medio")
        self.assertIn("la última de LABORA", e["frase"])
        e = evaluar(self.base(segmento=R.S2, seg_est=R.S2, sector_est=R.INDUSTRIAL), HOY)
        self.assertNotIn("DIGITALIZA", e["ayudas"], "3-9 empleados no llega a DIGITALIZA-CV")
        self.assertIsNone(evaluar(self.base(importe_3a=310000), HOY), "supera el límite de minimis")
        self.assertIsNone(evaluar(self.base(prioridad="DESCARTADA"), HOY))
        self.assertIsNone(evaluar(self.base(segmento=R.S250), HOY))


class Reglas(unittest.TestCase):
    def test_palancas_y_prioridades(self):
        self.assertEqual(R.palanca(R.S1, R.INDUSTRIAL), R.P_IVACE)
        self.assertEqual(R.palanca(R.S1, R.SERVICIOS), R.P_CAMARA)
        self.assertEqual(R.palanca(R.S2, ""), R.P_CAMARA)
        self.assertEqual(R.palanca(R.S3, R.INDUSTRIAL), R.P_MICRO)
        self.assertEqual(R.palanca(R.S45, R.SERVICIOS), R.P_45_SERVICIOS)
        self.assertEqual(R.palanca("", ""), R.P_PENDIENTE)
        self.assertEqual(R.prioridad(None, R.S45, False, 0, 0, "965000000"), "B")
        self.assertEqual(R.prioridad(None, R.S3, False, 1, 1, "965000000"), "C")
        self.assertEqual(R.prioridad(None, "", True, 0, 0, ""), "A")
        self.assertEqual(R.prioridad(None, R.S250, True, 0, 0, ""), "DESCARTADA")
        self.assertEqual(R.descarte_mercantil({"estado": "Activa", "constitucion": "2026-07-01"}, HOY), "Menos de 6 meses de antigüedad (BORME)")
        self.assertIsNone(R.descarte_mercantil({"estado": "Activa", "constitucion": "2026-04-01"}, HOY))

    def test_sector_cnae(self):
        self.assertEqual(R.sector_cnae("FABRICACION DE CALZADO"), R.INDUSTRIAL)
        self.assertEqual(R.sector_cnae("TRANSPORTE DE MERCANCIAS POR CARRETERA"), R.INDUSTRIAL)
        self.assertEqual(R.sector_cnae("ACTIVIDADES DE MAMPOSTERIA Y ALBAÑILERIA"), R.INDUSTRIAL)
        self.assertEqual(R.sector_cnae("COMERCIO AL POR MENOR DE FERRETERIA, MATERIALES DE CONSTRUCCION"), R.SERVICIOS)
        self.assertEqual(R.sector_cnae("ACTIVIDADES JURIDICAS"), R.SERVICIOS)
        self.assertEqual(R.sector_cnae(""), "")


if __name__ == "__main__":
    unittest.main()
