# R102 — Benchmark multi-activo 5 / 10 / 20

Capturas públicas mainnet del 2026-10-09. READ-ONLY; sin cuentas, wallets, firmas, órdenes ni inferencias.

**Resultado:** las tres capturas completaron sin overflow, errores, reconexiones o corrupción; la contabilidad local y la recuperación coinciden.

La viabilidad demostrada se limita al tráfico recibido y a este host durante tres ventanas de 60 s. No es una capacidad máxima ni certificación de operación continua o de Termux.

## 1. Entorno, preparación y metodología

- Raíz y Git: `.` (raíz del repositorio). Se conservaron los cambios previos; sin commit ni push.
- Suite de referencia: **253 tests offline, OK en 18,226 s**, ejecutados con `python/.venv/bin/python -B -m unittest discover -s tests` desde `python/`.
- Python 3.12.3 (main, Aug 31 2026, 10:18:26) [GCC 13.3.0]; SQLite 3.45.1; websockets 10.4.
- Host: Linux-6.8.0-146-generic-x86_64-with-glibc2.39; CPU Intel(R) Core(TM) i7-5600U CPU @ 2.60GHz; 4 CPU lógicas.
- Carga al seleccionar: `1.96 1.64 1.41 3/1349 27246`. Host compartido, sin afinidad, frecuencia fijada ni detención de procesos ajenos. Había 132 GiB libres en el filesystem del proyecto.
- Cada escenario fue un proceso Python nuevo, consecutivo y sin otros benchmarks propios concurrentes. Un PublicWSClient, una conexión física exitosa y un LiveStore compartido por ejecución; ningún cliente por activo.
- Suscripciones públicas trades y l2Book por activo, nSigFigs=5. Se usaron exactamente los primeros 5, 10 y 20 de una selección fija.
- Sync: `LiveStore.sync_ws` seguido de sleep de 0,1 s, como la captura existente. Máximo original de 512 trades por sync; no se reagrupan ni reordenan operaciones.
- Publicación auxiliar: aproximadamente una vez por segundo se leen `read` y `read_l2` para todos los activos, sin DOM. El intervalo es trabajo+sueño; no se afirma frecuencia dura garantizada.
- Instrumentación separada: [`multi_asset_benchmark.py`](../benchmarks/multi_asset_benchmark.py). Wrappers de instancia delegan los métodos originales de handle_message, sync/write y cierre; no cambian código productivo ni parámetros.
- Bases exclusivas nuevas, ninguna base operativa reutilizada o eliminada; datos y evidencia permanecen en `data/order_flow/`, ya ignorado por Git.

### Configuración conservada

SQLite: WAL, synchronous=FULL (2), wal_autocheckpoint=64 páginas, página=4096 bytes, max_page_count=16384: cuota de base principal de 64 MiB. Journal=100000 filas y retención=24 h. WAL/SHM no están incluidos en esa cuota de páginas.

WS: 64 activos máximo; cola de trades=8192 eventos/2 MiB; cache global de IDs=100000, TTL=1200 s; historial L2=128 por activo; cola nativa websockets max_queue=16. Order Flow: 64 activos, 4096 pendientes por activo, 960 buckets, 100000 IDs por activo, lateness=2000 ms y freshness=30000 ms. Libros/métricas L2 siguen en RAM, sin historial SQLite.

Se verificaron los [límites oficiales por IP](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/rate-limits-and-user-limits): 10 conexiones WS, 30 nuevas/min, 1000 suscripciones y 2000 mensajes salientes/min. Los escenarios necesitaron 10/20/40 suscripciones y 12/22/42 mensajes salientes; la selección hizo una sola consulta metaAndAssetCtxs, peso 20. No se ejecutó scanner. El uso de esa IP por procesos ajenos no se midió.

### Selección reproducible

Consulta pública solicitada 2026-10-09T17:22:44.566403+00:00; recibida 2026-10-09T17:22:45.145119+00:00. Criterio: top 20 de `dayNtlVlm`, universo nativo sin isDelisted, símbolos ASCII alfanuméricos y precio/volumen positivos finitos. Se conservó la grafía oficial. No se consultaron velas, cuentas ni el screener.

