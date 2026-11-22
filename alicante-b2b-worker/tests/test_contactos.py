import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from b2bworker import contactos  # noqa: E402
from b2bworker.red import ErrorConsulta  # noqa: E402

PORTADA = """<html><head><script>var x="soporte@jquery.com";</script></head><body>
<a href="/contacto">Contacto</a> <a href="https://otra.com/contacto">fuera</a> <a href="/aviso-legal.pdf">pdf</a>
<img src="logo@2x.png"> <p>Llámenos al 965 12 34 56</p></body></html>"""
CONTACTO = """<p>Escríbanos a <a href="mailto:ventas@talleres-ejemplo.es?subject=Hola">ventas</a> o a
info [at] talleres-ejemplo.es · Gerencia: juan.perez@gmail.com</p>
<a href="tel:+34 612 345 678">Móvil</a> <a href="https://wa.me/34698765432">WhatsApp</a> Pedido nº 6123456789012</p>"""


class ClienteFalso:
    def __init__(self, paginas):
        self.paginas, self.urls = paginas, []

    def get(self, url, timeout=30, **_):
        self.urls.append(url)
        if url not in self.paginas:
            raise ErrorConsulta("HTTP 404")
        return self.paginas[url]


class Contactos(unittest.TestCase):
    def test_extraccion(self):
        self.assertEqual(contactos.emails(CONTACTO), ["ventas@talleres-ejemplo.es", "info@talleres-ejemplo.es", "juan.perez@gmail.com"])
        self.assertEqual(contactos.emails(PORTADA), [], "ni librerías ni imágenes @2x")
        self.assertEqual(contactos.moviles(CONTACTO), ["612 34 56 78", "698 76 54 32"], "un número largo no es un móvil")
        self.assertEqual(contactos.moviles(PORTADA), [], "un fijo no es un móvil")
        self.assertEqual(contactos.normalizar_web("WWW.TALLERES-EJEMPLO.ES/"), "https://www.talleres-ejemplo.es")
        self.assertEqual(contactos.normalizar_web("no tiene"), "")

    def test_buscar(self):
        base = "https://www.talleres-ejemplo.es"
        c = ClienteFalso({base + "/robots.txt": "User-agent: *\nDisallow: /privado", base + "/": PORTADA, base + "/contacto": CONTACTO})
        r = contactos.buscar(c, "www.talleres-ejemplo.es")
        self.assertEqual(r, {"email": "info@talleres-ejemplo.es; ventas@talleres-ejemplo.es", "movil": "612 34 56 78; 698 76 54 32",
                             "fuente": base + "/contacto", "estado": "ok"})
        self.assertNotIn("https://otra.com/contacto", c.urls, "solo páginas de la propia web")
        self.assertFalse(any(u.endswith(".pdf") for u in c.urls))

    def test_robots_y_errores(self):
        base = "https://cerrada.es"
        c = ClienteFalso({base + "/robots.txt": "User-agent: *\nDisallow: /", base + "/": CONTACTO})
        self.assertEqual(contactos.buscar(c, "cerrada.es")["estado"], "bloqueada")
        self.assertNotIn(base + "/", c.urls, "no se lee nada si robots.txt lo prohíbe")
        self.assertEqual(contactos.buscar(ClienteFalso({}), "caida.es")["estado"], "error")
        self.assertEqual(contactos.buscar(ClienteFalso({"https://vacia.es/": "<p>Hola</p>"}), "vacia.es")["estado"], "sin_dato")
        rota = '<a href="http://[eliminado-tag id=1 name=x]/contacto">x</a><a href="/contacto">ok</a>'
        c = ClienteFalso({"https://rota.es/": rota, "https://rota.es/contacto": CONTACTO})
        self.assertEqual(contactos.buscar(c, "rota.es")["estado"], "ok", "un enlace mal formado se ignora")


class Esperas(unittest.TestCase):
    def test_retry_after_no_bloquea(self):
        import urllib.error
        from email.message import Message
        from b2bworker import red
        cab = Message()
        cab["Retry-After"] = "3600"

        def abrir(req, timeout=30):
            raise urllib.error.HTTPError(req.full_url, 503, "Busy", cab, None)
        esperas = []
        c = red.Cliente(pausa=(0, 0), reintentos=1, dormir=esperas.append, abrir=abrir)
        with self.assertRaises(ErrorConsulta):
            c.get("https://ocupada.es/")
        self.assertEqual([e for e in esperas if e > 1], [], "con un solo intento no se espera nada")
        esperas.clear()
        c = red.Cliente(pausa=(0, 0), reintentos=3, dormir=esperas.append, abrir=abrir)
        with self.assertRaises(ErrorConsulta):
            c.get("https://ocupada.es/")
        self.assertEqual([e for e in esperas if e > 1], [red.ESPERA_MAX, red.ESPERA_MAX], "nunca más de ESPERA_MAX, y no tras el último")


class Ipv4(unittest.TestCase):
    def test_forzar_ipv4(self):
        import socket
        from b2bworker import red
        original, llamadas = socket.getaddrinfo, []

        def falso(host, port, family=0, *a, **k):
            llamadas.append(family)
            if host == "solo6.es" and family == socket.AF_INET:
                raise socket.gaierror("sin IPv4")
            return [(family, None, None, "", (host, port))]
        socket.getaddrinfo = falso
        try:
            red.forzar_ipv4()
            self.assertEqual(socket.getaddrinfo("web.es", 443)[0][0], socket.AF_INET)
            self.assertEqual(socket.getaddrinfo("solo6.es", 443)[0][0], 0, "si no hay IPv4, se usa lo que haya")
            red.forzar_ipv4()
            self.assertTrue(socket.getaddrinfo._solo_ipv4)
        finally:
            socket.getaddrinfo = original


if __name__ == "__main__":
    unittest.main()
