"""Pruebas de drivers, cerrojo de rotación y sondas (sin red ni móviles):
python3 -m unittest discover -s granja-hp/tests"""
import os
import socket
import sys
import tempfile
import threading
import time
import types
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import captura  # noqa: E402
import drivers  # noqa: E402
import granja_lock  # noqa: E402
import http_worker  # noqa: E402


class Cerrojo(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        granja_lock.LOCK_DIR = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_uso_compartido_y_rotacion_exclusiva(self):
        with granja_lock.en_uso("m1"), granja_lock.en_uso("m1"):  # dos trabajadores a la vez
            with self.assertRaises(granja_lock.Ocupado):
                with granja_lock.rotacion("m1", espera=0.3, paso=0.05):
                    pass
        self.assertFalse(granja_lock.rotando("m1"), "la marca se retira aunque la rotación falle")
        with granja_lock.rotacion("m1", espera=0.3):
            self.assertTrue(granja_lock.rotando("m1"))
            with self.assertRaises(granja_lock.Rotando):
                with granja_lock.en_uso("m1"):
                    pass
            with self.assertRaises(granja_lock.Ocupado):  # segunda rotación simultánea
                with granja_lock.rotacion("m1", espera=0.1):
                    pass
            with granja_lock.en_uso("m2"):  # otro móvil no se ve afectado
                pass
        self.assertFalse(granja_lock.rotando("m1"))

    def test_rotacion_espera_a_la_peticion_en_vuelo(self):
        orden = []

        def peticion():
            with granja_lock.en_uso("m1"):
                orden.append("peticion_empieza")
                time.sleep(0.3)
                orden.append("peticion_termina")
        hilo = threading.Thread(target=peticion)
        hilo.start()
        time.sleep(0.05)
        with granja_lock.rotacion("m1", espera=2, paso=0.02):
            orden.append("rota")
        hilo.join()
        self.assertEqual(orden, ["peticion_empieza", "peticion_termina", "rota"])

    def test_marca_caducada_no_bloquea(self):
        marca = os.path.join(self.tmp.name, "m1.rotating")
        open(marca, "w").close()
        viejo = time.time() - granja_lock.MARCA_MAX_EDAD - 5
        os.utime(marca, (viejo, viejo))
        self.assertFalse(granja_lock.rotando("m1"))

    def test_id_no_valido(self):
        with self.assertRaises(ValueError):
            granja_lock.rotando("../etc")


class Ejecutor:
    def __init__(self, respuestas):
        self.respuestas, self.llamadas = respuestas, []

    def __call__(self, cmd, **kw):
        self.llamadas.append(cmd)
        r = self.respuestas.pop(0) if self.respuestas else (0, "", "")
        return types.SimpleNamespace(returncode=r[0], stdout=r[1], stderr=r[2])


class Drivers(unittest.TestCase):
    def test_android_rotacion_por_modo_avion_y_datos(self):
        e = Ejecutor([])
        drivers.AndroidDriver("ABC", "airplane", runner=e, dormir=lambda s: None).rotate_ip()
        self.assertEqual([c[4:] for c in e.llamadas], [
            ["settings", "put", "global", "airplane_mode_radios", "cell"],
            ["cmd", "connectivity", "airplane-mode", "enable"], ["cmd", "connectivity", "airplane-mode", "disable"]])
        self.assertEqual(e.llamadas[0][:4], ["adb", "-s", "ABC", "shell"])
        e = Ejecutor([])
        drivers.AndroidDriver("ABC", "data", runner=e, dormir=lambda s: None).rotate_ip()
        self.assertEqual([c[4:] for c in e.llamadas], [["svc", "data", "disable"], ["svc", "data", "enable"]])

    def test_android_error_adb(self):
        e = Ejecutor([(0, "", ""), (1, "", "error: device offline")])
        with self.assertRaises(drivers.DriverError):
            drivers.AndroidDriver("ABC", runner=e, dormir=lambda s: None).rotate_ip()

    def test_android_sms(self):
        salida = ("Row: 0 _id=12, address=BANCOEJ, date=1762500000000, body=Tu código es 482913, no lo compartas\n"
                  "Row: 1 _id=11, address=+34600111222, date=1762400000000, body=Hola\nsegunda línea\n")
        e = Ejecutor([(0, salida, "")])
        sms = drivers.AndroidDriver("ABC", runner=e).read_latest_sms(limit=5)
        self.assertEqual(sms[0], {"id": "12", "sender": "BANCOEJ", "body": "Tu código es 482913, no lo compartas", "date_ms": 1762500000000})
        self.assertEqual(sms[1]["body"], "Hola\nsegunda línea")
        e = Ejecutor([(0, "Error while accessing provider:sms\njava.lang.SecurityException: Permission Denial", "")])
        with self.assertRaises(drivers.DriverError):
            drivers.AndroidDriver("ABC", runner=e).read_latest_sms()

    def test_ios_por_ssh(self):
        e = Ejecutor([(0, "", "")])
        drivers.IosDriver("00008110-001", 2222, runner=e).rotate_ip()
        cmd = e.llamadas[0]
        self.assertEqual(cmd[:3], ["ssh", "-p", "2222"])
        self.assertIn("root@127.0.0.1", cmd)
        self.assertEqual(cmd[-1], "killall -9 CommCenter")
        nanos = 784000000 * 10**9  # iOS 11+: nanosegundos desde 2001
        e = Ejecutor([(0, f"7\x1fBANCOEJ\x1f{nanos}\x1fCódigo 123456\n", "")])
        sms = drivers.IosDriver("00008110-001", 2222, runner=e).read_latest_sms(3)
        self.assertEqual(sms, [{"id": "7", "sender": "BANCOEJ", "body": "Código 123456", "date_ms": 978307200000 + 784000000 * 1000}])
        self.assertIn("LIMIT 3", e.llamadas[0][-1])
        with self.assertRaises(drivers.DriverError):
            drivers.IosDriver("x", None)

    def test_driver_para(self):
        self.assertIsInstance(drivers.driver_para({"serial": "A", "rotation": "data"}), drivers.AndroidDriver)
        self.assertIsInstance(drivers.driver_para({"serial": "U", "driver": "ssh_jailbreak", "ssh_port": 2222}), drivers.IosDriver)
        with self.assertRaises(drivers.DriverError):
            drivers.driver_para({"serial": "A", "driver": "otro"})


class Inventario(unittest.TestCase):
    def test_columnas_opcionales(self):
        import api
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "modems.conf"), "w") as f:
                f.write("# comentario\nmodem_2_3 1-1.2.2 c698 20001 airplane\n"
                        "iphone_1 1-1.3 00008110-001 20003 commcenter ssh_jailbreak 2222  # iPhone\n")
            orig, api.CONF_DIR = api.CONF_DIR, d
            try:
                inv = api.inventario()
            finally:
                api.CONF_DIR = orig
        self.assertEqual(inv["modem_2_3"]["driver"], "adb")
        self.assertIsNone(inv["modem_2_3"]["ssh_port"])
        self.assertEqual(inv["iphone_1"], {"usb": "1-1.3", "serial": "00008110-001", "port": 20003, "rotation": "commcenter",
                                           "driver": "ssh_jailbreak", "ssh_port": 2222})


