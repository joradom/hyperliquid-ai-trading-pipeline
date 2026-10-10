# R102 — Propuesta WebSocket, Order Flow y DOM

Diseño FASE 0, 2026-10-08. **FASES 1, 2, 3 y 4 autorizadas e implementadas:
transporte, diagnóstico, agregación, persistencia y núcleo L2 compartido.
DOM foreground incluido. FASE 5 y posteriores pendientes de autorización.**
La [auditoría](PHASE0_AUDIT.md) describe el código actual y la suite estabilizada.
La entrega FASE 1 se especifica en [WebSocket Client](components/WEBSOCKET_CLIENT.md).
La entrega FASE 2 se especifica en [Order Flow](components/ORDER_FLOW.md).
La entrega FASE 3 se especifica en [Order Book](components/ORDERBOOK.md).
La entrega FASE 4 se especifica en [DOM](components/DOM.md).
El diagrama siguiente conserva el diseño objetivo: el pipeline sigue usando
REST y no consume las vistas WS de Live Store en FASE 3.

## Arquitectura propuesta

```text
REST hl_client ── contexto / histórico cerrado ─────────────┐
                                                          │
WS persistente ── validación / desduplicación / continuidad │
   │                                                      │
   ├─ trades ── order_flow ── agregados + calidad ──────────┤
   └─ l2Book ── orderbook ── libro + métricas + calidad ────┤
                          │                               ▼
                          └─ live_store ─────────── Market State versionado
                                 │                        │
                                 └─ DOM (solo render)     ▼
                                            Pre-analysis / Scanner
                                                          │
                                              Decision Snapshot versionado
                                                          │
                                           Codex + Agy (consenso existente)
                                                          │
                                                Fast Revalidation
```

Se mantiene `hl_client.py` para REST. El cliente WS será un transporte distinto
por protocolo, no otro cliente REST. `market_state.py` es la frontera que integra
observaciones WS en una captura inmutable; no calcula nuevamente Delta/CVD.
El módulo puro compartido `orderbook.py` suministra cálculos L2 a WS, Live Store,
Market State y revalidación mediante adaptadores compatibles. `read_l2` añade
una vista inmutable independiente, sin cambiar `read`. DOM consume ambas vistas.
`indicators.py` conserva el cálculo único de indicadores de velas cerradas.

Un servicio y un `live_store` por proceso administran los activos. DOM y scanner
del mismo proceso comparten ese estado. CLI separadas no pueden compartir una
conexión por una variable global: si se requiere simultaneidad entre procesos,
se diseñará después un servicio local con IPC de lectura y snapshots versionados.
No se incorpora un daemon de ejecución ni se utiliza el experimento independiente.

## A. Cliente WS persistente

Una conexión inicial a `wss://api.hyperliquid.xyz/ws`, con suscripciones públicas
`trades` y `l2Book` por símbolo; `activeAssetCtx` será opcional tras verificar
equivalencia con contexto REST. El protocolo permite subscribe/unsubscribe y
ack de suscripción. No se usarán canales de cuentas ni mensajes de ejecución.
[Endpoint oficial](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/websocket),
[suscripciones](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/websocket/subscriptions).

Decisiones propuestas para implementación:

- Máquina de estados: disconnected → connecting → subscribing → live; por
  activo, awaiting_data, warming_up, live, stale, gap o error.
- Registro de suscripciones deseadas/confirmadas; timeout de ack y reenvío
  idempotente tras reconexión. Una nueva conexión crea connection_epoch.
- Ping de aplicación cada 20 s y timeout pong 10 s, ambos configurables.
  El servidor cierra conexiones sin mensajes enviados al cliente durante 60 s;
  su heartbeat usa `method=ping` y respuesta `channel=pong`.
  [Heartbeats oficiales](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/websocket/timeouts-and-heartbeats).
- Reconexión con backoff exponencial y jitter (propuesta: 1–30 s), cancelación
  limpia y limitador compartido de conexiones/mensajes. Nunca resetear CVD
  silenciosamente ni afirmar recuperación completa por un nuevo ack.
