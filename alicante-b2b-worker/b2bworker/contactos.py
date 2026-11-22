"""Email y móvil que la propia empresa publica en su web (portada y páginas de contacto o aviso legal).

Solo se leen páginas públicas de la web de la empresa, respetando su robots.txt, con conexión directa, una
petición cada pocos segundos y sin reintentos insistentes. El email es para enviar la Nota Informativa cuando el
interlocutor lo autorice en la llamada (art. 21 LSSI), no para envíos en frío."""
import html
import re
import urllib.parse
import urllib.robotparser

from .red import AGENTE, ErrorConsulta

EMAIL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._%+-]*@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
MOVIL = re.compile(r"(?<![\d+])(?:(?:\+|00)?34[\s.\-]?)?([67](?:[\s.\-]?\d){8})(?!\d)")
ENLACE = re.compile(r"""href\s*=\s*["']([^"'#]+)["']""", re.I)
PAGINAS = ("contact", "aviso-legal", "aviso_legal", "avisolegal", "legal", "quienes", "nosotros", "empresa", "about")
FALSOS = ("example.", "sentry", "wixpress", "domain.", "dominio.", "email.com", "correo.com", "yourmail", "tuempresa",
          "godaddy", "squarespace", "wordpress", "jquery", "bootstrap", "@2x", "@3x", "user@", "usuario@", "nombre@")
EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".css", ".js", ".ico")
GENERICOS = ("info", "contacto", "contact", "hola", "hello", "administracion", "admin", "oficina", "comercial", "ventas")


def normalizar_web(web):
    w = str(web or "").strip().strip("/")
    if not w or " " in w or "." not in w:
        return ""
    if not re.match(r"https?://", w, re.I):
        w = "https://" + w
    return w.lower()


def _texto(pagina):
    p = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", pagina)
    p = html.unescape(re.sub(r"(?s)<[^>]+>", " ", p))
    p = re.sub(r"\s*[\[\(]\s*(?:at|arroba)\s*[\]\)]\s*", "@", p, flags=re.I)
    return re.sub(r"\s*[\[\(]\s*(?:dot|punto)\s*[\]\)]\s*", ".", p, flags=re.I)


def emails(pagina):
    hrefs = [urllib.parse.unquote(m.split("?")[0]) for m in re.findall(r"""mailto:([^"'>\s]+)""", pagina, re.I)]
    vistos = []
    for e in hrefs + EMAIL.findall(_texto(pagina)):
        e = e.strip(".").lower()
        if e.endswith(EXT) or any(f in e for f in FALSOS) or e in vistos:
            continue
        vistos.append(e)
    return vistos


def moviles(pagina):
    fuentes = re.findall(r"""(?:tel:|wa\.me/|phone=)([+\d\s.\-%20]+)""", pagina, re.I) + [_texto(pagina)]
    vistos = []
    for f in fuentes:
        for m in MOVIL.findall(urllib.parse.unquote(f)):
            d = re.sub(r"\D", "", m)
            t = f"{d[:3]} {d[3:5]} {d[5:7]} {d[7:]}"
            if t not in vistos:
                vistos.append(t)
    return vistos


def _orden_email(e, dominio):
    usuario, _, dom = e.partition("@")
    propio = dom == dominio or dom.endswith("." + dominio) or dominio.endswith("." + dom)
    return (0 if propio else 1, GENERICOS.index(usuario) if usuario in GENERICOS else len(GENERICOS))


def _enlaces(pagina, base):
    host = urllib.parse.urlsplit(base).hostname or ""
    res = []
    for h in ENLACE.findall(pagina):
        try:
            url = urllib.parse.urljoin(base + "/", h.strip())
            partes = urllib.parse.urlsplit(url)
            partes.hostname  # noqa: B018  (valida el host: un enlace mal formado lanza ValueError)
        except ValueError:
            continue  # enlaces rotos de la web (p. ej. "[eliminado-tag ...]"): se ignoran
        if partes.scheme not in ("http", "https") or (partes.hostname or "").removeprefix("www.") != host.removeprefix("www."):
            continue
        if any(p in partes.path.lower() for p in PAGINAS) and not partes.path.lower().endswith(EXT + (".pdf",)) and url not in res:
            res.append(url)
    return res[:3]


def buscar(cliente, web):
    """{'email', 'movil', 'fuente', 'estado'} con lo publicado en la web; estado: ok, sin_dato, bloqueada o error."""
    base = normalizar_web(web)
    if not base:
        return {"email": "", "movil": "", "fuente": "", "estado": "sin_web"}
    pagina, url = None, base
    for intento in (base, base.replace("https://", "http://", 1)):
        try:
            robots = urllib.robotparser.RobotFileParser()
            try:
                robots.parse(cliente.get(intento + "/robots.txt", timeout=10).splitlines())
            except ErrorConsulta:
                robots.parse([])
            if not robots.can_fetch(AGENTE, intento + "/"):
                return {"email": "", "movil": "", "fuente": "", "estado": "bloqueada"}
            pagina, url = cliente.get(intento + "/", timeout=15), intento
            break
        except ErrorConsulta:
            continue
    if pagina is None:
        return {"email": "", "movil": "", "fuente": "", "estado": "error"}
    encontrados, fuente = {"email": [], "movil": []}, ""
    for u, p in [(url, pagina)] + [(u, None) for u in _enlaces(pagina, url)]:
        if p is None:
            if not robots.can_fetch(AGENTE, u):
                continue
            try:
                p = cliente.get(u, timeout=15)
            except ErrorConsulta:
                continue
        nuevos_e, nuevos_m = [e for e in emails(p) if e not in encontrados["email"]], [m for m in moviles(p) if m not in encontrados["movil"]]
        if (nuevos_e or nuevos_m) and not fuente:
            fuente = u
        encontrados["email"] += nuevos_e
        encontrados["movil"] += nuevos_m
    dominio = (urllib.parse.urlsplit(url).hostname or "").removeprefix("www.")
    mails = sorted(encontrados["email"], key=lambda e: _orden_email(e, dominio))[:2]
    movs = encontrados["movil"][:2]
    return {"email": "; ".join(mails), "movil": "; ".join(movs), "fuente": fuente if (mails or movs) else "",
            "estado": "ok" if (mails or movs) else "sin_dato"}
