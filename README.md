# proxi-movil

Granja de proxies móviles 4G: cada cliente alquila un móvil/SIM dedicado (1:1) con HTTP y SOCKS5 en el mismo puerto, usuario y contraseña propios, y rotación de IP a demanda. Dos piezas:

- **VPS** (este directorio): API de control publicada en Cloudflare y entrada pública de los puertos de proxy. Convive con `syry` y `digamelo` sin tocarlos.
- **Servidor HP** (`granja-hp/`): los móviles por anclaje USB, cada uno aislado en su propio network namespace con su 3proxy.

## Arquitectura

```text
                    API de control (backend PHP en Hostinger)
                                    │ HTTPS + X-API-KEY
                                    ▼
Cloudflare ──Tunnel──> cloudflared ──> 127.0.0.1:3105  gateway (nginx): API key, rate limit, CORS
                                                │
Clientes (AdsPower, scripts…)                   │
   │ HTTP/SOCKS5 user:pass                      │
   ▼                                            │
VPS :20001-20040  stream (nginx, UFW)           │
   │                                            │
   └────────────── Tailscale ───────────────────┤
                                                ▼
                         HP (100.118.170.11)
                           :5000  API de la granja (api.py)
                           :2000N ──DNAT──> namespace modem_X  10.200.N.2:3128  3proxy (auto)
                                                   └── mdmN (USB) ── móvil ── 4G
```

- El tráfico de proxy **no pasa por Cloudflare** (no admite CONNECT/SOCKS): entra por los puertos `20001-20040` del VPS, abiertos en UFW.
- La API de control no abre puertos: `127.0.0.1:3105` + Cloudflare Tunnel.
- `syry_nginx` y los puertos 80/443 no se tocan.

## API de control

Todas las peticiones requieren la cabecera `X-API-KEY` y se hacen **desde el backend** (nunca desde el navegador).

| Método | Ruta | Descripción |
|--------|------|-------------|
| GET | `/status` | Por móvil: puerto, IP pública, `online`, `usb_present`, `limit_kbps` |
| GET | `/rotate/<id>` | Rota la IP del móvil `<id>` del inventario |
| GET | `/usage` | Tráfico acumulado por móvil (`down_bytes`, `up_bytes`, `total_bytes`) |
| GET | `/limit/<id>/<kbps>` | Limita la velocidad del móvil en ambos sentidos (64–1000000 kbps); se aplica en ≤30 s |
| GET | `/limit/<id>/off` | Quita el límite |
| GET | `/credentials/<id>/new` | Genera usuario y contraseña nuevos para el móvil y los devuelve (una sola vez); las anteriores dejan de valer |
| GET | `/health` | Estado de la granja sin consultar IPs (rápido) |

Respuesta de `/rotate/<id>`:

```json
{"status": "success", "device": "modem_2_3", "port": 20001, "old_ip": "90.167.42.37",
 "new_ip": "84.78.251.40", "changed": true, "duration_ms": 4200}
```

| Código | Motivo |
|--------|--------|
| 401 | Falta la API key o no es válida |
| 403 | Método distinto de GET, o `/healthz` desde internet |
| 404 | Ruta o móvil inexistente |
| 409 | Ya hay una rotación en curso de ese móvil |
| 429 | Rotación antes de 10 s desde la anterior (`retry_after`), o límite del gateway |
| 500 | Fallo de ADB al rotar (`adb_error`, con el mensaje del móvil) |
| 503 | La API del HP no responde (`farm_unreachable`), o tras rotar el móvil no recuperó IP pública en 25 s (`no_ip`) |

Límites del gateway: 30 peticiones/min por IP (ráfaga de 10) y 6 rotaciones/min por móvil (ráfaga de 2).

**Lista blanca de IPs:** `API_ALLOWED_IPS` en `.env` (IPs o CIDR separadas por comas; vacía = cualquier IP). Solo esas IPs (comparadas con `CF-Connecting-IP`) pueden usar la API; el resto recibe 403 aunque tenga la API key. Incluir la IP de salida del backend (hosting compartido `server1886`: `147.93.37.28`, puede cambiar si Hostinger migra la cuenta) y la IP pública del VPS (para `healthcheck.sh`). El propio VPS/Docker siempre está permitido en local. Tras editarla: `docker compose up -d --force-recreate gateway`.

**Tráfico y uso razonable (FUP, 350 GB/mes por puerto):** `/usage` devuelve contadores **acumulados desde la instalación**, que no se reinician. El backend PHP, por cada cliente:

1. Al inicio de su ciclo: guarda `total_bytes` como punto de partida y llama a `/limit/<id>/off`.
2. Periódicamente: consumo del ciclo = `total_bytes` actual − punto de partida.
3. Al **80 % (280 GB)**: aviso por email/panel de que se acerca al umbral.
4. Al **100 % (350 GB)**: aviso de entrada en FUP y `/limit/<id>/1500` (1,5 Mbps) hasta el siguiente ciclo.

