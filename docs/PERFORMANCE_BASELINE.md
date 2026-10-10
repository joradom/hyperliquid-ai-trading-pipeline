# R102 — Baseline de rendimiento Order Flow / SQLite

Diagnóstico, 2026-10-08. **Hay margen con SQLite: el coste dominante observado
está en representar, copiar y verificar estado Python por transacción, no en
insertar filas.** Los ~222 trades/s de FASE 2 eran una medición instrumentada,
no un límite estable de SQLite. No se implementó ninguna optimización.

## Metodología y entorno

- Python 3.12.3, SQLite 3.45.1, Linux x86_64; CPU reportada Intel i7-5600U
  2.60 GHz, 4 CPU lógicas. Bases temporales en `/tmp`, filesystem ext4.
- Entorno compartido, sin afinidad ni frecuencia fija; no se detuvieron otros
  procesos. Load average inicial: 6.43/5.17/3.67. Las muestras presentan
  variación importante; no son una capacidad garantizada de producción.
- Cada ejecución usa exactamente 20000 trades sintéticos únicos, 10000 de
  ETH y 10000 de HYPE, 20 trades/s de tiempo simulado durante 1000 s.
  Precio ETH=10, HYPE=20; tamaño=1; lados B/A alternados en parejas. No red,
  wallets, modelos ni órdenes. Misma cadencia de avance y watermark que
  `live_store.replay`: un avance por activo tras cada 256 trades y avance final.
- Se conservan el orden de operaciones y los cálculos de producción. Los
  lotes alternativos solo reagrupan esa secuencia en bases temporales.
  No se cambian validadores, hashes, rollback, SQL ni parámetros de durabilidad.
- SQLite: WAL, synchronous=FULL (2), wal_autocheckpoint=64, páginas=4096,
  quota principal=64 MiB, journal retenido=5000 filas, retención=24 h y
  cache RAM de identidades=512 por activo: **configuración del replay publicado**,
  no los defaults mayores del servicio live.
- Sin persistencia se retienen 10000 IDs por activo para deduplicar toda la
  carga sin provocar gaps artificiales por evicción. Esta diferencia de cache
  hace que la comparación de memoria no sea estrictamente uno-a-uno.
- Tres repeticiones por variante, procesos nuevos y bases nuevas; ejecución
  secuencial, sin benchmarks propios compitiendo entre sí. Los tiempos incluyen
  generación/validación, ingestión, snapshots, verificación/recuperación y cierre;
  excluyen arranque/imports del intérprete. CPU y pared se registran por separado.
- Memoria: VmHWM de `/proc/self/status`, máximo RSS del proceso actual, incluyendo
  intérprete/SQLite. No se usa ru_maxrss heredado del lanzador. Tracemalloc se
  mide aparte para no sesgar la tabla principal.

Todas las ejecuciones terminaron con 20000 trades contabilizados/cerrados,
ventanas 1/5/15m utilizables, CVD sintético=0 y mismo digest de los agregados:
`6afe9c86551c3f79eea0ce32b8149d528305c7398eb13b725fa197e2ffecb0e7`.
En SQLite se verificaron filas, manifest, checkpoints y recuperación exacta.
Ninguna comparación utiliza datos reales ni certifica side para mainnet.

## Comparativa sin instrumentación

Tiempo y CPU: medianas de 3 runs. Throughput=20000/mediana de tiempo total.
RSS: máximo de las tres observaciones, no memoria adicional sobre el intérprete.

| Variante | Transacciones | Tiempo total mediano, s | Rango, s | CPU mediana, s | Trades/s | Pico RSS, MiB |
|---|---:|---:|---:|---:|---:|---:|
| Python puro, sin persistencia | 0 | 0.929 | 0.719–2.106 | 0.916 | 21539 | 39.69 |
| SQLite actual: 256 trades + avances por lote | 80 | 16.419 | 10.819–21.660 | 13.412 | 1218 | 35.66 |
| SQLite, lotes de hasta 1000 operaciones | 22 | 11.572 | 5.706–12.789 | 10.110 | 1728 | 36.27 |
| SQLite, lotes de hasta 32 operaciones | 631 | 112.595 | 69.392–127.366 | 92.943 | 178 | 36.82 |

El límite alternativo cuenta **operaciones**, incluidos avances; el actual
agrupa 256 trades más 2 avances. La transacción inicial de conexión cuenta.
Los lotes actuales ya amortizan escrituras; no se partía de un COMMIT por trade.

Tiempos totales individuales, en orden de repetición:

