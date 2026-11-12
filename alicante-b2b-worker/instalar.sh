#!/usr/bin/env bash
# Instala el worker B2B en la carpeta de datos del usuario (sin sudo, sin servicios, sin tocar la granja).
#   ~/alicante-b2b-worker/            datos: entrada/, salida/, logs/, b2b.sqlite, venv/
#   ~/alicante-b2b-worker/worker      enlace al lanzador de este repositorio
# Uso: ./alicante-b2b-worker/instalar.sh
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATOS="${B2B_DATA:-$HOME/alicante-b2b-worker}"
[[ $EUID -ne 0 ]] || { echo "Ejecútalo con tu usuario, no con sudo." >&2; exit 1; }

mkdir -p "$DATOS"/{entrada,salida,logs}
chmod 700 "$DATOS"   # contiene datos de clientes: solo tu usuario
if [[ ! -x "$DATOS/venv/bin/python" ]]; then
    python3 -m venv "$DATOS/venv"
fi
"$DATOS/venv/bin/pip" install --quiet --disable-pip-version-check -r "$REPO/requirements.txt"
ln -sfn "$REPO/worker" "$DATOS/worker"
[[ -f "$DATOS/zonas.json" ]] || cp "$REPO/b2bworker/zonas.json" "$DATOS/zonas.json"
echo "Instalado en $DATOS"
echo "Pruebas: cd $REPO && $DATOS/venv/bin/python -m unittest discover -s tests"
