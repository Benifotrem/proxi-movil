#!/usr/bin/env bash
# Instala o actualiza proxi-granja en el HP desde este repositorio. Idempotente.
# Copia archivos y prepara 3proxy, pero NO activa servicios ni recarga networkd:
# la activación se hace aparte (ver README, "Granja HP").
#
# Uso:  sudo ./granja-hp/instalar.sh [usuario_api]     (por defecto: cesar)
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
API_USER="${1:-cesar}"
ROOTFS=/opt/proxi-granja/3proxy-rootfs

log() { printf '\033[1;34m[instalar]\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m[error]\033[0m %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "Ejecuta con sudo."
id "$API_USER" >/dev/null 2>&1 || die "No existe el usuario $API_USER."
for c in ip nft dhcpcd adb curl python3 docker; do
    command -v "$c" >/dev/null 2>&1 || die "Falta el comando: $c"
done

# Usuario de sistema para 3proxy.
id proxi3proxy >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin proxi3proxy

install -d -m 0755 /etc/proxi-granja /opt/proxi-granja /var/lib/proxi-granja
# Límites de velocidad (FUP): los escribe la API y los aplica proxi-granja sync.
install -d -m 0755 -o "$API_USER" /var/lib/proxi-granja/limites
# Credenciales de 3proxy generadas por la API (GET /credentials/<id>/new).
install -d -m 0700 -o "$API_USER" /var/lib/proxi-granja/credenciales
# Temporales de la API (fuera de los directorios vigilados por proxi-granja-sync.path).
install -d -m 0700 -o "$API_USER" /var/lib/proxi-granja/.tmp
install -d -m 0750 -g proxi3proxy /etc/proxi-granja/3proxy
# Claves ADB adicionales (*.adb_key), p. ej. la del PC desde el que se autorizó un móvil con scrcpy.
install -d -m 0700 -o "$API_USER" /etc/proxi-granja/adb_keys

# Inventario y credenciales: solo se crean si no existen (son datos de producción).
if [[ ! -f /etc/proxi-granja/modems.conf ]]; then
    install -m 0644 "$REPO/etc/modems.conf" /etc/proxi-granja/modems.conf
    log "Creado /etc/proxi-granja/modems.conf (revísalo)."
fi
if [[ ! -f /etc/proxi-granja/credentials ]]; then
    install -m 0640 -g "$API_USER" "$REPO/etc/credentials.example" /etc/proxi-granja/credentials
    log "Creado /etc/proxi-granja/credentials desde el ejemplo: CAMBIA LAS CONTRASEÑAS."
fi
chown "root:$API_USER" /etc/proxi-granja/credentials
chmod 0640 /etc/proxi-granja/credentials

# Usuario con el que la sincronización ejecuta ADB (el mismo que la API).
if [[ ! -f /etc/proxi-granja/granja.env ]]; then
    printf '# Ajustes de proxi-granja sync\nADB_USER=%s\n' "$API_USER" > /etc/proxi-granja/granja.env
    chmod 0644 /etc/proxi-granja/granja.env
fi

# Programas.
install -m 0755 "$REPO/proxi-granja.sh" /usr/local/sbin/proxi-granja
install -m 0644 "$REPO/api.py" /opt/proxi-granja/api.py

# Sistema de archivos de 3proxy, extraído de la imagen Docker (no se ejecuta como contenedor).
if [[ ! -x "$ROOTFS/usr/bin/3proxy" ]]; then
    docker image inspect proxy-3proxy >/dev/null 2>&1 || docker build -t proxy-3proxy "$REPO/3proxy"
    log "Extrayendo 3proxy de la imagen proxy-3proxy en $ROOTFS..."
    install -d "$ROOTFS"
    cid="$(docker create proxy-3proxy)"
    docker export "$cid" | tar -x -C "$ROOTFS"
    docker rm "$cid" >/dev/null
fi
install -d "$ROOTFS/etc/proxi-granja"

# systemd, udev y networkd.
install -m 0644 "$REPO"/systemd/proxi-*.service "$REPO"/systemd/proxi-*.timer "$REPO"/systemd/proxi-*.path /etc/systemd/system/
sed -i "s/^User=cesar$/User=$API_USER/" /etc/systemd/system/proxi-granja-api.service
install -m 0644 "$REPO/udev/90-proxi-granja.rules" /etc/udev/rules.d/
install -m 0644 "$REPO/profile.d/proxi-granja-adb.sh" /etc/profile.d/
install -m 0644 "$REPO/networkd/05-proxi-modems.network" /etc/systemd/network/
systemctl daemon-reload
udevadm control --reload

log "Instalado."
log "Siguiente: revisar /etc/proxi-granja/{modems.conf,credentials} y activar (README, 'Granja HP')."
