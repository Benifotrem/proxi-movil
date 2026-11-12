import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from b2bworker import cli, db, telefonos  # noqa: E402
from b2bworker.red import ErrorConsulta  # noqa: E402

HOY = date(2026, 11, 12)


class MapsFalso:
    def __init__(self, por_q):
        self.por_q, self.q = por_q, []

    def json(self, url, cabeceras=None):
        p = parse_qs(urlparse(url).query)
        assert p["engine"] == ["google_maps"] and p["api_key"] == ["clave-de-prueba"]
        self.q.append(p["q"][0])
        r = self.por_q.get(p["q"][0])
        if isinstance(r, Exception):
            raise r
        return r or {"local_results": []}


class SerperFalso:
    def __init__(self, por_q):
        self.por_q, self.q = por_q, []

    def json(self, url, cabeceras=None, enviar=None):
        assert url == telefonos.SERPER and cabeceras == {"X-API-KEY": "clave-serper"} and enviar["gl"] == "es"
        self.q.append(enviar["q"])
        return self.por_q.get(enviar["q"], {"places": []})


class Coincidencia(unittest.TestCase):
    def test_coincide_por_nombre_y_cp_o_municipio(self):
        emp = {"razon_social": "TALLERES MECANICOS GARCIA SL", "cp": "03600", "municipio": "ELDA"}
        self.assertTrue(telefonos.coincide(emp, {"title": "Talleres Mecánicos García", "address": "C. Mayor, 1, 03600 Elda, Alicante"}))
        self.assertTrue(telefonos.coincide(emp, {"title": "Talleres García S.L.", "address": "Polígono Campo Alto, Elda"}))
        self.assertFalse(telefonos.coincide(emp, {"title": "Talleres García", "address": "C. Real, 5, 03400 Villena"}), "otro municipio")
        self.assertFalse(telefonos.coincide(emp, {"title": "Pinturas López", "address": "03600 Elda"}), "otro nombre")

    def test_direccion_corta_sin_cp_ni_municipio(self):
        # Caso real (Serper): "Ara Calzados Sl | C. Conde Coloma, 7"
        emp = {"razon_social": "ARA CALZADOS S L", "cp": "03600", "municipio": "ELDA", "direccion": "CALLE CONDE COLOMA 7"}
        self.assertTrue(telefonos.coincide(emp, {"title": "Ara Calzados Sl", "address": "C. Conde Coloma, 7"}))
        self.assertFalse(telefonos.coincide(emp, {"title": "Ara Calzados Sl", "address": "C. Mayor, 3"}), "otra calle")
        self.assertFalse(telefonos.coincide(emp, {"title": "Ara Calzados Sl", "address": "C. Conde Coloma, 7, 03400 Villena"}), "otro CP")
        emp2 = {"razon_social": "TALLERES MECANICOS GARCIA PEREZ SL", "cp": "03600", "municipio": "ELDA", "direccion": "CALLE CONDE COLOMA 7"}
        self.assertFalse(telefonos.coincide(emp2, {"title": "Talleres García", "address": "C. Conde Coloma, 7"}), "nombre solo parecido sin localidad")

    def test_formato_telefono(self):
        self.assertEqual(telefonos.telefono_normalizado("+34 965 12 34 56"), "965 12 34 56")
        self.assertEqual(telefonos.telefono_normalizado("965123456"), "965 12 34 56")
        self.assertIsNone(telefonos.telefono_normalizado("112"))


