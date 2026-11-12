"""Validación de identificadores fiscales españoles y tipo de entidad."""
import re

LETRAS_CONTROL = "JABCDEFGHI"
# Personas jurídicas y entidades sin personalidad (las que se procesan).
TIPOS = {
    "A": "Sociedad anónima", "B": "Sociedad limitada", "C": "Sociedad colectiva", "D": "Sociedad comanditaria",
    "E": "Comunidad de bienes", "F": "Cooperativa", "G": "Asociación / fundación", "H": "Comunidad de propietarios",
    "J": "Sociedad civil", "N": "Entidad extranjera", "R": "Congregación religiosa", "U": "UTE", "V": "Otros tipos",
    "W": "Establecimiento permanente extranjero",
    "P": "Corporación local", "Q": "Organismo público", "S": "Órgano de la Administración",
}
PUBLICOS = {"P", "Q", "S"}
# Sociedades mercantiles: figuran en el BORME.
MERCANTILES = {"A", "B", "C", "D", "F", "N", "W", "U", "V"}


def normalizar(valor) -> str:
    return re.sub(r"[\s.\-/]", "", str(valor or "")).upper()


def es_persona_fisica(ident: str) -> bool:
    """DNI (8 dígitos + letra), NIE (X/Y/Z) o NIF especial de persona física (K/L/M)."""
    return bool(re.fullmatch(r"\d{8}[A-Z]|[XYZKLM]\d{7}[A-Z]", ident))


def cif_valido(cif: str) -> bool:
    """Dígito o letra de control de un CIF de persona jurídica."""
    m = re.fullmatch(r"([ABCDEFGHJNPQRSUVW])(\d{7})([0-9A-J])", cif)
    if not m:
        return False
    letra, num, control = m.groups()
    pares = sum(int(num[i]) for i in (1, 3, 5))
    impares = sum(sum(divmod(int(num[i]) * 2, 10)) for i in (0, 2, 4, 6))
    digito = (10 - (pares + impares) % 10) % 10
    if letra in "PQRSNW":
        return control == LETRAS_CONTROL[digito]
    if letra in "ABEH":
        return control == str(digito)
    return control in (str(digito), LETRAS_CONTROL[digito])


def clasificar(valor):
    """(cif normalizado, tipo de entidad o None, motivo de descarte o None)."""
    cif = normalizar(valor)
    if not cif:
        return cif, None, "Identificador vacío"
    if es_persona_fisica(cif) or re.fullmatch(r"\d{7,8}[A-Z]?", cif):
        return cif, "Persona física", "NIF de persona física (excluido por protección de datos)"
    if not cif_valido(cif):
        return cif, None, "CIF con formato o dígito de control incorrecto"
    if cif[0] in PUBLICOS:
        return cif, TIPOS[cif[0]], "Administración u organismo público"
    return cif, TIPOS[cif[0]], None
