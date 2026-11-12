"""Ayudas de la ficha comercial 2027 (Alicante) y frase de entrada para la llamada en frío.

Programas y ventanas según la "Nota Informativa: Ayudas Digitalización 2027" del comercial. Los requisitos
que no se pueden saber por fuentes públicas (empleados, sector, certificado digital) se piden en la llamada."""
import calendar
import os
from datetime import date

from . import reglas as R

DIGITALIZA = ("DIGITALIZA-CV (IVACE)", "Enero–Marzo 2027", 1)
CIBERSEGURA = ("Pyme Cibersegura (Cámara + Diputación)", "Abril–Mayo 2027", 4)
DIGITAL = ("Pyme Digital / Innova (Cámara + FEDER)", "Junio–Julio 2027", 6)
KIT = ("Kit Digital: bono vivo por canjear", "Inmediata", 0)
MINIMIS = 300_000
# Quién llama (para los guiones). Se puede cambiar con la variable de entorno B2B_COMERCIAL.
COMERCIAL = os.environ.get("B2B_COMERCIAL", "César Ruzafa")
BAJA = "Si prefiere que no le volvamos a contactar, me lo dice y no le llamo más."
CIERRE = "Con 20 minutos se lo dejo revisado. ¿Le viene mejor a principio o a final de semana?"


def _empresa(x):
    return (x.get("razon_social") or "su empresa").strip()


def _persona(x):
    """Primer administrador vigente según el BORME, con mayúscula inicial (el BORME da 'APELLIDOS NOMBRE')."""
    admin = (x.get("administradores") or "").split("; ")[0].split(" (")[0].strip()
    return admin.title() if admin else ""


