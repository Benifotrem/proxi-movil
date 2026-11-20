"""Reglas comerciales: descarte, segmento, sector, palanca de subvención 2027 y prioridad A/B/C.
Las mismas reglas se escriben como fórmulas en el Excel (excel.py) para que se recalculen al rellenar
los empleados o el sector confirmados en la llamada."""
import re
from datetime import date

from .textos import sin_acentos

S3, S2, S1, S45, S250 = "Segmento III (0-2 emp)", "Segmento II (3-9 emp)", "Segmento I (10-49 emp)", "Segmento IV-V (50-249 emp)", "250+ emp"
INDUSTRIAL, SERVICIOS = "Industrial", "Servicios"
P_IVACE = "DIGITALIZA-CV (IVACE - Apertura Enero 2027)"
P_CAMARA = "Pyme Digital / Pyme Cibersegura (Cámara de Alicante 2027)"
P_MICRO = "Auditoría Ahorro Convergente + Alerta Cámara Alicante"
P_I_SIN_SECTOR = "DIGITALIZA-CV (si es industrial) o Cámara (si es servicios): confirmar sector"
P_45_SERVICIOS = "Sin palanca específica 2027 (50-249 emp, no industrial)"
P_45_SIN_SECTOR = "DIGITALIZA-CV si es industrial: confirmar sector"
P_PENDIENTE = "Pendiente: confirmar empleados en la llamada"

# CNAE 10-43 (industria, energía, agua, construcción) y 49-52 (transporte y almacenamiento), por palabras clave
# de la razón social o del objeto social del BORME. Es una estimación: el sector confirmado manda.
CLAVES_INDUSTRIA = [
    "CONSTRUC", "OBRAS", "REFORMA", "EDIFICA", "PROMOCION", "INDUSTR", "FABRIC", "MANUFACT", "METAL", "SIDERUR", "PLASTIC",
    "CALZADO", "TEXTIL", "HILATURA", "TINTE", "JUGUET", "MUEBLE", "MADERA", "CARPINTER", "CERRAJER", "ALUMINIO", "VIDRIO",
    "TRANSPORT", "LOGIST", "MUDANZA", "ALMACENAJE", "PAQUETERIA", "MENSAJERIA", "EMBALAJ", "ENVASE", "CARTON", "PAPEL",
    "ALIMENTA", "CONSERV", "PANIFIC", "PANADERIA", "OBRADOR", "BODEGA", "TURRON", "HELADO", "ARIDOS", "CANTERA", "HORMIGON",
    "CERAMIC", "MARMOL", "PIEDRA", "GRAFIC", "IMPRENTA", "QUIMIC", "ELECTRIC", "FONTANER", "CLIMATIZ", "INSTALACION",
    "MECANIZ", "CALDERER", "SOLDADUR", "PREFABRIC", "EXCAVAC", "MOVIMIENTO DE TIERRAS", "PINTURA INDUSTRIAL", "RECICLA",
    # Categorías de Google Maps
    "FABRICANTE", "CONTRATISTA", "EMPRESA DE TRANSPORTE", "TRANSPORTISTA", "ALMACEN", "TALLER DE", "FUNDICION", "IMPRESION",
]


def sector_estimado(*textos):
    t = sin_acentos(" ".join(str(x or "") for x in textos)).upper()
    return INDUSTRIAL if any(c in t for c in CLAVES_INDUSTRIA) else ""


