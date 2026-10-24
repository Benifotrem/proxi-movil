# Instrucciones para Claude

## Forma de trabajo con el usuario

- Dar **un solo proceso por interacción** (un comando o una acción concreta) y **esperar el reporte del usuario** antes de dar el siguiente.
- No encadenar varios pasos en una misma respuesta ni adelantar pasos futuros.
- No generar informes ni archivos que no se hayan pedido.

## Flujo de cambios

- Trabajar siempre en una rama aparte y abrir un pull request hacia `main`; nunca hacer push directo a `main`.
- Claude fusiona él mismo los pull requests una vez probados, avisando al usuario de cada uno (qué cambia y cómo se probó) antes de fusionarlo.
- Al fusionar en `main`, el VPS despliega solo en ≤2 min (`scripts/auto_deploy.sh`, timer `proxi-movil-deploy`).
- Lo que está en `granja-hp/` no se despliega automáticamente: hay que aplicarlo a mano en el HP.