| Variante | Run 1 | Run 2 | Run 3 |
|---|---:|---:|---:|
| Python puro | 0.719 | 0.929 | 2.106 |
| SQLite actual | 10.819 | 16.419 | 21.660 |
| SQLite 1000 ops | 5.706 | 12.789 | 11.572 |
| SQLite 32 ops | 69.392 | 127.366 | 112.595 |

Lotes de 1000 mejoraron el throughput un 42 % comparando medianas; las razones
de tiempo actual/1000 por repetición fueron 1.90×, 1.28× y 1.87×. No se suman
ni se extrapolan como una mejora fija. Con 32 ops aumentaron aproximadamente
7.9× las transacciones y mucho más el trabajo repetido de estados/checkpoints.

En la variante actual, la fase generación+ingestión mediana fue 13.399 s;
snapshots+verificación+recuperación, 2.974 s. En lotes 1000 fueron 9.065 y
2.468 s. Las medianas de fases no tienen por qué sumar la mediana total.
La diferencia RAM/SQLite incluye garantías y trabajo de aplicación: no mide
aisladamente la velocidad del motor SQLite.

## Perfil del coste relativo

Run separado con temporizadores temporales, sin tracemalloc: 14.878 s totales,
13.343 s CPU. Wrappers de observación delegaron las mismas llamadas y resultados,
restaurándose al terminar; no se editaron módulos. Se restó tiempo de llamadas
anidadas para evitar doble conteo. Este perfil añade overhead propio y atribuye
tiempo de pared, por lo que sus porcentajes son orientativos.

| Componente exclusivo | Segundos | % del total perfilado |
|---|---:|---:|
| Motor: OrderFlow.apply | 0.572 | 3.85 % |
| Validación de Operation, también durante recuperación | 0.967 | 6.50 % |
| Exportar/copiar backups y checkpoints | 3.625 | 24.36 % |
| Canonicalización para hashes: deepcopy+JSON+UTF-8 | 3.633 | 24.42 % |
| Digest SHA-256 y envoltura, sin canonicalización | 0.454 | 3.05 % |
| JSON de almacenamiento, encode | 0.681 | 4.58 % |
| JSON de almacenamiento, decode/validación | 0.651 | 4.38 % |
| INSERT de journal | 0.377 | 2.53 % |
| Escritura SQL de checkpoints de aplicación | 0.019 | 0.13 % |
| COMMIT con FULL | 1.090 | 7.33 % |
| Otros SQL, BEGIN y PRAGMA | 0.478 | 3.21 % |
| Otro Python, control, auditoría y harness | 2.331 | 15.67 % |

Vista derivada, **solapada con la tabla anterior**: materializar estado y
procesar hashes/JSON de checkpoints/control de transacción costó 6.469 s
(43.48 %). Incluye backups 2.256 s, export de checkpoints 1.369 s y
canonicalización de checkpoint/control 2.194 s, entre otros. No añadir este
43.48 % al resto de porcentajes. Por separado, canonicalizar el journal costó
0.840 s; canonicalizar documentos de auditoría final, 0.599 s.

Se observaron 80 COMMIT, 20160 INSERT de journal, 160 escrituras de checkpoints,
158 exports de backup, 164 exports sin cache y 31590 hashes. Cada transacción
estable repite aproximadamente dos exports por activo: antes y después.

### Qué significa FULL y qué checkpoint se midió

