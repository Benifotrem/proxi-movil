"""HTTP con ritmo bajo: pausa con variación entre peticiones al mismo host y reintentos con espera creciente.
Conexión directa a fuentes públicas (BDNS, BOE); respeta los límites de cada servicio, no los elude."""
import http.client
import json
import logging
import random
import time
import urllib.error
import urllib.request

log = logging.getLogger("b2b")
AGENTE = "alicante-b2b-worker/1.0 (consulta de datos abiertos; contacto en la web del usuario)"


class ErrorConsulta(Exception):
    pass


def forzar_ipv4():
    """Resuelve solo direcciones IPv4 (el HP no tiene salida IPv6: las webs que anuncian IPv6 daban
    'Network is unreachable'). Si un host solo tiene IPv6, se usa lo que haya."""
    import socket
    if getattr(socket.getaddrinfo, "_solo_ipv4", False):
        return
    original = socket.getaddrinfo

    def getaddrinfo(host, port, family=0, *args, **kwargs):
        if family in (0, socket.AF_UNSPEC):
            try:
                return original(host, port, socket.AF_INET, *args, **kwargs)
            except socket.gaierror:
                pass
        return original(host, port, family, *args, **kwargs)
    getaddrinfo._solo_ipv4 = True
    socket.getaddrinfo = getaddrinfo


class Cliente:
    def __init__(self, pausa=(1.0, 2.0), reintentos=3, dormir=time.sleep, abrir=urllib.request.urlopen):
        self.pausa, self.reintentos, self.dormir, self.abrir = pausa, reintentos, dormir, abrir
        self._ultima = {}

    def _turno(self, host):
        espera = self._ultima.get(host, 0) + random.uniform(*self.pausa) - time.monotonic()
        if espera > 0:
            self.dormir(espera)
        self._ultima[host] = time.monotonic()

    def get(self, url, cabeceras=None, binario=False, timeout=30, cuerpo=None):
        host = url.split("/")[2]
        ultimo = None
        try:
            urllib.request.Request(url)
        except ValueError as e:  # URL mal formada: no se reintenta
            raise ErrorConsulta(f"URL no válida: {e}"[:120]) from None
        for intento in range(1, self.reintentos + 1):
            self._turno(host)
            req = urllib.request.Request(url, data=cuerpo, method="POST" if cuerpo is not None else "GET",
                                         headers={"User-Agent": AGENTE, **(cabeceras or {})})
            try:
                with self.abrir(req, timeout=timeout) as r:
                    datos = r.read()
                return datos if binario else datos.decode("utf-8", "replace")
            except urllib.error.HTTPError as e:
                ultimo = f"HTTP {e.code}"
                if e.code in (400, 401, 403, 404):
                    raise ErrorConsulta(ultimo) from None
                # 429/5xx: el servicio pide calma; se espera más en cada intento.
                retry = e.headers.get("Retry-After") if e.headers else None
                espera = int(retry) if retry and retry.isdigit() else 10 * intento
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError, http.client.HTTPException) as e:
                ultimo = f"{type(e).__name__}: {e}"[:120]
                espera = 5 * intento
            log.warning("  %s: %s (intento %d/%d), esperando %ds", host, ultimo, intento, self.reintentos, espera)
            self.dormir(espera)
        raise ErrorConsulta(ultimo or "sin respuesta")

    def json(self, url, cabeceras=None, enviar=None):
        cuerpo = json.dumps(enviar).encode() if enviar is not None else None
        extra = {"Content-Type": "application/json"} if cuerpo is not None else {}
        texto = self.get(url, {"Accept": "application/json", **extra, **(cabeceras or {})}, cuerpo=cuerpo)
        try:
            return json.loads(texto)
        except ValueError:
            raise ErrorConsulta("respuesta no JSON") from None