- Presupuesto por IP: documentar y respetar límites publicados —10 conexiones,
  30 nuevas/min, 1000 suscripciones y 2000 mensajes enviados/min—. El presupuesto
  REST también se comparte: 1200 de peso/min y coste adicional por filas de
  velas/historial. Un límite fijo de símbolos no garantiza cumplirlo.
  [Límites oficiales](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/rate-limits-and-user-limits).
- Allowlist de mensajes salientes: subscribe, unsubscribe, ping; URLs oficiales
  fijas por red, sin firmas ni acceso a configuración de cuentas.
- Validar coin/canal, precio/tamaño positivo finito, time entero y coherente,
  side permitido y forma de arrays antes de aceptar un evento.
- Eventos normalizados mínimos sin `users`, direcciones ni datos personales.
  No persistir paquetes crudos indiscriminadamente ni imprimirlos en errores.

Desduplicación: clave `(network, time, coin, tid)`, con time en milisegundos de
evento. No usar solo tid ni hash, ni asumir que tid es una secuencia creciente.
La documentación define tid como hash y recomienda tiempo/coin/tid para identidad.
[Tipos oficiales](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/websocket/subscriptions).
Sin tid, no inventar una identidad fiable: rechazar del agregador y degradar
calidad. Si misma clave contiene datos distintos, declarar conflicto y gap.

Backpressure y memoria: una cola de trades acotada por bytes/eventos, con worker
rápido para journal/agregación; overflow marca pérdida de continuidad y no
silencia pérdidas. Para L2, conservar el último snapshot completo por activo y
coalescer renderizaciones intermedias (contador explícito), sin perder trades.
Heartbeat y control no esperan al render. Buffer reordenador de eventos con
watermark configurable (propuesta inicial 2 s); eventos fuera del margen
invalidan cobertura o exigen recomputar una ventana retenida.

Guardar agregados de un segundo durante al menos 15 min más margen; cola,
reordenador y cache de IDs con límites explícitos. Si expulsa IDs que aún podrían
reaparecer, degradar continuidad: una cache limitada no prueba ausencia de duplicados.
Journal con identidad única permite deduplicar durante el horizonte retenido.
Capacidad, retención y límites se medirán con replay acelerado antes de fijarse.

## B. Order Flow por activo

### Verificación de side antes del signo

