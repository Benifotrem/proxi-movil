#!/usr/bin/env bash
# proxi-granja: reconcilia la granja de móviles con el inventario (/etc/proxi-granja/modems.conf).
#
# Por cada móvil:
#   - network namespace propio (nombre = id del móvil) con su interfaz USB dentro, renombrada a mdm<N>;
#   - enlace veth host<->namespace: pg<N> 10.200.N.1/30  <->  lan0 10.200.N.2/30;
#   - DHCP dentro del namespace (proxi-modem-dhcp@<id>) y 3proxy en 10.200.N.2:3128 (proxi-modem-proxy@<id>);
#   - DNAT del puerto público (llega por tailscale0) a 10.200.N.2:3128;
#   - contabilidad de tráfico acumulado (bytes del veth pg<N>) en /var/lib/proxi-granja/trafico.db;
#   - límite de velocidad (FUP) si existe /var/lib/proxi-granja/limites/<id> (kbps; lo escribe la API);
#   - anclaje USB: si el móvil está en ADB pero sin interfaz de red, se reactiva con
#     "svc usb setFunctions rndis" (Android 8+; como mucho un intento por minuto);
#   - credenciales de 3proxy: /var/lib/proxi-granja/credenciales/<id> (las genera la API) o, si no hay,
#     /etc/proxi-granja/credentials (alta manual).
# N = puerto_publico - 20000. Lo ejecuta proxi-granja-sync.timer cada 30 s y udev al conectar un móvil.
#
# Uso: proxi-granja [sync]
set -uo pipefail

CONF_DIR="${PROXI_GRANJA_CONF:-/etc/proxi-granja}"
INVENTARIO="$CONF_DIR/modems.conf"
CREDENCIALES="$CONF_DIR/credentials"
PROXY_CFG_DIR="$CONF_DIR/3proxy"
STATE_DIR="${PROXI_GRANJA_STATE:-/run/proxi-granja}"
DATA_DIR="${PROXI_GRANJA_DATA:-/var/lib/proxi-granja}"
LIMITES_DIR="$DATA_DIR/limites"
CREDENCIALES_DIR="$DATA_DIR/credenciales"
BASE_PUERTO=20000
PUERTO_INTERNO=3128
ENTRADA_IFACE="${PROXI_GRANJA_ENTRADA:-tailscale0}"

# Usuario con el que corre ADB (el de la API): su servidor ADB y sus claves autorizadas.
ADB_USER=""
[[ -r "$CONF_DIR/granja.env" ]] && . "$CONF_DIR/granja.env"

log() { echo "[$1] ${*:2}" >&2; }

USB_SYS="${PROXI_GRANJA_USB_SYS:-/sys/bus/usb/devices}"

# Ruta USB actual (p. ej. 1-1.2.2) del dispositivo con ese número de serie. Así un móvil se
# reconoce aunque el hub cambie de puerto o de controlador (antes: 4-1.2.2 → ahora 1-1.2.2).
usb_por_serial() {
    local serial="$1" d
    [[ -n "$serial" && "$serial" != "-" ]] || return 0
    for d in "$USB_SYS"/*; do
        [[ -r "$d/serial" ]] || continue
        if [[ "$(cat "$d/serial" 2>/dev/null)" == "$serial" ]]; then
            basename "$d"
            return
        fi
    done
}

iface_por_usb() {
    local usb="$1" n
    # "if:<nombre>" fuerza una interfaz concreta (útil para pruebas).
    if [[ "$usb" == if:* ]]; then
        ip link show "${usb#if:}" >/dev/null 2>&1 && echo "${usb#if:}"
        return
    fi
    for n in /sys/class/net/*; do
        if [[ "$(readlink -f "$n/device" 2>/dev/null)" == */"$usb"/"$usb":* ]]; then
            basename "$n"
            return
        fi
    done
}

adb_como_api() {
    [[ -n "$ADB_USER" ]] || return 1
    local home
    home="$(getent passwd "$ADB_USER" | cut -d: -f6)"
    timeout 15 runuser -u "$ADB_USER" -- env HOME="$home" ADB_VENDOR_KEYS="$CONF_DIR/adb_keys" adb "$@"
}

