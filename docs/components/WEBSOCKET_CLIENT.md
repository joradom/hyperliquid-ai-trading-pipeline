# WebSocket público — FASE 1

Cliente independiente de Hyperliquid mainnet/testnet, READ-ONLY. Implementación:
[`ws_client.py`](../../python/src/ws_client.py) y
[`stream_types.py`](../../python/src/stream_types.py).
No está conectado a Market State, scanner, análisis ni revalidación.
`execution_authorized=false` permanece en el informe de diagnóstico.

## Arquitectura y dependencia

Una instancia abre una sola conexión a `wss://api.hyperliquid.xyz/ws` para todos
sus símbolos. Cada símbolo tiene dos suscripciones: `trades` y `l2Book` con
`nSigFigs=5`. Se conserva la grafía del símbolo, incluida su capitalización;
no se consulta un catálogo ni ninguna cuenta para normalizarlo.

```text
Socket público compartido
  → parse_message: JSON/esquema/valores estrictos
  → confirmaciones / ping-pong
  → trades: identidad → cache acotada → cola de eventos normalizados
  → l2Book: snapshot validado → último libro e historial acotado
  → estadísticas y diagnóstico A/B contra BBO anterior
```

El transporte opcional utiliza `websockets.legacy.client.connect`; se probó
con Python 3.12.3 y websockets 10.4 ya instalado. No se importan las capas
existentes, ni se añaden clientes REST, SQLite, agregadores Delta/CVD o DOM.
La importación de websockets es diferida; los tests inyectan transportes y no
requieren la dependencia de red. El archivo de dependencias opcionales es
[`requirements-ws.txt`](../../python/requirements-ws.txt).

Instalación opcional dentro de un entorno del proyecto, si falta la biblioteca:

```bash
cd python
python3 -m pip install -r requirements-ws.txt
```

El rango declarado es >=10.4,<16; solo 10.4 quedó certificado con la captura
de esta fase. No se actualizó el entorno existente para esta intervención.

## Comandos

Desde `python/`:

```bash
# Diagnóstico público de 60 s después de la primera conexión
python3 -B -m src.ws_client ETH HYPE --seconds 60

# Mismas estadísticas en JSON; sin paquetes completos, users ni hash
python3 -B -m src.ws_client ETH HYPE --seconds 60 --json

# Transporte persistente; Ctrl+C solicita cancelación y cierre
python3 -B -m src.ws_client ETH HYPE --continuous

# Suite offline completa
python3 -B -m unittest discover -s tests
```

La ventana de observación comienza tras la primera conexión exitosa. Reconectar
no reinicia esos 60 s. Antes de esa primera conexión hay un presupuesto de
arranque de 2×open_timeout (10 s por defecto). La CLI no escribe archivos.
Su consumidor drena la cola de trades sin agregarlos; el resumen conserva
contadores de recepción/consumo/pérdida, no un tape completo.

La API `await PublicWSClient([...]).run(duration_seconds)` permite duración
finita o None. `stop()` despierta recv y backoff; cancelación de tarea cierra
el transporte en finally. Las instancias son de un solo uso y rechazan una
segunda ejecución concurrente. `pop_trade()` entrega eventos inmutables con
timestamp de recepción, conexión/continuidad, pre_connection y out_of_order.
El libro está disponible como snapshot inmutable en `books`, sin cálculo OBI.
En FASE 3 la normalización delega en el adaptador compatible de
[Order Book](ORDERBOOK.md), y el diagnóstico A/B reutiliza su función BBO.
`book_epochs` y `book_conflicts` registran procedencia/conflicto por activo;
Live Store los utiliza junto con recepción para su vista adicional `read_l2`.
Los campos del resumen WS y las métricas firmadas permanecen iguales.
Acceso y consumo pertenecen al mismo event loop; no es una API thread-safe.

## Protocolo y validación

Se verificaron los formatos contra documentación oficial y código del SDK:

- `{"method":"subscribe","subscription":{"type":"trades","coin":"ETH"}}`.
- Libro: mismo mensaje con type=l2Book y nSigFigs=5.
- Ack: `channel=subscriptionResponse`, data con method y subscription original.
- `channel=trades`, data array; identidad `(network, time, coin, tid)`.
- `channel=l2Book`, data con coin, time, levels=[bids,asks]; son snapshots,
  no cambios incrementales.
