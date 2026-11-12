"""Normalización de textos (nombres de empresa y municipios) para comparar fuentes."""
import re
import unicodedata

FORMAS = [
    (r"\bSOCIEDAD LIMITADA (LABORAL|NUEVA EMPRESA|UNIPERSONAL|PROFESIONAL)\b", "SL"),
    (r"\bSOCIEDAD ANONIMA (LABORAL|UNIPERSONAL|DEPORTIVA)\b", "SA"),
    (r"\bSOCIEDAD LIMITADA\b", "SL"), (r"\bSOCIEDAD ANONIMA\b", "SA"),
    (r"\bS L L\b|\bS L U\b|\bS L N E\b|\bS L P\b|\bSLL\b|\bSLU\b|\bSLNE\b|\bSLP\b", "SL"),
    (r"\bS A U\b|\bS A L\b|\bSAU\b|\bSAL\b", "SA"),
    (r"\bS L\b", "SL"), (r"\bS A\b", "SA"),
    (r"\bSOCIEDAD COOPERATIVA( VALENCIANA)?\b|\bS COOP( V)?\b|\bCOOP V\b", "COOP"),
]


def sin_acentos(t: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", t) if unicodedata.category(c) != "Mn")


def nombre_empresa(nombre: str) -> str:
    """Clave comparable: mayúsculas, sin acentos ni signos, forma jurídica abreviada (SL/SA/COOP)."""
    t = sin_acentos(str(nombre or "")).upper().replace("&", " Y ")
    t = re.sub(r"[^A-Z0-9 ]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    for patron, abrev in FORMAS:
        t = re.sub(patron, abrev, t)
    return re.sub(r"\s+", " ", t).strip()


def municipio(nombre: str) -> str:
    t = sin_acentos(str(nombre or "")).upper().split("/")[0]
    # "NUCIA (LA)" → "LA NUCIA", "ALFAS DEL PI (L)" → "L ALFAS DEL PI"
    t = re.sub(r"^(.*?)\s*\((LA|EL|L|LOS|LAS|LES|ELS)\)\s*$", r"\2 \1", t.strip())
    t = re.sub(r"\(.*?\)", " ", t)
    t = re.sub(r"[^A-Z ]", " ", t)
    return re.sub(r"\s+", " ", t).strip()
