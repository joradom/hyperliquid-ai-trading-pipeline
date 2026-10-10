# Scanner

## Qué es

El coordinador de la captura y del filtro determinista para uno o varios mercados.
Une Market State, Pre-analysis y Decision Snapshot en memoria.

## Para qué sirve

Busca candidatos para revisión sin invocar el modelo. Mantiene independientes
los resultados y errores de cada símbolo y entrega los resultados en el orden
solicitado.

## Entrada

Una lista de símbolos de perpetuos, por ejemplo HYPE, ETH y SOL.
`scan_symbols` valida y normaliza la lista antes de consultar la red.

## Salida

Un informe con schema 1.1 y filas `SKIP`/`WATCH`/`ANALYZE`.
Solo `ANALYZE` con generación correcta contiene `decision_snapshot`.
La CLI ofrece resumen legible o un objeto JSON con `--json`; no guarda
instantáneas ni modifica estado financiero.

## Referencia técnica

Los ejemplos se ejecutan desde `python/`. Fuente:
[`scanner.py`](../../python/src/scanner.py). Consulta
[Market State](MARKET_STATE.md), [Pre-analysis](PRE_ANALYSIS.md) y
[Decision Snapshot](DECISION_SNAPSHOT.md).

## Scanner v1 (en directo, de solo lectura)

Desde `python/`:

```bash
python3 -B -m src.scanner HYPE ETH SOL
python3 -B -m src.scanner HYPE ETH SOL --json
python3 -B -m src.scanner HYPE ETH SOL --profile experimental --json
```

`--config` y `--profile` son opcionales. Por defecto conserva volumen mínimo
de 1 millón USD y spread máximo de 10 bps. El perfil experimental usa
10 millones USD y 5 bps. El contrato del scanner permanece en 1.1; la política
efectiva aparece en cada preanálisis. Véase [Configuración](RESEARCH_CONFIG.md).

`src.scanner` orquesta las capas existentes en memoria:
`market_state -> pre_analysis -> decision_snapshot (ANALYZE only)`.
Captura hasta tres símbolos concurrentemente con un grupo acotado de hilos.
Cada trabajador filtra su propio símbolo inmediatamente después de capturarlo;
los resultados conservan el orden solicitado aunque las capturas terminen en
otro orden. Para `ANALYZE`, llama inmediatamente a
`build_decision_snapshot(market, pre_analysis=analysis, as_of_ms=as_of_ms)`
usando exactamente los mismos objetos de mercado y scanner en memoria y el
mismo reloj de evaluación. Esto ocurre en el trabajador antes de esperar a
cualquier otro símbolo, no después de la salida del lote ni de una entrega
humana. No se recarga ni se vuelve a capturar ningún mercado. Las llamadas HTTP
dentro de cada captura de Market State siguen siendo secuenciales y cada captura
usa su propia instancia de cliente. El número de velas por defecto, los endpoints
y los cálculos siguen siendo los de las capas existentes; solo volumen y spread
se seleccionan mediante el perfil, sin mutar la política global.
`pre_analysis.py` sigue funcionando sin red. No hay escrituras de instantáneas,
solicitudes de cuentas/wallets, comandos de negociación, llamadas a modelos,
reintentos, WebSockets ni nuevas fuentes. El tráfico HTTP usa solo las llamadas
de mercado existentes de la Info API de solo lectura. Decision Snapshot v1
conserva su transformación existente sin red; su comprobación de procedencia
recalcula el preanálisis en la misma marca temporal para verificar el resultado
proporcionado, sin otra llamada de red.

Los símbolos se validan antes de cualquier captura, se convierten a mayúsculas
y se deduplican según su primera aparición. Se aceptan letras ASCII y dígitos;
el cliente existente determina si existe el mercado perpetuo. Cada resultado
se evalúa en su propio `pre_analysis.as_of_utc`; el lote no es una observación
atómica del mercado. Los lotes grandes o capturas lentas pueden hacer que las
primeras observaciones sean antiguas al emitir la salida. Cualquier etapa
posterior en directo debe reevaluar la actualidad con su propio reloj.

La salida de texto muestra la etiqueta SKIP/WATCH/ANALYZE de cada símbolo,
los códigos de motivo originales, la calidad de origen, las duraciones de
captura/preanálisis/generación de decisión y si se preparó una instantánea de
decisión. Los bloqueos de SKIP incluyen alcance y detalle, de modo que pueda
distinguirse baja liquidez de datos malos/obsoletos. Una línea final informa de
la duración real total medida con un temporizador monotónico.

`--json` emite un único objeto compacto, sin texto alrededor:

