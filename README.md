# proxi-movil

Capa pública de la granja de proxies móviles. Expone la API de gestión y control (estado y rotación de IP) del servidor HP a través de Cloudflare, protegida con API key, sin tocar las aplicaciones que ya corren en el VPS (`syry`, `digamelo`).

## Arquitectura

```text
Cliente ──HTTPS──> Cloudflare ──Tunnel──> cloudflared (host VPS, servicio cloudflared-proxi-movil)
                                              │
                                              ▼
                                   127.0.0.1:3105  (contenedor proxi_movil_gateway, nginx)
                                              │  valida X-API-KEY, rate limit, solo 3 rutas
                                              ▼
                              Tailscale / WireGuard (VPS <-> servidor HP en LAN)
                                              │
                                              ▼
                              API Flask de la granja  <FARM_API_HOST>:5000
                                ├── 3proxy Redmi 6  :8081
                                └── 3proxy LG K9    :8082
```

- **No se abre ningún puerto nuevo**: el gateway escucha solo en `127.0.0.1:3105` y el túnel sale del VPS hacia Cloudflare. UFW sigue con 22/80/443.
- **`syry_nginx` no se modifica ni se reinicia**: el túnel no pasa por los puertos 80/443.
- **Tráfico de proxy (3proxy) no se expone por Cloudflare**: solo la API HTTP de gestión.

## Endpoints públicos

Todas las peticiones requieren la cabecera `X-API-KEY`.

| Método | Ruta            | Descripción                                        |
|--------|-----------------|----------------------------------------------------|
| GET    | `/status`       | Estado e IP pública actual de cada módem           |
| GET    | `/rotate/redmi` | Rota la IP del Redmi 6 (datos móviles off/on, ADB) |
| GET    | `/rotate/lg`    | Rota la IP del LG K9 (datos móviles off/on, ADB)   |

Respuestas del gateway:

| Código | Cuerpo                            | Motivo                                    |
|--------|-----------------------------------|-------------------------------------------|
| 401    | `{"error":"unauthorized"}`        | Falta la API key o no es válida           |
| 403    | `{"error":"forbidden"}`           | Método distinto de GET, o `/healthz` desde internet |
| 404    | `{"error":"not_found"}`           | Cualquier otra ruta                       |
| 429    | `{"error":"rate_limited"}`        | Límite superado                           |
| 502    | `{"error":"farm_unreachable"}`    | La API del servidor HP no responde        |

Límites: 30 peticiones/min por IP (ráfaga de 10) y 6 rotaciones/min por dispositivo (ráfaga de 2). La API key **no** se reenvía al servidor HP.

```bash
curl -H "X-API-KEY: $API_KEY" https://proxy.tudominio.com/status
curl -H "X-API-KEY: $API_KEY" https://proxy.tudominio.com/rotate/redmi
curl -H "X-API-KEY: $API_KEY" https://proxy.tudominio.com/rotate/lg
```

## Requisitos

**VPS**
- Docker con el plugin `docker compose`.
- `curl`, `ss` (iproute2) y `systemd`.
- Conectividad con el servidor HP por Tailscale o WireGuard.
- El dominio gestionado en Cloudflare.

**Servidor HP**
- La API Flask escuchando en el puerto `5000` en la interfaz de Tailscale/WireGuard, con las rutas `/status`, `/rotate/redmi` y `/rotate/lg`.
- Recomendado: que su firewall solo permita el puerto 5000 desde la IP del VPS en la red privada.

## Despliegue

1. **Conectar VPS y HP** (ejemplo con Tailscale, en ambas máquinas):
   ```bash
   curl -fsSL https://tailscale.com/install.sh | sh
   sudo tailscale up
   tailscale ip -4        # en el HP: anota la IP 100.x.y.z
   ```
   Desde el VPS: `curl http://100.x.y.z:5000/status` debe responder.

2. **Clonar y configurar**:
   ```bash
   git clone git@github.com:Benifotrem/proxi-movil.git /opt/proxi-movil
   cd /opt/proxi-movil
   cp .env.example .env && chmod 600 .env
   openssl rand -hex 32    # pega el resultado en API_KEY
   nano .env               # PUBLIC_HOSTNAME, API_KEY, FARM_API_HOST
   ```

3. **Levantar el gateway**:
   ```bash
   docker compose up -d
   ./scripts/healthcheck.sh --local
   ```

4. **Crear el Cloudflare Tunnel** (instala `cloudflared` si falta, crea el túnel y el CNAME, e instala el servicio `cloudflared-proxi-movil`):
   ```bash
   sudo ./scripts/setup_cloudflare_tunnel.sh
   ```
   El script no sobrescribe registros DNS existentes ni toca otros servicios `cloudflared`.

5. **Verificar toda la cadena**:
   ```bash
   ./scripts/healthcheck.sh
   ```

## Operación

```bash
docker compose logs -f gateway                    # logs del gateway
journalctl -u cloudflared-proxi-movil -f          # logs del túnel
git pull && docker compose up -d --force-recreate # actualizar
```

**Rotar la API key:** edita `API_KEY` en `.env` y ejecuta `docker compose up -d --force-recreate`.

**Desinstalar:**
```bash
docker compose down
sudo systemctl disable --now cloudflared-proxi-movil
sudo rm /etc/systemd/system/cloudflared-proxi-movil.service && sudo systemctl daemon-reload
sudo cloudflared tunnel delete proxi-movil        # y borra el CNAME en Cloudflare
```

## Notas de seguridad

- `.env` contiene la API key: no lo subas al repositorio (permisos `600`).
- Docker se salta UFW en los puertos publicados; por eso el puerto se publica explícitamente en `127.0.0.1:3105`. `healthcheck.sh` comprueba que no quede expuesto en `0.0.0.0`.
- La IP real del cliente se toma de `CF-Connecting-IP`; `/healthz` solo responde a orígenes locales.
- Opcional: añade en Cloudflare una regla WAF o de rate limiting para el subdominio como segunda capa.
