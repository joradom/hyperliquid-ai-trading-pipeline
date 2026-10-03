# Fast Revalidation

## Qué es

Una comprobación breve y determinista de compatibilidad tras una respuesta
`LONG`/`SHORT` que ha caducado durante la revisión. No vuelve a preguntar
al modelo.

## Para qué sirve

Distingue una propuesta aún compatible con condiciones evaluables de otra
invalidada o imposible de comprobar. `VALID` nunca equivale a permiso para
operar.

## Entrada

La fila original con snapshot, preanálisis y respuesta válida v2, ligados por
checksums y procedencia. Se consultan únicamente `l2Book` y
`metaAndAssetCtxs`.

## Salida

Documento con versión 1.0, `VALID`, `INVALID` o `INDETERMINATE`,
observaciones, reglas evaluadas, motivos, advertencias, tiempos y checksum.
Solo `VALID` incluye un nuevo `valid_until_utc`.

## Referencia técnica

**Versión: v1.0.** Los ejemplos se ejecutan desde `python/`. Fuente:
[`fast_revalidation.py`](../../python/src/fast_revalidation.py).
La especificación completa se conserva a continuación.

Evaluación determinista de compatibilidad, de solo lectura, tras caducar una
respuesta de análisis v2 con `LONG`/`SHORT`. No integra modelos, cuentas, wallets,
órdenes ni ejecución. `VALID` nunca autoriza la ejecución. Las reglas originales
de filtrado permanecen intactas.

## Integración

Analysis Pipeline v1.2 llama a `fast_revalidate(row)` dentro del trabajador de
revisión correspondiente, inmediatamente después de que termine una respuesta
`LONG`/`SHORT` válida con `requires_revalidation=true`. No espera a otro activo.
`WATCH`/`NO_TRADE` y `LONG`/`SHORT` sin caducar mantienen
`fast_revalidation=null` y `execution_authorized=false`.

```bash
python3 -B -m src.analysis_pipeline HYPE ETH SOL
python3 -B -m src.analysis_pipeline HYPE ETH SOL --json --revalidation-deadline-seconds 3
python3 -B -m unittest discover -s tests -q
```

La API Python expone `revalidation_deadline_seconds` (3.0 por defecto), un valor
positivo y finito que se comprueba antes de acceder al `scanner` o a la red. No hay
reintentos. La fila añade un documento `fast_revalidation` completo con checksum
y `timing.fast_revalidation_seconds`. La salida para personas muestra el estado
y los códigos de motivo. La respuesta original, `requires_revalidation`,
`expired_at_completion` y `snapshot.valid_until_utc` permanecen intactos. El tiempo
individual de Codex excluye la revalidación; el tiempo real de la fase conjunta
de revisión la incluye.

## Dos fuentes concurrentes y una espera acotada

Se realizan exactamente dos solicitudes existentes de la Info API oficial:

- `l2Book(symbol, nSigFigs=5)`: bid/ask, punto medio, diferencial absoluto y en
  bps, profundidad de ambos lados, número de niveles capturados y la fórmula
  existente de desequilibrio agregado.
- `metaAndAssetCtxs`: localización del símbolo exacto; mark, punto medio del
  contexto, oracle, premium, volumen nocional diario, funding/OI actuales y
  precios de impacto.

No se consultan velas, `fundingHistory`, `trade_state`, datos de cuenta ni nuevos
endpoints. No se emiten apalancamiento, tamaños de órdenes ni metadatos de cuenta.
Cada pareja tiene almacenamiento independiente de resultados; no hay caché entre
símbolos ni una barrera que retrase la entrega hasta completar el lote.