reactivar_anclaje() {
    # El modo USB vuelve a "solo carga" tras reconectar el cable si el móvil está bloqueado o
    # su Android no lo restaura solo. Se fuerza el anclaje por ADB, como mucho una vez por minuto.
    local id="$1" serial="$2" marca="$STATE_DIR/$id.anclaje"
    [[ -n "$serial" ]] || return
    if [[ -f "$marca" ]] && (( $(date +%s) - $(stat -c %Y "$marca") < 60 )); then
        return
    fi
    touch "$marca"
    [[ "$(adb_como_api -s "$serial" get-state 2>/dev/null)" == device ]] || return
    adb_como_api -s "$serial" shell svc usb setFunctions rndis >/dev/null 2>&1 \
        && log "$id" "anclaje USB reactivado por ADB"
}

credencial() {
    # Las generadas por la API ("usuario contraseña") tienen prioridad sobre el archivo manual.
    if [[ -r "$CREDENCIALES_DIR/$1" ]]; then
        awk 'NF >= 2 { print $1, $2; exit }' "$CREDENCIALES_DIR/$1"
        return
    fi
    [[ -r "$CREDENCIALES" ]] || return 1
    awk -v id="$1" '$1 == id { print $2, $3; exit }' "$CREDENCIALES"
}

generar_3proxy() {
    local n="$1" usuario="$2" clave="$3"
    cat <<CFG
nserver 8.8.8.8
nserver 1.1.1.1
nscache 65536
timeouts 1 5 30 60 180 1800 15 60
users $usuario:CL:$clave
auth strong
allow $usuario
auto -n -a -p$PUERTO_INTERNO -i10.200.$n.2
CFG
}

aplicar_limite() {
    # Límite en ambos sentidos: la bajada del cliente sale del namespace por lan0,
    # la subida sale del host por pg<N>. Imprime el límite vigente (kbps) o nada.
    local id="$1" ns="$2" hv="$3" deseado="" aplicado
    [[ -r "$LIMITES_DIR/$id" ]] && deseado="$(tr -dc '0-9' < "$LIMITES_DIR/$id")"
    if [[ -n "$deseado" ]] && (( deseado < 64 || deseado > 1000000 )); then
        log "$id" "límite inválido ($deseado kbps): ignorado"; deseado=""
    fi
    aplicado="$(cat "$STATE_DIR/$id.limit" 2>/dev/null)"
    # Si el veth se recreó, el qdisc ya no existe aunque el estado diga lo contrario.
    [[ -n "$aplicado" ]] && ! tc qdisc show dev "$hv" | grep -q tbf && aplicado=""

    if [[ "$deseado" != "$aplicado" ]]; then
        if [[ -n "$deseado" ]]; then
            local burst=$(( deseado / 50 > 32 ? deseado / 50 : 32 ))
            ip netns exec "$ns" tc qdisc replace dev lan0 root tbf rate "${deseado}kbit" burst "${burst}kbit" latency 400ms
            tc qdisc replace dev "$hv" root tbf rate "${deseado}kbit" burst "${burst}kbit" latency 400ms
            log "$id" "velocidad limitada a $deseado kbps"
        else
            ip netns exec "$ns" tc qdisc del dev lan0 root 2>/dev/null
            tc qdisc del dev "$hv" root 2>/dev/null
            log "$id" "límite de velocidad retirado"
        fi
        echo "$deseado" > "$STATE_DIR/$id.limit"
    fi
    echo "$deseado"
}

asegurar_servicio() {
    systemctl is-active --quiet "$1" || systemctl start --no-block "$1"
}