| Orden | Activo | Volumen nocional 24 h al seleccionar | Primer grupo |
|---:|---|---:|---:|
| 1 | BTC | 2,231,586,092.01 | 5 |
| 2 | ETH | 937,680,111.62 | 5 |
| 3 | HYPE | 346,939,857.08 | 5 |
| 4 | ZEC | 254,598,956.58 | 5 |
| 5 | SOL | 230,692,779.01 | 5 |
| 6 | NEAR | 222,639,596.34 | 10 |
| 7 | PUMP | 140,983,809.88 | 10 |
| 8 | STRK | 73,279,500.97 | 10 |
| 9 | XRP | 60,122,002.63 | 10 |
| 10 | ENA | 40,083,616.50 | 10 |
| 11 | LIT | 29,118,078.58 | 20 |
| 12 | UNI | 27,706,225.77 | 20 |
| 13 | ZRO | 23,077,821.90 | 20 |
| 14 | VVV | 22,879,397.24 | 20 |
| 15 | WLD | 22,403,380.72 | 20 |
| 16 | SUI | 20,557,882.81 | 20 |
| 17 | ONDO | 18,684,463.59 | 20 |
| 18 | DOGE | 14,543,524.57 | 20 |
| 19 | XMR | 13,666,818.27 | 20 |
| 20 | FARTCOIN | 13,207,676.91 | 20 |

### Criterios de parada fijados antes de capturar

Detener el escenario y no iniciar uno mayor ante overflow, conflictos, fallo de escritura/integridad/recuperación/contabilidad o reconexión; también ante cola o pendientes ≥80 % de capacidad, base o journal ≥80 % de cuota, RSS >512 MiB, o tres sync consecutivos >2 s. Errores WS o suscripciones incompletas impiden escalar al finalizar. Son límites de seguridad del experimento, no políticas financieras. No se activaron en las tres ejecuciones.

Los archivos existentes se protegen con rechazo de reutilización y escritura exclusiva de resultados. No se redujeron garantías de durabilidad para mejorar cifras.

## 2. Comparativa

El presupuesto WS de 60 s comienza en la primera conexión, como en producción. ACKs consumen parte de ese presupuesto; el tiempo de observación WS incluye su cierre. El tiempo con todas las suscripciones confirmadas se mide desde el último ACK hasta el retorno de run, también con cierre. `Total` incluye creación de base, captura, drenaje, auditoría, dos recuperaciones y cierre; excluye arranque/imports del intérprete y escritura del JSON final.