El plazo total medido con reloj monotónico comienza antes de validar el original
e incluye captura y evaluación. El timeout del socket utiliza el presupuesto
restante. Como esto por sí solo no acota DNS ni respuestas que llegan lentamente,
dos trabajadores daemon entregan los resultados mediante una cola por llamada.
La función nunca espera a que terminen más allá de su plazo. Los resultados
tardíos no pueden modificar el documento devuelto. Un semáforo global no
bloqueante permite como máximo seis trabajadores de captura (tres revisiones por
dos fuentes); los huecos retenidos por llamadas bloqueadas hacen que las capturas
posteriores sean `INDETERMINATE` en lugar de aumentar el número de trabajadores
o esperar. Los huecos solo se liberan cuando termina cada trabajador. Los hilos
daemon no pueden terminarse por la fuerza y pueden mantener lecturas de Info sin
firma hasta que responda el transporte, pero no impiden que termine el proceso
ni prolongan la espera de la función. El trabajo local final de JSON/checksum
añade una pequeña sobrecarga administrativa, no otra espera de red.

## Comprobaciones antes de volver a capturar

Se validan el schema/checksum de la instantánea, el checksum de `pre_analysis` y
su proyección completa en la instantánea, el símbolo, el enlace
`source_market_checksum`/`source_checksum`, `source_pre_analysis_checksum` y la
identidad de `as_of`. El `scanner` no conserva el cuerpo original de Market State:
su hash es una referencia enlazada, no un cuerpo verificado independientemente
aquí. SHA-256 protege la coherencia del documento, no la autenticidad del mercado.

Se exige `ANALYZE` original, `eligible_for_model_review=true`, ausencia de bloqueos
originales, ninguna excepción de benchmark aplicada y una revisión direccional
de solo lectura completada. Se validan v2 y la procedencia de los umbrales contra
la instantánea exacta; se exige `requires_revalidation=true` y tiempos originales
válidos de envío y finalización. La caducidad original en este momento es esperada
y nunca se amplía. Los errores de estas comprobaciones devuelven `INDETERMINATE`
antes de cualquier solicitud.

## Condiciones básicas y dependencias

Los valores requeridos de bid/ask/punto medio, mark, punto medio del contexto y
oracle son precios positivos y finitos. Los lados de L2 no están vacíos y están
ordenados; todos los precios de nivel son positivos y finitos, los tamaños son
finitos y no negativos, la profundidad total es estrictamente positiva y finita
y el libro no está cruzado. Los datos malformados o cruzados producen
`INDETERMINATE`, no una invalidación del mercado.

Todos los límites proceden de la política `pre_analysis` existente de la
instantánea validada:

- Antigüedad de L2 <= 15,000 ms, usando el tiempo de origen del mercado.
- Antigüedad de contexto/OI <= 30,000 ms, usando la recepción local de la
  respuesta; la actualidad de los datos del mercado sigue siendo desconocida y
  se advierte explícitamente.
- Desfase hacia el futuro <= 5,000 ms, conservando la comprobación de antigüedad
  con signo.
- Diferencial <= `max_spread_bps` original (10).
- Volumen diario >= mínimo original (1,000,000 USD).

Los incumplimientos de diferencial o liquidez con datos actuales son `INVALID`.
OI/funding/desequilibrio no tienen comprobaciones implícitas de dirección o
magnitud. Los campos opcionales ausentes generan advertencias; la ausencia de
valores usados por reglas explícitas obligatorias impide evaluarlas. Las reglas
de diagnóstico no bloquean.

El filtrado original dependía de los cuatro marcos de velas cerradas. Cada uno
debe seguir dentro de una antigüedad de 2 x intervalo y estrictamente antes de
`latest_candle.close_time_ms + interval`. Alcanzar ese próximo cierre produce
`NEW_CLOSED_CANDLE_UNVERIFIED`; no se consultan velas ni se recalcula después.
Las reglas que usan ese marco/ATR pasan a ser no evaluables. Esto puede provocar
una abstención conservadora cerca de los límites aunque los precios actuales
apenas se muevan.