El límite se guarda en `/var/lib/proxi-granja/limites/<id>` y sobrevive a reinicios.

**Credenciales de los clientes:** el backend PHP llama a `/credentials/<id>/new` al **asignar** un puerto a un cliente, al **dar de baja** a un cliente (para cortarle el acceso) y cuando el cliente pulsa **Regenerar contraseña**. Respuesta:

```json
{"device": "modem_2_4", "port": 20002, "user": "u5cf4a85c51", "password": "…32 hex…", "applies_within_s": 30}
```

La contraseña solo se devuelve en esa respuesta: el backend la guarda y la muestra en el panel del cliente. Se aplica en 1–2 s (vigilante `proxi-granja-sync.path`; como mucho 30 s) y reinicia solo el 3proxy de ese móvil.

**Change IP URL:** la sirve el backend PHP con un token por cliente (`https://soymas.pro/api/change/<token>`), que valida el token y llama a `/rotate/<id>` desde el servidor. La `X-API-KEY` nunca sale del backend.

**CORS:** solo para orígenes que cumplan `CORS_ORIGIN_REGEX` (por defecto `https://soymas.pro`); es una medida de respaldo.

## Acceso de los clientes al proxy

```text
host: <IP pública del VPS>   puerto: 2000N   usuario/contraseña: los de su móvil
```

HTTP y SOCKS5 en el mismo puerto (3proxy `auto`). Ejemplo: `curl -x http://usuario:clave@VPS:20001 https://api.ipify.org`.

## VPS

### Requisitos

- Docker con `docker compose`, `curl`, `ss`, `systemd`.
- Tailscale conectado al HP.
- Dominio en Cloudflare.

### Despliegue inicial

1. Tailscale en VPS y HP (`curl -fsSL https://tailscale.com/install.sh | sh`; en el VPS: `tailscale up --accept-dns=false`).
2. Clonar y configurar:
   ```bash
   git clone git@github.com:Benifotrem/proxi-movil.git /opt/proxi-movil
   cd /opt/proxi-movil
   cp .env.example .env && chmod 600 .env
   nano .env      # PUBLIC_HOSTNAME, API_KEY (openssl rand -hex 32), FARM_API_HOST, PROXY_PORTS
   ```
3. Levantar y abrir los puertos de proxy:
   ```bash
   docker compose up -d
   sudo ufw allow 20001:20040/tcp
   ```
4. Cloudflare Tunnel: `sudo ./scripts/setup_cloudflare_tunnel.sh` (no sobrescribe DNS ni toca otros `cloudflared`).
5. Verificar: `./scripts/healthcheck.sh`.

### Despliegue automático

El timer `proxi-movil-deploy` consulta `main` cada 2 minutos. `scripts/auto_deploy.sh`:

1. Hace fast-forward a `origin/main`; si no es fast-forward, se detiene.
2. Recrea los contenedores solo si cambiaron `docker-compose.yml` o `nginx/`.
3. Verifica: contenedores healthy, `/healthz` → 200 y `/status` sin key → 401.
4. Si falla, vuelve al commit anterior y no reintenta ese commit hasta que llegue uno más nuevo.

```bash
sudo cp systemd/proxi-movil-deploy.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now proxi-movil-deploy.timer
journalctl -u proxi-movil-deploy -f
```

Flujo: rama → pull request → merge en `main` → despliegue automático. **`granja-hp/` no se despliega solo**: se instala en el HP con `granja-hp/instalar.sh`.

### Operación

```bash
docker compose logs -f gateway            # API de control
docker compose logs -f stream             # entrada de proxies
journalctl -u cloudflared-proxi-movil -f  # túnel
sudo systemctl start proxi-movil-deploy   # forzar despliegue
```

Rotar la API key: editar `API_KEY` en `.env` y `docker compose up -d --force-recreate gateway`.

## Granja HP (`granja-hp/`)

### Diseño

Los Android dan al HP una IP aleatoria dentro de la misma subred de anclaje (`192.168.42.0/24`; los Android 11+ incluso cambian la subred). Con muchos móviles, dos interfaces acaban con la misma IP. Por eso **cada móvil vive en su propio network namespace**, y su IP local deja de importar:

| Pieza | Detalle |
|-------|---------|
| Inventario | `/etc/proxi-granja/modems.conf`: `id usb adb_serial puerto rotacion` (una línea por móvil) |
| Credenciales | `/var/lib/proxi-granja/credenciales/<id>` (las genera la API, 0600) y, para altas manuales, `/etc/proxi-granja/credentials` (root:cesar 0640): `id usuario contraseña`. Prevalecen las de la API |
| Namespace | `modem_<usb>`; dentro, la interfaz USB renombrada `mdm<N>` (DHCP con `dhcpcd`) y `lan0` 10.200.N.2/30 |
| 3proxy | `proxi-modem-proxy@<id>`: `auto` (HTTP+SOCKS5) con `auth strong` en 10.200.N.2:3128, ejecutado con `NetworkNamespacePath=` desde el rootfs de la imagen `proxy-3proxy` |
| Entrada | `tailscale0:2000N` → DNAT → 10.200.N.2:3128 (tabla nft `proxi_granja`) |
| Sincronización | `proxi-granja sync` cada 30 s (timer), al conectar un móvil (udev) y al cambiar credenciales o límites (`proxi-granja-sync.path`): crea namespaces, mueve interfaces, reactiva el anclaje USB por ADB, genera configs, aplica NAT y límites, cuenta tráfico. ADB se ejecuta como `ADB_USER` (`/etc/proxi-granja/granja.env`) |
| Tráfico | `/var/lib/proxi-granja/trafico.db`: bytes acumulados por móvil (contador del veth `pg<N>`) |
| FUP | `/var/lib/proxi-granja/limites/<id>` (kbps, lo escribe la API): `tc tbf` en `lan0` (bajada) y `pg<N>` (subida) |
| networkd | `05-proxi-modems.network`: ignora las interfaces de anclaje USB (`rndis_host`, `cdc_ether`, `cdc_ncm`) |
| API | `proxi-granja-api` (usuario `cesar`), `/opt/proxi-granja/api.py`, puerto 5000 |

`N = puerto - 20000`. Rotación por `airplane` (modo avión; intenta `airplane_mode_radios=cell`, que MIUI no permite por ADB sin "Depuración USB (ajustes de seguridad)" — no es imprescindible porque el anclaje USB no está en la lista por defecto) o `data` (`svc data`, Android < 8).

### Instalación / actualización

```bash
sudo ./granja-hp/instalar.sh          # copia archivos y prepara 3proxy; no activa nada
sudo nano /etc/proxi-granja/modems.conf /etc/proxi-granja/credentials
sudo networkctl reload
sudo systemctl enable --now proxi-granja-sync.timer proxi-granja-sync.path proxi-granja-api.service
```

### Requisitos de los móviles de producción

- **Android 8 o superior.** La granja reactiva sola el anclaje (`svc usb setFunctions rndis` por ADB, como mucho una vez por minuto) cuando un móvil del inventario está en ADB pero sin interfaz de red, por ejemplo tras reconectar el cable con el móvil bloqueado. Recomendado además: *Opciones de desarrollador → Configuración USB predeterminada → RNDIS (USB Ethernet)*. Así el anclaje USB se reactiva solo cada vez que el móvil se reconecta (corte de luz, reinicio del HP, cable). Comprobación: desenchufar y volver a enchufar; `adb -s <serial> shell getprop sys.usb.config` debe dar `rndis,adb` sin tocar el móvil.
- En Android 7 o anterior (por ejemplo, el LG K9 de pruebas) el anclaje **vuelve a MTP al reconectar** y hay que reactivarlo a mano: `svc usb setFunction rndis` no arranca el anclaje completo en esas versiones.
- Depuración USB activada y el HP autorizado de forma permanente. Si el aviso de autorización no puede aceptarse en el móvil (pantalla rota) pero sí se autorizó desde otro equipo (p. ej. el PC con scrcpy), copiar la clave privada de ese equipo (`%USERPROFILE%\.android\adbkey` en Windows) a `/etc/proxi-granja/adb_keys/<nombre>.adb_key` (cesar, 0600) y reiniciar el servidor ADB (`adb kill-server`): la granja la usa además de la suya (`ADB_VENDOR_KEYS`).
- Si `/health` pasa a `degraded` (`modems_with_network` < `modems_total`), algún móvil ha perdido la red: el backend debe avisar al administrador.

### Añadir un móvil

1. Conectarlo por USB con el anclaje activado y la depuración USB autorizada.
2. Puerto USB: `readlink -f /sys/class/net/<iface>/device` (por ejemplo, `.../2-5/2-5:1.0` → `2-5`); serial: `adb devices -l`.
3. Añadir la línea a `modems.conf` y generar sus credenciales con `/credentials/<id>/new` (o añadirlas a mano en `credentials`).
4. Esperar 30 s o ejecutar `sudo systemctl start proxi-granja-sync`. Sin reiniciar nada más.

### Diagnóstico

```bash
cat /run/proxi-granja/estado.json                     # estado por móvil
ip netns list; sudo ip -n modem_2_3 addr              # namespaces
systemctl status 'proxi-modem-*'                      # DHCP y 3proxy
sudo nft list table ip proxi_granja                   # DNAT
journalctl -u proxi-granja-sync -n 50                 # sincronización
```

## Notas de seguridad

- `.env` (VPS) y `/etc/proxi-granja/credentials` (HP) contienen secretos: nunca en el repositorio.
- Docker se salta UFW en los puertos publicados: el gateway se publica solo en `127.0.0.1:3105` y el contenedor `stream` usa la red del host para quedar sujeto a UFW.
- 3proxy solo escucha dentro de cada namespace y solo es accesible por Tailscale (DNAT en `tailscale0`), no desde la LAN.
- La IP real del cliente de la API se toma de `CF-Connecting-IP`; `/healthz` solo responde a orígenes locales.