| Métrica | 5 activos | 10 activos | 20 activos |
|---|---:|---:|---:|
| Estado | ok | ok | ok |
| Tiempo total, s | 61.623 | 62.217 | 62.649 |
| Captura con conexión/setup/cierre, s | 60.989 | 60.871 | 61.037 |
| Observación WS, s | 60.314 | 60.233 | 60.315 |
| Todas las suscripciones confirmadas hasta retorno, s | 59.644 | 59.233 | 58.442 |
| Primer conjunto completo de ACKs desde inicio, s | 1.345 | 1.638 | 2.594 |
| Intentos / conexiones / reconexiones | 1 / 1 / 0 | 1 / 1 / 0 | 1 / 1 / 0 |
| Suscripciones confirmadas / deseadas | 10 / 10 | 20 / 20 | 40 / 40 |
| Mensajes WS recibidos | 311 | 595 | 687 |
| Trades únicos WS = consumidos = journal | 712 | 1509 | 1783 |
| Snapshots L2 aceptados | 60 | 120 | 240 |
| Trades únicos / segundo de observación WS | 11.80 | 25.05 | 29.56 |
| Trades comprometidos / segundo de captura | 11.67 | 24.79 | 29.21 |
| Cola máxima, eventos | 63 | 228 | 555 |
| Cola máxima, bytes de payload contabilizado | 8895 | 32653 | 79367 |
| Overflow / descartes WS | 0 / 0 | 0 / 0 | 0 / 0 |
| Duplicados / conflictos / out-of-order WS | 0 / 0 / 0 | 0 / 0 / 0 | 0 / 0 / 0 |
| Interrupciones de continuidad WS / errores WS | 0 / 0 | 0 / 0 | 0 / 0 |
| Operaciones fallidas / mensajes inválidos | 0 / 0 | 0 / 0 | 0 / 0 |
| Timeouts registrados | 0 | 0 | 0 |
| Máxima edad exchange L2 muestreada, ms | 5532 | 5984 | 5822 |
| Máxima edad de recepción L2 muestreada, ms | 5077 | 5461 | 5233 |
| CPU hasta final de captura, s | 4.665 | 9.802 | 11.516 |
| CPU de captura / pared, % de un núcleo | 7.65 | 16.10 | 18.87 |
| CPU total del experimento, s | 5.249 | 11.026 | 13.028 |
| CPU user / system del proceso completo, s | 5.312 / 0.149 | 11.006 / 0.216 | 13.019 / 0.251 |
| RSS actual / pico al final de captura, MiB | 35.574 / 35.574 | 38.508 / 38.508 | 41.426 / 41.426 |
| RSS actual / pico final del proceso, MiB | 35.836 / 35.836 | 39.039 / 39.039 | 42.086 / 42.086 |
| SQLite / WAL antes de cierre, bytes | 610304 / 350232 | 1236992 / 457352 | 1683456 / 453232 |
| SQLite / WAL pico muestreado, bytes | 610304 / 350232 | 1236992 / 457352 | 1683456 / 453232 |
| SQLite / WAL después del cierre, bytes | 610304 / 0 | 1236992 / 0 | 1683456 / 0 |
| Filas journal / checkpoints actuales | 1027 / 5 | 2129 / 10 | 3023 / 20 |
| Payload de checkpoints actuales, bytes UTF-8 | 27888 | 49254 | 64835 |
| Transacciones comprometidas | 200 | 248 | 216 |
| Drenaje terminal / cierre socket, s | 0.028 / 0.312 | 0.043 / 0.231 | 0.095 / 0.243 |
| Cierre SQLite incluyendo checkpoint WAL, s | 0.005677 | 0.000609 | 0.000594 |
| Verificación / recuperación con verificación, s | 0.122 / 0.215 | 0.242 / 0.520 | 0.280 / 0.515 |
| Reapertura + segunda recuperación + cierre, s | 0.224 | 0.489 | 0.645 |
| Integridad / recuperación / reapertura | verified / True / True | verified / True / True | verified / True / True |
| Pendientes abandonados por cierre normal | 10 | 66 | 50 |
| Trades fuera de segmento activo / bootstrap | 90 / 63 | 288 / 44 | 512 / 142 |
| Trades agregados en segundos cerrados | 549 | 1111 | 1079 |

Los trades únicos incluyen el bootstrap recibido al suscribir y eventos anteriores al comienzo conservador del segmento observado. La tasa anterior no representa exclusivamente trades nuevos durante los 60 s. Las capturas fueron consecutivas: el tráfico de BTC/ETH/HYPE cambió, de modo que 20 activos no equivalen a duplicar la carga de 10.

### Tiempos locales de procesamiento

| Medición | 5 activos | 10 activos | 20 activos |
|---|---:|---:|---:|
| Handler WS, p50 / p95 / max, ms | 0.18 / 0.77 / 7.66 | 0.18 / 0.78 / 11.95 | 0.23 / 0.83 / 5.06 |
| FlowStore.write, p50 / p95 / max, ms | 20.86 / 49.31 / 120.44 | 37.48 / 81.97 / 191.38 | 45.25 / 90.28 / 200.58 |
| Sync, p50 / p95 / max, ms | 0.11 / 33.13 / 121.07 | 9.88 / 61.73 / 193.32 | 0.30 / 74.17 / 201.45 |
| Lectura/publicación de todos los activos, p50 / p95 / max, ms | 7.40 / 28.44 / 79.15 | 13.79 / 22.62 / 57.95 | 24.67 / 44.16 / 73.18 |
| Intervalo entre sync, p50 / p95 / max, ms | 101.60 / 135.44 / 250.43 | 117.58 / 167.26 / 295.35 | 104.02 / 181.32 / 312.04 |
| Recepción local normalizada → retorno de commit, p50 / p95 / max, ms | 76 / 135 / 223 | 113 / 208 / 292 | 130 / 199 / 286 |
| Tiempo acumulado write / sync / lecturas, s | 4.790 / 4.856 / 0.596 | 10.197 / 10.303 / 0.873 | 10.954 / 11.032 / 1.531 |
| Trades / segundo de pared dentro de write | 148.64 | 147.98 | 162.77 |

