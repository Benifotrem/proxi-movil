"""API de control de la granja de móviles (escucha en :5000, se accede desde el VPS por Tailscale).

Rutas:
  GET /status          estado e IP pública de cada móvil del inventario
  GET /rotate/<id>     rota la IP de un móvil (modo avión o datos móviles vía ADB)
  GET /usage           tráfico acumulado por móvil (bytes, desde la instalación)
  GET /health          estado de la granja sin consultar IPs (rápido)

El inventario y las credenciales se leen en cada petición: añadir un móvil no requiere reiniciar.
"""
import json
import os
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse

CONF_DIR = os.environ.get("PROXI_GRANJA_CONF", "/etc/proxi-granja")
STATE_DIR = os.environ.get("PROXI_GRANJA_STATE", "/run/proxi-granja")
DATA_DIR = os.environ.get("PROXI_GRANJA_DATA", "/var/lib/proxi-granja")
LISTEN_PORT = int(os.environ.get("PROXI_GRANJA_API_PORT", "5000"))
IP_URL = os.environ.get("PROXI_GRANJA_IP_URL", "https://api.ipify.org")

BASE_PUERTO = 20000
PUERTO_INTERNO = 3128
IP_TIMEOUT = 10             # segundos máximos por consulta de IP pública
ADB_TIMEOUT = 15            # segundos máximos por comando ADB
ROTATE_MIN_INTERVAL = 10    # segundos mínimos entre rotaciones del mismo móvil
ROTATE_PAUSE = 3            # segundos con la red celular apagada
NEW_IP_WAIT = 25            # segundos máximos esperando la IP nueva
ESTADO_MAX_EDAD = 120       # si estado.json es más viejo, la sincronización no está corriendo

_locks = {}
_last_rotation = {}
_locks_guard = threading.Lock()


def inventario():
    modems = {}
    with open(os.path.join(CONF_DIR, "modems.conf")) as f:
        for linea in f:
            campos = linea.split("#", 1)[0].split()
            if len(campos) < 5:
                continue
            mid, usb, serial, puerto, rotacion = campos[:5]
            modems[mid] = {"usb": usb, "serial": serial, "port": int(puerto), "rotation": rotacion}
    return modems


def credenciales():
    creds = {}
    try:
        with open(os.path.join(CONF_DIR, "credentials")) as f:
            for linea in f:
                campos = linea.split("#", 1)[0].split()
                if len(campos) >= 3:
                    creds[campos[0]] = (campos[1], campos[2])
    except FileNotFoundError:
        pass
    return creds


def estado_sync():
    try:
        with open(os.path.join(STATE_DIR, "estado.json")) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"updated": 0, "modems": {}}


def ip_publica(mid, modem, creds):
    """IP pública del móvil, consultada a través de su propio 3proxy."""
    if mid not in creds:
        return ""
    usuario, clave = creds[mid]
    n = modem["port"] - BASE_PUERTO
    # Las credenciales van por stdin (-K -) para que no aparezcan en la lista de procesos.
    config = f'proxy = "http://10.200.{n}.2:{PUERTO_INTERNO}"\nproxy-user = "{usuario}:{clave}"\n'
    try:
        out = subprocess.run(
            ["curl", "-s", "-m", str(IP_TIMEOUT), "-K", "-", IP_URL],
            input=config, capture_output=True, text=True, timeout=IP_TIMEOUT + 5
        )
        return out.stdout.strip()
    except subprocess.TimeoutExpired:
        return ""


def adb(serial, *args):
    subprocess.run(["adb", "-s", serial, "shell", *args],
                   capture_output=True, text=True, timeout=ADB_TIMEOUT, check=True)


def lock_de(mid):
    with _locks_guard:
        return _locks.setdefault(mid, threading.Lock())