def guiones(x, caso, datos):
    """Apertura por teléfono y en visita, según el caso de encaje."""
    yo, emp, muni = COMERCIAL, _empresa(x), (x.get("municipio") or "la zona").title()
    presentacion = f"Buenos días, soy {yo}, asesor comercial de empresas en {muni}."
    if caso == "kit":
        dato = (f"Le llamo porque en el registro público de subvenciones consta que a {emp} le concedieron el Kit Digital el "
                f"{datos['concedido']:%d/%m/%Y}. Ese bono se canjea en unos 6 meses, hacia el {datos['caduca']:%d/%m/%Y} "
                "(compruébelo en su resolución).")
        pregunta = "¿Lo tienen ya asignado a algún proveedor?"
        gancho = f"Su bono Kit Digital caduca hacia el {datos['caduca']:%d/%m/%Y} y le traigo cómo aprovecharlo."
    elif caso == "historial":
        dato = (f"He visto en el registro público de subvenciones que {emp} ya ha trabajado con ayudas {datos['quien']}. "
                "Las de digitalización de 2027 abren en enero y suelen agotarse en 24-48 horas.")
        pregunta = "¿Alguien les está preparando ya los presupuestos?"
        gancho = f"Ya conocen las ayudas {datos['quien']}: le traigo las fechas de 2027, que se agotan en 24-48 horas."
    elif caso == "kit_pasado":
        dato = (f"{emp} usó el Kit Digital en {datos['anio']}. Muchas empresas que lo hicieron están preparando ahora la Pyme "
                "Cibersegura de la Cámara, que cubre hasta el 60 %.")
        pregunta = "¿Les han hablado de ella?"
        gancho = "Después del Kit Digital, la Cámara cubre hasta el 60 % en ciberseguridad y gestión: le dejo las fechas."
    elif caso == "ayudas":
        dato = (f"Consta que {emp} ha recibido ayudas públicas, así que ya cumple el requisito de estar al corriente. "
                "En 2027 hay tres líneas de digitalización para empresas de Alicante.")
        pregunta = "¿Quién lleva estos temas en la empresa?"
        gancho = "Ya cumplen los requisitos de las ayudas de 2027; le dejo cuáles encajan y cuándo abren."
    else:
        dato = ("Estoy ayudando a empresas de la zona a preparar las ayudas de digitalización de 2027 y, de paso, hago un "
                "estudio gratuito de la factura de teléfono y fibra.")
        pregunta = "¿Quién se encarga de estos temas en la empresa?"
        gancho = "Además, le ofrezco un estudio gratuito de su factura de teléfono y fibra."
    persona = _persona(x)
    telefono = f"{presentacion} {dato} {pregunta} {CIERRE} {BAJA}"
    visita = (f"Buenos días, {'¿podría hablar con ' + persona + '?' if persona else '¿podría hablar con el responsable o la gerencia?'} "
              f"Soy {yo}; vengo a dejarles la guía de ayudas de digitalización 2027 de Alicante. {gancho} "
              f"¿Tiene cinco minutos ahora o prefiere que pase otro día? {BAJA}")
    return telefono, visita


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
    nombres = [n for n, v in (("del IVACE", x["ivace"]), ("de la Cámara de Comercio", x["camara"]), ("de la Diputación", x.get("diputacion"))) if v]
    quien = " y ".join([", ".join(nombres[:-1]), nombres[-1]]) if len(nombres) > 1 else (nombres[0] if nombres else "")
    datos = {"quien": quien, "anio": (x["kit_fecha"] or "")[:4]}
    encaje = "Alto" if x["kit_urgente"] == "SÍ" or historial else "Medio" if x["kit_fecha"] or x["n_ayudas"] else "Base"
    if x["kit_urgente"] == "SÍ":
        concedido = date.fromisoformat(x["kit_fecha"])
        caduca = mas_meses(concedido, 6)
        caso, datos["concedido"], datos["caduca"] = "kit", concedido, caduca
        frase = (f"Bono Kit Digital concedido el {concedido:%d/%m/%Y} ({_eur(x['kit_importe'] or 0)}): caduca hacia el "
                 f"{caduca:%d/%m/%Y}. Ofrécele ayuda para canjearlo antes de que lo pierda.")
    elif historial:
        caso = "historial"
        frase = (f"Ya obtuvo ayudas {quien}: conoce el proceso. Las de 2027 se agotan en 24-48 h; "
                 "ofrécele tener los presupuestos listos antes de que abran.")
    elif x["kit_fecha"]:
        caso = "kit_pasado"
        frase = (f"Usó el Kit Digital en {x['kit_fecha'][:4]}" + (f" ({x['kit_segmento']} empleados)" if x["kit_segmento"] else "")
                 + ": el siguiente paso natural es Pyme Digital o Pyme Cibersegura de la Cámara.")
    elif x["n_ayudas"]:
        caso = "ayudas"
        frase = (f"Ha recibido {x['n_ayudas']} ayuda(s) pública(s)"
                 + (f", la última de {x['ultima_organismo']}" if x.get("ultima_organismo") else "")
                 + ": está al corriente con Hacienda y Seguridad Social, requisito de todas las de 2027.")
    else:
        caso = "base"
        frase = ("Sin ayudas previas registradas: entra con el estudio gratuito de su factura de telefonía y fibra, "
                 "y explícale que Pyme Cibersegura (abril) cubre hasta el 60 %.")

    confirmar = [p for p, falta in (("nº de empleados" + (f" (estimado {x['emp_est'].split(' (')[0]})" if x.get("emp_est") else ""),
                                     x["emp_conf"] is None),
                                    ("sector (industrial o servicios)", DIGITALIZA in ayudas and not x["sector_conf"]),
                                    ("certificado digital del representante", True)) if falta]
    ayudas.sort(key=lambda a: a[2])
    telefono, visita = guiones(x, caso, datos)
    return {
        "guion_telefono": telefono, "guion_visita": visita,
        "encaje": encaje, "frase": frase, "ayudas": " · ".join(a[0] for a in ayudas), "ventana": ayudas[0][1],
        "minimis": f"{_eur(x.get('importe_3a') or 0)} usados de {_eur(MINIMIS)}",
        "confirmar": "; ".join(confirmar),
    }
