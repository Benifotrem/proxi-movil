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
        self.assertTrue(r["J12345674"]["consulta"].startswith("error BDNS"))

        # Reanudar: solo se repite lo que falló.
        falso2 = ClienteFalso(self.respuestas)
        cli.procesar(self.con, "cartera", cliente=falso2, hoy=HOY)
        self.assertEqual(len(falso2.urls), 1)
        self.assertFalse(cli.cargar_lote(self.con, "cartera", self.entrada), "el lote existente no se recarga")

        # Excel con las pestañas y fórmulas; el teléfono confirmado sube la prioridad.
        ruta = cli.generar_excel(self.con, "cartera", hoy=HOY)
        wb = load_workbook(ruta)
        self.assertEqual(wb.sheetnames, ["Resumen_Ejecutivo", "Prioridad_A_Urgentes", "Ruta_Por_Zonas_Alicante", "Por_Cualificar_Llamada",
                                         "Base_Completa_Enriquecida", "Descartadas_y_Errores"])
        base = wb["Base_Completa_Enriquecida"]
        cab = [c.value for c in base[1]]
        self.assertTrue(str(base.cell(2, cab.index("Prioridad") + 1).value).startswith("=IF("))
        self.assertEqual(base.freeze_panes, "A2")
        self.assertEqual(wb["Prioridad_A_Urgentes"].max_row, 2)
        fila_iv = next(i for i in range(2, base.max_row + 1) if base.cell(i, 1).value == "A28015865")
        base.cell(fila_iv, cab.index("Teléfono ✎") + 1, "965 123 456")
        base.cell(fila_iv, cab.index("Empleados (confirmado) ✎") + 1, 25)
        wb.save(ruta)
        self.assertGreaterEqual(cli.actualizar(self.con, "cartera", ruta), 1)
        iv = self.registros()["A28015865"]
        self.assertEqual((iv["emp_conf"], iv["prioridad"]), (25, "A"))

    def test_excluir(self):
        cli.cargar_lote(self.con, "cartera", self.entrada)
        cli.excluir(self.con, "a-28015865", "pidió no ser contactada")
        self.assertEqual(self.registros()["A28015865"]["prioridad"], "DESCARTADA")
        os.remove(self.entrada)


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


if __name__ == "__main__":
    unittest.main()
