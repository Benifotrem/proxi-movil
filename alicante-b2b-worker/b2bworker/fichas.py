"""Ayudas de la ficha comercial 2027 (Alicante) y frase de entrada para la llamada en frío.

Programas y ventanas según la "Nota Informativa: Ayudas Digitalización 2027" del comercial. Los requisitos
que no se pueden saber por fuentes públicas (empleados, sector, certificado digital) se piden en la llamada."""
import calendar
from datetime import date

from . import reglas as R

DIGITALIZA = ("DIGITALIZA-CV (IVACE)", "Enero–Marzo 2027", 1)
CIBERSEGURA = ("Pyme Cibersegura (Cámara + Diputación)", "Abril–Mayo 2027", 4)
DIGITAL = ("Pyme Digital / Innova (Cámara + FEDER)", "Junio–Julio 2027", 6)
KIT = ("Kit Digital: bono vivo por canjear", "Inmediata", 0)
MINIMIS = 300_000


def mas_meses(d: date, meses: int) -> date:
    m = d.month - 1 + meses
    y, m = d.year + m // 12, m % 12 + 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def _eur(v):
    return f"{float(v):,.0f} €".replace(",", ".")


def evaluar(x, hoy=None):
    """Para un registro clasificado (cli.clasificar): ayudas de la ficha que encajan, encaje, frase y ventana.
    None si no puede optar a ninguna (descartada o por encima del límite de minimis)."""
    hoy = hoy or date.today()
    if x["prioridad"] == "DESCARTADA" or (x.get("importe_3a") or 0) >= MINIMIS:
        return None
    seg, sector = x["segmento"], x["sector_conf"] or x["sector_est"]
    ayudas = []
    if x["kit_urgente"] == "SÍ":
        ayudas.append(KIT)
    pequena = seg in (R.S3, R.S2)
    if not pequena and (sector == R.INDUSTRIAL or seg in (R.S1, R.S45)) and seg != R.S250:
        ayudas.append(DIGITALIZA)
    if seg != R.S250:
        ayudas += [CIBERSEGURA, DIGITAL]
    if not ayudas:
        return None

    historial = (x["ivace"] or 0) + (x["camara"] or 0) + (x.get("diputacion") or 0)
    encaje = "Alto" if x["kit_urgente"] == "SÍ" or historial else "Medio" if x["kit_fecha"] or x["n_ayudas"] else "Base"
    if x["kit_urgente"] == "SÍ":
        concedido = date.fromisoformat(x["kit_fecha"])
        caduca = mas_meses(concedido, 6)
        frase = (f"Bono Kit Digital concedido el {concedido:%d/%m/%Y} ({_eur(x['kit_importe'] or 0)}): caduca hacia el "
                 f"{caduca:%d/%m/%Y}. Ofrécele ayuda para canjearlo antes de que lo pierda.")
    elif historial:
        quien = ", ".join(n for n, v in (("IVACE", x["ivace"]), ("Cámara", x["camara"]), ("Diputación", x.get("diputacion"))) if v)
        frase = (f"Ya obtuvo ayudas de {quien}: conoce el proceso. Las de 2027 se agotan en 24-48 h; "
                 "ofrécele tener los presupuestos listos antes de que abran.")
    elif x["kit_fecha"]:
        frase = (f"Usó el Kit Digital en {x['kit_fecha'][:4]}" + (f" ({x['kit_segmento']} empleados)" if x["kit_segmento"] else "")
                 + ": el siguiente paso natural es Pyme Digital o Pyme Cibersegura de la Cámara.")
    elif x["n_ayudas"]:
        frase = (f"Ha recibido {x['n_ayudas']} ayuda(s) pública(s)"
                 + (f", la última de {x['ultima_organismo']}" if x.get("ultima_organismo") else "")
                 + ": está al corriente con Hacienda y Seguridad Social, requisito de todas las de 2027.")
    else:
        frase = ("Sin ayudas previas registradas: entra con el estudio gratuito de su factura de telefonía y fibra, "
                 "y explícale que Pyme Cibersegura (abril) cubre hasta el 60 %.")

    confirmar = [p for p, falta in (("nº de empleados", not x["emp_conf"] and not x["seg_est"]),
                                    ("sector (industrial o servicios)", DIGITALIZA in ayudas and not x["sector_conf"]),
                                    ("certificado digital del representante", True)) if falta]
    ayudas.sort(key=lambda a: a[2])
    return {
        "encaje": encaje, "frase": frase, "ayudas": " · ".join(a[0] for a in ayudas), "ventana": ayudas[0][1],
        "minimis": f"{_eur(x.get('importe_3a') or 0)} usados de {_eur(MINIMIS)}",
        "confirmar": "; ".join(confirmar),
    }
