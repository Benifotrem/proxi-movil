"""Lectura del export de una campaña de TES (Salesforce de Telefónica) en Excel.

El informe "GEST_TES_CAMP_..." trae una fila por target con columnas fijas (Id Target Salesforce, Estado, cliente y
documento) y pares "Título columna N" / "Valor columna N" cuyo título (NUMERO_TELEFONO, NÚMERO EMPLEADOS, URL...)
viene repetido en cada fila. Se leen los pares por su título, así que el orden de las columnas da igual."""
import re
from datetime import datetime

from . import cif as cifmod
from .telefonos import telefono_normalizado

# Estados de los targets de campaña en TES y orden de trabajo en el Excel.
TRABAJAR = ("En Gestión", "Pendiente")
NO_TRABAJAR = ("Ganada", "No público objetivo")  # ya es cliente o TES lo marca fuera de objetivo: fuera de las pestañas de trabajo
ORDEN = {"En Gestión": 0, "Pendiente": 1, "": 2, "Perdida": 3, "Ofertado no interesa": 3}
SIN_CAMPANA = "Fuera de la campaña"


def _fecha(v):
    """'30MAR2025' -> '2025-03-30'; '01JAN1900' (sin visita) -> ''."""
    s = str(v or "").strip().upper()
    meses = {m: i for i, m in enumerate(("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"), start=1)}
    m = re.fullmatch(r"(\d{2})([A-Z]{3})(\d{4})", s)
    if not m or m.group(2) not in meses or m.group(3) == "1900":
        return ""
    try:
        return datetime(int(m.group(3)), meses[m.group(2)], int(m.group(1))).date().isoformat()
    except ValueError:
        return ""


def _texto(c):
    if c is None:
        return ""
    if isinstance(c, float) and c.is_integer():
        c = int(c)  # teléfonos y CP que Excel guarda como número
    s = str(c).strip()
    if "Ã" in s:  # texto UTF-8 leído como Latin-1 en el export ("FABRICACIÃ³N")
        try:
            s = s.encode("latin-1").decode("utf-8")
        except UnicodeError:
            pass
    return s


def _entero(v):
    try:
        n = int(float(str(v).strip()))
        return n if 0 <= n <= 100000 else None
    except (TypeError, ValueError):
        return None


def leer(ruta):
    """Devuelve (nombre de la campaña, {cif: datos}). Lanza ValueError si no parece un export de campaña de TES."""
    from openpyxl import load_workbook
    wb = load_workbook(ruta, read_only=True, data_only=True)
    try:
        filas = wb.worksheets[0].iter_rows(values_only=True)
        campana, cab = "", None
        for fila in filas:
            celdas = [str(c).strip() if c is not None else "" for c in fila]
            if not campana:
                campana = next((c for c in celdas if c.startswith("GEST_TES_CAMP")), "")
            if "Id Target Salesforce" in celdas:
                cab = celdas
                break
        if cab is None:
            raise ValueError("no encuentro la cabecera 'Id Target Salesforce': ¿es el export de una campaña de TES?")
        i_id, i_estado = cab.index("Id Target Salesforce"), cab.index("Estado")
        i_doc = next(i for i, c in enumerate(cab) if c.endswith("Número de documento"))
        i_nombre = next((i for i, c in enumerate(cab) if c.endswith("Nombre del cliente")), None)
        pares = [(i, cab.index(f"Valor columna {c[len('Título columna '):]}")) for i, c in enumerate(cab)
                 if c.startswith("Título columna ") and f"Valor columna {c[len('Título columna '):]}" in cab]
        datos = {}
        for fila in filas:
            v = lambda i: _texto(fila[i]) if i is not None and i < len(fila) else ""  # noqa: E731
            if not v(i_id).startswith("TGT-"):
                continue
            cif = cifmod.normalizar(v(i_doc))
            if not cif:
                continue
            campos = {v(t).upper(): v(c) for t, c in pares if v(t)}
            datos[cif] = {
                "campana": campana, "target": v(i_id), "estado": v(i_estado), "nombre": v(i_nombre),
                "telefono": telefono_normalizado(campos.get("NUMERO_TELEFONO")) or "",
                "empleados": _entero(campos.get("NÚMERO EMPLEADOS")),
                "web": campos.get("URL", "").lower(),
                "sector": campos.get("SECTOR ACTIVIDAD", ""),
                "valor": campos.get("VALOR POTENCIAL CLIENTES", ""),
                "ranking": campos.get("RANKING PRIORITARIO", ""),
                "bono_kd": campos.get("BONO CONCEDIDO KD", ""),
                "ultima_visita": _fecha(campos.get("FECHA ULTIMA VISITA")),
                "cobertura_fibra": campos.get("COBERTURA FIBRA TELEFONICA", ""),
            }
        return campana, datos
    finally:
        wb.close()
