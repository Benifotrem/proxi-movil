#!/usr/bin/env bash
# Verifica toda la cadena: contenedor -> puerto local 3105 -> API key -> granja HP -> Cloudflare Tunnel,
# y la entrada pública de proxies (contenedor stream -> UFW -> HP).
#
# Uso:  ./scripts/healthcheck.sh           (comprobación completa)
#       ./scripts/healthcheck.sh --local   (omite túnel y hostname público)
# Código de salida 0 si todo está OK, 1 si algo falla.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
ENV_FILE="$REPO_DIR/.env"
CONTAINER="proxi_movil_gateway"
STREAM_CONTAINER="proxi_movil_stream"
LOCAL_URL="http://127.0.0.1:3105"
LOCAL_ONLY=0
[[ "${1:-}" == "--local" ]] && LOCAL_ONLY=1

FAILS=0
ok()   { printf '  \033[1;32mOK\033[0m    %s\n' "$*"; }
fail() { printf '  \033[1;31mFALLO\033[0m %s\n' "$*"; FAILS=$((FAILS + 1)); }
skip() { printf '  \033[1;33m--\033[0m    %s\n' "$*"; }

[[ -f "$ENV_FILE" ]] || { echo "No existe $ENV_FILE"; exit 1; }
set -a
# shellcheck disable=SC1090
. "$ENV_FILE"
set +a
TUNNEL_NAME="${TUNNEL_NAME:-proxi-movil}"
FARM_API_PORT="${FARM_API_PORT:-5000}"
PROXY_PORTS="${PROXY_PORTS:-20001-20040}"
PRIMER_PUERTO="${PROXY_PORTS%%-*}"
ULTIMO_PUERTO="${PROXY_PORTS##*-}"

# Código HTTP de una petición; la API key se pasa por descriptor para no exponerla en 'ps'.
http_code() {
    local url="$1" with_key="${2:-0}" timeout="${3:-10}"
    if [[ "$with_key" == 1 ]]; then
        curl -s -o /dev/null -w '%{http_code}' -m "$timeout" \
            -H @<(printf 'X-API-KEY: %s\n' "$API_KEY") "$url"
    else
        curl -s -o /dev/null -w '%{http_code}' -m "$timeout" "$url"
    fi
}

echo "== Gateway local =="
state="$(docker inspect -f '{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{end}}' "$CONTAINER" 2>/dev/null)"
case "$state" in
    "running healthy") ok "Contenedor $CONTAINER: running (healthy)" ;;
    running*)          fail "Contenedor $CONTAINER: $state" ;;
    *)                 fail "Contenedor $CONTAINER no está en ejecución" ;;
esac

listen="$(ss -ltnH 2>/dev/null | awk '{print $4}' | grep -E ':3105$' | sort -u | tr '\n' ' ')"
if ! command -v ss >/dev/null 2>&1; then
    skip "ss no disponible: no se verifica el bind del puerto 3105"
elif [[ -z "$listen" ]]; then
    fail "Nada escucha en el puerto 3105"
elif [[ "$listen" =~ ^(127\.0\.0\.1:3105[[:space:]]*)+$ ]]; then
    ok "Puerto 3105 solo en 127.0.0.1"
else
    fail "Puerto 3105 expuesto fuera de localhost: $listen"
fi

code="$(http_code "$LOCAL_URL/healthz")"
[[ "$code" == 200 ]] && ok "/healthz -> 200" || fail "/healthz -> $code"

code="$(http_code "$LOCAL_URL/status")"
[[ "$code" == 401 ]] && ok "/status sin API key -> 401" || fail "/status sin API key -> $code (esperado 401)"

echo "== Granja HP (${FARM_API_HOST:-?}:$FARM_API_PORT) =="
if timeout 3 bash -c "</dev/tcp/${FARM_API_HOST}/${FARM_API_PORT}" 2>/dev/null; then
    ok "TCP alcanzable desde el VPS (Tailscale/WireGuard)"
else
    fail "TCP no alcanzable: revisa Tailscale/WireGuard y que Flask escuche en el puerto $FARM_API_PORT"
fi

code="$(http_code "$LOCAL_URL/status" 1 15)"
[[ "$code" == 200 ]] && ok "/status con API key -> 200" || fail "/status con API key -> $code"

echo "== Entrada de proxies ($PROXY_PORTS) =="
state="$(docker inspect -f '{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{end}}' "$STREAM_CONTAINER" 2>/dev/null)"
case "$state" in
    "running healthy") ok "Contenedor $STREAM_CONTAINER: running (healthy)" ;;
    running*)          fail "Contenedor $STREAM_CONTAINER: $state" ;;
    *)                 fail "Contenedor $STREAM_CONTAINER no está en ejecución" ;;
esac
if command -v ufw >/dev/null 2>&1 && [[ $EUID -eq 0 ]]; then
    if ufw status | grep -qE "^$PRIMER_PUERTO:$ULTIMO_PUERTO/tcp +ALLOW"; then
        ok "UFW permite $PRIMER_PUERTO:$ULTIMO_PUERTO/tcp"
    else
        fail "UFW no permite $PRIMER_PUERTO:$ULTIMO_PUERTO/tcp (sudo ufw allow $PRIMER_PUERTO:$ULTIMO_PUERTO/tcp)"
    fi
else
    skip "UFW no comprobado (requiere root)"
fi
if timeout 3 bash -c "</dev/tcp/${FARM_API_HOST}/${PRIMER_PUERTO}" 2>/dev/null; then
    ok "HP acepta conexiones en el puerto $PRIMER_PUERTO (3proxy del primer móvil)"
else
    fail "HP no acepta conexiones en el puerto $PRIMER_PUERTO (proxi-granja / 3proxy en el HP)"
fi

echo "== Cloudflare Tunnel =="
if [[ $LOCAL_ONLY == 1 ]]; then
    skip "Omitido (--local)"
else
    if systemctl is-active --quiet "cloudflared-$TUNNEL_NAME"; then
        ok "Servicio cloudflared-$TUNNEL_NAME activo"
    else
        fail "Servicio cloudflared-$TUNNEL_NAME inactivo"
    fi

    code="$(http_code "https://$PUBLIC_HOSTNAME/status")"
    [[ "$code" == 401 ]] && ok "https://$PUBLIC_HOSTNAME/status sin API key -> 401" || fail "https://$PUBLIC_HOSTNAME/status sin API key -> $code (esperado 401)"

    code="$(http_code "https://$PUBLIC_HOSTNAME/status" 1 20)"
    [[ "$code" == 200 ]] && ok "https://$PUBLIC_HOSTNAME/status con API key -> 200" || fail "https://$PUBLIC_HOSTNAME/status con API key -> $code"

    code="$(http_code "https://$PUBLIC_HOSTNAME/healthz")"
    [[ "$code" == 403 ]] && ok "/healthz bloqueado desde internet -> 403" || fail "/healthz desde internet -> $code (esperado 403)"
fi

echo
if [[ $FAILS -eq 0 ]]; then
    echo "Todo OK."
    exit 0
fi
echo "$FAILS comprobación(es) fallida(s)."
exit 1
