"""Lectura del archivo de entrada (CSV o Excel). Solo se leen CIF, razón social, dirección, población y CP:
el resto de columnas (datos comerciales de la cartera, contactos…) no se leen ni se guardan."""
import csv
from pathlib import Path

ALIAS = {
    "cif": ["CLI_ID_FISCAL", "CIF", "NIF", "CIF/NIF", "NIF/CIF", "ID_FISCAL"],
    "razon_social": ["CLI_NOMBRE", "RAZON SOCIAL", "RAZÓN SOCIAL", "NOMBRE", "EMPRESA", "DENOMINACION"],
    "direccion": ["CLI_DIRECCION", "DIRECCION", "DIRECCIÓN", "DOMICILIO"],
    "municipio": ["CLI_POBLACION", "POBLACION", "POBLACIÓN", "MUNICIPIO", "LOCALIDAD"],
    "cp": ["CLI_CODIGO_POSTAL", "CP", "CODIGO POSTAL", "CÓDIGO POSTAL", "C.P."],
}


def _filas(ruta):
    ruta = Path(ruta)
    if ruta.suffix.lower() in (".xlsx", ".xlsm"):
        from openpyxl import load_workbook
        wb = load_workbook(ruta, read_only=True, data_only=True)
        ws = wb.worksheets[0]
        for fila in ws.iter_rows(values_only=True):
            yield [("" if v is None else v) for v in fila]
        wb.close()
    else:
        texto = ruta.read_text(encoding="utf-8-sig", errors="replace")
        dialecto = csv.Sniffer().sniff(texto[:5000], delimiters=",;\t")
        yield from csv.reader(texto.splitlines(), dialecto)


def leer(ruta):
    """[{fila, cif, razon_social, direccion, municipio, cp}] — solo esas columnas."""
    filas = _filas(ruta)
    cabecera = [str(h).strip().upper() for h in next(filas)]
    indices = {}
    for campo, nombres in ALIAS.items():
        for n in nombres:
            if n.upper() in cabecera:
                indices[campo] = cabecera.index(n.upper())  # primera aparición (el archivo puede repetir columnas)
                break
    if "cif" not in indices:
        raise SystemExit(f"No encuentro la columna del CIF. Cabeceras admitidas: {', '.join(ALIAS['cif'])}")
    res = []
    for n, fila in enumerate(filas, start=2):
        reg = {"fila": n}
        for campo, i in indices.items():
            v = fila[i] if i < len(fila) else ""
            reg[campo] = str(v).strip() if v is not None else ""
        if reg.get("cp"):
            reg["cp"] = reg["cp"].split(".")[0].zfill(5)
        if reg["cif"]:
            res.append(reg)
    return res
