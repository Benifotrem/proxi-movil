"""Rango de empleados estimado a partir de los resúmenes de una búsqueda web por CIF (Serper).

Google muestra en sus resultados el resumen de directorios de empresas ("Empleados: 6 a 10"). Solo se lee
ese texto del resultado de búsqueda (no se visita ningún directorio) y solo se acepta si el resultado menciona
el CIF o casi toda la razón social. Es una estimación: el dato confirmado en la llamada siempre manda."""
import re
from urllib.parse import urlparse

from .telefonos import SERPER, _palabras

BUSQUEDA = "https://google.serper.dev/search"
PATRONES = [
    r"entre\s+(\d{1,4})\s+y\s+(\d{1,4})\s+(?:empleados|trabajadores)",
    r"(?:empleados|trabajadores|plantilla)\s*(?:\(\w+\))?\s*[:\-]?\s*(?:de\s+|entre\s+)?(\d{1,4})\s*(?:a|-|–|y)\s*(\d{1,4})",
    r"(\d{1,4})\s*(?:a|-|–)\s*(\d{1,4})\s+(?:empleados|trabajadores)",
    r"(?:empleados|trabajadores|plantilla)\s*(?:\(\w+\))?\s*[:\-]\s*(\d{1,4})(?!\s*(?:a|-|–|y)\s*\d)",
    r"(\d{1,4})\s+(?:empleados|trabajadores)(?!\s*(?:a|-|–))",
]


def extraer_rango(texto):
    t = re.sub(r"\s+", " ", str(texto or "")).lower()
    for p in PATRONES:
        m = re.search(p, t)
        if m:
            nums = [int(x) for x in m.groups() if x]
            lo, hi = (nums[0], nums[-1]) if len(nums) > 1 else (nums[0], nums[0])
            if lo <= hi <= 1000:  # cifras mayores suelen ser otra cosa (importes, años, visitas…)
                return lo, hi
    return None


def es_de_la_empresa(cif, razon_social, resultado):
    texto = " ".join(str(resultado.get(k) or "") for k in ("title", "snippet", "link"))
    if cif and cif.upper() in texto.upper().replace("-", ""):
        return True
    propias = _palabras(razon_social)
    return bool(propias) and len(propias & _palabras(texto)) / len(propias) >= 0.8


def buscar(cliente, clave, empresa):
    """(min, max, dominio) o None."""
    q = f'"{empresa["cif"]}" empleados'
    datos = cliente.json(BUSQUEDA, {"X-API-KEY": clave}, enviar={"q": q, "gl": "es", "hl": "es", "num": 10})
    for r in (datos.get("organic") or [])[:10]:
        if not es_de_la_empresa(empresa["cif"], empresa["razon_social"], r):
            continue
        rango = extraer_rango(f"{r.get('title', '')} {r.get('snippet', '')}")
        if rango:
            return rango[0], rango[1], urlparse(r.get("link") or "").netloc.removeprefix("www.")
    return None


assert SERPER  # mismo proveedor y clave que los teléfonos