class Busqueda(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        cli.DATOS = Path(self.tmp.name)
        self.con = db.abrir(cli.DATOS / "b2b.sqlite")
        filas = [("B00000001", 2, "ALFA SIN AYUDAS SL", "ELDA", "03600", None, None),
                 ("B00000002", 3, "BETA CON KIT SL", "ELDA", "03600", '{"kit_urgente": true, "kit_fecha": "2026-09-01", "kit_importe": 3000, "n_concesiones": 1}', None),
                 ("B00000003", 4, "GAMMA CONFIRMADA SL", "ALICANTE", "03001", None, "966 11 11 11")]
        for cif, fila, nombre, muni, cp, bdns, tel in filas:
            self.con.execute("INSERT INTO empresas (lote, cif, fila, razon_social, municipio, cp, tipo, bdns_estado, bdns, borme_estado, telefono) "
                             "VALUES ('l', ?, ?, ?, ?, ?, 'Sociedad limitada', 'ok', ?, 'no_encontrada', ?)", (cif, fila, nombre, muni, cp, bdns or "{}", tel))
        self.con.commit()

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()

    def test_orden_por_encaje_tope_y_sin_repetir(self):
        falso = MapsFalso({
            "BETA CON KIT SL ELDA": {"local_results": [{"title": "Beta con Kit", "phone": "+34 965 00 00 02", "website": "https://beta.es",
                                                        "address": "Av. Chapí 3, 03600 Elda", "type": "Fabricante de calzado"}]},
            "ALFA SIN AYUDAS SL ELDA": {"local_results": [{"title": "Otra Empresa", "address": "03600 Elda"}]},
        })
        n = cli.buscar_telefonos(self.con, "l", 10, cliente=falso, hoy=HOY, clave="clave-de-prueba", prov="serpapi")
        self.assertEqual(n, 2)
        self.assertEqual(falso.q, ["BETA CON KIT SL ELDA", "ALFA SIN AYUDAS SL ELDA"], "primero el encaje Alto; la confirmada no se busca")
        beta = self.con.execute("SELECT * FROM empresas WHERE cif='B00000002'").fetchone()
        self.assertEqual((beta["telefono"], beta["web"], beta["categoria"], beta["tel_fuente"]), ("965 00 00 02", "https://beta.es", "Fabricante de calzado", telefonos.FUENTE))
        self.assertEqual(self.con.execute("SELECT tel_estado FROM empresas WHERE cif='B00000001'").fetchone()[0], "no_encontrado")
        self.assertEqual(self.con.execute("SELECT telefono FROM empresas WHERE cif='B00000003'").fetchone()[0], "966 11 11 11")
        self.assertEqual(telefonos.usadas_mes(self.con, hoy=HOY), 2)
        # Segunda pasada: nada que repetir.
        self.assertEqual(cli.buscar_telefonos(self.con, "l", 10, cliente=MapsFalso({}), hoy=HOY, clave="clave-de-prueba", prov="serpapi"), 0)
        # La categoría de Google mejora el sector y el teléfono sube la prioridad.
        from b2bworker.zonas import Zonas
        x = cli.clasificar(beta, Zonas(), HOY)
        self.assertEqual((x["sector_est"], x["prioridad"], x["tel_fuente"]), ("Industrial", "A", telefonos.FUENTE))

    def test_tope_mensual(self):
        self.con.execute("INSERT INTO busquedas VALUES ('serpapi', '2026-11', 240)")
        self.assertEqual(cli.buscar_telefonos(self.con, "l", 10, cliente=MapsFalso({}), hoy=HOY, clave="clave-de-prueba", prov="serpapi"), 0)
        self.assertEqual(cli.buscar_telefonos(self.con, "l", 10, cliente=MapsFalso({}), hoy=date(2026, 12, 1), clave="clave-de-prueba", prov="serpapi"), 2)

    def test_clave_invalida_para(self):
        falso = MapsFalso({"BETA CON KIT SL ELDA": ErrorConsulta("HTTP 401")})
        with self.assertRaises(SystemExit):
            cli.buscar_telefonos(self.con, "l", 10, cliente=falso, hoy=HOY, clave="clave-de-prueba", prov="serpapi")

    def test_serper_preferente(self):
        (cli.DATOS / ".env").write_text("SERPAPI_KEY=a\nSERPER_API_KEY=clave-serper\n")
        import os
        for v in ("SERPAPI_KEY", "SERPER_API_KEY"):
            os.environ.pop(v, None)
        self.assertEqual(telefonos.proveedor(cli.DATOS), ("serper", "clave-serper"))
        falso = SerperFalso({"BETA CON KIT SL ELDA": {"places": [{"title": "Beta con Kit SL", "address": "Av. Chapí 3, 03600 Elda",
                                                                  "phoneNumber": "965 00 00 02", "website": "https://beta.es", "category": "Fabricante"}]}})
        self.assertEqual(cli.buscar_telefonos(self.con, "l", 10, cliente=falso, hoy=HOY), 2)
        beta = self.con.execute("SELECT telefono, tel_fuente FROM empresas WHERE cif='B00000002'").fetchone()
        self.assertEqual(tuple(beta), ("965 00 00 02", "Google Maps (Serper)"))
        self.assertEqual(telefonos.usadas_mes(self.con, "serper", hoy=HOY), 2)

    def test_importar_telefonos_de_otra_pestana(self):
        from openpyxl import Workbook
        wb = Workbook()
        wb.active.title = "Cartera"
        wb.active.append(["CLI_ID_FISCAL", "OTRA"])
        ws = wb.create_sheet("Telefonos")
        ws.append(["CIF", "CLI_NOMBRE", "TELEFONO FIJO", "MOVIL"])
        ws.append(["B00000001", "ALFA", "", "+34 600 11 22 33"])
        ws.append(["B00000003", "GAMMA", "965000000", ""])
        ws.append(["B99999999", "AJENA", "965111111", ""])
        ruta = cli.DATOS / "tels.xlsx"
        wb.save(ruta)
        self.con.execute("UPDATE empresas SET telefono='965 99 99 99', tel_fuente='Google Maps (Serper)' WHERE cif='B00000001'")
        self.con.execute("UPDATE empresas SET tel_fuente='tu archivo' WHERE cif='B00000003'")
        self.assertEqual(cli.importar_telefonos(self.con, "l", ruta), 1)
        alfa = self.con.execute("SELECT telefono, tel_fuente, tel_estado FROM empresas WHERE cif='B00000001'").fetchone()
        self.assertEqual(tuple(alfa), ("600 11 22 33", "tu archivo", "importado"), "tus teléfonos mandan sobre los de Google")
        self.assertEqual(self.con.execute("SELECT telefono FROM empresas WHERE cif='B00000003'").fetchone()[0], "966 11 11 11",
                         "no pisa un teléfono que ya era tuyo")

    def test_empleados_por_busqueda_web(self):
        from b2bworker import empleados as E
        self.assertEqual(E.extraer_rango("Número de empleados: 6 a 10. Ventas..."), (6, 10))
        self.assertEqual(E.extraer_rango("La empresa tiene entre 11 y 50 empleados"), (11, 50))
        self.assertEqual(E.extraer_rango("Empleados: 4"), (4, 4))
        self.assertEqual(E.extraer_rango("Plantilla (2023): 25"), (25, 25))
        self.assertIsNone(E.extraer_rango("Capital social 3.000 euros"))

        class Web:
            def __init__(self):
                self.q = []

            def json(self, url, cabeceras=None, enviar=None):
                assert url == E.BUSQUEDA
                self.q.append(enviar["q"])
                return {"organic": [
                    {"title": "Otra empresa SL", "snippet": "Empleados: 200", "link": "https://dir.es/otra"},
                    {"title": "ALFA SIN AYUDAS SL - B00000001", "snippet": "Empleados: 6 a 10", "link": "https://www.directorio.es/alfa"}]}
        self.con.execute("""UPDATE empresas SET bdns='{"kit_urgente": true, "kit_fecha": "2026-09-01", "kit_segmento": "3-9", "n_concesiones": 1}' WHERE cif='B00000002'""")
        web = Web()
        n = cli.buscar_empleados(self.con, "l", 10, cliente=web, hoy=HOY, clave="clave-serper")
        self.assertEqual(n, 2, "BETA tiene segmento de Kit Digital: no se busca; GAMMA y ALFA sí")
        self.assertNotIn('"B00000002" empleados', web.q)
        alfa = self.con.execute("SELECT * FROM empresas WHERE cif='B00000001'").fetchone()
        self.assertEqual((alfa["emp_web_min"], alfa["emp_web_max"], alfa["emp_web_fuente"]), (6, 10, "directorio.es"))
        from b2bworker.zonas import Zonas
        x = cli.clasificar(alfa, Zonas(), HOY)
        self.assertEqual((x["emp_est"], x["seg_est"]), ("6-10 (web: directorio.es)", "Segmento II (3-9 emp)"))
        gamma = self.con.execute("SELECT emp_web_estado, emp_web_min FROM empresas WHERE cif='B00000003'").fetchone()
        self.assertEqual(tuple(gamma), ("sin_dato", None), "el resultado de otra empresa no se usa")

    def test_la_clave_se_lee_del_env_de_datos(self):
        (cli.DATOS / ".env").write_text('OTRA=1\nSERPAPI_KEY="abc123"\n')
        import os
        os.environ.pop("SERPAPI_KEY", None)
        self.assertEqual(telefonos.cargar_clave(cli.DATOS), "abc123")


if __name__ == "__main__":
    unittest.main()