def resolver(tabla):
    def r(host, _port):
        if host not in tabla:
            raise socket.gaierror("no existe")
        return [(None, None, None, None, (ip, 0)) for ip in tabla[host]]
    return r


RES = resolver({"tienda.es": ["151.101.1.69"], "www.tienda.es": ["151.101.1.70"], "interna.es": ["192.168.1.10"]})


class Medidor:
    """Respuestas simuladas por URL: tupla (código, cuerpo, location, ttfb) o excepción."""

    def __init__(self, respuestas, control=(204, "", "", 30)):
        self.respuestas = dict(respuestas)
        self.respuestas.setdefault(http_worker.CONTROL_URL, control)
        self.llamadas = []

    def __call__(self, url, salida):
        self.llamadas.append(url)
        r = self.respuestas[url]
        if isinstance(r, Exception):
            raise r
        return r


class Sondas(unittest.TestCase):
    SALIDA = ("http://10.200.1.2:3128", "u:p")

    def sondear(self, respuestas, url="https://tienda.es/", **kw):
        m = Medidor(respuestas, **kw)
        return http_worker.sondear(url, self.SALIDA, RES, m), m

    def test_ok_con_redireccion(self):
        (status, r), m = self.sondear({"https://tienda.es/": (301, "", "https://www.tienda.es/es", 80),
                                       "https://www.tienda.es/es": (200, "<html>tienda</html>", "", 184)})
        self.assertEqual(status, "ok")
        self.assertEqual((r["classification"], r["http_code"], r["ttfb_ms"], r["redirects"]), ("ok", 200, 184, 1))

    def test_bloqueos(self):
        (_, r), _ = self.sondear({"https://tienda.es/": (451, "Unavailable For Legal Reasons", "", 90)})
        self.assertEqual((r["classification"], r["http_code"]), ("http_451", 451))
        (_, r), _ = self.sondear({"https://tienda.es/": (503, "<title>Just a moment...</title>", "", 90)})
        self.assertEqual(r["classification"], "challenge")
        (_, r), _ = self.sondear({"https://tienda.es/": (200, "<html><title>Un momento</title></html>", "", 90)})
        self.assertEqual(r["classification"], "challenge")
        (_, r), _ = self.sondear({"https://tienda.es/": (502, "Bad gateway", "", 90)})
        self.assertEqual((r["classification"], r["http_code"]), ("http_error", 502))

    def test_red_dominio_o_movil(self):
        fallo = captura.ErrorRed("curl 56: CONNECT tunnel failed, response 502")
        (status, r), m = self.sondear({"https://tienda.es/": fallo})
        self.assertEqual((status, r["classification"]), ("ok", "network"))
        self.assertIn(http_worker.CONTROL_URL, m.llamadas)
        (_, r), _ = self.sondear({"https://tienda.es/": fallo}, control=captura.ErrorRed("sin red"))
        self.assertEqual(r["classification"], "node_error", "si tampoco llega al control, el fallo es del móvil")
        (_, r), _ = self.sondear({"https://tienda.es/": http_worker.Timeout("28")})
        self.assertEqual(r["classification"], "timeout")

    def test_dns_y_ssrf(self):
        (status, r), m = self.sondear({}, url="https://noexiste.es/")
        self.assertEqual((status, r["classification"]), ("ok", "dns"))
        self.assertEqual(m.llamadas, [])
        (status, r), m = self.sondear({"https://tienda.es/": (302, "", "http://interna.es/admin", 50)})
        self.assertEqual((status, r["error"]), ("failed", "url_not_public"))
        self.assertEqual(m.llamadas, ["https://tienda.es/"], "nunca se visita la red interna")

    def test_ejecutar_respeta_exclusion_y_rotacion(self):
        tmp = tempfile.TemporaryDirectory()
        granja_lock.LOCK_DIR = tmp.name
        tarea = {"id": 1, "type": "probe", "node": "m1", "payload": {"url": "https://tienda.es/"}}
        m = Medidor({"https://tienda.es/": (200, "ok", "", 100)})
        salidas = lambda mid: self.SALIDA  # noqa: E731
        self.assertIsNone(http_worker.ejecutar(tarea, {"m1"}, RES, m, salidas))
        with granja_lock.rotacion("m1", espera=0.1):
            self.assertIsNone(http_worker.ejecutar(tarea, set(), RES, m, salidas))
        self.assertEqual(m.llamadas, [])
        self.assertEqual(http_worker.ejecutar(tarea, set(), RES, m, salidas)[1]["classification"], "ok")
        status, r = http_worker.ejecutar(tarea, set(), RES, m, lambda mid: None)
        self.assertEqual((status, r["classification"]), ("failed", "node_error"))
        tmp.cleanup()


