import sqlite3
import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from b2bworker import borme, db  # noqa: E402

TEXTO = """BOLETÍN OFICIAL DEL REGISTRO MERCANTIL
SECCIÓN PRIMERA
Empresarios
Actos inscritos
ALICANTE
466101 - EJEMPLO DIGITAL SOLUCIONES SL.
Constitución. Comienzo de operaciones: 1.10.26. Objeto social: Desarrollo de software y consultoría informá-
tica. Domicilio: C/ MAYOR 1 (ALICANTE). Capital: 3.000,00 Euros. Nombramientos. Adm. Unico: GARCIA PEREZ JUAN.
Datos registrales. T 4567 , F 12, S 8, H A 199999, I/A 1 (5.11.26).
466102 - CONSTRUCCIONES DEL SUR, SOCIEDAD LIMITADA.
Ceses/Dimisiones. Adm. Solid.: LOPEZ RUIZ ANA. Nombramientos. Adm. Solid.: MARTIN SOLER PEDRO;GOMEZ VIDAL
LUISA. Apoderado: SANCHEZ MORA EVA. Datos registrales. T 1, F 2, S 8, H A 1, I/A 9 (5.11.26).
466103 - PLASTICOS VIEJOS SA.
Disolución. Voluntaria. Extinción. Datos registrales. T 1, F 1, S 1, H A 2, I/A 5 (5.11.26).
466104 - PROSOLIA INTERNACIONAL SL. Fe de erratas: Error en la publicación de la inscripción 28ª en cuanto al apellido
del consejero. Datos registrales. S 8 , H A 148395, I/A 28 ( 2.12.24).
466105 - LUCENTUM AUTOS SL. Escisión parcial. Sociedades beneficiarias de la escisión: MUTXALUC86 SL. Datos registrales. S 8.
"""


class Borme(unittest.TestCase):
    def setUp(self):
        self.con = db.abrir(":memory:")

    def test_separar_y_ficha(self):
        entradas = borme.separar(TEXTO)
        self.assertEqual([n for n, _ in entradas], ["EJEMPLO DIGITAL SOLUCIONES SL", "CONSTRUCCIONES DEL SUR, SOCIEDAD LIMITADA", "PLASTICOS VIEJOS SA",
                                                    "PROSOLIA INTERNACIONAL SL", "LUCENTUM AUTOS SL"])
        self.assertTrue(entradas[3][1].startswith("Fe de erratas"))
        from b2bworker.textos import nombre_empresa
        self.con.executemany("INSERT INTO borme_actos VALUES (?, ?, ?, ?)", [("2026-11-06", nombre_empresa(n), n, t) for n, t in entradas])
        f = borme.ficha(self.con, "Ejemplo Digital Soluciones, S.L.")
        self.assertEqual((f["estado"], f["constitucion"]), ("Activa", "2026-10-01"))
        self.assertIn("software", f["objeto"])
        self.assertEqual(f["administradores"][0]["nombre"], "GARCIA PEREZ JUAN")
        f = borme.ficha(self.con, "CONSTRUCCIONES DEL SUR SL")
        self.assertEqual(sorted(a["nombre"] for a in f["administradores"]), ["GOMEZ VIDAL LUISA", "MARTIN SOLER PEDRO"])
        self.assertEqual(borme.ficha(self.con, "Plásticos Viejos S.A.")["estado"], "Extinguida")
        self.assertIsNone(borme.ficha(self.con, "NO EXISTE SL"))

    def test_ceses_posteriores_retiran_al_administrador(self):
        self.con.executemany("INSERT INTO borme_actos VALUES (?, ?, ?, ?)", [
            ("2025-01-10", "ALFA SL", "ALFA SL", "Nombramientos. Adm. Unico: PRIMERO ANTONIO."),
            ("2026-02-10", "ALFA SL", "ALFA SL", "Ceses/Dimisiones. Adm. Unico: PRIMERO ANTONIO. Nombramientos. Adm. Unico: SEGUNDA MARIA."),
        ])
        self.assertEqual([a["nombre"] for a in borme.ficha(self.con, "Alfa SL")["administradores"]], ["SEGUNDA MARIA"])

    def test_urls_y_dias(self):
        sumario = {"data": {"sumario": {"diario": [{"seccion": [{"codigo": "A", "item": [
            {"titulo": "ALBACETE", "url_pdf": {"texto": "https://x/albacete.pdf"}},
            {"titulo": "ALICANTE/ALACANT", "url_pdf": {"texto": "https://x/alicante.pdf"}}]}]}]}}}
        self.assertEqual(borme.urls_alicante(sumario), ["https://x/alicante.pdf"])
        dias = list(borme.dias_pendientes(self.con, date(2026, 11, 6), date(2026, 11, 10)))
        self.assertEqual([d.isoformat() for d in dias], ["2026-11-06", "2026-11-09", "2026-11-10"])


if __name__ == "__main__":
    unittest.main()
