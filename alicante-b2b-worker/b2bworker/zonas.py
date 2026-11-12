"""Zona comercial de la provincia de Alicante por código postal y municipio."""
import json
import os
from pathlib import Path

from .textos import municipio

DEFECTO = Path(__file__).with_name("zonas.json")


class Zonas:
    def __init__(self, ruta=None):
        datos = json.loads(Path(ruta if ruta and os.path.exists(ruta) else DEFECTO).read_text(encoding="utf-8"))
        self.resto = datos["resto"]
        self.por_cp, self.por_municipio, self.orden = {}, {}, []
        for z in datos["zonas"]:
            self.orden.append(z["nombre"])
            for cp in z.get("cp", []):
                self.por_cp[str(cp)] = z["nombre"]
            for m in z["municipios"]:
                self.por_municipio[municipio(m)] = z["nombre"]
        self.orden.append(self.resto)

    def zona(self, cp, poblacion):
        cp = str(cp or "").strip().split(".")[0].zfill(5) if cp else ""
        if cp in self.por_cp:
            return self.por_cp[cp]
        return self.por_municipio.get(municipio(poblacion), self.resto)

    def indice(self, zona):
        return self.orden.index(zona) if zona in self.orden else len(self.orden)
