"""Persistencia local (SQLite en la carpeta de datos del worker): permite reanudar un lote interrumpido."""
import json
import sqlite3

ESQUEMA = """
CREATE TABLE IF NOT EXISTS lotes (
    nombre TEXT PRIMARY KEY, entrada TEXT, creado TEXT
);
CREATE TABLE IF NOT EXISTS empresas (
    lote TEXT, cif TEXT, fila INTEGER, razon_social TEXT, direccion TEXT, municipio TEXT, cp TEXT,
    tipo TEXT, motivo_descarte TEXT,
    bdns_estado TEXT DEFAULT 'pendiente', bdns TEXT, bdns_error TEXT,
    borme_estado TEXT DEFAULT 'pendiente', borme TEXT,
    emp_conf INTEGER, sector_conf TEXT, telefono TEXT,
    actualizado TEXT,
    PRIMARY KEY (lote, cif)
);
CREATE TABLE IF NOT EXISTS borme_dias (
    fecha TEXT PRIMARY KEY, estado TEXT, entradas INTEGER, procesado TEXT
);
CREATE TABLE IF NOT EXISTS borme_actos (
    fecha TEXT, clave TEXT, nombre TEXT, texto TEXT
);
CREATE INDEX IF NOT EXISTS borme_actos_clave ON borme_actos (clave);
CREATE TABLE IF NOT EXISTS exclusiones (
    cif TEXT PRIMARY KEY, motivo TEXT, fecha TEXT
);
"""


def abrir(ruta):
    con = sqlite3.connect(ruta, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(ESQUEMA)
    return con


def cargar_json(valor, defecto=None):
    try:
        return json.loads(valor) if valor else defecto
    except (TypeError, ValueError):
        return defecto