class GranjaHandler(BaseHTTPRequestHandler):
    timeout = 30  # evita que una conexión a medias bloquee el hilo indefinidamente

    def do_GET(self):
        path = urlparse(self.path).path.strip("/")
        parts = path.split("/")
        try:
            if len(parts) == 2 and parts[0] == "rotate":
                self.handle_rotate(parts[1].lower())
            elif path == "status":
                self.handle_status()
            elif path == "usage":
                self.handle_usage()
            elif path == "health":
                self.handle_health()
            else:
                self.send_json(404, {"error": "Ruta no encontrada. Usa /status, /rotate/<id>, /usage o /health"})
        except Exception as exc:  # nunca dejar al cliente sin respuesta
            self.send_json(500, {"error": f"{type(exc).__name__}: {exc}"})

    def handle_status(self):
        modems = inventario()
        creds = credenciales()
        sync = estado_sync().get("modems", {})
        with ThreadPoolExecutor(max_workers=max(1, min(64, len(modems)))) as pool:
            ips = dict(zip(modems, pool.map(lambda m: ip_publica(m, modems[m], creds), modems)))
        self.send_json(200, {"modems": {
            mid: {
                "port": m["port"],
                "ip": ips[mid] or None,
                "online": bool(ips[mid]),
                "usb_present": sync.get(mid, {}).get("usb_present", False),
            } for mid, m in modems.items()
        }})

    def handle_rotate(self, mid):
        modems = inventario()
        if mid not in modems:
            self.send_json(404, {"error": f"Móvil '{mid}' no está en el inventario"})
            return
        modem = modems[mid]
        lock = lock_de(mid)
        if not lock.acquire(blocking=False):
            self.send_json(409, {"status": "busy", "device": mid, "error": "Rotación ya en curso"})
            return
        try:
            espera = ROTATE_MIN_INTERVAL - (time.monotonic() - _last_rotation.get(mid, -1e9))
            if espera > 0:
                self.send_json(429, {"status": "too_soon", "device": mid, "retry_after": round(espera, 1)})
                return

            inicio = time.monotonic()
            creds = credenciales()
            ip_anterior = ip_publica(mid, modem, creds)
            try:
                if modem["rotation"] == "airplane":
                    # Solo la radio celular: el anclaje USB debe seguir activo.
                    adb(modem["serial"], "settings", "put", "global", "airplane_mode_radios", "cell")
                    adb(modem["serial"], "cmd", "connectivity", "airplane-mode", "enable")
                    time.sleep(ROTATE_PAUSE)
                    adb(modem["serial"], "cmd", "connectivity", "airplane-mode", "disable")
                else:
                    adb(modem["serial"], "svc", "data", "disable")
                    time.sleep(ROTATE_PAUSE)
                    adb(modem["serial"], "svc", "data", "enable")
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
                self.send_json(502, {"status": "adb_error", "device": mid, "error": str(exc)})
                return
            finally:
                _last_rotation[mid] = time.monotonic()

            ip_nueva = ""
            limite = time.monotonic() + NEW_IP_WAIT
            while time.monotonic() < limite:
                ip_nueva = ip_publica(mid, modem, creds)
                if ip_nueva and ip_nueva != ip_anterior:
                    break
                time.sleep(1.5)

            self.send_json(200 if ip_nueva else 504, {
                "status": "success" if ip_nueva else "no_ip",
                "device": mid,
                "port": modem["port"],
                "old_ip": ip_anterior or None,
                "new_ip": ip_nueva or None,
                "changed": bool(ip_nueva) and ip_nueva != ip_anterior,
                "duration_ms": int((time.monotonic() - inicio) * 1000),
            })
        finally:
            lock.release()

    def handle_usage(self):
        modems = inventario()
        uso = {}
        db = os.path.join(DATA_DIR, "trafico.db")
        try:
            with open(db) as f:
                for linea in f:
                    campos = linea.split()
                    if len(campos) >= 3 and campos[0] in modems:
                        bajada, subida = int(campos[1]), int(campos[2])
                        uso[campos[0]] = {"down_bytes": bajada, "up_bytes": subida, "total_bytes": bajada + subida}
            actualizado = int(os.path.getmtime(db))
        except FileNotFoundError:
            actualizado = 0
        self.send_json(200, {"updated": actualizado, "modems": {
            mid: uso.get(mid, {"down_bytes": 0, "up_bytes": 0, "total_bytes": 0}) for mid in modems
        }})

    def handle_health(self):
        modems = inventario()
        sync = estado_sync()
        edad = int(time.time()) - int(sync.get("updated", 0))
        estados = sync.get("modems", {})
        conectados = sum(1 for m in modems if estados.get(m, {}).get("usb_present"))
        con_red = sum(1 for m in modems if estados.get(m, {}).get("gateway"))
        ok = edad <= ESTADO_MAX_EDAD and con_red == len(modems)
        self.send_json(200, {
            "status": "ok" if ok else "degraded",
            "sync_age_s": edad,
            "modems_total": len(modems),
            "modems_usb_present": conectados,
            "modems_with_network": con_red,
        })

    def send_json(self, code, payload):
        cuerpo = json.dumps(payload, indent=2).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(cuerpo)))
        self.end_headers()
        self.wfile.write(cuerpo)

    def log_message(self, format, *args):
        return


if __name__ == "__main__":
    server = ThreadingHTTPServer(("0.0.0.0", LISTEN_PORT), GranjaHandler)
    server.daemon_threads = True
    print(f"[*] API de la granja activa en puerto {LISTEN_PORT}...")
    server.serve_forever()