configurar_modem() {
    local id="$1" usb="$2" puerto="$3" serial="$4"
    local n=$((puerto - BASE_PUERTO))
    local ns="$id" hv="pg$n" md="mdm$n"

    if (( n < 1 || n > 250 )); then
        log "$id" "puerto $puerto fuera de rango (20001-20250)"; return
    fi

    # Namespace y enlace veth con el host.
    [[ -e "/run/netns/$ns" ]] || ip netns add "$ns"
    ip -n "$ns" link set lo up
    if ! ip link show "$hv" >/dev/null 2>&1; then
        ip link add "$hv" type veth peer name lan0 netns "$ns"
    fi
    ip addr replace "10.200.$n.1/30" dev "$hv"
    ip link set "$hv" up
    ip -n "$ns" addr replace "10.200.$n.2/30" dev lan0
    ip -n "$ns" link set lan0 up

    # Interfaz USB del móvil: si aparece en el host, se mueve al namespace. Se busca por número de
    # serie (vale en cualquier puerto); la ruta de modems.conf queda como respaldo.
    local iface ruta
    ruta="$(usb_por_serial "$serial")"
    if [[ -n "$ruta" && "$ruta" != "$usb" && "$usb" != if:* ]]; then
        log "$id" "localizado por número de serie en USB $ruta (modems.conf dice $usb)"
    fi
    [[ "$usb" == if:* ]] && ruta=""
    iface="$(iface_por_usb "${ruta:-$usb}")"
    if [[ -n "$iface" ]]; then
        ip link set dev "$iface" netns "$ns" \
            && ip -n "$ns" link set dev "$iface" name "$md" \
            && log "$id" "interfaz $iface movida al namespace como $md"
    fi
    local presente=0
    if ip -n "$ns" link show "$md" >/dev/null 2>&1; then
        presente=1
        ip -n "$ns" link set "$md" up
    else
        reactivar_anclaje "$id" "$serial"
    fi

    echo "IFACE=$md" > "$STATE_DIR/$id.env"

    local limite
    limite="$(aplicar_limite "$id" "$ns" "$hv")"

    # Configuración de 3proxy (solo si el móvil tiene credenciales).
    local cred usuario clave
    cred="$(credencial "$id")"
    read -r usuario clave <<<"$cred"
    if [[ ! "$usuario" =~ ^[A-Za-z0-9_.-]{3,64}$ || ! "$clave" =~ ^[A-Za-z0-9_.-]{8,128}$ ]]; then
        log "$id" "sin credenciales válidas en $CREDENCIALES: proxy desactivado"
        systemctl stop "proxi-modem-proxy@$id" 2>/dev/null
        usuario=""
    else
        local cfg="$PROXY_CFG_DIR/$id.cfg" nuevo
        nuevo="$(generar_3proxy "$n" "$usuario" "$clave")"
        if [[ "$(cat "$cfg" 2>/dev/null)" != "$nuevo" ]]; then
            ( umask 077; printf '%s\n' "$nuevo" > "$cfg" )
            # Lo lee 3proxy (usuario proxi3proxy); contiene la contraseña del cliente.
            chgrp proxi3proxy "$cfg" 2>/dev/null; chmod 0640 "$cfg"
            systemctl restart --no-block "proxi-modem-proxy@$id"
            log "$id" "configuración de 3proxy actualizada"
        fi
        asegurar_servicio "proxi-modem-proxy@$id"
    fi

    (( presente )) && asegurar_servicio "proxi-modem-dhcp@$id"

    # Estado para la API.
    local lan_ip gw
    lan_ip="$(ip -n "$ns" -4 -o addr show dev "$md" 2>/dev/null | awk '{split($4, a, "/"); print a[1]; exit}')"
    gw="$(ip -n "$ns" -4 route show default 2>/dev/null | awk '{print $3; exit}')"
    printf '"%s":{"port":%d,"usb_present":%s,"lan_ip":"%s","gateway":"%s","proxy":%s,"limit_kbps":%s}' \
        "$id" "$puerto" "$([[ $presente == 1 ]] && echo true || echo false)" "$lan_ip" "$gw" \
        "$([[ -n "$usuario" ]] && echo true || echo false)" "${limite:-null}"
}

contabilizar_trafico() {
    # trafico.db: "id total_bajada total_subida ultimo_rx ultimo_tx" (bytes, acumulado desde la instalación).
    # Bajada = lo que sale del namespace hacia el cliente (rx de pg<N> en el host); subida = al revés.
    local db="$DATA_DIR/trafico.db" actual="" id usb serial puerto rot n rx tx
    while read -r id usb serial puerto rot; do
        n=$((puerto - BASE_PUERTO))
        [[ -r "/sys/class/net/pg$n/statistics/rx_bytes" ]] || continue
        rx="$(cat "/sys/class/net/pg$n/statistics/rx_bytes")"
        tx="$(cat "/sys/class/net/pg$n/statistics/tx_bytes")"
        actual+="$id $rx $tx"$'\n'
    done < <(grep -vE '^[[:space:]]*(#|$)' "$INVENTARIO")

    touch "$db"
    awk -v actual="$actual" '
        BEGIN {
            n = split(actual, lineas, "\n")
            for (i = 1; i <= n; i++) if (split(lineas[i], c, " ") == 3) { rx[c[1]] = c[2]; tx[c[1]] = c[3] }
        }
        {
            id = $1; down = $2; up = $3; lrx = $4; ltx = $5
            if (id in rx) {
                # Si el contador bajó, el veth se recreó: se cuenta desde cero.
                down += (rx[id] >= lrx) ? rx[id] - lrx : rx[id]
                up   += (tx[id] >= ltx) ? tx[id] - ltx : tx[id]
                lrx = rx[id]; ltx = tx[id]; delete rx[id]
            }
            print id, down, up, lrx, ltx
        }
        END { for (id in rx) print id, rx[id], tx[id], rx[id], tx[id] }
    ' "$db" > "$db.tmp" && mv "$db.tmp" "$db"
    chmod 0644 "$db"
}

