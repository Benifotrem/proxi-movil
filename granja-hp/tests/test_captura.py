"""Pruebas del trabajador de captura (sin red): python3 -m unittest discover -s granja-hp/tests"""
import os
import socket
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import captura  # noqa: E402


def resolver(tabla):
    def r(host, _port):
        if host not in tabla:
            raise socket.gaierror("no existe")
        return [(None, None, None, None, (ip, 0)) for ip in tabla[host]]
    return r


class Ssrf(unittest.TestCase):
    def test_bloquea_redes_internas(self):
        res = resolver({"tienda.es": ["151.101.1.69"], "rebind.es": ["10.200.3.1"], "mixto.es": ["151.101.1.69", "192.168.1.1"],
                        "ts.es": ["100.101.102.103"], "v6.es": ["::1"], "mapped.es": ["::ffff:127.0.0.1"]})
        for url in ["http://localhost/", "http://127.0.0.1/", "http://10.200.3.2:3128/", "http://[::1]/",
                    "http://169.254.169.254/latest/", "https://tienda.es:5000/", "https://u:p@tienda.es/",
                    "ftp://tienda.es/", "http://intranet/", "http://impresora.local/", "http://rebind.es/p",
                    "http://mixto.es/p", "http://ts.es/p", "http://v6.es/p", "http://mapped.es/p", "http://noexiste.es/p",
                    "http://2130706433/", "http://0x7f000001/"]:
            self.assertFalse(captura.url_permitida(url, res), url)
        self.assertTrue(captura.url_permitida("https://tienda.es/p/1?x=2", res))
        self.assertTrue(captura.url_permitida("http://151.101.1.69/p", res))


class Precios(unittest.TestCase):
    def test_formatos(self):
        casos = {"24,50": 24.5, "24.50": 24.5, "1.234,56 €": 1234.56, "1,234.56": 1234.56, "1.234": 1234.0,
                 "€ 9,9": 9.9, 31: 31.0, "0": None, "": None, "gratis": None, None: None}
        for entrada, esperado in casos.items():
            self.assertEqual(captura.precio_num(entrada), esperado, entrada)


class Extraccion(unittest.TestCase):
    def test_jsonld_producto(self):
        html = """<html><head><title>Tienda</title><script type="application/ld+json">
        {"@context":"https://schema.org","@graph":[{"@type":"WebSite"},{"@type":"Product","name":"Protector solar SPF50",
        "offers":{"@type":"Offer","price":"22.90","priceCurrency":"EUR","availability":"https://schema.org/InStock"}}]}
        </script></head></html>"""
        self.assertEqual(captura.extraer(html), {"status": "ok", "price": 22.9, "currency": "EUR", "availability": "in_stock",
                                                 "title": "Protector solar SPF50", "method": "jsonld"})

    def test_jsonld_varias_ofertas_y_sin_stock(self):
        html = """<script type="application/ld+json">[{"@type":["Product"],"name":"Crema",
        "offers":[{"price":"12,95","priceCurrency":"EUR"},{"price":"10.45","priceCurrency":"EUR"}]}]</script>"""
        self.assertEqual(captura.extraer(html)["price"], 10.45)
        agotado = """<script type="application/ld+json">{"@type":"Product","name":"Sérum",
        "offers":{"@type":"Offer","availability":"http://schema.org/OutOfStock"}}</script>"""
        r = captura.extraer(agotado)
        self.assertEqual((r["status"], r["price"], r["availability"]), ("ok", None, "out_of_stock"))

    def test_metadatos_y_microdatos(self):
        meta = """<meta property="og:title" content="Agua micelar &amp; más"><meta property="product:price:amount" content="12.95">
        <meta property="product:price:currency" content="EUR"><meta property="product:availability" content="in stock">"""
        r = captura.extraer(meta)
        self.assertEqual((r["price"], r["method"], r["title"], r["availability"]), (12.95, "meta", "Agua micelar & más", "in_stock"))
        micro = """<div itemscope itemtype="https://schema.org/Product"><h1 itemprop="name">Sérum C</h1>
        <span itemprop="price" content="31.00">31,00 €</span><meta itemprop="priceCurrency" content="EUR">
        <link itemprop="availability" href="https://schema.org/InStock"></div>"""
        r = captura.extraer(micro)
        self.assertEqual((r["price"], r["method"], r["title"]), (31.0, "microdata", "Sérum C"))

    def test_sin_precio_falla_en_vez_de_inventar(self):
        r = captura.extraer("<html><title>Producto</title><p>Precio: llámenos</p></html>")
        self.assertEqual(r["status"], "failed")
        self.assertEqual(captura.extraer("<script type='application/ld+json'>{roto</script>")["status"], "failed")


