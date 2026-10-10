# R102 DOM — Diagnóstico TAPE GAP de ETH/HYPE

Base inspeccionada: `data/order_flow/dom_runs/dom_rcn_ujuo/flow.sqlite3`.
Lectura con SQLite `mode=ro&immutable=1`, después de comprobar ausencia de WAL.
Sin abrir FlowStore sobre esa ruta, conectar al WS o cambiar datos/configuración.

SHA-256 antes/después:
`b1b1520e6232ebf33df504b6fd7d81e994cd13a4df5b422496f78733db265e1f`.
Integridad, secuencia/manifest, checksums y anchors: verified; 12258 filas de
journal y 2 checkpoints. El periodo registrado es 2026-10-09 18:21:39.969 a
18:47:52.491 UTC: 26 min 12,522 s. Los 8298 trades normalizados permanecen
en los registros y en los acumulados.

## Causa identificada y evidencia

La caducidad de la caché de IDs WS dispara `dedup_horizon_expired` por cada
identidad expulsada. El TTL actual es 1200 s. `_expire_ids` llama a `_gap`,
que aumenta continuity_epoch del activo; Live Store observa ese cambio y
registra disconnect/connect con motivo genérico `ws_continuity_change`.
Es una frontera del segmento de flujo, no una desconexión física del socket.

Primer reinicio de ambos activos: **18:41:42.413 UTC**, 1202,444 s después
del primer evento registrado. El nuevo source_continuity es 33 para ETH y
34 para HYPE; source_epoch permanece 1. El instante encaja con el vencimiento
de IDs iniciales y el siguiente evento que activa su limpieza.

Se reprodujo en RAM la deduplicación del PublicWSClient existente con los
8298 trades del journal, usando sus tiempos locales de recepción como reloj
de replay y consumiendo inmediatamente su cola para no introducir overflow.
**Los 8298 source_continuity coinciden exactamente: cero discrepancias.**
El resultado es 419 expiraciones para ETH y 1489 para HYPE, todas con motivo
dedup_horizon_expired; reproduce los contadores finales de los checkpoints.
No se invocó run, un conector, REST ni una inferencia.

| Evidencia | ETH | HYPE |
|---|---:|---:|
| Trades observados/persistidos | 3094 | 5204 |
| Expiraciones de IDs reconstruidas | 419 | 1489 |
| Fronteras ws_continuity_change en el motor | 187 | 218 |
| Segmento final | 188 | 219 |
| Connection/source epoch de todos los trades | 1 | 1 |
| Trades clasificados accepted | 2652 | 4742 |
| Bootstrap | 404 | 462 |
| Fuera de época/segmento activo | 38 | 0 |
| Agregados en segundos cerrados, contador histórico | 2272 | 4580 |
| Pendientes abandonados al terminar segmentos | 380 | 162 |
| Cierre final capture_end, separado de los gaps | 1 | 1 |

Un sync puede agrupar varias expulsiones de IDs: por eso los 1908 incrementos
de continuidad no equivalen a 1908 pares disconnect/connect del motor.

No hay outcomes late, conflict, duplicate u overflow, ni motivos late_trade,
pending_overflow o stream_stale en el motor. Los trades y conexiones del
journal no muestran cambio de connection_epoch/reconexión. La reproducción
exacta no necesita ninguna interrupción WS adicional a la caducidad de IDs.

Hay trades con edad recepción−tiempo de evento de hasta 22,919 s en ETH y
6,252 s en HYPE; eso no es latencia de red medida ni equivale a un outcome late.
El motor clasifica eventos históricos/anteriores al segmento como bootstrap
o prior_epoch antes de aplicar su comprobación de tardanza.

## READY → WARMUP PARTIAL y obs 0

`OrderFlow._connect` inicia otro segment_id y vacía buckets, pendientes y el
agregado cerrado del segmento. Conserva `observed_total` y los contadores
históricos. La validez de cada ventana exige su duración completa dentro del
segmento continuo actual; cada reinicio vuelve a empezar ese periodo.

Un segundo replay en RAM de todas las operaciones del journal reproduce sus
outcomes sin discrepancias y termina con estados idénticos a ambos checkpoints.
Se comprobaron las ventanas alrededor del primer reinicio:

| Activo / momento | Acumulado observado | Segmento | 1m ready / obs | 5m ready / obs | 15m ready / obs |
|---|---:|---:|---|---|---|
| ETH antes | 2193 | 1 | true / 235 | true / 538 | true / 1832 |
| ETH después | 2195 | 2 | false / 0 | false / 0 | false / 0 |
| HYPE antes | 4467 | 1 | true / 172 | true / 624 | true / 3261 |
| HYPE después | 4472 | 2 | false / 0 | false / 0 | false / 0 |

Las expulsiones continuadas provocan segmentos muy cortos. Con margen de
reordenación de 2 s, muchos terminan antes de cerrar siquiera un bucket nuevo.
En los checkpoints finales hay cero buckets y el segmento más reciente duró
0,691 s: sus tres ventanas tienen cero trades parciales aunque crezca el total
de todos los segmentos. `obs 0` pertenece al segmento actual, no a la sesión.

Los 542 pendientes abandonados no se añadieron a segundos cerrados, pero sus
identidades normalizadas están en el journal/acumulados. La integridad SQLite
no certifica por sí misma continuidad del tape ni recupera ventanas válidas.
TAPE GAP también conserva el historial de interrupciones; no significa que
el socket esté desconectado en ese instante.

## Límites y alcance

El journal persiste la frontera genérica ws_continuity_change, no cada motivo
original del transporte, los paquetes L2 rechazados o un resumen completo de
errores WS. Por tanto no permite inventariar cualquier rechazo de mensaje que
no altere continuidad. La atribución al TTL se apoya en tiempo, código actual
y coincidencia exacta del replay, no en un log original de _gap por ID.

No se modificó TTL, tratamiento de gaps, ventanas, buckets, persistencia o
reglas de calidad. El problema de continuidad de sesiones prolongadas sigue
presente; corregirlo requeriría una tarea autorizada sobre el transporte/motores.
Este trabajo solo añade pulido y ayuda de presentación al DOM. H/? muestra los
motivos ya disponibles en las vistas WS/Flow de futuras sesiones, sin cambiarlos.
