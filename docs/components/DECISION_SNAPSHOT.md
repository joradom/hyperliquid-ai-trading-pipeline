# Decision Snapshot

## Qué es

El resumen verificable que recibe el modelo para revisar un mercado. Incluye
datos, reglas, unidades, limitaciones y el plazo durante el que pueden utilizarse.

## Para qué sirve

Evita que el modelo tenga que explorar el repositorio o reconstruir cálculos.
Fija un contrato estable de entrada, no una recomendación de operación.

## Entrada

Market State v1.1 y hora de evaluación; opcionalmente un `pre_analysis`
coincidente. Funciona sin consultas de red.

## Salida

Un contrato con schema y derivación 1.0, checksums enlazados,
`eligible_for_model_review`, `valid_until_utc` y
`account_constraints=null`. La CLI emite un array JSON compacto.

## Referencia técnica

**Versión: v1.0.** Los ejemplos se ejecutan desde `python/`. Fuente:
[`decision_snapshot.py`](../../python/src/decision_snapshot.py).
La especificación completa se conserva a continuación.

`src.decision_snapshot` define el contrato de solo lectura, sin acceso a la red,
entre el `scanner` del proyecto y un futuro consumidor basado en un modelo. Formaliza
los datos del benchmark de HYPE. Python genera todos los datos y resúmenes
derivados; el consumidor solo necesita el JSON emitido, sin inspeccionar el
repositorio ni descubrir APIs.

## Uso

Ejecutar desde `python/`, con archivos JSON existentes de Market State v1.1:

```bash
python3 -B -m src.decision_snapshot HYPE.json
python3 -B -m src.decision_snapshot HYPE.json ETH.json SOL.json
python3 -B -m src.decision_snapshot HYPE.json --as-of 2026-10-02T10:34:16.716Z
python3 -B -m src.decision_snapshot HYPE.json --as-of 2026-10-02T10:34:16.716Z --allow-watch-for-benchmark
python3 -B -m src.decision_snapshot --schema
```

Los comandos de instantáneas emiten un **array** JSON compacto, en el orden de
entrada, seguido de un salto de línea. No se guardan archivos, no se obtienen
datos ni se invoca un modelo o comando de negociación. `--schema` emite el
JSON Schema completo (Draft 2020-12) como objeto y no lee archivos de
instantáneas. No se requieren dependencias de terceros.

La hora de evaluación por defecto es la UTC actual, compartida por todas las
entradas del lote. `--as-of` exige una marca temporal ISO con zona horaria y
permite reproducir evaluaciones históricas. Esa reproducción nunca convierte una
observación guardada en datos actuales según el reloj real. Las entradas
malformadas o corruptas producen un error en stderr y salida 1, sin JSON parcial.
Las entradas válidas pero obsoletas o bloqueadas por el filtrado siguen produciendo
un contrato con `SKIP` y `eligible_for_model_review=false`.

La API pura hace explícito el reloj de evaluación:

```python
from src.decision_snapshot import (
    build_decision_snapshot,
    get_decision_snapshot_schema,
    serialize_decision_snapshot,
    validate_decision_snapshot,
)

# market_state is a verified document already obtained by the project.
snapshot = build_decision_snapshot(market_state, as_of_ms=evaluation_time_ms)
assert validate_decision_snapshot(snapshot) == []
payload = serialize_decision_snapshot(snapshot)
size_bytes = len(payload.encode("utf-8"))
```

El argumento opcional `pre_analysis=` acepta un resultado existente del `scanner`
solo si su checksum es válido y todo su contenido coincide con un resultado
recalculado a partir de este documento de mercado en este momento de evaluación.
Las discrepancias de símbolo, origen, reloj, reglas, clasificación o política
provocan rechazo. Si se omite, se recalcula el `scanner` localmente.
`allow_watch_for_benchmark` es false por defecto y debe ser booleano.

## Schema estable

Todas las claves enumeradas están siempre presentes. Los objetos fijos rechazan
propiedades adicionales; los arrays de observaciones/umbrales de reglas conservan
sus valores específicos del `scanner`. El schema formal se expone mediante
`get_decision_snapshot_schema()` y `--schema`.
`validate_decision_snapshot()` comprueba las palabras clave del schema utilizadas
localmente, los números finitos, el checksum y la coherencia del enrutamiento y
el plazo. No es un motor genérico de JSON Schema; un validador estándar externo
también puede consumir el schema exportado. El contrato no incluye una decisión
de negociación generada.

