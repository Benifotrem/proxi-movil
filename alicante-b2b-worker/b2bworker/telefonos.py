"""Teléfono, web y categoría del negocio en Google Maps, a través de un proveedor de búsqueda con API.

Proveedores: Serper (google.serper.dev/places; preferente si hay SERPER_API_KEY) o SerpApi (motor google_maps,
SERPAPI_KEY). Una búsqueda por empresa ("razón social + municipio"), solo
para empresas válidas sin teléfono, con tope mensual de búsquedas. El resultado solo se acepta si coincide
el nombre y el código postal o el municipio: mejor sin teléfono que con el de otra empresa."""
import os
import re
from datetime import date
from pathlib import Path
from urllib.parse import urlencode

from .textos import municipio as norm_municipio
from .textos import nombre_empresa

SERPAPI = "https://serpapi.com/search.json?"
SERPER = "https://google.serper.dev/places"
FUENTES = {"serper": "Google Maps (Serper)", "serpapi": "Google Maps (SerpApi)"}
FUENTE = FUENTES["serpapi"]
# Búsquedas que se permiten por mes con cada proveedor (SerpApi gratis: 250/mes; Serper: créditos de alta, sin mes).
TOPES = {"serpapi": 240, "serper": 2000}
GENERICAS = {"SL", "SA", "COOP", "SLU", "Y", "DE", "DEL", "LA", "EL", "LOS", "LAS", "E", "HIJOS", "GRUPO", "CB", "SC"}


def _variable(datos: Path, nombre):
    if os.environ.get(nombre):
        return os.environ[nombre]
    env = datos / ".env"
    if env.exists():
        for linea in env.read_text(encoding="utf-8").splitlines():
            if linea.strip().startswith(nombre + "="):
                return linea.split("=", 1)[1].strip().strip('"').strip("'") or None
    return None


def proveedor(datos: Path):
    """(nombre, clave) del proveedor configurado en ~/alicante-b2b-worker/.env (nunca en el repositorio)."""
    for nombre, var in (("serper", "SERPER_API_KEY"), ("serpapi", "SERPAPI_KEY")):
        clave = _variable(datos, var)
        if clave:
            return nombre, clave
    return None, None


def cargar_clave(datos: Path):
    return _variable(datos, "SERPAPI_KEY")


def _palabras(nombre):
    return {p for p in nombre_empresa(nombre).split() if p not in GENERICAS and len(p) > 1}


CALLE_GENERICAS = {"CALLE", "C", "CL", "CL.", "AVENIDA", "AV", "AVDA", "PLAZA", "PZA", "PL", "CAMINO", "CTRA", "CARRETERA", "PASEO",
                   "PARTIDA", "PTDA", "POLIGONO", "POL", "IND", "INDUSTRIAL", "NAVE", "PARCELA", "LOCAL", "BAJO", "PISO", "PUERTA",
                   "DE", "DEL", "LA", "EL", "LOS", "LAS", "SN", "S", "N", "NUM", "Y"}


def _calle(texto):
    """Palabras significativas de una dirección (sin tipo de vía ni números)."""
    t = nombre_empresa(texto)
    return {p for p in t.split() if p not in CALLE_GENERICAS and not p.isdigit() and len(p) > 2}


def coincide(empresa, resultado):
    """¿El resultado de Maps es esta empresa? Nombre parecido y misma localización.
    Si la dirección de Maps trae CP o municipio, han de coincidir. Si no los trae (Serper a veces da solo la
    calle), se exige un nombre casi idéntico y que la calle coincida con la dirección del archivo."""
    propias, suyas = _palabras(empresa["razon_social"]), _palabras(resultado.get("title", ""))
    if not propias or not suyas:
        return False
    parecido = len(propias & suyas) / min(len(propias), len(suyas))
    if parecido < 0.6:
        return False
    direccion = str(resultado.get("address") or "")
    cp = (empresa.get("cp") or "").strip()
    if cp and cp in direccion:
        return True
    muni = norm_municipio(empresa.get("municipio") or "")
    if muni and muni in norm_municipio(direccion.replace(",", " ")):
        return True
    if re.search(r"\b\d{5}\b", direccion):
        return False  # trae otro código postal: es otro sitio
    cobertura = len(propias & suyas) / len(propias)  # cuánto de NUESTRA razón social aparece en Maps
    return cobertura >= 0.8 and bool(_calle(direccion) & _calle(empresa.get("direccion") or ""))


def telefono_normalizado(t):
    d = re.sub(r"\D", "", str(t or ""))
    if d.startswith("34") and len(d) == 11:
        d = d[2:]
    return f"{d[:3]} {d[3:5]} {d[5:7]} {d[7:]}" if len(d) == 9 else None


def buscar(cliente, clave, empresa, prov="serpapi"):
    """Primer resultado de Maps que coincide con la empresa, o None."""
    q = f"{empresa['razon_social']} {(empresa.get('municipio') or '').split('/')[0]}".strip()
    if prov == "serper":
        datos = cliente.json(SERPER, {"X-API-KEY": clave}, enviar={"q": q, "gl": "es", "hl": "es"})
        candidatos = [{"title": p.get("title"), "address": p.get("address"), "phone": p.get("phoneNumber"),
                       "website": p.get("website"), "type": p.get("category")} for p in (datos.get("places") or [])]
    else:
        datos = cliente.json(SERPAPI + urlencode({"engine": "google_maps", "type": "search", "q": q, "hl": "es", "gl": "es", "api_key": clave}))
        candidatos = list(datos.get("local_results") or [])
        if isinstance(datos.get("place_results"), dict):
            candidatos.insert(0, datos["place_results"])
    for r in candidatos[:5]:
        if coincide(empresa, r):
            return {"telefono": telefono_normalizado(r.get("phone")), "web": r.get("website"),
                    "categoria": r.get("type") or ", ".join(r.get("types") or [])[:80] or None, "nombre_maps": r.get("title")}
    return None


def usadas_mes(con, proveedor="serpapi", hoy=None):
    mes = (hoy or date.today()).strftime("%Y-%m")
    r = con.execute("SELECT n FROM busquedas WHERE proveedor=? AND mes=?", (proveedor, mes)).fetchone()
    return r[0] if r else 0


def contar(con, proveedor="serpapi", hoy=None):
    mes = (hoy or date.today()).strftime("%Y-%m")
    con.execute("INSERT INTO busquedas (proveedor, mes, n) VALUES (?, ?, 1) ON CONFLICT(proveedor, mes) DO UPDATE SET n = n + 1", (proveedor, mes))
