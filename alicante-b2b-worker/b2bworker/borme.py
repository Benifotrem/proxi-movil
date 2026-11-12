"""Índice local del BORME (Sección Primera, provincia de Alicante) a partir de los datos abiertos del BOE.

Cada día publicado se descarga una vez: el sumario (JSON) y el PDF de Alicante; los anuncios se separan
por empresa y se guardan en SQLite. Después, cada empresa se busca por su razón social normalizada.
Los datos del BORME son de reutilización libre citando la fuente (BOE)."""
import io
import logging
import re
from datetime import date, datetime, timedelta

from .db import cargar_json
from .red import ErrorConsulta
from .textos import nombre_empresa, sin_acentos

log = logging.getLogger("b2b")
SUMARIO = "https://www.boe.es/datosabiertos/api/borme/sumario/{fecha}"
PROVINCIA = {"ALICANTE", "ALICANTE/ALACANT", "ALACANT", "ALACANT/ALICANTE"}

ACTOS = [
    "Constitución", "Nombramientos", "Ceses/Dimisiones", "Revocaciones", "Reelecciones", "Disolución", "Extinción",
    "Declaración de unipersonalidad", "Sociedad unipersonal", "Pérdida del caracter de unipersonalidad",
    "Cambio de identidad del socio único", "Cambio de domicilio social", "Cambio de objeto social",
    "Cambio de denominación social", "Modificaciones estatutarias", "Ampliación de capital", "Reducción de capital",
    "Otros conceptos", "Situación concursal", "Declaración de concurso", "Fusión por absorción", "Fusión por unión",
    "Escisión", "Transformación de sociedad", "Cierre provisional hoja registral", "Reapertura hoja registral",
    "Cancelaciones de oficio", "Primera inscripción", "Apertura de sucursal", "Cierre de Sucursal", "Depósito de libros",
    "Emisión de obligaciones", "Desembolso de dividendos pasivos", "Adaptación", "Fe de erratas", "Anotación preventiva",
    "Empresario Individual", "Datos registrales", "Adaptación Ley 2/95", "Adaptación Ley 44/2015",
]
_ACTO_RE = "|".join(re.escape(a) for a in sorted(ACTOS, key=len, reverse=True))
ENTRADA = re.compile(r"(?:(?<=\s)|^)(\d{5,7}) - (?=\S)")
PRIMER_ACTO = re.compile(r"\.?\s*(?:" + _ACTO_RE + r")\.")
CARGOS_CONTACTO = re.compile(r"^(ADM\.? ?UNICO|ADM\.? ?SOLID|ADM\.? ?MANCOM|ADMINISTRADOR|CONS\.? ?DEL|CON\.? ?DELEG|CONSEJERO DELEGADO|PRESIDENTE|LIQUIDADOR)", re.I)


# --------------------------------------------------------------------------- descarga y separación

def urls_alicante(sumario):
    """URLs del PDF de Alicante en un sumario (estructura recorrida de forma tolerante)."""
    res = []

    def visitar(n):
        if isinstance(n, dict):
            titulo = sin_acentos(str(n.get("titulo", ""))).upper().strip()
            pdf = n.get("url_pdf")
            if titulo in PROVINCIA and pdf:
                res.append(pdf.get("texto") if isinstance(pdf, dict) else pdf)
            for v in n.values():
                visitar(v)
        elif isinstance(n, list):
            for v in n:
                visitar(v)
    visitar(sumario)
    return [u for u in res if u]


def texto_pdf(datos):
    from pypdf import PdfReader  # dependencia del entorno propio del worker
    lector = PdfReader(io.BytesIO(datos))
    return "\n".join(p.extract_text() or "" for p in lector.pages)


def separar(texto):
    """[(nombre, texto de actos)] de las inscripciones de un PDF de la Sección Primera."""
    plano = re.sub(r"-\n(?=[a-záéíóú])", "", texto)          # palabras partidas al final de línea
    plano = re.sub(r"\s+", " ", plano)
    partes = ENTRADA.split(plano)
    res = []
    for i in range(1, len(partes) - 1, 2):
        cuerpo = partes[i + 1].strip()
        m = PRIMER_ACTO.search(cuerpo)
        if not m or m.start() == 0:
            continue
        nombre = cuerpo[:m.start()].strip().rstrip(".")
        actos = cuerpo[m.start():].lstrip(". ")
        actos = actos.split(" Datos registrales.")[0]
        if 2 <= len(nombre) <= 200:
            res.append((nombre, actos[:4000]))
    return res