class Flujo(unittest.TestCase):
    """capturar() con la red simulada: redirecciones revisadas, bloqueos y robots."""

    def setUp(self):
        self.orig = captura.descargar
        captura.DOMAIN_DELAY = 0

    def tearDown(self):
        captura.descargar = self.orig

    class Salidas:
        modems = ["m1", "m2"]

        def __init__(self):
            self.i = 0

        def siguiente(self):
            self.i += 1
            return (f"http://10.200.{self.i}.2:3128", "u:p")

    class RobotsSi:
        def permitido(self, url, salida):
            return "privado" not in url

    def run_con(self, respuestas, url="https://tienda.es/p"):
        llamadas = []

        def falso(u, salida, cabeceras_extra=()):
            llamadas.append(u)
            r = respuestas[u]
            if isinstance(r, dict):  # respuesta por salida
                r = r[salida[0]]
            if isinstance(r, Exception):
                raise r
            return r
        captura.descargar = falso
        res = resolver({"tienda.es": ["151.101.1.69"], "interna.es": ["192.168.1.10"]})
        self.llamadas = llamadas
        return captura.capturar(url, self.Salidas(), self.RobotsSi(), res), llamadas

    def test_redireccion_a_red_interna_bloqueada(self):
        r, llamadas = self.run_con({"https://tienda.es/p": (302, "", "http://interna.es/admin", "")})
        self.assertEqual(r["error"], "url_not_public")
        self.assertEqual(llamadas, ["https://tienda.es/p"])

    def test_bloqueo_antibot_no_se_salta(self):
        r, _ = self.run_con({"https://tienda.es/p": (403, "Access denied", "", "")})
        self.assertEqual(r["error"], "blocked_403")
        r, _ = self.run_con({"https://tienda.es/p": (200, "<html>Please complete the captcha</html>", "", "")})
        self.assertEqual(r["error"], "blocked_challenge")
        desafio = "<html><head><title>Client Challenge</title></head><body><script src='/x.js'></script></body></html>"
        r, _ = self.run_con({"https://tienda.es/p": (200, desafio, "", "")})
        self.assertEqual(r["error"], "blocked_challenge")

    def test_error_de_red_prueba_otro_movil_y_si_no_se_reintenta(self):
        html = '<meta property="product:price:amount" content="9.90">'
        caida = captura.ErrorRed("curl 56: CONNECT tunnel failed, response 502")
        r, llamadas = self.run_con({"https://tienda.es/p": {"http://10.200.1.2:3128": caida, "http://10.200.2.2:3128": (200, html, "", "")}})
        self.assertEqual((r["status"], r["price"], len(llamadas)), ("ok", 9.9, 2))
        r, _ = self.run_con({"https://tienda.es/p": caida})
        self.assertIsNone(r)  # ninguna salida: no se informa; la reserva caduca y se reintenta

    def test_robots_y_ok(self):
        r, llamadas = self.run_con({}, url="https://tienda.es/privado")
        self.assertEqual((r["error"], llamadas), ("robots_disallow", []))
        html = '<meta property="product:price:amount" content="9.90">'
        r, _ = self.run_con({"https://tienda.es/p": (301, "", "/p2", ""), "https://tienda.es/p2": (200, html, "", "")})
        self.assertEqual((r["status"], r["price"]), ("ok", 9.9))


if __name__ == "__main__":
    unittest.main()
