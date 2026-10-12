# Order Flow — FASE 2

Motor standalone READ-ONLY para trades públicos normalizados, por red y activo.
No modifica el pipeline REST, contratos de análisis, reglas de consenso ni
umbrales. El resultado mantiene `execution_authorized=false`. Fuentes:
[`order_flow.py`](../../python/src/order_flow.py),
[`flow_store.py`](../../python/src/flow_store.py),
[`live_store.py`](../../python/src/live_store.py).

## Arquitectura y reutilización

```text
PublicWSClient de FASE 1 (una conexión, validación, ACK, heartbeat, dedup)
    → ReceivedTrade + epoch/continuidad → LiveStore.sync_ws
    → operaciones normalizadas → FlowStore.write
        ├─ journal SQLite (identidades únicas, control de continuidad)
        ├─ OrderFlow (buffer ordenado, segundos cerrados, segmentos)
        └─ checkpoint + manifest, en la MISMA transacción
    → LiveStore.read → snapshot desacoplado y profundamente inmutable
```

Se reutilizan transporte, cola, normalización, identidades, metadata de FASE 1
y su último Book inmutable. `stream_types.py` añade solo representación mínima
durable y restauración mediante el validador de trades existente. No se creó
otro parser WS, otro transporte ni un cliente REST. La vista `read` conserva
L2 sin métricas como en FASE 2. FASE 3 añade
[`read_l2` y el módulo Order Book](ORDERBOOK.md), con BBO, spread, profundidad,
OBI observado/válido y calidad independiente; no cambia motor o persistencia.

El journal no tiene datos de usuarios, hashes de transacción, direcciones,
wallets ni paquetes completos. Guarda coin, side, precio/tamaño decimal,
time/tid, red, recepción, epochs y códigos de control. Los snapshots no están
conectados a DOM, scanner o análisis; no hay daemon ni IPC. Una instancia es
propiedad de un único event loop, con un único writer SQLite.

## Estado de side y acceso a métricas firmadas