aplicar_nat() {
    # Reglas DNAT/masquerade regeneradas desde el inventario; solo se aplican si cambian.
    local reglas="$STATE_DIR/nft.conf" nuevo id usb serial puerto rot n
    nuevo="table ip proxi_granja
delete table ip proxi_granja
table ip proxi_granja {
  chain pre {
    type nat hook prerouting priority dstnat; policy accept;"
    while read -r id usb serial puerto rot; do
        n=$((puerto - BASE_PUERTO))
        nuevo+="
    iifname \"$ENTRADA_IFACE\" tcp dport $puerto dnat to 10.200.$n.2:$PUERTO_INTERNO"
    done < <(grep -vE '^[[:space:]]*(#|$)' "$INVENTARIO")
    nuevo+="
  }
  chain post {
    type nat hook postrouting priority srcnat; policy accept;
    oifname \"pg*\" masquerade
  }
}"
    if [[ "$(cat "$reglas" 2>/dev/null)" != "$nuevo" ]]; then
        printf '%s\n' "$nuevo" > "$reglas"
        nft -f "$reglas" && log nat "reglas DNAT aplicadas"
    fi

    # Docker deja FORWARD en DROP: permitir entrada -> 3proxy y sus respuestas.
    if iptables -n -L DOCKER-USER >/dev/null 2>&1; then
        iptables -C DOCKER-USER -i "$ENTRADA_IFACE" -o pg+ -p tcp --dport "$PUERTO_INTERNO" -j ACCEPT 2>/dev/null \
            || iptables -I DOCKER-USER -i "$ENTRADA_IFACE" -o pg+ -p tcp --dport "$PUERTO_INTERNO" -j ACCEPT
        iptables -C DOCKER-USER -i pg+ -o "$ENTRADA_IFACE" -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT 2>/dev/null \
            || iptables -I DOCKER-USER -i pg+ -o "$ENTRADA_IFACE" -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
    fi
}

sync() {
    [[ $EUID -eq 0 ]] || { echo "Ejecutar como root." >&2; exit 1; }
    [[ -r "$INVENTARIO" ]] || { echo "No existe $INVENTARIO" >&2; exit 1; }
    mkdir -p "$STATE_DIR" "$PROXY_CFG_DIR" "$DATA_DIR" "$LIMITES_DIR"
    sysctl -qw net.ipv4.ip_forward=1

    exec 9>"$STATE_DIR/sync.lock"
    flock 9

    local estado="{" sep="" frag id usb serial puerto rot
    while read -r id usb serial puerto rot; do
        [[ "$id" =~ ^[a-z0-9_]{1,15}$ ]] || { log "$id" "id inválido (a-z0-9_, máx. 15)"; continue; }
        frag="$(configurar_modem "$id" "$usb" "$puerto" "$serial")"
        [[ -n "$frag" ]] && { estado+="$sep$frag"; sep=","; }
    done < <(grep -vE '^[[:space:]]*(#|$)' "$INVENTARIO")
    estado+="}"

    aplicar_nat
    contabilizar_trafico
    printf '{"updated":%d,"modems":%s}\n' "$(date +%s)" "$estado" > "$STATE_DIR/estado.json.tmp"
    mv "$STATE_DIR/estado.json.tmp" "$STATE_DIR/estado.json"
    chmod 0644 "$STATE_DIR/estado.json"
}

case "${1:-sync}" in
    sync) sync ;;
    *) echo "Uso: $0 [sync]" >&2; exit 1 ;;
esac
