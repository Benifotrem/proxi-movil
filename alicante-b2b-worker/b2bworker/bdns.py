"""Base de Datos Nacional de Subvenciones (infosubvenciones.es): concesiones públicas por CIF del beneficiario."""
import re
from datetime import date

from .fichas import mas_meses
from .textos import sin_acentos

URL = "https://www.infosubvenciones.es/bdnstrans/api/concesiones/busqueda?vpd=GE&page={page}&pageSize={size}&nifCif={cif}"
TAM = 50
MAX_PAGINAS = 6
SEGMENTOS_KIT = {"I": "10-49", "II": "3-9", "III": "0-2", "IV": "50-99", "V": "100-249"}


def consultar(cliente, cif):
    """Lista de concesiones cuyo beneficiario es exactamente ese CIF."""
    res = []
    for page in range(MAX_PAGINAS):
        datos = cliente.json(URL.format(page=page, size=TAM, cif=cif))
        contenido = datos.get("content") or []
        for c in contenido:
            if str(c.get("beneficiario", "")).upper().startswith(cif):
                res.append({k: c.get(k) for k in ("fechaConcesion", "beneficiario", "importe", "ayudaEquivalente", "convocatoria",
                                                  "nivel1", "nivel2", "nivel3", "numeroConvocatoria")})
        if len(contenido) < TAM:
            break
    return res


def _texto(c):
    return sin_acentos(" ".join(str(c.get(k) or "") for k in ("convocatoria", "nivel1", "nivel2", "nivel3"))).upper()


def es_kit_digital(c):
    t = _texto(c)
    return "KIT DIGITAL" in t or ("RED.ES" in t and "DIGITALIZACION" in t and "SEGMENTO" in t)


def es_kit_consulting(c):
    t = _texto(c)
    return "KIT CONSULTING" in t or ("RED.ES" in t and "ASESORAMIENTO" in t)


def es_ivace(c):
    t = _texto(c)
    return "IVACE" in t or "COMPETITIVITAT EMPRESARIAL" in t or "COMPETITIVIDAD EMPRESARIAL" in t


def es_diputacion_alicante(c):
    t = _texto(c)
    return "DIPUTACI" in t and "ALICANTE" in t


def es_camara_alicante(c):
    t = _texto(c)
    return "CAMARA" in t and "ALICANTE" in t


def segmento_kit(c):
    m = re.search(r"SEGMENTO[S]?\s+(III|II|IV|V|I)\b", _texto(c))
    return SEGMENTOS_KIT.get(m.group(1)) if m else None


def _fecha(c):
    try:
        return date.fromisoformat(str(c.get("fechaConcesion"))[:10])
    except ValueError:
        return None


def organismo(c):
    """Órgano concedente legible: el nivel más concreto ('ENTIDAD PÚBLICA EMPRESARIAL RED.ES' → 'Red.es')."""
    nombre = str(c.get("nivel3") or c.get("nivel2") or c.get("nivel1") or "").strip()
    cortos = {"RED.ES": "Red.es", "IVACE": "IVACE", "COMPETITIVITAT": "IVACE", "LABORA": "LABORA", "CAMARA": "Cámara de Comercio",
              "DIPUTACI": "Diputación", "AYUNTAMIENTO": "Ayuntamiento", "AJUNTAMENT": "Ayuntamiento", "SEPE": "SEPE"}
    t = sin_acentos(nombre).upper()
    for clave, corto in cortos.items():
        if clave in t:
            return corto + (" " + nombre.split(" DE ")[-1].title() if corto in ("Diputación", "Ayuntamiento", "Cámara de Comercio") and " DE " in nombre else "")
    return nombre.title()[:60]


def detalle(concesiones, n=5):
    """Últimas ayudas en texto: '2025 · IVACE · 3.000 € (Programa …)'."""
    orden = sorted([c for c in concesiones if _fecha(c)], key=_fecha, reverse=True)[:n]
    partes = []
    for c in orden:
        importe = float(c.get("importe") or 0)
        conv = re.sub(r"\s+", " ", str(c.get("convocatoria") or ""))[:70]
        partes.append(f"{_fecha(c).year} · {organismo(c)} · {importe:,.0f} €".replace(",", ".") + (f" ({conv})" if conv else ""))
    return " | ".join(partes)


def resumen(concesiones, hoy=None):
    """Indicadores comerciales a partir de las concesiones."""
    hoy = hoy or date.today()
    kit = sorted([c for c in concesiones if es_kit_digital(c) and _fecha(c)], key=_fecha, reverse=True)
    kitc = sorted([c for c in concesiones if es_kit_consulting(c) and _fecha(c)], key=_fecha, reverse=True)
    ivace = [c for c in concesiones if es_ivace(c)]
    camara = [c for c in concesiones if es_camara_alicante(c)]
    diputacion = [c for c in concesiones if es_diputacion_alicante(c)]
    # Ayudas de los últimos 3 años (aprox. del límite de minimis de 300.000 €): ayuda equivalente si consta.
    tres = [c for c in concesiones if _fecha(c) and (hoy - _fecha(c)).days <= 3 * 365]
    importe_3a = round(sum(float(c.get("ayudaEquivalente") or c.get("importe") or 0) for c in tres), 2)
    recientes = [c for c in concesiones if (_fecha(c) or date.min).year >= 2025]
    ultima_kit = _fecha(kit[0]) if kit else None
    return {
        "beneficiario": concesiones[0].get("beneficiario") if concesiones else None,
        "n_concesiones": len(concesiones),
        "kit_fecha": ultima_kit.isoformat() if ultima_kit else None,
        "kit_importe": round(sum(float(c.get("importe") or 0) for c in kit), 2) if kit else None,
        "kit_segmento": next((segmento_kit(c) for c in kit if segmento_kit(c)), None),
        "kit_urgente": bool(ultima_kit and hoy < mas_meses(ultima_kit, 6)),  # bono aún por canjear
        "kitc_fecha": _fecha(kitc[0]).isoformat() if kitc else None,
        "kitc_importe": round(sum(float(c.get("importe") or 0) for c in kitc), 2) if kitc else None,
        "ivace": len(ivace),
        "camara": len(camara),
        "recientes": len(recientes),
        "diputacion": len(diputacion),
        "importe_3a": importe_3a,
        "ultima_organismo": organismo(max(concesiones, key=lambda c: _fecha(c) or date.min)) if concesiones else None,
        "detalle": detalle(concesiones),
        "ultima": (max(fechas).isoformat() if (fechas := [_fecha(c) for c in concesiones if _fecha(c)]) else None),
    }
