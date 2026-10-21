#!/usr/bin/env bash
# Crea (o reutiliza) un Cloudflare Tunnel dedicado que publica PUBLIC_HOSTNAME -> http://127.0.0.1:3105
# y lo instala como servicio systemd propio (cloudflared-<TUNNEL_NAME>), sin tocar syry_nginx
# ni ningún otro servicio cloudflared existente.
#
# Uso:  sudo ./scripts/setup_cloudflare_tunnel.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
ENV_FILE="$REPO_DIR/.env"
LOCAL_SERVICE="http://127.0.0.1:3105"

log()  { printf '\033[1;34m[tunnel]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[aviso]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[error]\033[0m %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "Ejecuta este script con sudo/root."
[[ -f "$ENV_FILE" ]] || die "No existe $ENV_FILE. Copia .env.example a .env y complétalo."

set -a
# shellcheck disable=SC1090
. "$ENV_FILE"
set +a

PUBLIC_HOSTNAME="${PUBLIC_HOSTNAME:-}"
TUNNEL_NAME="${TUNNEL_NAME:-proxi-movil}"
[[ -n "$PUBLIC_HOSTNAME" && "$PUBLIC_HOSTNAME" != "proxy.tudominio.com" ]] \
    || die "Define PUBLIC_HOSTNAME en .env con tu subdominio real."

CF_HOME="${CLOUDFLARED_HOME:-/root/.cloudflared}"
CONF_DIR="/etc/cloudflared/$TUNNEL_NAME"
CONF_FILE="$CONF_DIR/config.yml"
SERVICE_NAME="cloudflared-$TUNNEL_NAME"
UNIT_FILE="/etc/systemd/system/$SERVICE_NAME.service"

# 1. Instalar cloudflared si no existe (Debian/Ubuntu, repositorio oficial de Cloudflare).
if ! command -v cloudflared >/dev/null 2>&1; then
    log "Instalando cloudflared..."
    command -v apt-get >/dev/null 2>&1 || die "Instala cloudflared manualmente: https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/"
    mkdir -p --mode=0755 /usr/share/keyrings
    curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg -o /usr/share/keyrings/cloudflare-main.gpg
    echo "deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared any main" \
        > /etc/apt/sources.list.d/cloudflared.list
    apt-get update -qq
    apt-get install -y -qq cloudflared
fi
CLOUDFLARED_BIN="$(command -v cloudflared)"
log "cloudflared: $("$CLOUDFLARED_BIN" --version 2>&1 | head -n1)"

# 2. Autenticación con la cuenta de Cloudflare (abre una URL para autorizar la zona).
if [[ ! -f "$CF_HOME/cert.pem" ]]; then
    log "Autorizando cloudflared en tu cuenta de Cloudflare (abre la URL que aparece)..."
    "$CLOUDFLARED_BIN" tunnel login
fi
[[ -f "$CF_HOME/cert.pem" ]] || die "No se encontró $CF_HOME/cert.pem tras el login."

# 3. Crear o reutilizar el túnel.
get_tunnel_id() {
    "$CLOUDFLARED_BIN" tunnel list --name "$TUNNEL_NAME" --output json 2>/dev/null \
        | grep -oE '"id": *"[0-9a-f-]{36}"' | head -n1 | grep -oE '[0-9a-f-]{36}' || true
}
TUNNEL_ID="$(get_tunnel_id)"
if [[ -z "$TUNNEL_ID" ]]; then
    log "Creando túnel '$TUNNEL_NAME'..."
    "$CLOUDFLARED_BIN" tunnel create "$TUNNEL_NAME"
    TUNNEL_ID="$(get_tunnel_id)"
fi
[[ -n "$TUNNEL_ID" ]] || die "No se pudo obtener el ID del túnel '$TUNNEL_NAME'."
log "Túnel: $TUNNEL_NAME ($TUNNEL_ID)"

# 4. Credenciales y configuración en un directorio propio.
install -d -m 0700 "$CONF_DIR"
CRED_SRC="$CF_HOME/$TUNNEL_ID.json"
CRED_DST="$CONF_DIR/$TUNNEL_ID.json"
if [[ ! -f "$CRED_DST" ]]; then
    [[ -f "$CRED_SRC" ]] || die "Faltan las credenciales $CRED_SRC (el túnel se creó en otra máquina). Bórralo con 'cloudflared tunnel delete $TUNNEL_NAME' y repite."
    install -m 0600 "$CRED_SRC" "$CRED_DST"
fi

cat > "$CONF_FILE" <<CONF
tunnel: $TUNNEL_ID
credentials-file: $CRED_DST
no-autoupdate: true

ingress:
  - hostname: $PUBLIC_HOSTNAME
    service: $LOCAL_SERVICE
    originRequest:
      connectTimeout: 10s
  - service: http_status:404
CONF
chmod 0600 "$CONF_FILE"
"$CLOUDFLARED_BIN" tunnel --config "$CONF_FILE" ingress validate

# 5. Registro DNS (CNAME). No se sobrescriben registros existentes.
log "Creando CNAME $PUBLIC_HOSTNAME -> túnel..."
if ! "$CLOUDFLARED_BIN" tunnel route dns "$TUNNEL_ID" "$PUBLIC_HOSTNAME"; then
    warn "No se pudo crear el CNAME (¿ya existe un registro para $PUBLIC_HOSTNAME?)."
    warn "Revísalo en Cloudflare DNS: debe ser CNAME -> $TUNNEL_ID.cfargotunnel.com (proxied)."
fi

# 6. Servicio systemd dedicado.
cat > "$UNIT_FILE" <<UNIT
[Unit]
Description=Cloudflare Tunnel $TUNNEL_NAME ($PUBLIC_HOSTNAME -> $LOCAL_SERVICE)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=$CLOUDFLARED_BIN --no-autoupdate --config $CONF_FILE tunnel run
Restart=on-failure
RestartSec=5s
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable "$SERVICE_NAME" >/dev/null
systemctl restart "$SERVICE_NAME"
sleep 3
systemctl is-active --quiet "$SERVICE_NAME" || die "El servicio $SERVICE_NAME no arrancó. Revisa: journalctl -u $SERVICE_NAME -n 50"

if ! curl -fsS -m 5 "$LOCAL_SERVICE/healthz" >/dev/null 2>&1; then
    warn "El gateway local ($LOCAL_SERVICE) no responde. Arráncalo con: docker compose up -d"
fi

log "Listo. https://$PUBLIC_HOSTNAME -> $LOCAL_SERVICE (servicio: $SERVICE_NAME)"