Los precios/OI originales usados como anclas o bases fijas siguen siendo hechos
del momento de la observación, sin considerar que su captura antigua se haya
actualizado. Las referencias históricas explícitas y obligatorias de funding
reutilizan su tiempo original de origen y la tolerancia de 5,400,000 ms; no hay
una comprobación del historial de funding si ninguna regla obligatoria lo usa.

## Semántica del conjunto cerrado de predicados

Solo se admiten los once predicados de respuesta v2:

- `price_above/below`: > / < estrictos; `price_in_range`: límites inferior y
  superior inclusivos.
- `spread_lte`, `funding_lte/gte`, `imbalance_lte/gte`: comparaciones directas.
- `oi_change_lte/gte`: cambio fraccional (actual-original)/original, con OI
  original positivo. El evaluador puro los implementa. V2 sigue rechazando
  umbrales ejecutables de OI sin una política autorizada de cambio de OI; esas
  propuestas son `INDETERMINATE` antes de la captura. No se inventa ni se añade
  ninguna política a v2.
- `max_price_displacement_atr`: abs(actual-original del mismo campo)/ATR original
  <= umbral explícito verificado del contrato. No hay un corredor o coeficiente
  general.

Los predicados de `entry`/`quality` son requisitos que deben ser verdaderos. Los
predicados de `invalidation` son desencadenantes que deben ser falsos. Todas las
invalidaciones son obligatorias en v2. La tesis narrativa, `entry_conditions`,
`invalidation` y las descripciones nunca se interpretan como lógica ni se analizan
para extraer números. La declaración `complete` del modelo sigue siendo una
afirmación de cobertura, no un hecho que Python pueda inferir de su texto.

## Resultados e integridad

- `VALID`: todas las comprobaciones básicas/obligatorias pasan, `complete=true`,
  no hay condiciones sin resolver, las dependencias requeridas son utilizables
  y se cumple el plazo total.
- `INVALID`: una condición básica/obligatoria incumple de forma concluyente con
  evidencia actual y utilizable.
- `INDETERMINATE`: faltan datos requeridos o están malformados/obsoletos, la
  cobertura es incompleta, hay un cierre de vela sin verificar, un error de
  procedencia/contrato o un fallo de transporte/plazo.

Los fallos de transporte/plazo fuerzan `INDETERMINATE` aunque otra fuente sugiera
un incumplimiento. En los demás casos, los incumplimientos concluyentes con datos
actuales tienen prioridad sobre comprobaciones desconocidas no relacionadas,
conservando todos los motivos. Un fallo de diagnóstico por sí solo no bloquea.
La revalidación fallida conserva la revisión completada del modelo y aísla los
símbolos.

El documento incluye versiones de schema/reglas, símbolo/decisión, hashes de
origen de instantánea/preanálisis/respuesta/contrato, `original_valid_until_utc`,
tiempos/duraciones de las fuentes capturadas, observaciones normalizadas,
antigüedades/bases de dependencias, cobertura, verdad/polaridad/resultados de
reglas, motivos estructurados, advertencias, evaluación/finalización en UTC y
mediciones monotónicas. Los motivos contienen `code`, `scope`, `effect` y
`rule_id`. El SHA-256 canónico del proyecto cubre todo excepto el campo `integrity`.

Solo `VALID` tiene un nuevo `valid_until_utc`: el mínimo de los plazos de las
nuevas fuentes, las dependencias obligatorias reutilizadas y el instante anterior
al siguiente cierre de vela relevante. Se comprueba antes de devolver el
resultado. Los consumidores deben comprobar otra vez el checksum, la vinculación
con la propuesta y este plazo al usarlo; esperar a otro símbolo puede hacerlo
caducar. No hay un consumidor operativo implementado y
`execution_authorized=false` siempre.

Dos observaciones no pueden demostrar que un nivel nunca se cruzó durante el
razonamiento, un desequilibrio persistente, operaciones/CVD, liquidez para un tamaño
de orden ni calidad de ejecución futura. Estas siguen siendo limitaciones
explícitas de cobertura del contrato.
