"""Trabajador de sondas HTTP de la plataforma (Sentinel ISP de nodecellular.com).

Pide tareas al backend (POST {API}/v2/claim), comprueba la URL desde el móvil que indica cada tarea
(su 3proxy interno) y devuelve la clasificación (POST {API}/v2/tasks/<id>/result):

  ok           respuesta 2xx/3xx sin desafío
  http_451     bloqueo por motivos legales
  challenge    página de control antibot (no se intenta superar)
  http_error   otro 4xx/5xx (con http_code)
  timeout      sin respuesta a tiempo
  dns          el dominio no resuelve
  network      el móvil tiene red, pero no llega al dominio (p. ej. bloqueo por IP o DNS del operador)
  node_error   el propio móvil no tiene salida (la plataforma no lo cuenta contra el dominio)

Respeta la rotación: no usa móviles marcados como rotando ni los que la plataforma excluye
(excluded_nodes), y cada sonda va dentro del cerrojo compartido del móvil (granja_lock).
Solo sondea dominios que el cliente ha verificado como propios (lo controla el backend), a ritmo bajo.

Configuración: la misma que el trabajador de captura (/etc/proxi-granja/captura.env):
  CAPTURA_API_URL, CAPTURA_TOKEN;  opcional SONDA_CONTROL_URL (por defecto un 204 público).
"""
import os
import socket
import subprocess
import sys
import tempfile
import time
from urllib.parse import urljoin, urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import captura  # noqa: E402
import granja_lock  # noqa: E402

CONTROL_URL = os.environ.get("SONDA_CONTROL_URL", "https://www.gstatic.com/generate_204")
IDLE_SLEEP = int(os.environ.get("SONDA_IDLE_SLEEP", "15"))
TIMEOUT = 20
MAX_REDIRECTS = 5
LEER_BYTES = 200_000
CURL_TIMEOUT = 28


class Timeout(Exception):
    pass


def salida_de(mid):
    """(proxy, usuario:clave) del 3proxy de un móvil concreto, o None si no está en el inventario."""
    inv, creds = captura._inventario(), captura._credenciales()
    if mid not in inv or mid not in creds:
        return None
    n = inv[mid]["port"] - captura.BASE_PUERTO
    usuario, clave = creds[mid]
    return f"http://10.200.{n}.2:{captura.PUERTO_INTERNO}", f"{usuario}:{clave}"


def medir(url, salida):
    """GET por el proxy del móvil. Devuelve (código, cuerpo inicial, location, ttfb_ms)."""
    proxy, auth = salida
    with tempfile.NamedTemporaryFile() as cuerpo:
        config = f'proxy = "{proxy}"\nproxy-user = "{auth}"\n'  # por stdin: no aparece en la lista de procesos
        cmd = ["curl", "-sS", "--compressed", "--proto", "=http,https", "--max-time", str(TIMEOUT),
               "--max-filesize", str(captura.MAX_BYTES), "-A", captura.USER_AGENT,
               "-H", "Accept: text/html,application/xhtml+xml,*/*;q=0.8", "-H", "Accept-Language: es-ES,es;q=0.9",
               "-o", cuerpo.name, "-w", "%{http_code}\n%{redirect_url}\n%{time_starttransfer}", "-K", "-", url]
        out = subprocess.run(cmd, input=config, capture_output=True, text=True, timeout=TIMEOUT + 10)
        if out.returncode == CURL_TIMEOUT:
            raise Timeout(out.stderr.strip()[:120])
        if out.returncode in captura.CURL_RED:
            raise captura.ErrorRed(f"curl {out.returncode}: {out.stderr.strip()[:120]}")
        if out.returncode not in (0, 63):
            raise RuntimeError(f"curl {out.returncode}: {out.stderr.strip()[:120]}")
        codigo, location, ttfb = (out.stdout.split("\n") + ["", "", ""])[:3]
        datos = open(cuerpo.name, "rb").read(LEER_BYTES)
    try:
        ttfb_ms = int(float(ttfb) * 1000)
    except ValueError:
        ttfb_ms = None
    return int(codigo or 0), datos.decode("utf-8", "replace"), location.strip(), ttfb_ms


def control_ok(salida, medidor):
    """¿Tiene salida a Internet el móvil? (si no, el fallo es del móvil, no del dominio)."""
    try:
        codigo, _, _, _ = medidor(CONTROL_URL, salida)
        return 200 <= codigo < 400
    except Exception:  # noqa: BLE001
        return False


