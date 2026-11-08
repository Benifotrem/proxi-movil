"""Lector de mensajes del laboratorio QA (nodecellular.com).

Cada SMS_POLL segundos lee los mensajes entrantes de cada móvil del inventario con su driver (Android:
notificaciones de la app de mensajes por ADB; iPhone: sms.db por SSH) y envía los nuevos a la plataforma
(POST {API}/internal/node/<id>/sms). La plataforma solo guarda los que van a una sesión QA activa en ese
móvil con el remitente autorizado; el resto se descarta allí. Este proceso no guarda ni registra el texto.

Hora de llegada: la del propio aviso si el móvil la da (Android 10+, iPhone); si no (Android 9), el momento
en que el lector lo ve por primera vez. Al arrancar, los avisos ya presentes no se envían salvo que traigan
hora y sean de los últimos REENVIO_ARRANQUE segundos (cubre un reinicio del lector).

Configuración: la de los trabajadores (/etc/proxi-granja/captura.env): CAPTURA_API_URL, CAPTURA_TOKEN.
"""
import json
import os
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import drivers  # noqa: E402

API_URL = os.environ.get("CAPTURA_API_URL", "https://nodecellular.com/worker").rstrip("/")
TOKEN = os.environ.get("CAPTURA_TOKEN", "")
POLL = float(os.environ.get("SMS_POLL", "3"))
REENVIO_ARRANQUE = 120
OLVIDO = 24 * 3600
MAX_PENDIENTES = 200


def _inventario():
    from api import inventario
    return inventario()


def api(ruta, datos):
    req = urllib.request.Request(API_URL + ruta, data=json.dumps(datos).encode(), method="POST", headers={
        "Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json", "Accept": "application/json",
        "User-Agent": "proxi-sms/1"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode() or "{}")


class Lector:
    def __init__(self, enviar=api, reloj=time.time):
        self.enviar, self.reloj = enviar, reloj
        self.vistos = {}         # (móvil, key, texto) → cuándo se vio
        self.iniciados = set()   # móviles con la primera lectura hecha
        self.pendientes = {}     # móvil → mensajes que no se pudieron enviar

    def nuevos(self, mid, avisos):
        """Mensajes que no se habían visto, con su hora de llegada en ms."""
        ahora = self.reloj()
        primera = mid not in self.iniciados
        self.iniciados.add(mid)
        res = []
        for a in avisos:
            clave = (mid, a["key"], a["body"])
            if clave in self.vistos:
                continue
            self.vistos[clave] = ahora
            when = a.get("when_ms")
            if primera and not (when and ahora * 1000 - when <= REENVIO_ARRANQUE * 1000):
                continue  # ya estaba antes de arrancar
            res.append({"sender": a["sender"][:64], "body": a["body"][:4000], "received_at_ms": int(when or ahora * 1000)})
        for clave, t in list(self.vistos.items()):
            if ahora - t > OLVIDO:
                del self.vistos[clave]
        return res

    def ciclo(self, inventario, driver_para=drivers.driver_para):
        for mid, modem in inventario.items():
            try:
                avisos = driver_para(modem).read_incoming()
            except Exception as exc:  # noqa: BLE001 - un móvil sin ADB no para a los demás
                print(f"sms: {mid}: no se pudo leer ({type(exc).__name__})", flush=True)
                continue
            lote = self.pendientes.pop(mid, []) + self.nuevos(mid, avisos)
            if not lote:
                continue
            try:
                r = self.enviar(f"/internal/node/{mid}/sms", {"messages": lote[:50]})
                print(f"sms: {mid}: {len(lote[:50])} mensaje(s), {r.get('stored', 0)} entregado(s) a sesiones", flush=True)
                if lote[50:]:
                    self.pendientes[mid] = lote[50:]
            except Exception as exc:  # noqa: BLE001 - se reintenta en el siguiente ciclo
                self.pendientes[mid] = lote[-MAX_PENDIENTES:]
                print(f"sms: {mid}: envío fallido, se reintentará ({type(exc).__name__})", flush=True)


def main():
    if len(TOKEN) < 32:
        sys.exit("Configura CAPTURA_TOKEN (>=32 caracteres) en /etc/proxi-granja/captura.env")
    print(f"sms: API {API_URL}, cada {POLL:g} s", flush=True)
    lector = Lector()
    while True:
        inicio = time.monotonic()
        try:
            lector.ciclo(_inventario())
        except Exception as exc:  # noqa: BLE001
            print(f"sms: ciclo con error: {type(exc).__name__}: {exc}", flush=True)
        time.sleep(max(0.5, POLL - (time.monotonic() - inicio)))


if __name__ == "__main__":
    main()
