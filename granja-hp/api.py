import subprocess
import json
import threading
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse

ROTATE_SCRIPT = "/home/cesar/mobile-proxy/rotate.sh"
PORTS = {"redmi": 8081, "lg": 8082}
ROTATE_TIMEOUT = 60  # segundos máximos para rotate.sh
IP_TIMEOUT = 10      # segundos máximos para consultar la IP pública por el proxy

# Una sola rotación a la vez por dispositivo.
ROTATE_LOCKS = {device: threading.Lock() for device in PORTS}


def public_ip(port):
    try:
        out = subprocess.run(
            ["curl", "-s", "-m", str(IP_TIMEOUT), "-x", f"http://127.0.0.1:{port}", "https://api.ipify.org"],
            capture_output=True, text=True, timeout=IP_TIMEOUT + 5
        )
        return out.stdout.strip()
    except subprocess.TimeoutExpired:
        return ""


class ProxyRotateHandler(BaseHTTPRequestHandler):
    timeout = 30  # evita que una conexión a medias bloquee el hilo indefinidamente

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.strip("/")

        # Rutas válidas: /rotate/redmi, /rotate/lg, /status
        parts = path.split("/")

        if len(parts) >= 2 and parts[0] == "rotate":
            device = parts[1].lower()
            if device in PORTS:
                self.handle_rotate(device)
            else:
                self.send_json(400, {"error": "Dispositivo no reconocido. Usa 'redmi' o 'lg'"})
        elif path == "status":
            self.handle_status()
        else:
            self.send_json(404, {"error": "Ruta no encontrada. Usa /rotate/redmi, /rotate/lg o /status"})

    def handle_rotate(self, device):
        lock = ROTATE_LOCKS[device]
        if not lock.acquire(blocking=False):
            self.send_json(409, {"status": "busy", "device": device, "error": "Rotación ya en curso"})
            return
        try:
            try:
                proc = subprocess.run([ROTATE_SCRIPT, device], capture_output=True, text=True, timeout=ROTATE_TIMEOUT)
                log = proc.stdout
            except subprocess.TimeoutExpired:
                self.send_json(504, {"status": "timeout", "device": device, "error": f"rotate.sh superó {ROTATE_TIMEOUT}s"})
                return

            port = PORTS[device]
            self.send_json(200, {
                "status": "success",
                "device": device,
                "port": port,
                "new_ip": public_ip(port),
                "log": log
            })
        finally:
            lock.release()

    def handle_status(self):
        results = {}
        threads = [threading.Thread(target=lambda d=d: results.__setitem__(d, public_ip(PORTS[d]))) for d in PORTS]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.send_json(200, {
            "redmi6": {"port": PORTS["redmi"], "ip": results.get("redmi") or "offline"},
            "lgk9": {"port": PORTS["lg"], "ip": results.get("lg") or "offline"}
        })

    def send_json(self, code, payload):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(payload, indent=2).encode("utf-8"))

    def log_message(self, format, *args):
        # Limitar logs en consola
        return


if __name__ == "__main__":
    server = ThreadingHTTPServer(("0.0.0.0", 5000), ProxyRotateHandler)
    server.daemon_threads = True
    print("[*] Servidor API de rotación activo en puerto 5000...")
    server.serve_forever()