| Bloque | Contenido |
|---|---|
| Identidad | `kind=decision_snapshot`, `schema_version=1.0`, `derivation_version=1.0`, symbol, `as_of_utc`, `market_timestamp_utc`, `valid_until_utc`, `read_only=true` |
| Procedencia | `source_market_checksum`, `source_pre_analysis_checksum`, plataforma/API/endpoint y tipos originales de solicitud |
| `pre_analysis` | Versiones de schema/reglas, clasificación original SKIP/WATCH/ANALYZE, motivos, **política completa sin cambios**, comprobaciones y umbrales observados |
| `benchmark_override` | Si se solicitó y aplicó el envío de WATCH; `rules_and_thresholds_changed=false` |
| `quality` | Estado/incidencias/errores originales de calidad, bloqueos/advertencias del scanner y antigüedades reevaluadas por fuente; los valores numéricos ausentes son advertencias explícitas |
| `price` | mark, oracle, mid, previous_day |
| `market_context` | Apalancamiento máximo, decimales de tamaño, volumen nocional diario, premium, precios de impacto |
| `timeframes` | Exactamente 5m/15m/1h/4h: número de velas, último cierre, tendencia derivada de EMA, última vela cerrada, todos los valores existentes de indicadores, máximo/mínimo de las últimas 20 velas y número de muestras |
| `derivatives` | OI y funding actuales, número/mínimo/máximo/media de las tasas de funding devueltas para 72h |
| `orderbook` | Tiempo de origen, bid/ask/punto medio, diferenciales incluidos bps, profundidad total/número de niveles, valor/fórmula de desequilibrio; sin niveles |
| `account_constraints` | Siempre `null` en v1, limitada al mercado; no se consulta ni se infiere ninguna cuenta |
| `semantics` | Unidades, métodos de cálculo de indicadores, derivaciones de rango/funding/tendencia |
| `limitations` | Ausencia de microestructura temporal, cuenta/comisiones/presupuesto de riesgo, tiempos de origen desconocidos e inferencias no admitidas |
| `decision_contract` | Elegibilidad/enrutamiento al modelo, decisiones permitidas, campos de respuesta esperados, semántica de confianza, alcance temporal y `execution_authorized=false` |
| `integrity` | Metadatos SHA-256 canónicos estándar del proyecto |

Los indicadores conservan EMA8/21/50/120, distancias a las EMA, RSI14, línea/señal9/
histograma MACD, Bollinger20/2, ATR14/porcentaje, media20 de volumen y volumen
relativo20. Los valores se copian con su precisión original. Los campos numéricos
opcionales ausentes permanecen en `null` con advertencias
`missing_numeric_value`; nunca se sustituyen por cero. Las entradas obligatorias
del `scanner` siguen sujetas a sus comprobaciones intactas.

Los rangos son el máximo de los máximos y el mínimo de los mínimos de hasta las
últimas 20 velas normalizadas cerradas. Son envolventes históricas, no soportes o
resistencias validados. Se omiten las series completas, por lo que no se aportan
pendientes temporales ni confirmaciones de ruptura. La última vela y su marca de
cierre deben coincidir con la serie de origen conservada; las velas del rango
deben tener OHLCV coherente, valores finitos y estar cerradas al evaluar.
Las discrepancias de recuento, schemas no admitidos, tasas malformadas y errores
de checksum se rechazan en lugar de repararse silenciosamente. Las tasas de
funding pueden ser cadenas numéricas de la API; mínimo/máximo/media del resumen
usan valores float finitos y una media aritmética sin ponderar. Un historial
devuelto vacío tiene recuento cero y estadísticas null. Solicitar una ventana de
72h no certifica que el historial devuelto esté completo.

Las tasas conservan la convención fraccional de origen, sin anualizar. OI permanece
en la cantidad nativa de la fuente y no debe interpretarse como nocional en USD.
Precios y tamaños conservan las unidades de origen; el volumen nocional diario
está en USD. `_pct` expresa porcentaje; 1 bps es 0.01%. La actualidad guardada
del funding describe el último evento del historial, no una marca temporal del
mercado para la tasa actual del contexto.