Write incluye backup/export de estado, validaciones, hashes, SQL y COMMIT FULL; está contenido en sync. No sumar ambas duraciones. Las lecturas incluyen métricas L2, copias/inmutabilidad y vista de flujo; no son latencia por evento. El cociente trades/tiempo write describe esta carga, no el techo de SQLite. Los percentiles conservan todas las observaciones de estas capturas; ningún buffer de muestras alcanzó su límite de 100000.

**NO DISPONIBLE:** latencia exchange→recepción de socket, espera en buffers socket→handler y latencia por trade commit→publicación. No se estiman. La edad exchange del libro es una métrica de frescura, no una medida de latencia de red; la edad de recepción es distinta. Recepción→commit usa dos lecturas del reloj local en milisegundos, no sincronización con el exchange.

## 3. Resultados por activo

C/P/U = complete/partial/unknown; bandas ordenadas ±0,10 / ±0,25 / ±0,50 / ±1,00 %. La cobertura de la tabla es la última vista muestreada, relativa al rango agregado visible, no al libro entero. `Fresco` cuenta muestras con quality=ok; todas las degradadas fueron missing_book durante inicialización. No hubo muestras stale/future tras disponer del libro.

### 5 activos

Inicio UTC 2026-10-09T17:23:35.348986+00:00; fin con auditoría 2026-10-09T17:24:36.972035+00:00. Carga inicial del host: `1.98 1.69 1.43 2/1377 27354`.

| Activo | Trades únicos | L2 | Edad exchange / recepción máx., ms | Fresco / muestras | Bandas | Pendientes máx. | Cola terminal no agregada |
|---|---:|---:|---:|---:|---|---:|---:|
| BTC | 314 | 12 | 5532 / 5077 | 55 / 57 | P / P / P / P | 46 | 3 |
| ETH | 138 | 12 | 5532 / 5077 | 55 / 57 | P / P / P / P | 16 | 3 |
| HYPE | 114 | 12 | 5532 / 5076 | 55 / 57 | P / P / P / P | 27 | 3 |
| ZEC | 71 | 12 | 5532 / 5076 | 55 / 57 | C / P / P / P | 5 | 1 |
| SOL | 75 | 12 | 5532 / 5076 | 55 / 57 | C / P / P / P | 19 | 0 |

Por activo: duplicados, conflictos, out-of-order, descartes WS, pending_overflow y gaps internos de flujo fueron cero. Hubo un cierre capture_end por activo, explícito y esperado. Contadores completos, motivos temporales y distribución de cobertura de todas las muestras se conservan en el JSON enlazado al final.

### 10 activos

Inicio UTC 2026-10-09T17:25:20.592878+00:00; fin con auditoría 2026-10-09T17:26:22.810111+00:00. Carga inicial del host: `1.86 1.72 1.47 2/1346 27491`.

| Activo | Trades únicos | L2 | Edad exchange / recepción máx., ms | Fresco / muestras | Bandas | Pendientes máx. | Cola terminal no agregada |
|---|---:|---:|---:|---:|---|---:|---:|
| BTC | 196 | 12 | 5984 / 5461 | 55 / 57 | P / P / P / P | 35 | 11 |
| ETH | 198 | 12 | 5984 / 5461 | 55 / 57 | P / P / P / P | 44 | 12 |
| HYPE | 215 | 12 | 5984 / 5460 | 55 / 57 | P / P / P / P | 61 | 14 |
| ZEC | 57 | 12 | 5984 / 5317 | 55 / 57 | C / P / P / P | 4 | 4 |
| SOL | 135 | 12 | 5984 / 5459 | 55 / 57 | C / P / P / P | 11 | 11 |
| NEAR | 166 | 12 | 5984 / 5460 | 55 / 57 | P / P / P / P | 49 | 9 |
| PUMP | 200 | 12 | 5984 / 5459 | 55 / 57 | C / C / P / P | 41 | 2 |
| STRK | 170 | 12 | 5984 / 5459 | 55 / 57 | C / C / P / P | 76 | 0 |
| XRP | 44 | 12 | 5984 / 5317 | 55 / 57 | C / P / P / P | 8 | 1 |
| ENA | 128 | 12 | 5984 / 5461 | 55 / 57 | P / P / P / P | 27 | 2 |