# Sector de actividad tal como lo da TES (descripción CNAE): industrial = secciones B-F (CNAE 05-43) y transporte y
# almacenamiento (49-52), lo que pide DIGITALIZA-CV. Con descripción CNAE, todo lo demás es servicios.
CNAE_INDUSTRIA = (
    "EXTRACCION", "FABRICACION", "ELABORACION", "PROCESADO Y CONSERVACION", "PREPARACION E HILADO", "FABRICA",
    "ACABADO DE TEXTILES", "CONFECCION", "CURTIDO", "ASERRADO", "IMPRESION", "ARTES GRAFICAS", "REPRODUCCION DE SOPORTES",
    "COQUERIA", "REFINO", "REPARACION E INSTALACION DE MAQUINARIA", "REPARACION DE PRODUCTOS METALICOS", "REPARACION DE MAQUINARIA",
    "INSTALACION DE MAQUINAS", "SUMINISTRO DE ENERGIA", "PRODUCCION DE ENERGIA", "CAPTACION, DEPURACION", "AGUAS RESIDUALES",
    "RECOGIDA DE RESIDUOS", "TRATAMIENTO Y ELIMINACION", "VALORIZACION", "DESCONTAMINACION",
    "PROMOCION INMOBILIARIA", "CONSTRUCCION DE", "INGENIERIA CIVIL", "DEMOLICION", "PREPARACION DE TERRENOS", "PERFORACIONES",
    "INSTALACIONES ELECTRICAS", "FONTANERIA", "INSTALACION DE CARPINTERIA", "REVOCAMIENTO", "REVESTIMIENTO DE SUELOS",
    "PINTURA Y ACRISTALAMIENTO", "ACABADO DE EDIFICIOS", "OTRAS INSTALACIONES EN OBRAS", "CONSTRUCCION ESPECIALIZADA",
    "ACTIVIDADES DE CONSTRUCCION", "TRANSPORTE DE MERCANCIAS", "TRANSPORTE TERRESTRE", "TRANSPORTE POR", "TRANSPORTE MARITIMO",
    "TRANSPORTE AEREO", "TRANSPORTE URBANO", "TRANSPORTE DE PASAJEROS", "MUDANZAS", "DEPOSITO Y ALMACENAMIENTO",
    "ACTIVIDADES ANEXAS AL TRANSPORTE", "ACTIVIDADES AUXILIARES DEL TRANSPORTE", "MANIPULACION DE MERCANCIAS", "TAXI",
    "SERVICIOS LOGISTICOS", "MAMPOSTERIA", "ALBANILERIA", "ACABADOS DE EDIFICIOS", "TALLADO Y ACABADO DE LA PIEDRA",
    "INDUSTRIAS MANUFACTURERAS", "TRANSFORMACION DE VIDRIO", "REVESTIMIENTO DE METALES", "TRATAMIENTO Y REVESTIMIENTO",
    "REPARACION Y MANTENIMIENTO DE MAQUINARIA", "REPARACION Y MANTENIMIENTO DE BUQUES", "OTRO MATERIAL DE TRANSPORTE",
)


def sector_cnae(descripcion):
    t = sin_acentos(str(descripcion or "")).upper().strip()
    if not t:
        return ""
    if t.startswith(("COMERCIO", "VENTA", "ALQUILER", "ACTIVIDADES DE INTERMEDIARIOS")):
        return SERVICIOS  # "Comercio al por mayor de materiales de construcción" no es construcción
    return INDUSTRIAL if any(c in t for c in CNAE_INDUSTRIA) else SERVICIOS


def segmento_por_empleados(n):
    if n is None:
        return ""
    return S3 if n <= 2 else S2 if n <= 9 else S1 if n <= 49 else S45 if n <= 249 else S250


def segmento_por_rango(rango):
    """Rango del Kit Digital ('0-2', '3-9', '10-49', '50-99', '100-249')."""
    return {"0-2": S3, "3-9": S2, "10-49": S1, "50-99": S45, "100-249": S45}.get(rango or "", "")


def segmento_por_estimacion(lo, hi):
    """Segmento a partir de un rango estimado (p. ej. 6-10): por su punto medio."""
    if lo is None or hi is None:
        return ""
    return segmento_por_empleados((lo + hi) // 2)


def palanca(segmento, sector):
    if segmento == S3:
        return P_MICRO
    if segmento == S2:
        return P_CAMARA
    if segmento == S1:
        return P_IVACE if sector == INDUSTRIAL else P_CAMARA if sector == SERVICIOS else P_I_SIN_SECTOR
    if segmento == S45:
        return P_IVACE if sector == INDUSTRIAL else P_45_SERVICIOS if sector == SERVICIOS else P_45_SIN_SECTOR
    if segmento == S250:
        return "—"
    return P_PENDIENTE


def prioridad(descarte, segmento, kit_urgente, ivace, camara, telefono):
    if descarte or segmento == S250:
        return "DESCARTADA"
    if kit_urgente:
        return "A"
    if segmento in (S2, S1) and (ivace or camara) and telefono:
        return "A"
    if segmento in (S2, S1, S45) and telefono:
        return "B"
    return "C"


def meses_entre(desde: date, hasta: date):
    return (hasta.year - desde.year) * 12 + hasta.month - desde.month - (1 if hasta.day < desde.day else 0)


def descarte_mercantil(ficha_borme, hoy):
    """Motivo de descarte según el BORME (filtro eliminatorio 1), o None."""
    if not ficha_borme:
        return None
    estado = ficha_borme.get("estado") or ""
    if estado == "Extinguida":
        return "Sociedad extinguida (BORME)"
    if estado == "Concurso":
        return "Sociedad en concurso (BORME)"
    if estado.startswith("Disuelta"):
        return "Sociedad disuelta, en liquidación (BORME)"
    if ficha_borme.get("constitucion"):
        if meses_entre(date.fromisoformat(ficha_borme["constitucion"]), hoy) < 6:
            return "Menos de 6 meses de antigüedad (BORME)"
    return None


def telefono_valido(t):
    d = re.sub(r"\D", "", str(t or ""))
    return len(d) >= 9
