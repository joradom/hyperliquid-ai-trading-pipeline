# Research Pipeline v1.0

Descubrimiento y revisión de oportunidades sin una lista fija de símbolos:

```text
screen_market → run_pipeline → scan_symbols una vez → revisiones elegibles
```

No importa Night Guard. No invoca wallets, órdenes ni ejecución financiera.
No necesita WebSocket, TAPE o DOM. Usa las API públicas existentes y las CLI
de IA elegidas por el perfil, exclusivamente para investigación.

## Consola

Desde `python/`, una primera prueba real pequeña:

```bash
python3 -B -m src.research_pipeline --profile default --top-n 10 --limit 3 --max-workers 3 --json
```

Sin `--json` ofrece un informe legible. No requiere símbolos. `--config` y
`--profile` funcionan como en scanner/analysis_pipeline. Puede sobrescribir
`--top-n`, `--limit`, `--sort-by`, `--min-volume`, `--max-workers` y
`--revalidation-deadline-seconds` sin escribir configuración.

El resultado esperado tiene `kind=research_pipeline`, `schema_version=1.0`,
`read_only=true` y `execution_authorized=false`. Conserva el ranking del
screener y los resultados de todos los activos capturados. Solo `ANALYZE`
elegible invoca modelos: Codex por defecto, Codex+Agy con consenso en el perfil
experimental. La clasificación y la decisión reales dependen de las condiciones
actuales. `SKIP` y `WATCH` no invocan modelos. Los proveedores deben estar
disponibles y autenticados previamente; el pipeline no instala ni autentica.

## Configuración y API

Cada perfil admite un bloque opcional `screener`, por ejemplo:

```yaml
screener:
  top_n: 50
  limit: 10
  sort_by: vol_delta
  min_volume: 1000000
  max_workers: 8
```

Precedencia: opciones explícitas de CLI/API, bloque del perfil y valores por
defecto. Se aceptan YAML anteriores sin `screener` y bloques parciales.
Los valores omitidos son `top_n=50`, `limit=10`, `sort_by=vol_delta`,
`max_workers=8`; `min_volume` hereda el mínimo diario del scanner del perfil.
Así el perfil original descubre desde 1 millón USD y el experimental desde
10 millones USD. El comando independiente `market_screener` mantiene su
mínimo original de 10 millones USD y los demás valores predeterminados.

`top_n`, `limit` y `max_workers` son enteros positivos. `min_volume` debe ser
finito y no negativo. YAML conserva el subconjunto sencillo del cargador:
volúmenes enteros; la API y CLI también aceptan valores numéricos decimales.
Los criterios de ranking y la blacklist existente no se alteran. El screener
consulta velas para todo el pool `top_n`, incluso al ordenar por volumen;
limitar solo `limit` no limita todo el coste de descubrimiento.

Para agentes o aplicaciones:

```python
from src.research_config import load_config
from src.research_pipeline import run_research

report = run_research(configuration=load_config(profile="default"), limit=3)
```

`run_research` sin configuración usa los valores originales y no lee archivos.
Acepta dependencias `screener`, `pipeline`, `scanner`, `runner`, `revalidator`,
`clock_ms` y `timer`. Cada dependencia recibe los argumentos de la API existente.
El scanner se inyecta mediante un adaptador que guarda su informe y lo entrega
al pipeline; nunca se ejecuta un scanner previo seguido de otra captura.
El adaptador nativo transmite la reserva y los callbacks de la sesión incremental:
la revisión de cada ANALYZE comienza sin esperar al informe final del scanner.
Los scanners inyectados solo participan si declaran `supports_incremental_review`
exactamente `True` o se solicita `incremental=True`. Los callables antiguos
conservan sus argumentos y comportamiento por lote. Las opciones de API
`incremental`, `max_admitted` (1 a 3) y `admission_wait` pertenecen al pipeline
nativo; no se transmiten implícitamente a un pipeline externo inyectado.


## Contrato del informe

