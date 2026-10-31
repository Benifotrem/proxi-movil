"""Trabajador de captura de muestras (nodecellular.com).

Pide tareas al backend (POST {API}/tasks/claim), visita cada URL de producto a través de un móvil de
la granja y devuelve precio, moneda, disponibilidad y título (POST {API}/tasks/<id>/result).
El backend solo envía la muestra al cliente si TODAS las URLs se verifican.

Reglas de captura:
  - Solo URLs http(s) públicas en 80/443 (filtro SSRF también aquí, tras resolver DNS) y redirecciones
    revisadas una a una: nunca se visita la red local, la granja ni Tailscale.
  - Ritmo bajo: una petición cada CAPTURA_DOMAIN_DELAY segundos por dominio, de una en una.
  - Respeta robots.txt. No resuelve CAPTCHAs ni intenta saltar sistemas antibot: si la web bloquea
    (403/429/503, desafío), la tarea vuelve como fallida y la muestra pasa a revisión humana.
  - Lee los datos que la propia página publica: JSON-LD schema.org Product/Offer, metadatos
    product:/og: y microdatos itemprop.

Configuración (/etc/proxi-granja/captura.env):
  CAPTURA_API_URL=https://nodecellular.com/worker
  CAPTURA_TOKEN=<CAPTURE_WORKER_TOKEN del backend>
  CAPTURA_MODEMS=modem_2_3,modem_2_4      móviles por los que salen las visitas (rotación)
"""
import ipaddress
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from html import unescape
from html.parser import HTMLParser
from urllib import robotparser
from urllib.parse import urljoin, urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

API_URL = os.environ.get("CAPTURA_API_URL", "https://nodecellular.com/worker").rstrip("/")
TOKEN = os.environ.get("CAPTURA_TOKEN", "")
MODEMS = [m for m in os.environ.get("CAPTURA_MODEMS", "").replace(" ", "").split(",") if m]
DOMAIN_DELAY = float(os.environ.get("CAPTURA_DOMAIN_DELAY", "8"))
IDLE_SLEEP = int(os.environ.get("CAPTURA_IDLE_SLEEP", "30"))
FETCH_TIMEOUT = 25
MAX_BYTES = 3_000_000
MAX_REDIRECTS = 4
USER_AGENT = os.environ.get(
    "CAPTURA_USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0 Safari/537.36",
)
BASE_PUERTO = 20000
PUERTO_INTERNO = 3128
CGNAT = ipaddress.ip_network("100.64.0.0/10")


# --------------------------------------------------------------------------- filtro SSRF

def ip_publica(ip: str) -> bool:
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if a.version == 6 and a.ipv4_mapped:
        a = a.ipv4_mapped
    return a.is_global and not (a.version == 4 and a in CGNAT)


def url_permitida(url: str, resolver=socket.getaddrinfo) -> bool:
    """http(s), puerto 80/443, sin usuario, host con dominio y TODAS sus IPs públicas."""
    try:
        p = urlparse(url)
    except ValueError:
        return False
    if p.scheme not in ("http", "https") or not p.hostname or p.username or p.password:
        return False
    try:
        if p.port not in (None, 80, 443):
            return False
    except ValueError:
        return False
    host = p.hostname.rstrip(".").lower()
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        return False
    try:  # IP literal
        return ip_publica(str(ipaddress.ip_address(host)))
    except ValueError:
        pass
    if not re.fullmatch(r"([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}", host):
        return False
    try:
        ips = {info[4][0] for info in resolver(host, None)}
    except (socket.gaierror, UnicodeError):
        return False
    return bool(ips) and all(ip_publica(ip) for ip in ips)


# --------------------------------------------------------------------------- extracción