Por activo: duplicados, conflictos, out-of-order, descartes WS, pending_overflow y gaps internos de flujo fueron cero. Hubo un cierre capture_end por activo, explícito y esperado. Contadores completos, motivos temporales y distribución de cobertura de todas las muestras se conservan en el JSON enlazado al final.

### 20 activos

Inicio UTC 2026-10-09T17:26:56.101693+00:00; fin con auditoría 2026-10-09T17:27:58.751140+00:00. Carga inicial del host: `2.45 1.88 1.55 2/1366 27680`.

| Activo | Trades únicos | L2 | Edad exchange / recepción máx., ms | Fresco / muestras | Bandas | Pendientes máx. | Cola terminal no agregada |
|---|---:|---:|---:|---:|---|---:|---:|
| BTC | 153 | 12 | 5822 / 5233 | 54 / 57 | P / P / P / P | 17 | 1 |
| ETH | 106 | 12 | 5822 / 5232 | 54 / 57 | P / P / P / P | 16 | 1 |
| HYPE | 64 | 12 | 5822 / 5231 | 54 / 57 | P / P / P / P | 7 | 3 |
| ZEC | 52 | 12 | 5822 / 4999 | 54 / 57 | C / P / P / P | 4 | 1 |
| SOL | 57 | 12 | 5822 / 5017 | 54 / 57 | C / P / P / P | 7 | 1 |
| NEAR | 171 | 12 | 5822 / 5211 | 54 / 57 | P / P / P / P | 103 | 2 |
| PUMP | 231 | 12 | 5822 / 5022 | 54 / 57 | C / C / P / P | 105 | 0 |
| STRK | 260 | 12 | 5822 / 5012 | 54 / 57 | C / C / P / P | 51 | 10 |
| XRP | 39 | 12 | 5822 / 5000 | 54 / 57 | C / P / P / P | 2 | 0 |
| ENA | 147 | 12 | 5822 / 5232 | 54 / 57 | P / P / P / P | 103 | 0 |
| LIT | 64 | 12 | 5822 / 5230 | 54 / 57 | P / P / P / P | 8 | 3 |
| UNI | 38 | 12 | 5822 / 5007 | 54 / 57 | P / P / P / P | 2 | 2 |
| ZRO | 70 | 12 | 5822 / 4998 | 54 / 57 | C / P / P / P | 23 | 23 |
| VVV | 69 | 12 | 5822 / 5003 | 54 / 57 | C / P / P / P | 6 | 0 |
| WLD | 57 | 12 | 5822 / 5002 | 54 / 57 | P / P / P / P | 8 | 1 |
| SUI | 55 | 12 | 5822 / 5008 | 54 / 57 | C / P / P / P | 18 | 1 |
| ONDO | 33 | 12 | 5822 / 5211 | 54 / 57 | P / P / P / P | 2 | 0 |
| DOGE | 33 | 12 | 5822 / 5232 | 54 / 57 | P / P / P / P | 2 | 1 |
| XMR | 34 | 12 | 5822 / 5001 | 54 / 57 | P / P / P / P | 4 | 0 |
| FARTCOIN | 50 | 12 | 5822 / 5231 | 54 / 57 | C / P / P / P | 19 | 0 |

Por activo: duplicados, conflictos, out-of-order, descartes WS, pending_overflow y gaps internos de flujo fueron cero. Hubo un cierre capture_end por activo, explícito y esperado. Contadores completos, motivos temporales y distribución de cobertura de todas las muestras se conservan en el JSON enlazado al final.

### Calidad temporal, espacial y del tape

Los 12 snapshots L2 por activo observados en cada minuto equivalen a una cadencia media de ~0,2 snapshots/s. No se certifica refresco de 0,5 s: la especificación del feed y el tráfico efectivo son diferentes. Los máximos de edad son muestreados a ~1 Hz; un pico intermedio puede no estar registrado.