if __name__ == "__main__":
    unittest.main()


class RotacionApi(unittest.TestCase):
    """GET /rotate/<id> con el servidor real, móvil e IP simulados."""

    def setUp(self):
        import json
        import urllib.request
        from http.server import ThreadingHTTPServer
        import api
        self.api, self.json, self.urlopen = api, json, urllib.request.urlopen
        self.tmp = tempfile.TemporaryDirectory()
        with open(os.path.join(self.tmp.name, "modems.conf"), "w") as f:
            f.write("m1 1-1 SERIAL 20001 airplane\n")
        granja_lock.LOCK_DIR = os.path.join(self.tmp.name, "locks")
        self.ips = iter(["1.1.1.1", "2.2.2.2"])
        self.avisos, self.rotaciones = [], []
        rot = self.rotaciones

        class Falso(drivers.DeviceDriver):
            def rotate_ip(self):
                rot.append(granja_lock.rotando("m1"))

            def read_latest_sms(self, limit=10):
                return []

            def reboot_radio(self):
                pass
        self.orig = (api.CONF_DIR, api.ip_publica, api.notificar_plataforma, drivers.driver_para, api.ROTATE_DRAIN_WAIT)
        api.CONF_DIR = self.tmp.name
        api.ip_publica = lambda mid, modem, creds: next(self.ips, "2.2.2.2")
        api.notificar_plataforma = lambda mid, estado: self.avisos.append((mid, estado))
        drivers.driver_para = lambda modem: Falso()
        api.ROTATE_DRAIN_WAIT = 0.3
        api._last_rotation.clear()
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), api.GranjaHandler)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        (self.api.CONF_DIR, self.api.ip_publica, self.api.notificar_plataforma, drivers.driver_para, self.api.ROTATE_DRAIN_WAIT) = self.orig
        self.tmp.cleanup()

    def get(self, ruta):
        import urllib.error
        try:
            with self.urlopen(f"http://127.0.0.1:{self.srv.server_port}{ruta}", timeout=10) as r:
                return r.status, self.json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, self.json.loads(e.read())

    def test_rota_con_cerrojo_y_avisa(self):
        codigo, r = self.get("/rotate/m1")
        self.assertEqual(codigo, 200, r)
        self.assertEqual((r["old_ip"], r["new_ip"], r["changed"]), ("1.1.1.1", "2.2.2.2", True))
        self.assertEqual(self.rotaciones, [True], "rota con la marca puesta")
        self.assertEqual(self.avisos, [("m1", "start"), ("m1", "end")])
        self.assertFalse(granja_lock.rotando("m1"))

    def test_no_rota_con_peticiones_en_vuelo(self):
        with granja_lock.en_uso("m1"):
            codigo, r = self.get("/rotate/m1")
        self.assertEqual((codigo, r["status"]), (409, "busy"))
        self.assertEqual(self.rotaciones, [])
        self.assertEqual(self.avisos, [], "sin cerrojo no se avisa a la plataforma")


