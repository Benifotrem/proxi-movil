#!/usr/bin/env bash
# Mantiene el enrutamiento por origen de cada móvil y el -e de su 3proxy.
# Cada móvil se identifica por su puerto USB físico, no por el nombre de interfaz.
set -u

# puerto_usb  tabla  prioridad  config_3proxy  contenedor
DISPOSITIVOS=(
  "2-3 101 100 /home/cesar/mobile-proxy/redmi6/3proxy.cfg proxy-redmi6"
  "2-4 102 101 /home/cesar/mobile-proxy/lgk9/3proxy.cfg proxy-lgk9"
)

iface_por_usb() {
  local usb="$1" n
  for n in /sys/class/net/*; do
    if [[ "$(readlink -f "$n/device" 2>/dev/null)" == */"$usb"/"$usb":* ]]; then
      basename "$n"
      return
    fi
  done
}

for d in "${DISPOSITIVOS[@]}"; do
  read -r USB TABLA PRIO CFG CONT <<<"$d"

  IFACE="$(iface_por_usb "$USB")"
  [[ -n "$IFACE" ]] || { echo "[$CONT] sin interfaz en USB $USB"; continue; }

  IP="$(ip -4 -o addr show dev "$IFACE" | awk '{split($4, a, "/"); print a[1]; exit}')"
  [[ -n "$IP" ]] || { echo "[$CONT] $IFACE sin IP"; continue; }

  GW="$(ip -4 route show default dev "$IFACE" | awk '{print $3; exit}')"
  GW="${GW:-192.168.42.129}"

  # Tabla propia: todo lo que salga con la IP del móvil va por su interfaz.
  ip route replace default via "$GW" dev "$IFACE" table "$TABLA"
  # rp_filter laxo: las respuestas llegan por la interfaz del móvil, no por la ruta por defecto.
  sysctl -qw "net.ipv4.conf.$IFACE.rp_filter=2"

  if ! ip rule show pref "$PRIO" | grep -q "from $IP lookup $TABLA"; then
    while ip rule del pref "$PRIO" 2>/dev/null; do :; done
    ip rule add from "$IP" lookup "$TABLA" pref "$PRIO"
    echo "[$CONT] regla: from $IP -> tabla $TABLA ($IFACE)"
  fi

  # Si el móvil cambió de IP local, actualizar -e en 3proxy (conservando el inode del bind mount).
  if ! grep -qE -- "-e${IP//./\\.}( |$)" "$CFG"; then
    NUEVO="$(sed -E "s/-e[0-9.]+/-e$IP/g" "$CFG")"
    printf '%s\n' "$NUEVO" > "$CFG"
    docker restart "$CONT" >/dev/null
    echo "[$CONT] 3proxy actualizado a -e$IP y reiniciado"
  fi
done