| Campo | Significado |
|---|---|
| `kind`, `schema_version`, `read_only` | `scanner`, `1.1`, `true`; integración aditiva de decision_snapshot en scanner v1 |
| `status` | `ok`, `partial_failure` o `failed` |
| `started_at_utc`, `completed_at_utc` | Marcas temporales locales del reloj real para el lote |
| `total_seconds` | Tiempo real transcurrido de captura/preanálisis/generación de decisión y orquestación, excluido el formato de stdout |
| `capture_seconds_total`, `pre_analysis_seconds_total` | Suma de duraciones por símbolo de cada etapa, incluidas las fallidas; las capturas concurrentes se solapan, por lo que las sumas pueden superar el tiempo real total |
| `decision_snapshot_seconds_total` | Suma de duraciones de generación inmediata de decision_snapshot, incluidos los intentos fallidos |
| `symbols`, `counts` | Símbolos normalizados y recuentos por clasificación |
| `results` | Registros ordenados con símbolo, estado, clasificación, motivos, marca temporal/calidad de mercado, `pre_analysis` completo sin modificar, `decision_snapshot`, tiempos por etapa y error opcional |

Las filas correctas tienen `status=ok`, `error=null` y el resultado completo
del scanner, incluidas comprobaciones, características, reglas, política intacta,
checksum y metadatos de calidad/actualidad. El informe no conserva instantáneas
completas de mercado, arrays de velas ni niveles L2. El resultado del scanner
puro sigue siendo determinista; los tiempos del lote en directo y las
observaciones recién recopiladas varían de forma natural.

`results[].decision_snapshot` contiene el contrato v1 existente solo para
`ANALYZE` cuando la generación termina correctamente; siempre es `null` para
`SKIP`/`WATCH` y fallos de captura/scanner/generación.
`timing.decision_snapshot_seconds` es cero cuando no se intentó la generación.
No hay excepción de benchmark para WATCH en esta ruta en directo.
`quality=degraded` debido únicamente a las dos marcas temporales de origen
ausentes esperadas sigue siendo una advertencia cuando se superan las
comprobaciones originales de usabilidad. No se relaja ningún umbral de
calidad/actualidad ni clasificación. Una fuente obsoleta sigue bloqueada.

La generación comparte la marca temporal de evaluación del preanálisis y conserva
todas las antigüedades medidas y el `valid_until_utc` original; no reinicia marcas
temporales ni amplía la validez. La entrega inmediata en memoria elimina retrasos
de disco/humanos, pero un lote lento o un consumidor posterior aún pueden superar
la validez. Los futuros consumidores deben comprobar ese plazo con su hora actual
antes de usar el contrato. El scanner solo prepara datos y no invoca Codex ni
autoriza la ejecución.

Los fallos de captura o normalización se aíslan por símbolo y se devuelven como
`SKIP` con `capture_failed` o `pre_analysis_failed`; el siguiente símbolo
sigue procesándose. Estas filas tienen `status=error`, `pre_analysis=null` y
etapa/tipo/mensaje original de error; no se envía ningún mercado vacío o
fabricado al scanner sin red. Los fallos de origen ya tratados por Market State
conservan su instantánea degradada normal y los bloqueos del scanner.

Si falla la generación de decisión, se conservan la clasificación/motivos
originales de `ANALYZE` y el preanálisis, pero la fila tiene `status=error`,
`decision_snapshot=null` y `error.stage=decision_snapshot`. El lote continúa;
esa fila no debe enviarse a un modelo. Esto distingue un fallo de preparación
de un cambio en la clasificación de filtrado del mercado.

Salida 0 significa que se procesaron todos los símbolos, aunque todas las
etiquetas sean `SKIP`. Salida 1 significa al menos un fallo de orquestación;
la salida JSON sigue disponible para todo el lote. Una entrada de CLI inválida
termina con 2 antes de acceder a la red. El informe distingue un filtrado sin
oportunidad de una recopilación fallida.

El punto de entrada Python es `scan_symbols(["HYPE", "ETH", "SOL"])`.
Se proporciona inyección del constructor y del reloj/temporizador para pruebas
sin red. La suite unitaria completa usa respuestas simuladas de mercado y
verifica que las clasificaciones coincidan con el scanner existente, el
aislamiento por símbolo, la validación de símbolos, la inmutabilidad, los tiempos,
la salida de texto/JSON, los códigos de salida y la ausencia de persistencia:
`python3 -B -m unittest discover -s tests -v`.

Las pruebas de concurrencia usan barreras/eventos en lugar de suposiciones de
tiempo para verificar tres capturas solapadas, el límite de tres trabajadores en
lotes mayores, resultados ordenados pese a terminar fuera de orden, preanálisis
inmediato en cada trabajador y aislamiento de fallos concurrentes. Las pruebas
de integración verifican identidad exacta de objetos, checksums de
origen/preanálisis, evaluación en el mismo momento, advertencias de degradación
intactas, supresión de SKIP/WATCH, ausencia de reinicio de antigüedad, generación
inmediata antes de que termine un primer activo lento, fallos de generación
aislados y duraciones por etapa. Las opciones de CLI y los códigos de salida
permanecen intactos; el schema JSON 1.1 añade campos/tiempos de decision_snapshot.
Las reglas de Pre-analysis y Decision Snapshot v1 permanecen intactas.