## Enrutamiento y validez

El enrutamiento normal permite revisión por el modelo solo para `ANALYZE` sin
bloqueos. Los contratos `WATCH` y `SKIP` siguen disponibles para inspección,
pero no son elegibles. Una excepción explícita de benchmark permite **solo
WATCH** sin bloqueos; nunca reescribe la clasificación, los umbrales ni las
comprobaciones, y no puede eludir `SKIP`. La elegibilidad de salida es un
metadato de enrutamiento, no una recomendación `LONG`/`SHORT`.

`valid_until_utc` es la primera caducidad entre las comprobaciones de antigüedad
del `scanner`, calculada a partir de sus tiempos y tolerancias originales. Puede
ser anterior a `as_of_utc` si la entrada está obsoleta. Las antigüedades y la
elegibilidad se evalúan en `as_of_utc`; no constituyen una prueba permanente de
actualidad. Un futuro consumidor en directo debe comparar su hora actual con
`valid_until_utc` antes de la revisión por el modelo. Este plazo breve no implica
que los precios permanezcan intactos durante toda la revisión; la actualidad
necesaria para ejecutar queda fuera de v1. La aproximación basada en la recepción
de contexto/OI conserva sus advertencias explícitas de tiempo del mercado
desconocido.

Los campos de respuesta esperados del futuro consumidor son `decision`,
`confidence`, `thesis`, `entry_conditions`, `invalidation` y `limiting_data`.
Las etiquetas de decisión permitidas son NO_TRADE/WATCH/LONG/SHORT. Un bloqueo
de calidad/filtrado se corresponde con `NO_TRADE`; un contrato no elegible no
debe invocar el modelo. La confianza es cualitativa o explícitamente heurística,
no una probabilidad calibrada. Las condiciones de entrada que requieran nuevas
observaciones deben describirse como futuras y no observadas.
`account_constraints=null` no permite concluir tamaños de operación ni seguridad
de la cuenta.

## Integridad, determinismo y alcance

Documentos de entrada, hora de evaluación y opción de benchmark idénticos producen
un JSON compacto idéntico byte a byte. Las mediciones de tiempo se excluyen
deliberadamente. No se modifican las entradas de origen, la política del
`scanner` ni las copias exportadas del schema. Los campos permitidos impiden
que nuevos campos de origen amplíen silenciosamente el contrato del consumidor.
SHA-256 detecta cambios de contenido; no es una firma ni una prueba de origen de
la API. Los checksums de origen permiten auditorías posteriores sin exigir que
el modelo los cargue. El tamaño se mide sobre el objeto UTF-8 serializado,
incluida la integridad y excluidos el array envolvente de la CLI y el salto de
línea final.

Medido sobre la instantánea guardada de HYPE
`20261002_103413_HYPE_market_state.json`, evaluada en
`2026-10-02T10:34:16.716Z` con la excepción de benchmark para WATCH, v1 ocupa
**10,395 bytes UTF-8** (unos 10.4 kB). Supera los 8,126 bytes del benchmark
ad hoc porque el contrato estable también incorpora la política completa, las
unidades, la semántica de derivación, el plazo de validez y los requisitos de
respuesta. El tamaño varía con las representaciones numéricas y las incidencias
de calidad; no se elimina precisión mediante redondeo.

Los cambios de estructura/tipos/claves requieren un cambio de versión del schema;
los cambios de semántica de los resúmenes requieren un cambio de versión de
derivación. Las reglas del `scanner` conservan su propia versión y se incorpora
la política completa. v1 solo acepta Market State 1.1 y Pre-analysis 1.0.
No añade integración de cuentas, llamadas HTTP o a modelos, `codex exec`,
integración con mm, escrituras, WebSockets ni nuevas fuentes de datos.

Las pruebas se ejecutan con la capa existente:
`python3 -B -m unittest discover -s tests -v`. Cubren proyección, resúmenes de
rango/funding, bytes deterministas, inmutabilidad, entradas del scanner
coincidentes/discrepantes, enrutamiento/excepción, datos obsoletos/caducidad,
valores ausentes, corrupción, validación del schema, lotes compactos y
funcionamiento sin red con salida exclusiva a stdout.