def indexar_dia(con, cliente, dia: date):
    """Descarga y guarda un día. Devuelve el número de inscripciones o None si no hubo BORME."""
    fecha = dia.strftime("%Y%m%d")
    try:
        sumario = cliente.json(SUMARIO.format(fecha=fecha))
    except ErrorConsulta as e:
        if "404" in str(e):
            con.execute("INSERT OR REPLACE INTO borme_dias VALUES (?, 'sin_publicacion', 0, ?)", (dia.isoformat(), datetime.now().isoformat(timespec="seconds")))
            con.commit()
            return None
        raise
    entradas = []
    for url in urls_alicante(sumario):
        entradas += separar(texto_pdf(cliente.get(url, binario=True, timeout=60)))
    con.execute("DELETE FROM borme_actos WHERE fecha = ?", (dia.isoformat(),))
    con.executemany("INSERT INTO borme_actos VALUES (?, ?, ?, ?)",
                    [(dia.isoformat(), nombre_empresa(n), n, t) for n, t in entradas])
    con.execute("INSERT OR REPLACE INTO borme_dias VALUES (?, 'ok', ?, ?)", (dia.isoformat(), len(entradas), datetime.now().isoformat(timespec="seconds")))
    con.commit()
    return len(entradas)


def dias_pendientes(con, desde: date, hasta: date):
    hechos = {r[0] for r in con.execute("SELECT fecha FROM borme_dias")}
    d = desde
    while d <= hasta:
        if d.weekday() < 5 and d.isoformat() not in hechos:  # el BORME no se publica en fin de semana
            yield d
        d += timedelta(days=1)


def cobertura(con):
    r = con.execute("SELECT MIN(fecha), MAX(fecha), SUM(estado='ok') FROM borme_dias").fetchone()
    return (r[0], r[1], r[2] or 0)


# --------------------------------------------------------------------------- interpretación por empresa

def _cargos(texto_acto):
    """{cargo: [personas]} de un bloque 'Nombramientos.' o 'Ceses/Dimisiones.'."""
    res = {}
    for m in re.finditer(r"([A-Za-zÁÉÍÓÚÑáéíóúñ\.\s/]{3,40}?):\s*(.+?)(?=\.\s+[A-Za-zÁÉÍÓÚÑ\.\s/]{3,40}?:|\.?$)", texto_acto):
        cargo = re.sub(r"\s+", " ", m.group(1)).strip(" .")
        personas = [p.strip(" .") for p in m.group(2).split(";") if p.strip(" .")]
        res.setdefault(cargo, []).extend(personas)
    return res


def _bloques(texto):
    """[(acto, contenido)] de una inscripción."""
    trozos = re.split(r"(?:^|\.\s+|\s)(" + _ACTO_RE + r")\.\s*", texto)
    res = []
    for i in range(1, len(trozos), 2):
        res.append((trozos[i], trozos[i + 1].strip() if i + 1 < len(trozos) else ""))
    return res


def _fecha_operaciones(contenido):
    m = re.search(r"Comienzo de operaciones:\s*(\d{1,2})\.(\d{1,2})\.(\d{2,4})", contenido)
    if not m:
        return None
    d, mth, y = (int(x) for x in m.groups())
    y = y + 2000 if y < 100 else y
    try:
        return date(y, mth, d)
    except ValueError:
        return None


def ficha(con, razon_social, hoy=None):
    """Estado, constitución y administradores vigentes según el índice local. None si no aparece."""
    hoy = hoy or date.today()
    filas = con.execute("SELECT fecha, nombre, texto FROM borme_actos WHERE clave = ? ORDER BY fecha", (nombre_empresa(razon_social),)).fetchall()
    if not filas:
        return None
    estado, constitucion, objeto = "Activa", None, None
    cargos = {}  # persona -> (cargo, fecha)
    for f in filas:
        for acto, contenido in _bloques(f["texto"]):
            if acto == "Constitución":
                constitucion = (_fecha_operaciones(contenido) or date.fromisoformat(f["fecha"])).isoformat()
                m = re.search(r"Objeto social:\s*(.+?)\.\s*(?:Domicilio|Capital)", contenido)
                objeto = m.group(1)[:300] if m else objeto
            elif acto == "Nombramientos" or acto == "Reelecciones":
                for cargo, personas in _cargos(contenido).items():
                    if CARGOS_CONTACTO.match(sin_acentos(cargo).upper()):
                        for p in personas:
                            cargos[p] = (cargo, f["fecha"])
            elif acto in ("Ceses/Dimisiones", "Revocaciones"):
                for personas in _cargos(contenido).values():
                    for p in personas:
                        cargos.pop(p, None)
            elif acto == "Extinción":
                estado = "Extinguida"
            elif acto in ("Situación concursal", "Declaración de concurso") and estado != "Extinguida":
                estado = "Concurso"
            elif acto == "Disolución" and estado not in ("Extinguida", "Concurso"):
                estado = "Disuelta (en liquidación)"
            elif acto == "Cierre provisional hoja registral" and estado == "Activa":
                estado = "Inactiva (hoja registral cerrada)"
            elif acto == "Reapertura hoja registral" and estado.startswith("Inactiva"):
                estado = "Activa"
    return {
        "nombre_borme": filas[-1]["nombre"], "estado": estado, "constitucion": constitucion, "objeto": objeto,
        "administradores": [{"nombre": p, "cargo": c, "desde": d} for p, (c, d) in sorted(cargos.items(), key=lambda x: x[1][1], reverse=True)][:5],
        "ultimo_acto": filas[-1]["fecha"], "n_inscripciones": len(filas),
    }
