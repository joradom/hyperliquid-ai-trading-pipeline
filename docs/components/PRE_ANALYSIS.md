# Pre-analysis

## Qué es

Un filtro reproducible que examina una instantánea ya capturada. No pregunta al
modelo ni obtiene nuevos datos: aplica siempre las mismas reglas a las mismas
entradas y al mismo momento de evaluación.

## Para qué sirve

Clasifica un mercado como `SKIP`, `WATCH` o `ANALYZE` antes de dedicar
una revisión de IA. Es un filtrado de investigación, no una estrategia calibrada.

## Entrada

Market State v1.1 con checksum y una hora explícita de evaluación en la API
`build_pre_analysis`. En la CLI la hora es UTC actual salvo `--as-of`.

## Salida

Resultados compactos con schema y reglas 1.0, clasificación, motivos,
comprobaciones, bloqueos, advertencias, política y checksum. No incluye todas
las velas o niveles L2 ni cambia las entradas.

## Referencia técnica

El primer bloque incluye `cd python` y parte de la raíz del proyecto;
los demás comandos se ejecutan desde `python/`. Fuente:
[`pre_analysis.py`](../../python/src/pre_analysis.py).
La [guía de uso](../USAGE.md) explica cómo leer los estados.

## Pre-analysis v1 (scanner sin red)

`src.pre_analysis` consume **instantáneas JSON existentes de Market State
v1.1**. No recopila datos, accede a cuentas, importa el cliente HTTP ni guarda
archivos. La CLI imprime un array de resultados JSON compactos en stdout, en el
orden de entrada:

```bash
cd python
python3 -B -m src.pre_analysis ../data/snapshots/SNAPSHOT_HYPE_market_state.json
python3 -B -m src.pre_analysis HYPE.json ETH.json --as-of 2026-10-01T19:36:28Z
```

La hora de evaluación por defecto es la UTC actual. `--as-of` acepta una marca
temporal ISO con zona horaria para reproducir evaluaciones históricas. Las
instantáneas antiguas normalmente producen `SKIP` a la hora actual; los
resultados históricos no deben tratarse como señales en directo. La API Python
pura exige una hora de evaluación explícita:

```python
from src.pre_analysis import build_pre_analysis
result = build_pre_analysis(market_snapshot, as_of_ms=evaluation_time_ms)
```

La clasificación es determinista y se evalúa en este orden:

1. **SKIP** si el checksum/schema es inválido, faltan marcas temporales o son
   obsoletas/futuras, falla la validación de origen, los campos/indicadores
   requeridos no son utilizables o existe cualquier incidencia de origen salvo
   las dos incidencias esperadas `unknown_freshness` de contexto de mercado y OI.
   SKIP también se aplica cuando el volumen nocional diario está por debajo de
   1 millón USD, el diferencial supera 10 bps o el ATR de 15m queda fuera de
   0.1–5%.
2. **ANALYZE** cuando se superan todas las comprobaciones, EMA21/50/120 están
   estrictamente ordenadas en la misma dirección en 15m y 1h, 4h va en esa
   dirección o es mixto y el volumen relativo es al menos 1.2 en 5m o 15m.
3. **WATCH** cuando se superan las comprobaciones pero falta confluencia de
   tendencia o volumen relativo.

Una tendencia ascendente significa EMA21 > EMA50 > EMA120; descendente significa
el orden inverso; cualquier otra ordenación es mixta. Los límites de los umbrales
son inclusivos. Los cuatro marcos temporales requieren al menos 120 velas, EMA
finitas y positivas y ATR y volumen relativo no negativos. Los datos de volumen
cero con volumen relativo null no son utilizables. Son heurísticas iniciales de
filtrado, no reglas calibradas de rentabilidad o negociación.

La actualidad se recalcula **en el momento de evaluación**, ignorando las
etiquetas `fresh` guardadas: el tiempo de origen de L2 debe estar dentro de
15 segundos; el de funding, dentro de 90 minutos; y las últimas velas cerradas,
dentro de dos veces su intervalo. El inicio de la instantánea y la recepción
local de contexto/OI deben estar dentro de 30 segundos. Se permite hasta
5 segundos de desfase de reloj hacia el futuro. Contexto/OI conservan
advertencias explícitas: la recepción local reciente es solo una aproximación
acotada, no evidencia de una marca temporal del mercado. Las demás incidencias
de origen bloquean conservadoramente el filtrado, incluidas las ventanas de
velas incompletas.

Cada resultado contiene `classification`, códigos de motivo,
bloqueos/advertencias, antigüedades recalculadas, entradas y umbrales observados
de las reglas, características compactas, `rules_version`, la política completa,
el checksum de origen original y su propio SHA-256. No se copian series completas
de velas ni niveles del libro. Las instantáneas de origen y las constantes de
política nunca se modifican. Los resultados `ANALYZE` se envían al modelo para
una revisión profunda; `WATCH` puede reevaluarse con la siguiente instantánea
recopilada independientemente. `SKIP` por sí solo no distingue una mala
oportunidad de datos malos: consultar `quality.blockers` y `checks`. Este
módulo no toma decisiones `LONG`/`SHORT`, calcula tamaños de operaciones ni
cambia el flujo existente de recopilación/ejecución.

Las pruebas cubren las tres clasificaciones, ambas direcciones de tendencia,
límites, marcas temporales obsoletas/futuras/ausentes, manipulación del checksum,
datos malformados, actualidad desconocida esperada, lotes de CLI sin red e
inmutabilidad de entradas. Ejecutar toda la suite sin escribir bytecode mediante
`python3 -B -m unittest discover -s tests -v`.

