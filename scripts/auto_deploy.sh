#!/usr/bin/env bash
# Despliegue automático desde GitHub (lo ejecuta el timer proxi-movil-deploy).
# Si origin/main tiene commits nuevos: hace fast-forward, recrea el gateway si cambió su
# configuración y lo verifica. Si la verificación falla, vuelve a la versión anterior y
# no reintenta ese commit hasta que llegue otro más nuevo.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
BRANCH="${DEPLOY_BRANCH:-main}"
LOCAL_URL="http://127.0.0.1:3105"
FAILED_FILE="$REPO_DIR/.git/proxi-movil-deploy-failed"

log() { echo "[deploy] $*"; }

cd "$REPO_DIR"

# Evita dos despliegues a la vez.
exec 9>"$REPO_DIR/.git/proxi-movil-deploy.lock"
flock -n 9 || exit 0

git fetch -q origin "$BRANCH"
ACTUAL="$(git rev-parse HEAD)"
NUEVO="$(git rev-parse "origin/$BRANCH")"

[[ "$ACTUAL" == "$NUEVO" ]] && exit 0
if [[ -f "$FAILED_FILE" && "$(cat "$FAILED_FILE")" == "$NUEVO" ]]; then
    exit 0
fi
if ! git merge-base --is-ancestor "$ACTUAL" "$NUEVO"; then
    log "origin/$BRANCH no es fast-forward de ${ACTUAL:0:7}; requiere despliegue manual."
    exit 1
fi

CAMBIOS="$(git diff --name-only "$ACTUAL" "$NUEVO")"
log "Desplegando ${ACTUAL:0:7} -> ${NUEVO:0:7}"
git merge -q --ff-only "$NUEVO"

gateway_cambiado() {
    grep -qE '^(docker-compose\.yml|nginx/)' <<<"$CAMBIOS"
}

verificar() {
    docker compose up -d --wait --wait-timeout 60 >/dev/null 2>&1 || return 1
    [[ "$(curl -s -o /dev/null -w '%{http_code}' -m 10 "$LOCAL_URL/healthz")" == 200 ]] || return 1
    [[ "$(curl -s -o /dev/null -w '%{http_code}' -m 10 "$LOCAL_URL/status")" == 401 ]] || return 1
}

if gateway_cambiado; then
    log "Cambió la configuración del gateway: recreando contenedor."
    docker compose up -d --force-recreate >/dev/null 2>&1 || true
fi

if verificar; then
    rm -f "$FAILED_FILE"
    log "OK: desplegado ${NUEVO:0:7}"
    exit 0
fi

log "FALLO al verificar ${NUEVO:0:7}: volviendo a ${ACTUAL:0:7}"
git reset -q --hard "$ACTUAL"
echo "$NUEVO" > "$FAILED_FILE"
if gateway_cambiado; then
    docker compose up -d --force-recreate >/dev/null 2>&1 || true
fi
if verificar; then
    log "Rollback OK: en ${ACTUAL:0:7}"
else
    log "ATENCIÓN: el gateway tampoco responde tras el rollback"
fi
exit 1
