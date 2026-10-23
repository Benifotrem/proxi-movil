#!/bin/bash

DEVICE=$1

rotate_redmi() {
  local SERIAL="c698d0867d32"
  local PORT="8081"
  echo "[*] IP actual (Redmi 6): $(curl -s -m 10 -x http://127.0.0.1:$PORT https://api.ipify.org)"
  echo "[*] Rotando vía Modo Avión..."
  adb -s $SERIAL shell cmd connectivity airplane-mode enable
  sleep 3
  adb -s $SERIAL shell cmd connectivity airplane-mode disable
  sleep 5
  echo "[*] Nueva IP (Redmi 6): $(curl -s -m 10 -x http://127.0.0.1:$PORT https://api.ipify.org)"
}

rotate_lg() {
  local SERIAL="LMX210c09bb44c"
  local PORT="8082"
  echo "[*] IP actual (LG K9): $(curl -s -m 10 -x http://127.0.0.1:$PORT https://api.ipify.org)"
  echo "[*] Rotando vía reinicio de datos móviles..."
  adb -s $SERIAL shell svc data disable
  sleep 3
  adb -s $SERIAL shell svc data enable
  sleep 6
  echo "[*] Nueva IP (LG K9): $(curl -s -m 10 -x http://127.0.0.1:$PORT https://api.ipify.org)"
}

case "$DEVICE" in
  redmi|redmi6|8081)
    rotate_redmi
    ;;
  lg|lgk9|8082)
    rotate_lg
    ;;
  all|todos)
    rotate_redmi
    echo "----------------------------------------"
    rotate_lg
    ;;
  *)
    echo "Uso: $0 {redmi|lg|all}"
    exit 1
    ;;
esac
