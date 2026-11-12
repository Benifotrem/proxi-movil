# Worker B2B Alicante

Cualifica una cartera de empresas de la provincia de Alicante a partir de su CIF y genera un Excel de trabajo
comercial (prioridades A/B/C, ruta por zonas, palanca de subvención 2027). Se ejecuta **bajo demanda por SSH**
en el HP: un solo proceso con prioridad baja (`nice`/`ionice`), sin servicios ni puertos, sin tocar la granja.

## Fuentes (solo públicas y de reutilización permitida)

| Fuente | Para qué | Cómo |
|---|---|---|
| BDNS (infosubvenciones.es) | Kit Digital / Kit Consulting (fecha, importe, segmento), IVACE, Cámara de Alicante, ayudas 2025-2026 | API pública por CIF, conexión directa, 1 petición cada 1-2,5 s |
| BORME (datos abiertos del BOE) | Estado (activa, extinguida, concurso, disuelta), constitución, objeto social, administradores vigentes | Índice local de la Sección Primera de Alicante: se descarga una vez y luego solo los días nuevos |
| Tu archivo | CIF, razón social, dirección, población, CP | Solo se leen esas columnas |

No se consultan directorios privados (Empresite, Einforma, Axesor…) ni se usan los móviles para esquivar límites:
sus condiciones lo prohíben y sus bases de datos están protegidas. Empleados, sector y teléfono se confirman en la
llamada y se escriben en el Excel (columnas ✎).

## Datos

Todo vive en `~/alicante-b2b-worker/` (permisos 700): `entrada/`, `salida/`, `logs/`, `b2b.sqlite`, `venv/`,
`zonas.json` (editable). Nada de eso va al repositorio.

## Instalación (una vez)

```bash
cd ~/proxi-movil && git pull && ./alicante-b2b-worker/instalar.sh
```

## Uso

```bash
W=~/alicante-b2b-worker/worker
$W borme-probar --fecha 2026-11-11            # prueba el lector del BORME con un día
$W borme-indexar --desde 2025-11-01           # índice BORME (el primer año tarda ~10 min; luego, solo días nuevos)
$W procesar --input ~/alicante-b2b-worker/entrada/cartera.xlsx --lote cartera --limit 20   # piloto
$W procesar --input ~/alicante-b2b-worker/entrada/cartera.xlsx --lote cartera --resume     # sigue donde se quedó
$W actualizar --excel ~/alicante-b2b-worker/salida/cartera_AAAAMMDD-HHMM.xlsx               # relee las columnas ✎
$W excluir B12345678                          # oposición: no vuelve a salir
$W estado
```

Lote completo en segundo plano (sigue aunque se corte el SSH):

```bash
tmux new -d -s b2b "~/alicante-b2b-worker/worker procesar --input ~/alicante-b2b-worker/entrada/cartera.xlsx --lote cartera --resume"
tmux attach -t b2b        # ver progreso (salir sin pararlo: Ctrl+b y luego d)
```

## Reglas

- **Descarte:** NIF de persona física, administración u organismo público, extinguida, en concurso o disuelta (BORME),
  menos de 6 meses (BORME) o 250 o más empleados (confirmados).
- **Segmento:** por empleados confirmados; si no, por el segmento de su Kit Digital.
- **Palanca 2027:** 10-249 e industrial/logística/construcción → DIGITALIZA-CV (IVACE); 3-49 servicios → Pyme Digital /
  Cibersegura (Cámara); 0-2 → Auditoría + Alerta Cámara. El sector se estima por palabras clave (razón social y objeto
  social) y manda el confirmado.
- **Prioridad:** A = Kit Digital con menos de 6 meses, o 3-49 empleados con historial IVACE/Cámara y teléfono;
  B = 3-249 empleados con teléfono; C = el resto. Segmento, palanca y prioridad son fórmulas en la base completa.
- **Zonas:** `zonas.json` (CP y municipio).

## Uso responsable de los datos

- No enviar publicidad por email sin consentimiento previo (art. 21 LSSI): teléfono o visita.
- Los administradores salen del BORME: informar en el primer contacto (art. 14 RGPD) y atender la oposición con `excluir`.