Un libro temporalmente fresco puede tener todas sus bandas parciales. En esos casos se conserva OBI observado, pero OBI válido es null. No se etiquetó como completo el libro entero por recibir 20 niveles.

El cierre normal usa la convención existente: al desconectar se abandona el buffer de reordenación pendiente sin añadirlo a segundos cerrados. Esos trades siguen en journal/observed_total y su cantidad está arriba; no equivalen a overflow ni pérdida silenciosa de identidades. El bootstrap y los eventos fuera del segmento activo tampoco se incluyen como tape continuo certificado.

Mainnet mantuvo side_semantics=unverified y Delta/CVD/buy/sell firmados null. Las ventanas 5/15 min siguen en warmup; 60 s tampoco certifican una ventana 1 min utilizable tras margen de reordenación y cierre. No se interpretan métricas firmadas reales.

## 4. Integridad y ausencia de pérdidas locales no explicadas

En cada base: quick_check, checksums del journal, secuencia/manifest, cursores y anchors de checkpoints verificados mediante FlowStore.verify. Recuperación exacta de todos los estados exportados sin cache de IDs, primero en la conexión original y luego tras cerrar físicamente y reabrir la base. Se conservaron los PRAGMA y el manifest de configuración.

Para los tres escenarios se cumple: WS únicos = consumidos = operaciones trade comprometidas = observed_total = filas trade retenidas. Cola final=0, queue_dropped=0, retired_through_seq=0. Sin dedup evictions, errores de integridad, fallos de operaciones ni timeouts. No se encontró pérdida silenciosa en la ruta local aceptada; sin secuencias del exchange, esto no demuestra completitud absoluta del mercado.

Todos los hashes de `python/src/*.py` coinciden antes/después y entre escenarios. No se modificaron producción, scanner, pipeline, consenso, reglas, SQLite o dependencias. No hubo reaprovechamiento ni borrado de bases.

## 5. Cuellos de botella y limitaciones

La mayor ocupación síncrona medida corresponde a FlowStore.write: ~99 % del tiempo de sync. Ese tramo abarca trabajo Python de estado/serialización/integridad y SQLite; no se instrumentó SQL o fsync por separado y no puede atribuirse todo al motor SQLite. El p95 de write crece al aumentar activos; el event loop se bloquea mientras escribe.

Las lecturas de vistas también crecen con el grupo, aunque su tiempo acumulado fue menor. La cola máxima de 555/8192 eventos (6,77 %) y los pendientes observados quedaron lejos de los umbrales de parada. No se observó saturación importante bajo esta carga.

Una repetición por tamaño, activos anidados pero tráfico diferente, solo 60 s y bootstrap inicial: no es una prueba de capacidad constante ni de picos fuertes. No hay control de CPU/frecuencia, red, congestión o procesos ajenos. Los wrappers y lecturas auxiliares añaden overhead; no se usó tracemalloc. El RSS incluye intérprete, SQLite, buffers e instrumentación; VmHWM es el pico del propio proceso, también durante recuperación.

En un minuto no se llenan 960 buckets, caches de 100000 IDs por activo, retención de 24 h o cuota del journal. Es posible que memoria/coste por transacción aumenten después. La cola reportada no incluye buffers TCP ni la cola interna de websockets. Tamaño de base/WAL y edades son máximos muestreados; el pico RSS sí es kernel VmHWM. Publicación/handler/commit se distinguen y no se usan sus tiempos como latencia de red.

## 6. Viabilidad

La implementación actual pudo monitorizar 5, 10 y 20 perpetuos simultáneamente en este host, con una conexión y durabilidad FULL, a las tasas recibidas. Los tres escenarios preservaron journal, checkpoints y recuperación sin pérdidas locales no explicadas. El pico final de 20 activos fue ~42,09 MiB y su CPU de captura equivalió al ~18,87 % de un núcleo en esa ventana.

Es evidencia favorable para monitorización ligera de corto plazo. No permite asegurar estabilidad 24/7, capacidad máxima de trades/s, precisión continua subsegundo o autonomía/temperatura en un teléfono. No justifica cambiar SQLite ni relajar durabilidad.

## 7. Recomendaciones para una futura implementación ligera en Termux

