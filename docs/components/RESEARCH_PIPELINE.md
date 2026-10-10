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

Las observaciones del screener no sustituyen el Market State: tiene contexto
y unas 48 horas de velas horarias, insuficientes para las cuatro temporalidades,
L2 y funding del scanner. Persisten consultas parcialmente solapadas entre
descubrimiento y captura; se elimina la duplicación de captura completa del
scanner. La revalidación REST de una propuesta direccional caducada mantiene
su comportamiento existente.

## Estados y errores

- `ok`: selección y análisis válidos; también selección vacía normal, con
  `outcome=no_candidates`, scanner/analysis null y sin modelos.
- `partial_failure`: datos de selección degradados o errores parciales del
  pipeline, conservando los resultados utilizables. Los errores de velas por
  activo no se convierten por sí solos en fallo total, aunque afecten a todos
  los candidatos, si captura y análisis pueden continuar.
- `failed`: excepción/contrato inválido de etapa, descubrimiento fallido,
  ningún símbolo seleccionado compatible, o pipeline con fallo total.

El screener original puede devolver `status=ok` aunque alguna fila tenga
`error` o delta ausente. Ese informe se conserva y el estado externo refleja
la degradación. `null` y campos ausentes nunca se consideran métricas válidas.
Los números NaN/Infinity del descubrimiento se convierten en null únicamente
en la copia del informe, con una incidencia que identifica su ruta; el objeto
original no se modifica. Esto permite JSON estricto sin inventar cero.
Los errores previos al recorte final del ranking que no figuren en el informe
del screener no están disponibles para el orquestador.

Salida CLI 0 para `ok`; 1 para `partial_failure` o `failed`, con informe JSON
completo igualmente disponible. Argumentos/configuración inválidos producen
salida 2 antes de consultar mercado. Un agente puede consumir el informe
parcial aunque el comando termine con código 1.

Tests de integración: `test_research_pipeline.py`; configuración:
`test_research_config.py`. Utilizan mercado y modelos simulados, sin red real.