Se revisaron nuevamente las fuentes oficiales: el SDK nombra A=Ask, B=Bid en
sus órdenes; los tipos de trade público no establecen inequívocamente si es
el agresor o la liquidez consumida. Los campos crossed de fills de cuentas no
constituyen evidencia de este canal y no se consultaron cuentas.
[Tipos oficiales](https://github.com/hyperliquid-dex/hyperliquid-python-sdk/blob/master/hyperliquid/utils/types.py),
[ejemplo oficial de órdenes](https://github.com/hyperliquid-dex/hyperliquid-python-sdk/blob/master/examples/basic_adding.py),
[trades WS oficiales](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/websocket/subscriptions).

La comparación pública reproducible de FASE 1 se ejecuta también durante la
captura de FASE 2. Solo cuenta coincidencias con BBO estrictamente anterior,
con antigüedad <=1500 ms; excluye historia, datos tardíos, mismo timestamp y
quotes que ya no permiten concluir. La interpretación es observacional y
permanece inconcluyente, no una regla heurística de signos.

Por tanto **mainnet y testnet conservan `side_semantics=unverified`**. Volumen
total y conteo observado están disponibles, pero comprador/vendedor agresor,
Delta base/nocional, CVD, pendiente y ratio son null. No se calculan signos
internos para estas redes. No existe un flag CLI para activarlos.

Los fixtures y replay utilizan exclusivamente la red `synthetic`, cuya
convención conocida es B comprador agresor y A vendedor agresor. Permiten
validar las matemáticas sin convertir esa convención en verdad de mainnet.
Checkpoints que intenten activar `signed=true` para otra red se rechazan.

## Definiciones matemáticas y unidades

Precio p en moneda cotizada por unidad base, tamaño q en unidades base:

- Volumen total base = Σq; nocional observado quote = Σ(p·q).
- Cuando la semántica es conocida, B_base/S_base suman los tamaños compradores/
  vendedores y B_quote/S_quote sus nocionales. Delta_base=B_base−S_base;
  Delta_quote=B_quote−S_quote. No se suman cantidades de ETH con HYPE ni de
  distintas redes.
- CVD acumulado = suma de Delta de segundos cerrados desde el inicio de un
  segmento continuo identificado. No es histórico desde el inicio del exchange.
- CVD de ventana es exactamente el Delta de esa ventana; reutiliza el valor,
  no ejecuta otro cálculo ni resta dos segmentos distintos.
- Pendiente de CVD: OLS de la curva acumulada por segundo de la ventana,
  en base/s y quote/s, por separado. Un desplazamiento constante del origen
  no cambia la pendiente. Requiere ventana utilizable y semántica conocida.
- Buy/sell ratio = B_base/S_base. S_base=0 produce null y
  `zero_sell_denominator`, incluso cuando todo el volumen es comprador.
- Trades/s = matches únicos de la ventana / duración completa en segundos.

Se usan Decimal con precisión local 192, sin cambiar el contexto global ni
mezclar float y nocional. Valores monetarios/volúmenes se serializan como
cadenas decimales canónicas (pueden usar notación exponencial); null conserva
su significado. Los ratios de cobertura son fracciones de tiempo, no monedas.
Para ETH/HYPE perpetuos probados, quote representa nocional en USDC, no fees
ni PnL; en una ampliación se debe verificar la unidad de cada mercado.

## Tiempo de evento, ordenación y cierre

Ventanas 1/5/15 minutos: `[to_ms−W, to_ms)`, extremo inicial inclusivo y final
exclusivo. `to_ms=floor((as_of_ms−2000)/1000)*1000` por defecto. No se incluyen
trades de una frontera futura ni segundos incompletos. El buffer ordena por
`(event_time_ms, tid)` por activo; tid es desempate determinista, no secuencia
del exchange. Las sumas por segundo son independientes de ese desempate.

Un evento fuera de orden antes del watermark se acepta en su segundo correcto.
Uno anterior a un watermark ya cerrado se registra como late, no reescribe la
ventana publicada y abre otro segmento con gap. Pendientes se guardan en el
checkpoint y se recuperan exactamente. Un avance de reloj de evaluación
hacia atrás se rechaza; una consulta con as_of anterior al estado evaluado
tampoco fabrica un replay histórico.

Los primeros trades anteriores a la conexión o al inicio conservador observado
son bootstrap: forman parte del volumen total observado pero no inician CVD
ni demuestran cobertura previa. Eventos de epochs anteriores también quedan
solo en las observaciones. `observed_total` indica expresamente que incluye
bootstrap/prior epochs y no es un volumen con duración certificada.

## Cobertura, frescura y segmentos

`LiveStore.sync_ws` comienza la cobertura al observar las confirmaciones,
conservadoramente después del ACK real. Muestrea estado/epochs cada ~100 ms.
No intenta reconstruir la conexión antes de ese punto ni una interrupción
entre observaciones. La recepción de mensajes/pong acota salud del stream;
un trade antiguo por inactividad no implica por sí solo desconexión.

Contacto local usable: hasta 30 s de edad y 5 s futuros por defecto. Ventanas
solo se declaran ready si todo su intervalo pertenece al segmento activo,
el contacto sigue utilizable y su duración cabe en la retención de buckets.
Cobertura_ms/ratio mide conectividad conocida, **no porcentaje de trades
garantizado por el exchange**. `exchange_completeness=unknown` siempre.

En warmup, desconexión, contacto stale o historia insuficiente, valores válidos
de ventana son null. `observed_partial` permanece separado y etiquetado. Antes
de observar conexión o un trade, incluso observed_total es unavailable/null.
Cero solo significa cero observado en un intervalo de cobertura conocido, o
un origen convencional de CVD sintético; no sustituye datos ausentes.

Conflictos de identidad, datos tardíos, overflow, cambios de continuidad y
desconexiones interrumpen el segmento. Un segmento nuevo nunca conserva el CVD
acumulado del anterior. Una parada normal de captura es `capture_end`, una
frontera explícita que no se cuenta como pérdida de datos. Pending no cerrado
al interrumpir se cuenta como pending_abandoned; no se integra forzando el
watermark. Se conservan las últimas 8 descripciones de segmentos cerrados.

Después de recuperar un checkpoint, la vista se marca `recovered_not_live` y
no entrega métricas como señales actuales. La primera observación nueva cierra
una conexión anterior que quedó activa en el checkpoint con motivo restart,
y abre otro segmento. Reabrir SQLite no demuestra conectividad durante la parada.
No se reconstruyen huecos mediante REST ni se unen CVD artificialmente.

## SQLite y consistencia transaccional

Schema DB=1; cálculo=1.0, independientes del schema de snapshot=1.0.
SQLite estándar Python, WAL y synchronous=FULL. No nueva dependencia externa.

Journal append-only dentro del horizonte retenido: identidad económica única
`(network,coin,event_time,tid)` para filas trade. Reenvío igual se registra como
duplicate sin acumular otra vez; payload distinto con misma identidad es
conflict y conserva el original. Controles connect/advance/gap/disconnect y
resultados excluidos también son normalizados y verificables.

Cada transacción (<=1024 operaciones) actualiza journal, motor y checkpoints
por activo, con cursor, parámetros, estado cerrado, pendientes y retención.
Journal y checkpoint se confirman en **un único COMMIT**. Ante error/fallo
inyectado/quota SQLite, ROLLBACK revierte ambas tablas y restaura la copia
en memoria. El consumidor devuelve a la cola WS los eventos retirados antes
de un fallo, sin publicar progreso no confirmado. Cancelación cierra socket
y base local, sin ejecución financiera.

El writer rechaza un motor que no coincide con el checkpoint autorizado bajo
el lock de escritura: se debe recuperar primero. No se acepta sobreescribir
un estado persistido mediante un motor nuevo o desactualizado. Tampoco se
aceptan bases ajenas ni versiones desconocidas. El replay exige archivo nuevo.

Recuperación: PRAGMA quick_check, checksums de filas/checkpoints, metadata,
manifest del prefijo retirado/tail, secuencias retenidas sin agujeros y anclaje
del cursor. El estado recuperado incluye pendientes; se reconstruye la cache
de IDs reciente desde filas únicas retenidas. No puede haber journal confirmado
posterior al checkpoint de ese activo; esa anomalía falla cerrada. Se comprueba
igualdad de estado de cálculo antes y después de recuperar, sin exigir la misma
cache auxiliar cuando una parte del journal fue legítimamente retirada.

SHA-256 reutiliza la canonicalización de `integrity.py` (JSON UTF-8 compacto,
claves ordenadas, sin NaN ni campo superior integrity). Detecta corrupción,
eliminación no declarada de filas y desacuerdos locales; no es una firma ni
prueba criptográfica del exchange. Un atacante que reescriba también hashes
no queda excluido por este mecanismo.

## Retención, memoria y backpressure

Defaults configurables del motor por activo: 4096 pendientes, 960 buckets
de segundos, 100000 IDs, hasta 64 activos en total. No se retienen trades
completos por cada ventana; la memoria analítica usa agregados de segundos.
La cola/frame/cache WS conserva los límites de FASE 1. Un overflow declara
pérdida, no bloquea indefinidamente recv ni convierte incompletos en válidos.

Defaults SQLite: 100000 filas, retención máxima solicitada 24 h y 64 MiB para
páginas de la base principal. Los límites se persisten en el manifest y se
recuperan salvo override explícito. Retención elimina un prefijo por edad o
capacidad; con tiempos de recepción fuera de orden puede retirar además filas
intermedias recientes. Declara el floor temporal de identidades por activo.
Reenvíos fuera de ese horizonte no se suman como nuevos: se rechazan con
outside_retention; si podrían afectar al segmento activo se degrada continuidad.
No se promete deduplicación eterna ni replay de todo el historial desde cero.

Con SQLite activo, la cache RAM puede evictar IDs sin invalidar el segmento
porque la unicidad retenida está en el journal; fuera de persistencia, olvidar
una identidad degrada continuidad. Checkpoints conservan los acumulados y
buckets aunque el journal anterior se haya podado. El floor y su pérdida de
capacidad de auditoría individual son explícitos.

La expiración normal de IDs WS tras 1200s no cambia continuidad. Live Store
declara al transporte que existe un consumidor durable: los replays antiguos
y las expulsiones por capacidad se resuelven mediante la identidad retenida
en SQLite, antes de aplicar los gates de tardanza del motor. Duplicados no se
suman; conflictos, eventos fuera de retención capaces de afectar al segmento,
trades realmente tardíos, pérdidas de cola y cambios de época siguen degradando
continuidad. Los contadores de deduplicación WS describen su caché local; los
acumulados del motor conservan la unicidad durable. Sin SQLite no se promete
deduplicación eterna. Mainnet/testnet mantienen `side_semantics=unverified`.

La quota principal usa max_page_count y un fallo FULL es atómico. El WAL y
archivos auxiliares pueden añadir espacio transitorio; no se presenta 64 MiB
como cuota dura de todos los archivos. Se hace autocheckpoint a 64 páginas y
TRUNCATE al cerrar. La instrumentación tracemalloc mide objetos Python, no toda
la memoria nativa SQLite/socket; RSS es el máximo del proceso Linux completo.
El backup transaccional y publicación de snapshots tienen coste creciente
dentro de sus límites; el replay no acredita rendimiento de producción.

## CLI y comandos de verificación

Desde `python/`, con la dependencia WS opcional de FASE 1 instalada:

```bash
# Captura foreground; carpeta privada ignorada, distinta del experimento
python3 -B -m src.live_store ETH HYPE --seconds 60 --json

# Verificar y recuperar sin abrir una conexión ni reclamar estado live
python3 -B -m src.live_store --recover-only --json

# Límites opcionales (override explícito de los persistidos)
python3 -B -m src.live_store ETH HYPE --max-journal-rows 100000 --retention-seconds 86400 --max-db-mib 64

# Replay acelerado sintético: elegir una base nueva, nunca la mainnet existente
python3 -B -m src.live_store --replay-trades 20000 --db ../data/order_flow/replay_new.sqlite3 --json

# Suite offline, sin modelos, wallets ni conexión
python3 -B -m unittest discover -s tests
```

Base por defecto: `data/order_flow/mainnet.sqlite3`, ignorada con sus WAL/SHM.
Los reports no incluyen rutas personales, direcciones ni paquetes completos.
Los snapshots de la API incluyen el último libro normalizado; el resumen CLI
solo imprime timestamp y número de niveles. L2 no se persiste y al recuperar
sin WS aparece null, nunca un libro inventado.

## Validación y resultados

Los nuevos tests cubren vectores Delta/CVD positivos/negativos/neutros,
nocional diferente de base, pendiente conocida, fronteras half-open, warmup,
reordenación, late, conflictos, múltiples redes/activos, segmentos, cuotas,
retención, crashes, corrupción/eliminación, recuperación de pendientes,
rechazo de motores desactualizados y vistas inmutables. Mainnet/testnet con
ventana temporal completa siguen devolviendo Delta/CVD null por side no verificado.

Suite final: **220 tests, OK** —190 anteriores y 30 nuevos—; ningún fallo
pendiente. `git diff --check` limpio. Los seis hashes de módulos protegidos
coinciden con los registrados al iniciar FASE 2. Se preservó el trabajo pendiente,
sin commit/push. El experimento independiente permanece excluido e intacto.

Replay acelerado, con SQLite FULL, checksums, retención, verificación de motor
autorizado e instrumentación tracemalloc: 20000 trades, 10000 por activo,
1000 segundos sintéticos, 90.249 s de ejecución (~221.6 trades/s). Conservó
5000 filas y 2 checkpoints, 960 buckets y 512 IDs por activo, cero pendientes
al finalizar. Volumen ETH=10000 base/100000 quote, HYPE=10000 base/200000 quote;
CVD sintético final=0, ventanas 1/5/15m ready y estado recuperado exactamente igual.
Pico Python instrumentado=6469318 bytes (~6.17 MiB); base=3022848 bytes,
WAL transitorio observado=634512 bytes. El backup y validación completa de
checkpoints hacen este resultado sensiblemente más lento que una suma en RAM;
no es un benchmark sin instrumentación ni garantía para mercados más rápidos.

Captura mainnet controlada: 2026-10-08 12:03:20.272–12:04:20.572 UTC,
60 s de presupuesto, 60.3 s hasta cerrar WS. Tiempo total incluyendo verificación
y recuperación=64.786 s; 1 conexión, 4 ACK, 2 ping/pong, cierre limpio,
309 mensajes, 277 batches, 26 snapshots L2. Duplicados, conflictos, mensajes
inválidos, reconexiones, evicciones y pérdidas de cola=0. Pico cola WS=283.

| Activo | Trades únicos observados | Volumen base observado | Nocional quote observado | Cerrados / bootstrap / pending al cierre |
|---|---|---|---|---|
| ETH | 358 | 1227.3647 ETH | 3105528.74187 USDC | 309 / 34 / 15 |
| HYPE | 795 | 29140.99 HYPE | 2482407.8863 USDC | 758 / 31 / 6 |

Estos totales incluyen la historia inicial y los eventos pendientes: no se
etiquetan como volumen válido de una ventana de exactamente 60 s. Con el
watermark y la confirmación conservadora, la cobertura cerrada fue 57020 ms:
95.03 % de la ventana 1m, 19.01 % de 5m y 6.34 % de 15m, siempre de tiempo
de conectividad conocido, no de trades del exchange. Las ventanas quedaron
warmup/no-live al cerrar. Capture_end cuenta una frontera por activo y no
una pérdida inesperada; los pending no se forzaron a una ventana cerrada.

Persistencia: 1279 filas normalizadas, 2 checkpoints, SHA-256/verificación
SQLite/cursors correctos, recuperación con igualdad de estado. Base=770048
bytes y WAL al cerrar=0. Se abrió de nuevo en otro proceso, sin conexión:
los conteos/volúmenes se mantuvieron y recovered_not_live bloqueó métricas
vigentes. Una edad negativa en el primer resumen reveló que la vista final
usaba la última evaluación previa a recibir/cerrar; se corrigió as_of para
incluir recepción y frontera final, con un test específico. La recuperación
posterior produjo edades de recepción no negativas sin cambiar acumulados.

Evidencia side de la misma captura: ETH B_at_ask=37, A_at_bid=28, sin
coincidencias opuestas; HYPE B_at_ask=26, A_at_bid=18, B_at_bid=2, A_at_ask=3.
ETH excluyó 30 históricos/late, 221 por libro antiguo, 37 fuera/dentro de BBO
y 5 por mismo timestamp; HYPE excluyó 30, 362, 352 y 2 respectivamente.
Favorece observacionalmente agresor, pero no certifica el signo por las
contradicciones y la cobertura parcial del BBO. Side sigue **unverified** y
todos los Delta/CVD reales y volúmenes agresores son **null**.

Pico Python de captura=4545837 bytes (~4.34 MiB). En este entorno ru_maxrss
arrojó 479060 KiB incluso en un proceso que solo importaba módulos y cuyo
VmHWM era 26480 KiB: arrastra el máximo anterior al exec del lanzador. No se
utiliza ese valor como consumo del motor. La recuperación en proceso separado
midió VmRSS/VmHWM=27672 KiB (~27.0 MiB). Se añadieron métricas /proc VmRSS/VmHWM
para futuras capturas; el primer run no las registró y no se inventa su RSS.

Problemas/limitaciones pendientes: side no certificado, feed sin secuencia
completa, recuperación histórica limitada al checkpoint/horizonte retenido,
WAL fuera de la quota principal, costo del backup/hash transaccional y API de
un solo writer/event loop. Retención se evalúa al confirmar transacciones;
sin actividad no hay una tarea de limpieza periódica ni borrado al hacer una
lectura de recuperación. No se optimizaron estas limitaciones cambiando reglas
de trading o afirmando una cobertura del 100 %.

## Alcance y siguientes fases

FASE 2 no integra datos con Market State, Pre-analysis, Scanner, Decision
Snapshot, Analysis Pipeline ni Fast Revalidation. No hay indicadores/filtros
nuevos, cálculo L2 compartido, DOM o ejecución. FASE 3 (libro compartido)
requiere autorización expresa antes de implementar o refactorizar esos módulos.