Las fuentes oficiales consultadas tipan `side` como A/B y el SDK identifica
A=Ask, B=Bid. El tipo `WsTrade` no explica inequívocamente si es el lado agresor
o el lado consumido; `crossed` pertenece a fills de usuario y no a WsTrade.
[SDK oficial: tipos](https://github.com/hyperliquid-dex/hyperliquid-python-sdk/blob/master/hyperliquid/utils/types.py),
[ejemplo oficial de lados](https://github.com/hyperliquid-dex/hyperliquid-python-sdk/blob/master/examples/basic_adding.py),
[WsTrade oficial](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/websocket/subscriptions).

**No se certifica aún el signo de agresión.** La hipótesis a verificar es B como
comprador agresor y A como vendedor agresor; no se utilizará para señales ni
se asignarán signos en código hasta obtener evidencia oficial explícita o una
validación reproducible con datos públicos oficiales. No se ha abierto WS en FASE 0.

Prueba de aceptación futura: grabar exclusivamente precios/tamaños/lados/times
de trades y BBO/l2Book públicos, comparar prints con el BBO estrictamente
anterior, separar casos de quotes cambiantes/mismo bloque e inconclusos, y
documentar contadores y discrepancias. El BBO de ~0.5 s no permite clasificar
todos los trades. Si no puede confirmarse el mapeo, `side_semantics=unverified`
y todas las métricas firmadas siguen null; volumen total/count sí pueden existir.
No inferir agresión desde `users`, una lista de fills de cuenta o el hash.

### Definiciones y unidades propuestas

Un contador por match único. A partir del mapeo verificado, volumen comprador
`B_W=sum(sz)` y vendedor `S_W=sum(sz)`; variantes quote `sum(px*sz)` separadas,
sin mezclar unidades. Ventanas UTC de 1m, 5m y 15m:
`[as_of-W, as_of)`, as_of alineado al segundo. Agregados de segundos completos
proporcionan esas ventanas sin aproximación intrasegundo.

| Métrica | Definición / límite |
|---|---|
| Delta | `B_W-S_W`, en base; otra métrica quote explícita |
| CVD acumulado | suma de delta desde inicio de un segmento continuo identificado, no desde el inicio del exchange |
| CVD de ventana | suma del delta de esa ventana; equivale a Delta_W y debe reutilizar su cálculo |
| Pendiente CVD | OLS sobre CVD muestreado por segundo en la ventana; base/s o quote/s, requiere serie sin gaps |
| Buy/sell ratio | `B_W/S_W`; si S_W=0, null con motivo (sin Infinity ni división por cero) |
| Trades/s | matches únicos/W en segundos; cero solo si hubo cobertura operativa sin trades |

Cada ventana publica period_start/end, observed_from/to, last_event_at,
last_received_at, watermark, coverage_seconds/ratio, segment_id,
side_semantics, duplicates, late/conflicting/rejected events, known_gaps,
buffer_overflow y quality.status/reasons. Coverage mide tiempo observado y
conectividad, no una fracción garantizada de trades del exchange. Un feed sin
trades puede estar sano por heartbeat; no confundir inactividad con caída.
Sin números de secuencia verificables no se demuestra ausencia absoluta de
pérdidas; `complete` significará únicamente sin huecos conocidos bajo el método.

Durante warmup, gap, overflow, side sin verificar o datos stale, el valor válido
de Delta/CVD de ventana es null; puede mostrarse `observed_partial_value` con
etiqueta parcial, nunca consumirse como métrica válida. No convertir null en 0.
Una ventana recupera validez después de acumular su duración completa sin
huecos conocidos. El CVD acumulado anterior a un gap nunca recupera continuidad
por esperar 15 minutos; requiere replay comprobado o nuevo segmento explícito.

### Inicialización, persistencia y recuperación

Al iniciar sin journal: CVD=0 como origen convencional de nuevo segment_id,
coverage_from=instante de suscripción efectiva; no es CVD histórico. Ventanas
en warmup. Un snapshot de trades previos se integra solo en el intervalo que
pueda validarse y desduplicarse, separado del inicio del segmento en vivo.

Propuesta local: SQLite con transacciones para journal normalizado + checkpoint
por activo (schema/calculation version, red, segmento, CVD base/quote, límites
temporales, último checkpoint, gaps y checksum). Índice único de identidad.
Checkpoint y estado de eventos aplicados se actualizan atómicamente; al reiniciar
se verifica y reproduce desde el checkpoint, sin doble contabilización.
Ficheros privados de ejecución/flujo irían en un nuevo directorio de datos
ignorado, distinto del experimento excluido; retención y tamaño acotados.

Toda desconexión, parada o pérdida local abre un gap. Consultar recentTrades
por REST oficial sería solo una posible ayuda de recuperación limitada, nunca
un histórico completo garantizado. `hl_client.py` aún no tiene ese método.
No reconstruir Delta desde OHLCV: volumen de vela no identifica agresión.
Si no puede demostrarse cobertura hasta el cursor previo, preservar el gap y
empezar un segmento nuevo, sin unir dos CVD como si fueran continuos.
La documentación general menciona snapshots al reconectar, pero no garantiza
historial completo de trades para cualquier duración/carga de desconexión.
[Reconexiones oficiales](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/websocket).

## C. Order Book

`l2Book` publica snapshots de niveles, no deltas que deban aplicarse como
incrementos. Reemplazar el libro por completo tras validar símbolo, tiempo,
ordenación, ambos lados, cantidades y no cruce. El feed documenta publicación
por bloque con al menos ~0.5 s desde la anterior; REST devuelve como máximo
20 niveles por lado y permite agregación por nSigFigs.
[Feed L2](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/websocket/subscriptions),
[REST L2](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint#l2-book-snapshot).

Mantener nSigFigs=5 al comparar con Market State actual. No cambiar su OBI:
`(bid_total_size-ask_total_size)/(bid_total_size+ask_total_size)`, en unidades
base, sobre todos los niveles devueltos. No representa el libro entero real.
BBO toma primer bid/ask; spread absoluto=ask-bid, bps=spread/mid×10000,
spread_pct=spread/mid×100. Profundidad acumulada por lado desde el mejor precio
hacia afuera, base y quote separadas, calculadas en el módulo compartido.

OBI por banda b ∈ {0.001,0.0025,0.005,0.01}: sumar bids con
`mid*(1-b) <= px <= mid` y asks con `mid <= px <= mid*(1+b)`; mismo cociente
en unidades base. Campos nuevos separados del imbalance legacy.
Guardar extremos observados, precisión/agregación, band bounds y cobertura
por lado. Si niveles visibles no alcanzan un límite de banda y no hay prueba
de que el lado esté completo, cobertura desconocida/parcial: OBI válido null,
con observado parcial aparte. No presentar ±1 % como completo por tener 20 filas.
Si denominador=0, null con motivo de ausencia de liquidez visible en esa banda.

Tras reconnect, un snapshot fresco restablece el libro actual; no restaura
persistencia histórica ni la continuidad del tape. No tratar pong como nuevo
timestamp del libro. Tolerancias de frescura y cobertura se especificarán como
metadata antes de convertirse en una política de filtrado.

## D. DOM de terminal

CLI implementada desde `python/`: `.venv/bin/python -m src.dom ETH`, opción
`--levels 1..20`. Rich Live/Table publica con objetivo 4 Hz, configurable;
el reloj de render no regula la ingestión ni la agregación.
[Rich Live oficial](https://rich.readthedocs.io/en/stable/live.html).

Pantalla: símbolo y conexión/epoch; 10–20 niveles por lado con precio, tamaño
base, nocional y profundidad acumulada; BBO/spread; OBI observado y por bandas;
Delta/CVD base/quote y ventanas; segmento, coverage, ages y estado de calidad.
Destacar WARMUP/PARTIAL/GAP/STALE, mostrar `—` para null. No hay botones de
órdenes, wallets ni acciones financieras. Interacciones posibles: salir,
pausar solo el render y cambiar niveles/ventana sin detener ingestión.

DOM recibe una vista inmutable del live_store con métricas ya calculadas.
No suma volumen, deduplica, calcula OBI, decide frescura ni mantiene otro CVD.
Pruebas de render con fixtures, terminal estrecha y sin colores; reloj virtual,
Ctrl-C/cancelación y ausencia de llamadas de ejecución.

## E. Indicadores de velas adicionales

ATR porcentual **ya existe** (`atr_14_pct=ATR/close*100`, Wilder). Validar
equivalencia de cualquier biblioteca antes de cambiar seeding/método.

| Indicador | Propuesta, cálculo solo en indicators.py | Pruebas requeridas |
|---|---|---|
| BB Width | `(upper-lower)/middle*100` con Bollinger20 población actual | plano=0, serie conocida, menos de 20 velas/null, denominador inválido |
| ADX/DMI14 | TR y ±DM Wilder, ±DI y DX, ADX suavizado con warmup definido | tendencia/plano, empates DM, seed, historial corto, divisiones por cero |
| Volatilidad | percentil empírico de ATR% o desviación de log-retornos en N velas cerradas del mismo timeframe | tamaño mínimo, ties/ranking, outliers, sin lookahead, ventana/seed y versión |

Evitar llamar régimen tendencial al percentil de volatilidad. Especificar
lookback, unidades y método de rango/percentil; etiquetas de régimen serían
descriptivas y versionadas. Validación por vectores independientes y fixtures
antes de exponer a modelos. No se añaden filtros ni umbrales de trading.

## Integración y compatibilidad futura

Primera etapa: módulos WS/Order Flow/L2 standalone y DOM opcional, sin alterar
Market State 1.1, Decision Snapshot 1.0 o el consenso. Datos incompletos no
entran todavía en clasificación.

Segunda etapa, previa autorización: Market State 1.2 opt-in con sección
microstructure, observaciones/quality por ventana, metadata de transporte y
enlace al checkpoint/flow snapshot. Captura atómica del live_store bajo lock
y as_of coherente; indicadores se calculan una vez. REST conserva histórico
cerrado/contexto si WS no sustituye esas dependencias. Una caída WS no se cubre
con números antiguos ni ceros. SHA-256 incluye datos y calidad, excluyendo
integrity como ahora; los tiempos monotónicos internos no se serializan como UTC.

Decision Snapshot 1.0 tiene additionalProperties=false y exige Market State
1.1 y POLICY exacta: añadir campos sin versionar rompería consumidores.
Proponer Decision Snapshot 1.1, schema explícito y validadores por versión;
mantener productor/validador legacy 1.0 y esquema de respuesta v2.0. La proyección
legacy sería una captura 1.1 explícita con checksum propio, no recortar campos
conservando el checksum de un documento 1.2. El contrato direccional sigue
referenciando exactamente el snapshot entregado a cada modelo.

Pre-analysis/Scanner solo transportarán y describirán las métricas nuevas al
principio; clasificación v1 se conserva. Una política que use order flow sería
una nueva rules_version autorizada por separado. Si los modelos ven métricas
diagnósticas nuevas, no generan reglas ejecutables de CVD/Delta: el vocabulario
de análisis v2 sigue cerrado; condiciones no representables van a unresolved.
Pipeline no cambia su matriz Codex+Agy ni execution_authorized=false.

Fast Revalidation puede reutilizar observaciones frescas inmutables del
live_store para L2 solo tras demostrar equivalencia con la captura REST y
preservar gates/deadline/calidad. La fuente y as_of deben quedar registrados.
No afirmar revalidación de Delta/CVD sin contratos y predicados versionados;
datos WS insuficientes implican INDETERMINATE para dependencias requeridas,
no VALID. El modo REST actual permanece disponible. No fusionar módulos.

## Plan incremental y criterios de aceptación

Cada etapa requiere autorización expresa después de FASE 0.

| Etapa | Archivos propuestos/afectados | Verificación antes de continuar |
|---|---|---|
| 1. Semántica y transporte público | nuevos `ws_client.py`, `stream_types.py`; tests WS; dependencias opcionales | Signo de side confirmado o métricas firmadas bloqueadas; allowlist READ-ONLY, ack/ping/reconnect, streams multi-activo, overflow/cancelación, sin datos personales |
| 2. Agregación y recuperación | nuevos `order_flow.py`, `flow_store.py`, `live_store.py`; posible método recentTrades en hl_client | replay determinista, duplicados/conflictos/out-of-order, ventanas y warmup, crash entre journal/checkpoint, gap no recuperable, memoria acotada |
| 3. Libro compartido — implementada | `orderbook.py`, adaptadores WS/REST, `LiveStore.read_l2`, tests y documentación | equivalencia legacy por fixtures/checksums; precisión/OBI/spread, libros vacíos/cruzados, cobertura por banda, frescura, conflictos e inmutabilidad |
| 4. DOM standalone — implementada | `dom.py`, `requirements-dom.txt`, tests y documentación | vistas inmutables sin cálculos duplicados, null/stale/gap visibles, controles POSIX, pausa independiente, cierre/teclado restaurado |
| 5. Indicadores | indicators.py y test_indicators.py | vectores independientes, seed/unidades, warmup, series planas, sin filtros nuevos |
| 6. Contratos opt-in | market_state.py, pre_analysis.py, scanner.py, decision_snapshot.py y schemas/tests | lecturas legacy intactas, nuevos schemas explícitos, checksum/as_of/quality, flujo ausente no sustituido por cero |
| 7. Consumo por IA/revalidación | analysis_pipeline.py, analysis_response solo si se autorizan nuevos predicados, fast_revalidation.py y tests | mismo consenso, snapshots iguales para ambos, procedencia por versión, ejecución false, fallbacks de calidad deterministas |

Antes de optimizar REST, proponer por separado contexto global compartido por
lote y entrada de pipeline desde scanner validado en memoria; comprobar
inmutabilidad, procedencia, frescura al enviar y conteos REST. No eliminar la
recaptura necesaria solo para ahorrar llamadas. No se implementó esa optimización.

FASE 0 termina con tests y documentación. No se instalaron dependencias WS/Rich,
no hay nuevo comando DOM funcional y no se abrió una conexión WS.