class _Recolector(HTMLParser):
    """JSON-LD, metadatos y microdatos de precio de una página."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.jsonld, self.meta, self.itemprop = [], {}, {}
        self.title = ""
        self._en_jsonld = self._en_title = False
        self._buf = []
        self._prop_texto = None

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "script" and a.get("type", "").lower().strip() == "application/ld+json":
            self._en_jsonld, self._buf = True, []
        elif tag == "title":
            self._en_title = True
        elif tag == "meta":
            clave = (a.get("property") or a.get("name") or a.get("itemprop") or "").lower()
            if clave and "content" in a:
                self.meta.setdefault(clave, a["content"].strip())
        prop = a.get("itemprop", "").lower()
        if prop in ("price", "pricecurrency", "availability", "name", "lowprice"):
            valor = a.get("content") or a.get("href") or ""
            if valor:
                self.itemprop.setdefault(prop, valor.strip())
            elif tag != "meta":
                self._prop_texto, self._buf = prop, []

    def handle_endtag(self, tag):
        if tag == "script" and self._en_jsonld:
            self._en_jsonld = False
            self.jsonld.append("".join(self._buf))
        elif tag == "title":
            self._en_title = False
        elif self._prop_texto:
            self.itemprop.setdefault(self._prop_texto, "".join(self._buf).strip())
            self._prop_texto = None

    def handle_data(self, data):
        if self._en_jsonld or self._prop_texto:
            self._buf.append(data)
        elif self._en_title:
            self.title += data


def precio_num(valor):
    """'24,50', '24.50', '1.234,56 €', '1,234.56' → float; None si no es un precio."""
    if valor is None:
        return None
    if isinstance(valor, (int, float)):
        return float(valor) if valor > 0 else None
    s = re.sub(r"[^\d,.]", "", str(valor))
    if not s or not re.search(r"\d", s):
        return None
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".") if s.rfind(",") > s.rfind(".") else s.replace(",", "")
    elif "," in s:
        partes = s.split(",")
        s = s.replace(",", ".") if len(partes) == 2 and len(partes[1]) in (1, 2) else s.replace(",", "")
    elif s.count(".") > 1 or (s.count(".") == 1 and len(s.split(".")[1]) == 3):
        s = s.replace(".", "")  # "1.234" o "1.234.567": separador de miles
    try:
        n = float(s)
    except ValueError:
        return None
    return n if 0 < n < 10_000_000 else None


def disponibilidad(valor):
    v = str(valor or "").lower()
    if any(x in v for x in ("outofstock", "out of stock", "soldout", "discontinued", "agotado", "out_of_stock", "oos")):
        return "out_of_stock"
    if any(x in v for x in ("instock", "in stock", "limitedavailability", "preorder", "onlineonly", "in_stock", "instoreonly")):
        return "in_stock"
    return "unknown"


def _nodos(obj):
    if isinstance(obj, list):
        for x in obj:
            yield from _nodos(x)
    elif isinstance(obj, dict):
        yield obj
        for clave in ("@graph", "mainEntity", "itemListElement"):
            if clave in obj:
                yield from _nodos(obj[clave])


def _tipos(nodo):
    t = nodo.get("@type", [])
    return {x.lower() for x in (t if isinstance(t, list) else [t]) if isinstance(x, str)}


def _oferta(producto):
    ofertas = producto.get("offers")
    lista = ofertas if isinstance(ofertas, list) else [ofertas] if isinstance(ofertas, dict) else []
    mejores = []
    for o in lista:
        if not isinstance(o, dict):
            continue
        spec = o.get("priceSpecification")
        spec = spec[0] if isinstance(spec, list) and spec else spec
        precio = precio_num(o.get("price") or o.get("lowPrice") or (spec or {}).get("price"))
        mejores.append((precio, o.get("priceCurrency") or (spec or {}).get("priceCurrency"), disponibilidad(o.get("availability"))))
    con_precio = [m for m in mejores if m[0] is not None]
    if con_precio:
        return min(con_precio, key=lambda m: m[0])
    return mejores[0] if mejores else (None, None, "unknown")


def extraer(html: str) -> dict:
    """Datos de producto de una página. status 'ok' si hay precio (o consta sin stock)."""
    r = _Recolector()
    try:
        r.feed(html)
    except Exception:  # HTML roto: nos quedamos con lo recogido
        pass

    for bloque in r.jsonld:
        try:
            datos = json.loads(bloque.strip().strip(";"))
        except json.JSONDecodeError:
            continue
        for nodo in _nodos(datos):
            tipos = _tipos(nodo)
            if tipos & {"product", "productgroup", "offer", "aggregateoffer"}:
                if tipos & {"offer", "aggregateoffer"}:
                    precio, moneda, disp = _oferta({"offers": nodo})
                else:
                    precio, moneda, disp = _oferta(nodo)
                if precio is not None or disp == "out_of_stock":
                    return _resultado(precio, moneda, disp, nodo.get("name") or r.meta.get("og:title") or r.title, "jsonld")

    m = r.meta
    precio = precio_num(m.get("product:price:amount") or m.get("og:price:amount") or m.get("price"))
    disp = disponibilidad(m.get("product:availability") or m.get("og:availability"))
    if precio is not None:
        return _resultado(precio, m.get("product:price:currency") or m.get("og:price:currency"), disp,
                          m.get("og:title") or r.title, "meta")

    ip = r.itemprop
    precio = precio_num(ip.get("price") or ip.get("lowprice"))
    disp = disponibilidad(ip.get("availability"))
    if precio is not None or disp == "out_of_stock":
        return _resultado(precio, ip.get("pricecurrency"), disp, ip.get("name") or m.get("og:title") or r.title, "microdata")

    return {"status": "failed", "error": "no_price_found", "title": _limpio(m.get("og:title") or r.title)}


def _limpio(texto):
    return re.sub(r"\s+", " ", unescape(str(texto or ""))).strip()[:300] or None


def _resultado(precio, moneda, disp, titulo, metodo):
    moneda = (str(moneda).upper()[:3] if moneda else "EUR")
    return {"status": "ok", "price": round(precio, 2) if precio is not None else None, "currency": moneda,
            "availability": disp, "title": _limpio(titulo), "method": metodo}


BLOQUEO = re.compile(r"captcha|are you a robot|verify you are human|access denied|cf-chl|challenge-platform|px-captcha|distil", re.I)


# --------------------------------------------------------------------------- red (por un móvil de la granja)

def _credenciales():
    from api import credenciales  # mismo directorio (/opt/proxi-granja)
    return credenciales()


def _inventario():
    from api import inventario
    return inventario()


class Salida:
    """Proxy 3proxy de un móvil de la granja, en rotación entre CAPTURA_MODEMS."""

    def __init__(self, modems):
        self.modems, self.i = modems, 0

    def siguiente(self):
        inv, creds = _inventario(), _credenciales()
        for _ in range(len(self.modems)):
            mid = self.modems[self.i % len(self.modems)]
            self.i += 1
            if mid in inv and mid in creds:
                n = inv[mid]["port"] - BASE_PUERTO
                usuario, clave = creds[mid]
                return f"http://10.200.{n}.2:{PUERTO_INTERNO}", f"{usuario}:{clave}"
        raise RuntimeError("ningún móvil de CAPTURA_MODEMS disponible")


def descargar(url, salida, cabeceras_extra=()):
    """GET con curl por el proxy del móvil. Devuelve (código, cuerpo, location, content_type)."""
    proxy, auth = salida
    with tempfile.NamedTemporaryFile() as cuerpo:
        config = f'proxy = "{proxy}"\nproxy-user = "{auth}"\n'  # por stdin: no aparece en la lista de procesos
        cmd = ["curl", "-sS", "--compressed", "--proto", "=http,https", "--max-time", str(FETCH_TIMEOUT),
               "--max-filesize", str(MAX_BYTES), "-A", USER_AGENT,
               "-H", "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
               "-H", "Accept-Language: es-ES,es;q=0.9,en;q=0.6",
               "-o", cuerpo.name, "-w", "%{http_code}\n%{redirect_url}\n%{content_type}", "-K", "-"]
        for h in cabeceras_extra:
            cmd[1:1] = ["-H", h]
        out = subprocess.run(cmd + [url], input=config, capture_output=True, text=True, timeout=FETCH_TIMEOUT + 10)
        if out.returncode not in (0, 63):  # 63: supera max-filesize
            raise RuntimeError(f"curl {out.returncode}: {out.stderr.strip()[:120]}")
        codigo, location, ctype = (out.stdout.split("\n") + ["", "", ""])[:3]
        datos = open(cuerpo.name, "rb").read(MAX_BYTES)
    return int(codigo or 0), datos.decode("utf-8", "replace"), location.strip(), ctype.strip()


class Robots:
    def __init__(self):
        self.cache = {}

    def permitido(self, url, salida):
        p = urlparse(url)
        base = f"{p.scheme}://{p.netloc}"
        ahora = time.time()
        if base not in self.cache or self.cache[base][0] < ahora - 3600:
            rp = robotparser.RobotFileParser()
            try:
                codigo, texto, _, _ = descargar(base + "/robots.txt", salida)
                rp.parse(texto.splitlines() if codigo == 200 else [])
            except Exception:
                rp.parse([])
            self.cache[base] = (ahora, rp)
        return self.cache[base][1].can_fetch(USER_AGENT, url)


_ultimo = {}


def _esperar_turno(dominio):
    espera = _ultimo.get(dominio, 0) + DOMAIN_DELAY - time.monotonic()
    if espera > 0:
        time.sleep(espera)
    _ultimo[dominio] = time.monotonic()


def capturar(url, salidas, robots, resolver=socket.getaddrinfo):
    """Resultado para el backend. Nunca lanza: los errores vuelven como status 'failed'."""
    try:
        salida = salidas.siguiente()
        actual = url
        for _ in range(MAX_REDIRECTS + 1):
            if not url_permitida(actual, resolver):
                return {"status": "failed", "error": "url_not_public"}
            if not robots.permitido(actual, salida):
                return {"status": "failed", "error": "robots_disallow"}
            _esperar_turno(urlparse(actual).hostname)
            codigo, html, location, _ = descargar(actual, salida)
            if codigo in (301, 302, 303, 307, 308) and location:
                actual = urljoin(actual, location)
                continue
            if codigo in (401, 403, 429, 503) or (codigo == 200 and BLOQUEO.search(html[:20000]) and "ld+json" not in html):
                return {"status": "failed", "error": f"blocked_{codigo}"}
            if codigo == 404 or codigo == 410:
                return {"status": "failed", "error": f"not_found_{codigo}"}
            if codigo != 200:
                return {"status": "failed", "error": f"http_{codigo}"}
            return extraer(html)
        return {"status": "failed", "error": "too_many_redirects"}
    except Exception as exc:  # noqa: BLE001 - se informa al backend
        return {"status": "failed", "error": f"{type(exc).__name__}: {exc}"[:300]}


# --------------------------------------------------------------------------- API del backend

def api(ruta, datos):
    req = urllib.request.Request(API_URL + ruta, data=json.dumps(datos).encode(), method="POST", headers={
        "Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json", "Accept": "application/json",
        "User-Agent": "proxi-captura/1",
    })
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode() or "{}")


def main():
    if len(TOKEN) < 32 or not MODEMS:
        sys.exit("Configura CAPTURA_TOKEN (>=32 caracteres) y CAPTURA_MODEMS en /etc/proxi-granja/captura.env")
    salidas, robots = Salida(MODEMS), Robots()
    print(f"captura: API {API_URL}, móviles {MODEMS}", flush=True)
    while True:
        try:
            tareas = api("/tasks/claim", {"limit": 5}).get("tasks", [])
        except Exception as exc:  # noqa: BLE001
            print(f"captura: no se pudo pedir tareas: {exc}", flush=True)
            time.sleep(IDLE_SLEEP)
            continue
        if not tareas:
            time.sleep(IDLE_SLEEP)
            continue
        for t in tareas:
            r = capturar(t["url"], salidas, robots)
            print(f"captura: tarea {t['id']} {urlparse(t['url']).hostname} → {r['status']} {r.get('price') or r.get('error') or ''}", flush=True)
            try:
                api(f"/tasks/{t['id']}/result", r)
            except Exception as exc:  # noqa: BLE001 - la reserva caduca y se reintenta
                print(f"captura: no se pudo enviar el resultado {t['id']}: {exc}", flush=True)


if __name__ == "__main__":
    main()