- Heartbeat: `method=ping`, respuesta `channel=pong`.
- Se admite el aviso literal opcional `Websocket connection established.`
  reconocido por el SDK, sin tratar otros textos como datos válidos.

[Suscripciones y tipos oficiales](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/websocket/subscriptions),
[heartbeat oficial](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/websocket/timeouts-and-heartbeats),
[SDK oficial](https://github.com/hyperliquid-dex/hyperliquid-python-sdk/blob/master/hyperliquid/websocket_manager.py).

JSON debe tener claves únicas y campos conocidos; no NaN, valores booleanos
como números, timestamps futuros >5 s, lados distintos de A/B o símbolos no
suscritos. Precio/tamaño son cadenas decimales positivas finitas, acotadas,
normalizadas con Decimal; tid es un entero de 50 bits. Se valida el batch entero
antes de procesarlo. Un trade sin tid se rechaza, sin inventar otra identidad.
Hash/users se validan solo si están presentes y se descartan al normalizar.

L2 exige ambos lados, 1–20 niveles por lado, precios ordenados estrictamente,
cantidades y n positivos, bid<ask. Un libro viejo no reemplaza al nuevo; un
snapshot diferente con mismo time es conflicto. Actualizaciones idénticas se
cuentan aparte. No se aplica un delta a los niveles.

La primera captura reveló dos ampliaciones reales frente al tipo WsBook
documentado: ACK de L2 expande `mantissa=null, fast=false`, y el snapshot incluye
`spread` como cadena decimal. Se verificó la forma directamente en el endpoint
oficial, mostrando solo claves/opciones públicas. El parser admite únicamente
esos defaults compatibles y valida spread finito no negativo antes de
descartarlo: no usa un campo cuyas unidades no están documentadas. Modos fast
o mantissa distintos se rechazan. Los fixtures incluyen este formato observado.

Datos antes del ack o una confirmación que no corresponde a una solicitud
pendiente/ya confirmada se rechazan. Los mensajes salientes solo pueden ser
subscribe/unsubscribe públicos o ping: no hay post, canales de cuenta ni órdenes.

## Heartbeat, reconexión y errores

Un loop coordina recv, deadlines de ack y heartbeat; un timeout de recv para
despertar el reloj no es por sí mismo desconexión. Ping de aplicación cada
20 s, pong esperado en 10 s; ninguna otra actividad sustituye al pong. Ack
timeout 10 s; conexión y envíos 5 s, cierre 2 s por defecto. El transporte no
envía otro heartbeat de aplicación automático.

Desconexión remota, error de transporte, timeout de heartbeat/ack o ráfaga de
3 mensajes inválidos fuerzan cierre y backoff. Un error del servidor lo fuerza
inmediatamente. Backoff base 2.1 s, crecimiento exponencial hasta 30 s más
jitter de 0–20 %, siempre acotado por el plazo restante. Esto limita los intentos
de esta instancia; los demás procesos comparten los límites reales por IP.

Al reconectar se limpian confirmaciones/libro/historial y se vuelven a enviar
las mismas suscripciones. La cache de trades conserva IDs durante su horizonte
para no duplicar el replay. Nuevos eventos llevan otro connection_epoch.
Cada interrupción/conflicto/pérdida aumenta continuity_epoch y conserva motivos.
No se afirma que un ack recupere todos los trades perdidos.

Errores registrados exclusivamente como códigos cerrados: no excepciones
arbitrarias, paquetes completos, direcciones, stdout de otros programas ni
credenciales. El JSON final contiene contadores, timestamps, edades, motivos
de gaps, capacidad y evidencia agregada de side. Estado terminal `closed` no
significa conexión en vivo; book_fresh evalúa la edad del último dato observado.

La CLI devuelve 0 cuando hubo conexión, ambos canales se confirmaron para
todos los activos y no hubo errores; 1 si falla esa comprobación, 2 para
argumentos inválidos. Las pérdidas de datos se consultan además en continuity
y queue_dropped; conexión estable no equivale a tape completo.

## Memoria y pérdida de continuidad

| Recurso | Límite por defecto |
|---|---|
| Símbolos por instancia | 64, con 2 suscripciones por símbolo |
| Frame de aplicación | 1 MiB, tanto en librería como parser |
| Frames pendientes del transporte | 16 |
| Trades en un batch | 4096 |
| Cola de trades | 8192 eventos y 2 MiB de payload contabilizado |
| Cache dedup | 100000 identidades, TTL recepción de 20 min |
| Historial de snapshots L2 | 128 por símbolo; cada snapshot <=20 niveles/lado |
| Envíos de aplicación | separación mínima 40 ms, hasta 25/s por instancia |

El conteo de bytes de cola no es una medida exacta del heap de CPython; todos
los campos y el número de objetos también están acotados. Saturar la cola
descarta el nuevo evento y registra explícitamente queue_dropped/gap. La
expiración TTL solo libera IDs y aumenta `dedup_evictions`: no implica pérdida
de trades ni cambia continuidad. Sin consumidor durable, la evicción por
capacidad conserva su gap conservador y no se garantiza deduplicación fuera
de la caché o al reiniciar el proceso. Con Live Store, SQLite verifica las
identidades retenidas antes de sumar o clasificar tardanza; la evicción de
caché por capacidad tampoco necesita cortar el segmento.

Eventos de trades fuera de orden se entregan con metadata. Si retroceden más
de 2 s respecto al máximo observado por activo, el cliente standalone marca
gap. Con consumidor durable, el motor decide después de comprobar duplicados,
conflictos y retención; un replay duplicado no es una pérdida de continuidad.
El transporte no reordena agregados: esa responsabilidad pertenece a Order Flow.
Pre_connection
identifica datos anteriores a la conexión; no certifica un snapshot completo.
No hay números de secuencia continuos: ausencia de errores conocidos no demuestra
ausencia de pérdida, y silencio de trades no prueba desconexión si heartbeat vive.

## Semántica de side: evidencia y certeza

La [notación oficial](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/notation)
define **B comprador agresor y A vendedor agresor** en trades. El reporte usa
`side_semantics=documented_hyperliquid_aggressor`. Trade/ReceivedTrade conservan
el A/B original; el transporte no calcula Delta/CVD ni consulta fills de cuentas.
La versión de semántica y las matemáticas firmadas pertenecen al motor de
[Order Flow](ORDER_FLOW.md#estado-de-side-y-acceso-a-métricas-firmadas).

La API `PublicWSClient(..., network="testnet")` y la CLI `--network testnet`
seleccionan el [endpoint oficial de testnet](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/websocket).
Mainnet sigue siendo el valor por defecto. La caché de identidades usa la red
seleccionada y Live Store la conserva en operaciones y claves SQLite; datos de
testnet no se etiquetan ni suman como mainnet.

Prueba reproducible incorporada a la CLI, usando únicamente este stream:

1. Para cada trade nuevo, usar solo un libro ya recibido cuyo time sea
   estrictamente anterior al trade y cuya antigüedad no supere 1500 ms.
2. Excluir pre_connection, trades fuera de orden y snapshots del mismo timestamp.
3. Si el precio coincide exactamente con el mejor bid o ask anterior, contar
   A_at_bid/A_at_ask/B_at_bid/B_at_ask. No modificar el trade ni inferir un signo.
4. Separar no_preceding_book, preceding_book_too_old,
   same_timestamp_ambiguous, inside_or_outside_previous_bbo e historial/late.

La hipótesis "lado agresor" predice B_at_ask y A_at_bid; la hipótesis "liquidez
consumida" predice B_at_bid y A_at_ask. Los contadores comparan ambas sin
seleccionar automáticamente una. Es evidencia observacional, no verdad de
ground truth: entre snapshots puede cambiar el libro, varias órdenes comparten
bloque y nSigFigs=5 agrega precios. Que un trade contradiga el BBO anterior no
demuestra necesariamente que su side esté invertido. Se requiere evidencia
adicional para deducir signos a partir del BBO. La semántica actual procede de
la documentación del protocolo; estos contadores no la cambian ni la habilitan.

## Pruebas y captura de aceptación

Los tests son `test_stream_types.py` y `test_ws_client.py`, con fixtures sintéticas
y sockets async inyectados; no abren conexiones ni llaman CLI de modelos/wallets.
Cubren validación, varios símbolos, ACK, datos antes de ACK, identidad y conflictos,
orden, límites de memoria, heartbeat, desconexión, backoff, gaps, stop/cancelación
y allowlist de mensajes sin escritura financiera.

Suite completa: **190 tests, OK** (162 anteriores y 28 nuevos). `git diff --check`
sin incidencias. Dependencia real: websockets 10.4; no hubo instalación ni upgrade.

Captura de aceptación mainnet, 2026-10-08, 11:04:49.619–11:05:49.852 UTC:
60 s de presupuesto de observación, 60.233 s incluyendo cierre. Comando ETH/HYPE
con `--seconds 60 --json`; sin persistencia de paquetes ni información personal.

| Estadística | Resultado |
|---|---|
| Conexiones / reconexiones / cierre limpio | 1 / 0 / 1 |
| Suscripciones confirmadas | 4 (trades+l2Book en ambos activos) |
| Mensajes / batches trades / snapshots L2 | 235 / 205 / 24 |
| Trades recibidos / únicos / consumidos | 489 / 489 / 489 |
| Duplicados / conflictos / inválidos | 0 / 0 / 0 |
| Pérdidas de cola / evicciones de dedup / gaps conocidos | 0 / 0 / 0 |
| Ping / pong | 2 / 2 |
| Pico de cola / cola final | 34 / 0 |

| Activo | Trades | Libros | Edad final exchange L2 | Edad final trade | Niveles L2 |
|---|---|---|---|---|---|
| ETH | 171 | 12 | 2825 ms | 768 ms | 20+20 |
| HYPE | 318 | 12 | 2825 ms | 768 ms | 20+20 |

Ambos libros eran frescos bajo el criterio diagnóstico de 15 s. La recepción
final de trades tenía 260 ms (ETH) y 258 ms (HYPE); última recepción de mensaje
258 ms y último pong ~20 s. Se observaron ~12 snapshots por activo en 60 s:
el feed slow no produjo libro previo cercano para muchos trades. No se
extrapola una garantía de frecuencia o continuidad a partir de esta muestra.

Evidencia de side de esa misma captura (solo coincidencias exactas con BBO
estrictamente anterior y <=1500 ms):

| Activo | B_at_ask | A_at_bid | B_at_bid | A_at_ask |
|---|---|---|---|---|
| ETH | 16 | 22 | 0 | 0 |
| HYPE | 2 | 6 | 6 | 1 |

ETH favorece la hipótesis de agresor en 38 casos; HYPE aporta 8 coincidencias
con esa hipótesis y 7 en sentido contrario. No es una verificación concluyente.
Casos excluidos ETH: 30 pre_connection/late, 88 libro demasiado antiguo, 15
precio interior/exterior del BBO previo. HYPE: 30 pre_connection/late, 223 libro
antiguo, 38 precio interior/exterior y 12 timestamp de libro igual al trade.
Todos los contadores corresponden a eventos únicos; en aquella captura la
semántica estaba marcada unverified. Las cifras históricas no se han recalculado.

Una primera captura de 60 s falló por los campos L2 ampliados descritos arriba:
solo trades confirmó, 28 mensajes rechazados, 4 ACK timeout y 3 reconexiones.
Terminó con cierres limpios y gaps explícitos; no se consideró válida. Dos
sondeos breves adicionales inspeccionaron solo claves/opciones/tipo de spread
para corregir el parser. Después se añadieron fixtures y se repitió la captura
de aceptación completa. Esa discrepancia real forma parte de las limitaciones
de formatos documentados y queda registrada, no se ocultó flexibilizando todo JSON.

## Límites de esta entrega

FASE 1 solo transporta eventos públicos y diagnósticos. FASE 2 ya añade el
consumidor standalone [Order Flow](ORDER_FLOW.md), SQLite y matemáticas Delta/CVD
validadas con fixtures; el motor 2.0 habilita valores firmados reales conforme
a semántica documentada, cobertura y frescura. Los checkpoints 1.0 se conservan
sin reinterpretarlos. Este cliente mantiene sus responsabilidades de transporte.
REST de trades perdidos, indicadores adicionales, OBI por bandas, DOM y
integración con consumidores existentes continúan pendientes de autorización.
