#!/bin/sh
# Genera /etc/nginx/proxi-allowlist.conf (entradas del geo $proxi_ip_permitida) a partir de
# API_ALLOWED_IPS: IPs o redes CIDR separadas por comas o espacios. Vacía = se permite cualquier IP.
# El propio VPS/Docker (127.0.0.1, redes privadas) siempre está permitido.
set -eu
salida=/etc/nginx/proxi-allowlist.conf
{
    echo "127.0.0.1 1;"
    echo "172.16.0.0/12 1;"
    echo "192.168.0.0/16 1;"
    if [ -z "${API_ALLOWED_IPS:-}" ]; then
        echo "0.0.0.0/1 1;"
        echo "128.0.0.0/1 1;"
    else
        for ip in $(echo "$API_ALLOWED_IPS" | tr ',' ' '); do
            if echo "$ip" | grep -Eq '^([0-9]{1,3}\.){3}[0-9]{1,3}(/[0-9]{1,2})?$'; then
                echo "$ip 1;"
            else
                echo "proxi-allowlist: entrada ignorada (no es IPv4/CIDR): $ip" >&2
            fi
        done
    fi
} > "$salida"
echo "proxi-allowlist: $(grep -c . "$salida") entradas en $salida"
