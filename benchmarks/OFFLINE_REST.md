# Benchmark REST offline

Desde la raíz del repositorio:

```bash
python3 -B benchmarks/offline_rest_benchmark.py --mode all --output benchmarks/results/offline-rest-run-01
```

El destino debe ser nuevo: no se sobrescriben ejecuciones anteriores. `all`
ejecuta primero un smoke de 120 segundos. Solo si no hay incumplimientos inicia
las fases completas: calentamiento 120s, volume 360s, vol_delta 360s, errores
360s y revalidación 240s. Son tiempos reales. Cada fase deja terminar operaciones
ya iniciadas y registra cualquier exceso como `drain_overrun_seconds`. El tiempo
ocioso también cuenta; los informes indican el número real de operaciones.

`--mode smoke` y `--mode full` permiten reproducir cada parte por separado.
No se modifican módulos de producción ni sus límites. Se usa RESTConfig por
defecto, threads reales, respuestas de `python/tests/virtual_pipeline.py`,
runner WATCH simulado y reloj UTC sintético derivado de monotonic. El transporte
rechaza endpoints no presentes en las fixtures. Los sockets, urllib y procesos
externos quedan bloqueados durante el ensayo. No hacen falta claves ni red.

El coordinador persiste entre rondas de una fase. Las sondas de errores y
revalidación usan coordinadores aislados explícitos; no simulan una cuota conjunta
por IP. El orden de los fallos, los datos y el jitter son fijos. El orden de los
threads y las latencias observadas varían con la planificación del sistema.
Las variantes de saturación con capacidad 1 y 3 usan dos procesos aislados,
con threads reales y presupuesto propio, para completar ambas cargas 50/10 y
runner de 20s dentro de los seis minutos. Se crean mediante fork tras drenar los
workers del padre. No se suman sus cuotas como si fueran una única ventana.

## Archivos locales

- `manifest.json`: commit, hashes de fuentes, configuración y parámetros.
- `events.jsonl`: eventos de admisión, contabilidad, transporte, captura,
  contexto, revisión, errores y fases, escritos progresivamente.
- `round-*.json`: informes JSON originales del pipeline, con datos sintéticos.
- `summary.json`: métricas agregadas, rondas, limitaciones e incumplimientos.
- `saturation-capacity-*/`: evidencia separada de las variantes 1 y 3.

Después de completar cada ejecución:

```bash
python3 -B benchmarks/report_offline_rest.py benchmarks/results/offline-rest-run-01/full
```

Genera `analysis.json` y `REPORT.md` sin modificar la evidencia original, con
percentiles por escenario, comparación lote/incremental y ventanas de consumo
por coordinador. Los percentiles captura→IA incluyen solo snapshots invocados;
los motivos de caducidad se presentan por separado.
`analysis.json` calcula la reutilización por fase, ronda y timestamp: timestamps
UTC sintéticos iguales en procesos distintos no significan que compartan caché.
Cuando hay una corrección posterior únicamente de métricas, `archived_sources`
en el manifest identifica la copia exacta del harness ejecutado; sus hashes
siguen permitiendo reconstruir el ensayo original.

Estos resultados se guardan bajo `benchmarks/results/`, ignorado por Git.
El código y la documentación contienen únicamente identificadores técnicos y
datos sintéticos; los metadatos no incluyen rutas personales, hostname ni cuentas.

## Interpretación

Una admisión normal no puede superar 900 unidades incluyendo compromisos y margen.
Las respuestas sobredimensionadas pueden generar deuda: se permite observar un
máximo superior a 900, pero ninguna nueva petición puede admitirse mientras
falta capacidad. Cada intento fallido conserva el peso reservado. Los scopes
de deuda aparecen separados en el resumen.

Las invariantes incluyen concurrencia HTTP ≤3, capacidad incremental, liberación
de recursos, una captura por símbolo, identidad del snapshot scanner→análisis,
ausencia de invocaciones duplicadas/caducadas y contexto dentro de TTL. Una captura
nominal debe completar sin incidencias distintas de `unknown_freshness`. La espera
REST previa se separa de los relojes de captura; las tolerancias de observación
son 0,2s para captura/admisión/revalidación y no cambian los deadlines de producción.

El comparador por lote puede producir `snapshot_expired`; se conserva como resultado
de comparación, no como defecto de admisión incremental. Las sondas verifican 429,
Retry-After numérico/fecha, espera excesiva, timeout, JSON inválido, fallo de refresco,
deuda, retorno de revalidación con transporte bloqueado y aislamiento de resultados
tardíos. El transporte bloqueado se libera antes de terminar la sonda.

Una excepción no clasificada detiene el paso al benchmark completo y se registra
sin payload ni traceback. Hay que investigarla antes de atribuirla al pipeline.
`external_io_attempt` es un fallo de aislamiento del harness. Los resultados
sintéticos no demuestran la cuota externa por IP, cancelación DNS/socket dura ni
rendimiento de modelos o servicios reales.

Verificación rápida del harness desde `python/`:

```bash
python3 -B -m unittest discover -s tests -p test_offline_rest_benchmark.py
```