1. Mantener un solo proceso de monitorización, una conexión compartida y un LiveStore. Empezar con 5 activos en el teléfono objetivo y repetir escalada con tráfico fuerte y sesiones largas antes de habilitar 20 permanentemente. No ejecutar scanner o modelos dentro del proceso de captura.
2. Publicar snapshots desacoplados a ~1 Hz y calcular métricas espaciales solo cuando cambia el libro; actualizar frescura aparte. Coalescer únicamente vistas L2, con contador explícito; nunca descartar trades para acelerar el render.
3. Medir/profilar exportación, JSON y hashes de checkpoints antes de sustituir SQLite. Explorar por separado batching acotado por tiempo/operaciones y un único writer dueño de estado/conexión, preservando FULL, atomicidad, rollback y publicación tras commit. No se implementó esa optimización aquí.
4. Fijar un presupuesto de memoria y retención basado en el dispositivo y en cargas prolongadas. Cualquier reducción de caches debe conservar deduplicación/retención y declarar gaps cuando ya no pueda demostrarse continuidad. No extrapolar el RSS del primer minuto a horas de servicio.
5. Probar pantalla apagada, suspensión, cambio Wi-Fi/datos, reconexión y reinicio. Termux documenta restricciones de procesos/CPU en Android y la necesidad de revisar optimización de batería; no basta con que el ensayo desktop pase. [README oficial de Termux](https://github.com/termux/termux-app/blob/master/README.md). No se cambiaron ajustes del teléfono ni permisos del host.
6. Mantener telemetría de overflow, edad exchange/recepción, epochs, cuotas y auditoría de recuperación. Calidad L2, cobertura espacial y continuidad del tape deben permanecer separadas; side=A/B seguirá bloqueando signos hasta verificación independiente.

## Evidencia y reproducción

Directorio conservado e ignorado: `data/order_flow/multi_asset_benchmark_1zrhuaxz`, relativo a la raíz del repositorio. No contiene la base operativa. Los JSON contienen métricas y vistas normalizadas públicas, sin paquetes crudos, users, direcciones ni credenciales.

Estas evidencias son archivos locales, no distribuidos en GitHub. La tabla conserva sus nombres y hashes para identificar el ensayo histórico; no son enlaces ni dependencias para leer este informe. Los scripts publicados permiten realizar un nuevo ensayo y obtener evidencias propias en un directorio ignorado.

| Archivo de evidencia | SHA-256 |
|---|---|
| `selection.json` | `ec047e920bf5e4dbc7578d75480c993aaa6c8127ba6669f361fead4a9b83a598` |
| `scenario_05.json` | `da1c71ed53515cbc6bd0f2d2e68096e8b30a1476549577ca9ffb328dcb0f6650` |
| `scenario_10.json` | `1233b2aa9e2d0bb7c600afb1ff9bbc56af7e6c1e70a2de13f83c4c4ab2cc99ff` |
| `scenario_20.json` | `b14b6a2f3f7742a05463e211822b0cc408bf279f582f5ff9540b2da0182313a4` |

Los hashes de producción y configuración exacta están dentro de cada JSON. SHA-256 del harness utilizado: `492d6e9af1e4283582001d46f6df1d07f36b79f5a465e0ffbf8ce361505129f8`.

Para repetir, desde la raíz canónica; select crea otro directorio exclusivo y devuelve su ruta. Sustituir `<nueva-seleccion>` por el nuevo selection.json y comprobar status/integridad antes de cada escalada:

```bash
python/.venv/bin/python -B benchmarks/multi_asset_benchmark.py select
python/.venv/bin/python -B benchmarks/multi_asset_benchmark.py capture --selection <nueva-seleccion> --size 5 --seconds 60
python/.venv/bin/python -B benchmarks/multi_asset_benchmark.py capture --selection <nueva-seleccion> --size 10 --seconds 60
python/.venv/bin/python -B benchmarks/multi_asset_benchmark.py capture --selection <nueva-seleccion> --size 20 --seconds 60
```

No ejecutar los tres comandos de captura en paralelo ni repetirlos sobre los archivos existentes. Si un escenario devuelve stop_escalation, conservar sus artefactos y no iniciar tamaños mayores. El generador `benchmarks/report_multi_asset_benchmark.py` reconstruye este documento desde esos JSON y rechaza sobrescribir un informe existente.