def resuelve(host, resolver):
    try:
        return bool(resolver(host, None))
    except (socket.gaierror, UnicodeError):
        return False


def sondear(url, salida, resolver=socket.getaddrinfo, medidor=medir):
    """Clasificación de una URL desde un móvil. Devuelve (status, resultado) para el backend."""
    actual, saltos = url, 0
    while True:
        host = (urlparse(actual).hostname or "").lower()
        if host and not resuelve(host, resolver):
            return "ok", {"classification": "dns", "error": f"{host} no resuelve", "final_url": actual}
        if not captura.url_permitida(actual, resolver):
            return "failed", {"classification": "node_error", "error": "url_not_public"}
        try:
            codigo, cuerpo, location, ttfb = medidor(actual, salida)
        except Timeout as exc:
            clase = "timeout" if control_ok(salida, medidor) else "node_error"
            return "ok", {"classification": clase, "error": str(exc)[:200], "final_url": actual}
        except captura.ErrorRed as exc:
            clase = "network" if control_ok(salida, medidor) else "node_error"
            return "ok", {"classification": clase, "error": str(exc)[:200], "final_url": actual}
        except Exception as exc:  # noqa: BLE001
            return "failed", {"classification": "node_error", "error": f"{type(exc).__name__}: {exc}"[:200]}

        if codigo in (301, 302, 303, 307, 308) and location and saltos < MAX_REDIRECTS:
            actual, saltos = urljoin(actual, location), saltos + 1
            continue
        base = {"http_code": codigo or None, "ttfb_ms": ttfb, "final_url": actual, "redirects": saltos}
        if codigo == 0:
            clase = "network" if control_ok(salida, medidor) else "node_error"
            return "ok", {"classification": clase, **base}
        if codigo == 451:
            return "ok", {"classification": "http_451", **base}
        if codigo < 400:
            return "ok", {"classification": "challenge" if captura.es_desafio(cuerpo) else "ok", **base}
        if codigo in (403, 429, 503) and captura.es_desafio(cuerpo):
            return "ok", {"classification": "challenge", **base}
        return "ok", {"classification": "http_error", **base}


def ejecutar(tarea, excluidos, resolver=socket.getaddrinfo, medidor=medir, salidas=salida_de):
    """Resultado (status, resultado) de una tarea, o None si no se puede ejecutar ahora (no se informa:
    la tarea caduca y la plataforma programa la siguiente)."""
    mid = tarea.get("node")
    if tarea.get("type") != "probe" or not mid or mid in excluidos:
        return None
    salida = salidas(mid)
    if salida is None:
        return "failed", {"classification": "node_error", "error": "móvil fuera del inventario o sin credenciales"}
    try:
        with granja_lock.en_uso(mid):
            return sondear(tarea["payload"]["url"], salida, resolver, medidor)
    except granja_lock.Rotando:
        return None


def main():
    if len(captura.TOKEN) < 32:
        sys.exit("Configura CAPTURA_TOKEN (>=32 caracteres) en /etc/proxi-granja/captura.env")
    print(f"sondas: API {captura.API_URL}", flush=True)
    while True:
        try:
            r = captura.api("/v2/claim", {"types": ["probe"], "limit": 10})
        except Exception as exc:  # noqa: BLE001
            print(f"sondas: no se pudieron pedir tareas: {exc}", flush=True)
            time.sleep(IDLE_SLEEP)
            continue
        tareas, excluidos = r.get("tasks", []), set(r.get("excluded_nodes", []))
        for t in tareas:
            hecho = ejecutar(t, excluidos)
            if hecho is None:
                print(f"sondas: tarea {t['id']} omitida (móvil {t.get('node')} rotando o excluido)", flush=True)
                continue
            status, resultado = hecho
            print(f"sondas: tarea {t['id']} {t.get('node')} {urlparse(t['payload']['url']).hostname} → "
                  f"{resultado.get('classification')} {resultado.get('http_code') or ''} {resultado.get('ttfb_ms') or ''}", flush=True)
            try:
                captura.api(f"/v2/tasks/{t['id']}/result", {"status": status, "result": resultado})
            except Exception as exc:  # noqa: BLE001 - la tarea caduca y la plataforma programa la siguiente
                print(f"sondas: no se pudo enviar el resultado {t['id']}: {exc}", flush=True)
        if not tareas:
            time.sleep(IDLE_SLEEP)


if __name__ == "__main__":
    main()
