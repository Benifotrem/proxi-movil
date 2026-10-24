"""API de control de la granja de móviles (escucha en :5000, se accede desde el VPS por Tailscale).

Rutas:
  GET /status          estado e IP pública de cada móvil del inventario
  GET /rotate/<id>     rota la IP de un móvil (modo avión o datos móviles vía ADB)
  GET /usage           tráfico acumulado por móvil (bytes, desde la instalación)
  GET /limit/<id>/<kbps>  limita la velocidad del móvil (FUP); /limit/<id>/off la restablece
  GET /credentials/<id>/new  genera usuario y contraseña nuevos para el móvil (las anteriores dejan de valer)
  GET /health          estado de la granja sin consultar IPs (rápido)

El inventario y las credenciales se leen en cada petición: añadir un móvil no requiere reiniciar.
"""
import ipaddress
import json
import os
import secrets
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
LIMITE_MIN_KBPS, LIMITE_MAX_KBPS = 64, 1000000
SYNC_INTERVALO = 30         # proxi-granja sync aplica los límites en como mucho este tiempo

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
    """Credenciales de 3proxy por móvil: las generadas por la API tienen prioridad sobre el archivo manual."""
    creds = {}
    try:
        with open(os.path.join(CONF_DIR, "credentials")) as f:
            for linea in f:
                campos = linea.split("#", 1)[0].split()
                if len(campos) >= 3:
                    creds[campos[0]] = (campos[1], campos[2])
    except FileNotFoundError:
        pass
    directorio = os.path.join(DATA_DIR, "credenciales")
    if os.path.isdir(directorio):
        for mid in os.listdir(directorio):
            if mid.endswith(".tmp"):
                continue
            with open(os.path.join(directorio, mid)) as f:
                campos = f.read().split()
            if len(campos) >= 2:
                creds[mid] = (campos[0], campos[1])
    return creds


def escribir_atomico(ruta, contenido, modo=0o644):
    # El temporal se crea fuera del directorio de destino: proxi-granja-sync.path vigila esos
    # directorios y debe dispararse una sola vez, con el archivo ya completo (rename atómico).
    os.makedirs(os.path.dirname(ruta), exist_ok=True)
    tmp = os.path.join(DATA_DIR, ".tmp", f"{os.path.basename(ruta)}.{secrets.token_hex(4)}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, modo)
    with os.fdopen(fd, "w") as f:
        f.write(contenido)
    os.replace(tmp, ruta)


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
    except subprocess.TimeoutExpired:
        return ""
    ip = out.stdout.strip()
    # Solo una IP: un portal cautivo o un error del servicio no deben pasar por "IP".
    try:
        ipaddress.ip_address(ip)
        return ip
    except ValueError:
        return ""


class AdbError(Exception):
    pass


def limite_kbps(mid):
    try:
        with open(os.path.join(DATA_DIR, "limites", mid)) as f:
            valor = f.read().strip()
        return int(valor) if valor.isdigit() else None
    except FileNotFoundError:
        return None


def adb(serial, *args):
    out = subprocess.run(["adb", "-s", serial, "shell", *args],
                         capture_output=True, text=True, timeout=ADB_TIMEOUT)
    if out.returncode != 0:
        lineas = (out.stderr or out.stdout).strip().splitlines()
        raise AdbError(f"adb {' '.join(args)}: {lineas[0] if lineas else f'código {out.returncode}'}")


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
            elif len(parts) == 3 and parts[0] == "limit":
                self.handle_limit(parts[1].lower(), parts[2].lower())
            elif len(parts) == 3 and parts[0] == "credentials" and parts[2] == "new":
                self.handle_new_credentials(parts[1].lower())
            elif path == "status":
                self.handle_status()
            elif path == "usage":
                self.handle_usage()
            elif path == "health":
                self.handle_health()
            else:
                self.send_json(404, {"error": "Ruta no encontrada. Usa /status, /rotate/<id>, /limit/<id>/<kbps|off>, /credentials/<id>/new, /usage o /health"})
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
                "limit_kbps": limite_kbps(mid),
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
                    # Que el modo avión apague solo la radio celular. Algunas ROM (MIUI) no dejan
                    # cambiar este ajuste por ADB; el anclaje USB no suele estar en la lista por defecto.
                    try:
                        adb(modem["serial"], "settings", "put", "global", "airplane_mode_radios", "cell")
                    except AdbError:
                        pass
                    adb(modem["serial"], "cmd", "connectivity", "airplane-mode", "enable")
                    time.sleep(ROTATE_PAUSE)
                    adb(modem["serial"], "cmd", "connectivity", "airplane-mode", "disable")
                else:
                    adb(modem["serial"], "svc", "data", "disable")
                    time.sleep(ROTATE_PAUSE)
                    adb(modem["serial"], "svc", "data", "enable")
            except (AdbError, subprocess.TimeoutExpired) as exc:
                # 500 y 503 (no 502/504): Cloudflare sustituye esos por su propia página de error.
                self.send_json(500, {"status": "adb_error", "device": mid, "error": str(exc)})
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

            self.send_json(200 if ip_nueva else 503, {
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

    def handle_limit(self, mid, valor):
        if mid not in inventario():
            self.send_json(404, {"error": f"Móvil '{mid}' no está en el inventario"})
            return
        directorio = os.path.join(DATA_DIR, "limites")
        ruta = os.path.join(directorio, mid)
        if valor == "off":
            try:
                os.remove(ruta)
            except FileNotFoundError:
                pass
            kbps = None
        elif valor.isdigit() and LIMITE_MIN_KBPS <= int(valor) <= LIMITE_MAX_KBPS:
            kbps = int(valor)
            escribir_atomico(ruta, f"{kbps}\n")
        else:
            self.send_json(400, {"error": f"Usa /limit/<id>/off o /limit/<id>/<kbps> ({LIMITE_MIN_KBPS}-{LIMITE_MAX_KBPS})"})
            return
        self.send_json(200, {"device": mid, "limit_kbps": kbps, "applies_within_s": SYNC_INTERVALO})

    def handle_new_credentials(self, mid):
        modems = inventario()
        if mid not in modems:
            self.send_json(404, {"error": f"Móvil '{mid}' no está en el inventario"})
            return
        usuario = "u" + secrets.token_hex(5)
        clave = secrets.token_hex(16)
        escribir_atomico(os.path.join(DATA_DIR, "credenciales", mid), f"{usuario} {clave}\n", 0o600)
        self.send_json(200, {
            "device": mid,
            "port": modems[mid]["port"],
            "user": usuario,
            "password": clave,
            "applies_within_s": SYNC_INTERVALO,
        })

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