Se mantuvo FULL en todas las pruebas. En WAL, SQLite sincroniza el WAL al
confirmar cada transacción; no se probaron NORMAL/OFF ni se propusieron como
optimización. El tiempo de COMMIT incluye ejecución SQLite, sincronización,
esperas y potencial autocheckpoint; **no es una medición de fsync aislado**.
[SQLite: synchronous](https://www.sqlite.org/pragma.html#pragma_synchronous).

Un checkpoint de **aplicación** guarda el estado del activo en la tabla
checkpoints; su coste Python es distinto del pequeño INSERT SQL. Un checkpoint
**WAL** transfiere páginas a la base principal; puede ejecutarse dentro del
COMMIT que cruza el umbral y queda incluido en esa categoría.
[SQLite: WAL y checkpoints](https://www.sqlite.org/wal.html#performance_considerations).

## Efecto de tracemalloc y los ~222 trades/s

Run diagnóstico aparte, misma carga/configuración y sin wrappers de perfil:
**45.533 s, 439 trades/s**, CPU=43.905 s, pico Python trazado=6494398 bytes
(6.19 MiB), pico RSS=44.32 MiB. Recuperación e integridad correctas.
Frente a la mediana no instrumentada de 16.419 s supone aproximadamente
2.77× de tiempo, pero no es un ensayo en host exclusivo ni un factor universal.

No se reprodujeron exactamente los 90.249 s/~222 trades/s anteriores. La
instrumentación y el trabajo de objetos/copias explican una parte medible de
la penalización; carga/frecuencia/planificación del entorno y pequeñas
diferencias de harness impiden atribuir toda la diferencia a una sola causa.
La evidencia sí descarta usar 222 como techo demostrado de SQLite o como
throughput de producción. Las cifras publicadas deben distinguir ingestión,
auditoría final y medición de memoria.

## Hasta tres recomendaciones, por beneficio esperado y riesgo

1. **Batching adaptativo con límite de operaciones y de tiempo.** Beneficio
   demostrado en esta carga: 22 en vez de 80 transacciones, 42 % más throughput
   por medianas. Mantener FULL y journal+checkpoint en el mismo COMMIT; publicar
   o reconocer datos solo después. Riesgo medio: aumenta espera de confirmación,
   datos aún no confirmados y trabajo de rollback. A 20 trades/s llenar 1000
   operaciones puede tardar decenas de segundos: no adoptar ese lote sin un
   timeout corto y flush en stop/error. El benchmark no fija el timeout de live.
2. **Serializar documentos una vez y reutilizar bytes canónicos para SQL/hash.**
   Beneficio esperado medio-alto sobre canonicalización/encode repetidos;
   SHA-256 en sí solo representó ~3 %. Riesgo bajo-medio: debe conservar
   exactamente orden, UTF-8, separadores, rechazo de no finitos, reglas de
   integrity y checksums existentes. Exigir equivalencia byte a byte, tests
   de corrupción/Unicode/null y rollback; no eliminar verificaciones.
3. **Reducir la materialización repetida del estado de checkpoints/backups.**
   Beneficio potencial alto para estados grandes y lotes pequeños: exports
   consumieron ~24 %. Riesgo medio-alto: alias mutables, pérdida del backup,
   modificación de caches/pendientes y efectos de retención sobre otros activos.
   Explorar snapshots versionados/desacoplados o representación reutilizable,
   manteniendo comparación contra el checkpoint autorizado y rollback exacto
   de todos los activos que puedan cambiar. Requiere pruebas multi-activo,
   restart/crash/retención y memoria antes de aplicar.

Los beneficios potenciales 2/3 se solapan y **no son speedups medidos**; no
sumarlos ni prometer multiplicaciones. Todas las recomendaciones mantienen
SQLite y sus garantías; ninguna se implementó en esta intervención.

## Reproducibilidad y comprobaciones

Harness temporal: `/tmp/r102_sqlite_benchmark.py`, fuera de Git. Mientras exista,
desde `python/`:

```bash
python3 -B /tmp/r102_sqlite_benchmark.py ram
python3 -B /tmp/r102_sqlite_benchmark.py sqlite_current
python3 -B /tmp/r102_sqlite_benchmark.py sqlite_batch1000
python3 -B /tmp/r102_sqlite_benchmark.py sqlite_batch32
python3 -B /tmp/r102_sqlite_benchmark.py sqlite_current --profile
python3 -B /tmp/r102_sqlite_benchmark.py sqlite_current --tracemalloc
python3 -B -m unittest discover -s tests
```

Repetir las primeras cuatro tres veces, en ese orden, sin concurrencia propia.
El script genera fixtures deterministas, valida la salida semántica, usa una
base nueva por proceso y limpia únicamente su directorio temporal. Las
modificaciones de instrumentación son locales al proceso, no a archivos.

**Suite existente: 220 tests, OK.** No se añadieron dependencias, optimizaciones,
reglas, firmas o llamadas financieras. No se tocaron datos de ejecución reales.
Git conserva el trabajo anterior; el único archivo añadido al repositorio
por esta intervención es este informe. No commit/push ni FASE 3.

Hashes de producción iguales antes/después:

- order_flow.py: `feddef057d02bcdc8b312d9839011619dc2d89d15a86160dfac800bfcd962209`
- flow_store.py: `7c8462973a0dbf28378df070417163ef0decc8af3d46ac391a889ab733e45479`
- live_store.py: `849bc973d5647dd4ad40c9cde44d0a9a7a0b3c40412e6ee99560b303288db1f6`

Los hashes de scanner y analysis_pipeline también permanecen sin cambios.
La comparación no mide contención multi-writer, readers largos, fallo eléctrico,
latencia p99 ni el crecimiento del live con cache de 100000 IDs. Esas son
validaciones futuras, no motivos para cambiar de base de datos ahora.