| Campo | Contenido |
| --- | --- |
| `screener` | Informe completo de selección v1.0, con ranking, parámetros, métricas y errores |
| `scanner` | Informe original completo v1.1, sin añadirle campos; null si no se ejecutó |
| `analysis` | Informe completo del pipeline v1.2 original o v1.3 alternativo; null si no se ejecutó |
| `selected_symbols` | Lista original ordenada del screener |
| `pipeline_symbols` | Símbolos admitidos, normalizados y deduplicados para la captura |
| `selection_quality.assets` | Rank original, símbolo de pipeline, calidad y métricas no disponibles |
| `selection_quality.issues` | Errores por activo, datos ausentes, símbolos incompatibles y números no finitos |
| `errors` | Excepciones o contratos inválidos de una etapa completa |
| `parameters` | Parámetros efectivos del screener, incluyendo `max_workers` |
| `timing` | Duración del screener, del pipeline completo y total; los informes internos conservan sus tiempos |

`scanner.results[].decision_snapshot` y `analysis.results[].decision_snapshot`
son los mismos objetos en memoria. La serialización JSON los representa en
ambos informes para conservar cada contrato completo. El Market State se usa
en memoria durante el scanner y no se añade al informe ni se persiste.

Las observaciones del screener no sustituyen el Market State. Solo `vol_delta`
consulta histórico: solicita 49 horas y compara dos bloques de 24 velas horarias
cerradas. `volume`, `oi`, `px_change` y `momentum` usan el contexto; conservan
`volume_prev_24h_usd` y `vol_delta_pct_24h` como null, con
`history_status=not_requested` y `volume_today_usd` igual al volumen del contexto.
El scanner captura sus cuatro temporalidades, L2 y funding; reutiliza el contexto
solo mientras sea válido. No se duplica una captura completa del scanner.
La revalidación REST de una propuesta direccional caducada conserva consultas nuevas.

## Estados y errores

- `ok`: selección y análisis válidos; también selección vacía normal, con
  `outcome=no_candidates`, scanner/analysis null y sin modelos.
- `partial_failure`: datos de selección degradados o errores parciales del
  pipeline, conservando los resultados utilizables. Los errores de velas por
  activo no se convierten por sí solos en fallo total, aunque afecten a todos
  los candidatos, si captura y análisis pueden continuar.
- `failed`: excepción/contrato inválido de etapa, descubrimiento fallido,
  ningún símbolo seleccionado compatible, o pipeline con fallo total.

El screener nativo devuelve `partial_failure` cuando falla una consulta de velas
y conserva los errores de todo el pool en `errors`, antes de aplicar `limit`.
`history_status` distingue histórico disponible, insuficiente, fallido o no
solicitado. Un screener inyectado antiguo puede devolver `status=ok` con errores
por fila; Research conserva ese informe y refleja la degradación externa.
`null` y campos ausentes no se consideran métricas válidas; los campos históricos
no solicitados son opcionales en los rankings que usan solo contexto.
Los números NaN/Infinity del descubrimiento se convierten en null únicamente
en la copia del informe, con una incidencia que identifica su ruta; el objeto
original no se modifica. Esto permite JSON estricto sin inventar cero.
Los errores del pool descartados por `limit` permanecen en el informe nativo;
Research propaga su estado parcial aunque las filas seleccionadas sean útiles.

Salida CLI 0 para `ok`; 1 para `partial_failure` o `failed`, con informe JSON
completo igualmente disponible. Argumentos/configuración inválidos producen
salida 2 antes de consultar mercado. Un agente puede consumir el informe
parcial aunque el comando termine con código 1.

Tests de integración: `test_research_pipeline.py`; configuración:
`test_research_config.py`. Utilizan mercado y modelos simulados, sin red real.


## Presupuesto entre descubrimiento y captura

El recorrido nativo protege el coste estimado de una captura durante el screener
(163 unidades por defecto). El contexto y las velas de descubrimiento esperan presupuesto antes
del plazo HTTP, con espera previa de etapa acotada a 180s. Se libera el margen al
terminar selección. El scanner reserva capacidad de revisión y REST antes de
construir datos caducables; no cambia frescura, deadlines, ranking ni interfaces
JSON. Los escenarios grandes pueden necesitar varias ventanas de 60s para
conservar métricas completas. Ver [Protección REST](REST_PROTECTION.md).