class Recorridos(unittest.TestCase):
    """trace de Ad Compliance con la red simulada."""
    SALIDA = ("http://10.200.1.2:3128", "u:p")
    RES = staticmethod(resolver({"trk.red.com": ["151.101.1.1"], "tienda.es": ["151.101.1.2"], "interna.es": ["10.0.0.5"],
                                 "www.googleadservices.com": ["142.250.1.1"]}))
    PAYLOAD = {"url": "https://trk.red.com/c/1", "max_hops": 10, "user_agent": "UA-movil",
               "blocked_hosts": ["googleadservices.com", "doubleclick.net"]}

    def medidor(self, respuestas, control=(204, "", "", 30)):
        respuestas = dict(respuestas)
        respuestas.setdefault(http_worker.CONTROL_URL, control)
        llamadas = []

        def m(url, salida, ua=None, cab=None):
            llamadas.append((url, ua))
            r = respuestas[url]
            if isinstance(r, Exception):
                raise r
            codigo, cuerpo, location, ttfb, *cabeceras = r
            if cab is not None:
                cab.extend(cabeceras[0] if cabeceras else [])
            return codigo, cuerpo, location, ttfb
        m.llamadas = llamadas
        return m

    def test_redirecciones_meta_y_cabeceras(self):
        m = self.medidor({
            "https://trk.red.com/c/1": (302, "", "/r?id=9", 120, [("Location", "/r?id=9"), ("Set-Cookie", "sid=secreto; Path=/"), ("Server", "nginx")]),
            "https://trk.red.com/r?id=9": (200, '<meta http-equiv="refresh" content="0; url=https://tienda.es/oferta?a=1&amp;b=2">', "", 90),
            "https://tienda.es/oferta?a=1&b=2": (200, "<html>oferta</html>", "", 200),
        })
        status, r = http_worker.trazar(self.PAYLOAD, self.SALIDA, self.RES, m)
        self.assertEqual(status, "ok")
        self.assertEqual([(h["status"], h["kind"]) for h in r["hops"]], [(302, "http"), (200, "http"), (200, "meta")])
        self.assertEqual(r["final_url"], "https://tienda.es/oferta?a=1&b=2")
        self.assertEqual(r["final_status"], 200)
        self.assertEqual(r["hops"][0]["headers"]["set-cookie"], "sid", "de las cookies solo el nombre")
        self.assertEqual(r["hops"][0]["headers"]["server"], "nginx")
        self.assertTrue(all(ua == "UA-movil" for _, ua in m.llamadas))

    def test_nunca_visita_clics_de_anuncios_ni_red_interna(self):
        m = self.medidor({"https://trk.red.com/c/1": (302, "", "https://www.googleadservices.com/pagead/aclk?x=1", 50)})
        _, r = http_worker.trazar(self.PAYLOAD, self.SALIDA, self.RES, m)
        self.assertEqual(r["error"], "ad_click_host")
        self.assertEqual([u for u, _ in m.llamadas], ["https://trk.red.com/c/1"])
        m = self.medidor({"https://trk.red.com/c/1": (302, "", "http://interna.es/admin", 50)})
        _, r = http_worker.trazar(self.PAYLOAD, self.SALIDA, self.RES, m)
        self.assertEqual(r["error"], "url_not_public")
        self.assertEqual(len(m.llamadas), 1)

    def test_bucle_y_fallo_del_movil(self):
        m = self.medidor({"https://trk.red.com/c/1": (302, "", "https://trk.red.com/c/1", 50)})
        _, r = http_worker.trazar(dict(self.PAYLOAD, max_hops=3), self.SALIDA, self.RES, m)
        self.assertEqual((r["error"], len(r["hops"])), ("too_many_hops", 4))
        m = self.medidor({"https://trk.red.com/c/1": captura.ErrorRed("curl 56")}, control=captura.ErrorRed("sin red"))
        _, r = http_worker.trazar(self.PAYLOAD, self.SALIDA, self.RES, m)
        self.assertEqual(r["classification"], "node_error")

    def test_ejecutar_trace(self):
        tmp = tempfile.TemporaryDirectory()
        granja_lock.LOCK_DIR = tmp.name
        m = self.medidor({"https://trk.red.com/c/1": (200, "ok", "", 40)})
        tarea = {"id": 3, "type": "trace", "node": "m1", "payload": self.PAYLOAD}
        status, r = http_worker.ejecutar(tarea, set(), self.RES, m, lambda mid: self.SALIDA)
        self.assertEqual((status, r["final_url"]), ("ok", "https://trk.red.com/c/1"))
        tmp.cleanup()
