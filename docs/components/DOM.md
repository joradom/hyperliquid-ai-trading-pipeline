# DOM interactivo de consola — FASE 4

Monitor foreground READ-ONLY de datos públicos mainnet/testnet. Implementación:
[`dom.py`](../../python/src/dom.py). No usa cuentas, wallets, firmas, órdenes,
scanner ni modelos. No interpreta OBI como señal de compra/venta.

## Instalar y abrir

Desde la raíz del repositorio, independientemente de dónde se haya clonado:

```bash
cd python
.venv/bin/python -m pip install -r requirements-dom.txt
.venv/bin/python -m src.dom ETH
```

La dependencia opcional de presentación es Rich 15.0.0. El archivo incluye
`requirements-ws.txt`; no cambia el rango WS ni requiere actualizar websockets
10.4 ya instalado. Los motores de mercado siguen sin depender de Rich.
`--help` funciona sin la dependencia; si falta, el DOM informa el comando de
instalación antes de abrir una base o conectar al WS.

Más opciones:

```bash
.venv/bin/python -m src.dom ETH HYPE SOL
.venv/bin/python -m src.dom ETH HYPE --network testnet
.venv/bin/python -m src.dom ETH HYPE --levels 15 --refresh 2
.venv/bin/python -m src.dom ETH HYPE --seconds 15 --no-color
```

`--levels`: 1–20, inicialmente 10 por lado. `--refresh`: objetivo visual
0,5–20 Hz, inicialmente 4. `--seconds` permite una captura pública corta con
cierre automático; su presupuesto comienza tras la primera conexión, como en
PublicWSClient, e incluye la espera de ACKs. Los símbolos conservan la grafía
que acepta el cliente existente; no se consulta un catálogo ni una cuenta.

## Controles

| Tecla | Acción |
|---|---|
| Q | detener captura, drenar la cola aceptada, verificar/recuperar y cerrar |
| P | pausar/reanudar solamente la presentación |
| + / - | aumentar/reducir niveles solicitados, hasta 20 o mínimo 1 |
| Tab | siguiente activo de la lista, con vuelta al primero |
| H / ? | abrir/cerrar ayuda de abreviaturas, calidad y motivos de gap disponibles |
| Ctrl-C | cancelar y cerrar socket, renderer, teclado y SQLite |

No hace falta Enter en terminales POSIX interactivas. La entrada usa cbreak de
la biblioteca estándar, conserva señales Ctrl-C y restaura termios al salir.
No se añade una biblioteca de teclado. `=` también aumenta niveles y Ctrl-D
equivale a salir.

P congela valores, edades y hora de la vista mostrada: el rótulo **PAUSED** deja
claro que no describe actualidad. Ingestión, heartbeat, journal y cálculos
siguen activos. Cambiar niveles durante pausa reutiliza esa vista congelada;
Tab toma una vista puntual del nuevo activo y la congela. Reanudar obtiene una
vista actual. Cambiar activo no abre conexiones ni cambia suscripciones.

Sin stdin TTY o soporte POSIX se desactivan esas teclas y se muestra el aviso;
se puede salir con Ctrl-C o usar `--seconds`. En salida redirigida Rich conserva
una vista final legible; no se promete una pantalla interactiva ni 4 Hz al
consumidor de un pipe. `--no-color` y la detección habitual de Rich permiten
terminales sin colores.

Un TTY con `TERM=dumb` se rechaza antes de conectar, con una explicación; Rich
no puede dibujar su pantalla interactiva en ese tipo de terminal. Si el emulador
sí soporta ANSI pero declara ese valor, se puede lanzar temporalmente con
`TERM=xterm-256color .venv/bin/python -m src.dom ETH`. No se cambia configuración
del shell o del usuario. Una salida redirigida mantiene el modo de vista final.

## Arquitectura y propiedad de recursos

```text
Una instancia foreground
  PublicWSClient (una conexión, trades+l2Book para toda la lista)
    → tarea de ingestión: LiveStore.sync_ws, sleep 0,1 s
    → FlowStore + OrderFlow actuales, sin nuevos parámetros
    → vistas LiveStore.read / read_l2 + diagnóstico WS congelado
    → DOMFrame profundamente inmutable
    → reloj independiente de presentación
    → un worker Rich, sin acceso al LiveStore ni a SQLite
```

El event loop foreground es el único dueño de client/live/journal. Renderer y
controles no deduplican trades, calculan OBI, acumulan Delta/CVD, suman nocionales
o profundidad, ni deciden frescura. Las métricas proceden de Order Flow y
Order Book a través de las vistas existentes. El diagnóstico de conexión WS
se copia y congela antes de enviarse al renderer.

Rich Live usa `auto_refresh=False`: el reloj de la aplicación publica frames
y el worker dibuja con el tamaño actual del terminal. Solo puede haber un
dibujo en curso: no crece una cola de frames si el terminal es lento. El trabajo
de render/IO del terminal no se ejecuta en el bucle de recepción. Una pantalla
pausada no solicita nuevos frames automáticamente.

El objetivo de 4 Hz es de presentación, no de llegada de datos ni una garantía
de latencia. `sync_ws`, SQLite y la construcción de vistas mantienen su trabajo
síncrono existente en el dueño foreground; bajo carga fuerte pueden retrasar
el event loop. FASE 4 no introduce optimizaciones ni cambia durabilidad para
evitar ese límite.

## Pantalla y adaptación

Encabezado: símbolo/posición en la lista, READ-ONLY, hora UTC de evaluación,
estado WS, connection_epoch y estado temporal L2, edades exchange/recepción
y continuidad del tape. BBO, midpoint y spread absoluto/bps vienen de
`read_l2`. Se mantienen separados L2 FRESH/STALE/DEGRADED, TAPE GAP y conexión.

Libro: bids/asks alineados desde el mejor precio hacia fuera, precio, base,
profundidad acumulada base y barras. En una terminal amplia se añade nocional
quote por nivel; a partir de 128 columnas se añade acumulado quote. El ancho
útil se limita a 136 columnas incluso en terminales mayores. Las tablas no se
expanden innecesariamente y se usan colores más discretos. Las barras
se escalan al máximo tamaño base entre **los niveles actualmente visibles**,
solo como presentación; no cambian ninguna métrica.

OBI legacy se etiqueta como observado/válido sobre todos los niveles retornados,
no sobre las filas visibles ni sobre el libro entero. Las cuatro bandas muestran
su cobertura y OBI observado/válido. En formato compacto C/P/U significa
complete/partial/unknown; por debajo de 80 columnas se oculta primero el OBI
observado de cada banda, conservando cobertura y valor válido. El observado
legacy permanece visible. Todos los OBI se muestran como porcentaje con dos
decimales (`ratio × 100`); un valor ausente permanece `—`. Los datos internos
siguen siendo ratios originales. Cantidades se formatean hasta cuatro decimales,
nocionales/bps hasta dos, y los sufijos k/M/G con dos; valores diminutos usan
notación científica para no convertirse visualmente en cero. Precios, midpoint
y spread absoluto no reciben redondeo adicional. La precisión del motor no se
modifica.

H/? abre ayuda temporal sin pausar la ingestión. Explica READY, PARTIAL, WARMUP,
GAP, UNVERIFIED, C/P/U, obs/val, base/quote, Σ y k/M/G; muestra los motivos de
gap ya presentes en el diagnóstico WS y la vista Flow. La ayuda no ocupa filas
permanentes salvo el indicador H/? del pie. Un GAP histórico puede coexistir
con WS LIVE; no se cambia ninguna regla de continuidad o calidad al presentarlo.

Flujo: trades y volumen base/quote observados, y ventanas 1/5/15 min. Una ventana
no utilizable se marca WARMUP/PARTIAL/STALE/GAP; sus conteos parciales se etiquetan
`obs`, nunca se presentan como valores completos. Los totales observados pueden
incluir bootstrap o eventos fuera del segmento activo, según el contrato actual.

Mainnet/testnet de cálculo 2.0 y semántica 1.0 muestran **AGGRESSOR A/B**:
B comprador y A vendedor agresor, según el protocolo, sin inferirlos mediante L2.
La cabecera identifica la red; mainnet sigue siendo el default. Delta/CVD de
ventana exige `ready`, `signed_available` y contacto live/fresco. El CVD acumulado
exige también `cvd.available`, muestra el segmento y la edad del contacto; no
cruza gaps ni muestra valores recuperados/stale como actuales. En vista amplia
se muestra la cobertura temporal por ventana, que no certifica todos los trades
del exchange. No se duplican cálculos: render consume las métricas del motor.

Checkpoints 1.0 reales siguen mostrando **UNVERIFIED** y valores firmados ausentes.
Las vistas synthetic conservan su etiqueta y convención verificadas. Una versión
o semántica desconocida no habilita valores firmados. No existe un flag de UI
para forzar signos ni convertir historia unsigned en firmada.

Las filas visibles se reducen si la altura no alcanza para los niveles solicitados.
El pie indica visibles/solicitados. Con menos de 42 columnas o 14 filas se cambia
a un resumen mínimo: conexión, calidad, UNVERIFIED, BBO/spread, observados, OBI
legacy y controles. En ese modo se oculta el libro detallado y las ventanas.
Para ver inicialmente 10 niveles por lado y ventanas, se recomienda al menos
80×24; para añadir nocionales y tabla completa de bandas, 110×30 o más.
Los valores ausentes siempre son `—`, no cero ni precios de ejemplo.

## Bases nuevas y cierre

Cada ejecución crea exclusivamente
`data/order_flow/dom_runs/dom_<id>/flow.sqlite3`, ignorado por Git, y muestra su
ruta antes de abrir pantalla. No se reutiliza la base operativa, una captura
anterior ni un benchmark. No se eliminan bases al salir.

`--db` permite indicar una ruta **nueva** dentro de `data/order_flow/`; rechaza
archivos existentes o sidecars WAL/SHM. No es una opción de recuperación de una
base previa. SQLite conserva WAL, FULL, autocheckpoint y límites originales.
No se almacenan libros L2 en SQLite.

Q, vencimiento de `--seconds` y Ctrl-C paran el WS, cancelan las tareas de UI,
drenan trades ya aceptados según la convención existente y verifican journal,
checkpoints y coincidencia de recuperación. Se cierra Rich, se restaura el
terminal y se cierra SQLite. Como en la captura actual, el cierre desconecta
el segmento y registra los pendientes no agregados del margen de reordenación;
sus trades normalizados permanecen en journal/observed_total.

Un fallo de ingestión/escritura detiene la sesión; no se reintenta, reduce
durabilidad ni oculta el fallo descartando la cola. La base se conserva para
inspección. Un cierre previo a cualquier transacción indica SIN DATOS en lugar
de fabricar un estado de mercado recuperado.

## Verificación

Desde `python/`:

```bash
.venv/bin/python -B -m unittest discover -s tests -p test_dom.py -v
.venv/bin/python -B -m unittest discover -s tests
```

[`test_dom.py`](../../python/tests/test_dom.py) usa fixtures públicas sintéticas,
reloj simulado, socket inyectado y bases temporales. Comprueba null/degradación,
pantallas estrechas sin colores, tamaños visibles, ratios suministrados sin
recalcularlos, inmutabilidad, cambio de activo, pausa sin detener el journal,
renderer lento, pausa durante un dibujo en curso, cancelación, fallo de escritura,
protección de bases anteriores y restauración de teclado mediante PTY offline.
Los tests de presentación se omiten si falta Rich; para la suite completa FASE 4
hay que instalar las dependencias opcionales.

Verificación final: **279 tests, OK en 21,640 s**, los 253 anteriores más
26 nuevos de FASE 4; `pip check` sin incompatibilidades. Los hashes de los
motores anteriores y de sus dependencias/configuración permanecieron iguales.

Prueba manual pública ETH+HYPE, con Rich 15.0.0 y websockets 10.4: conexión LIVE
epoch 1, precios/volúmenes reales y UNVERIFIED; Tab cambió a HYPE, + mostró
11 niveles y P congeló la vista. Durante pausa, el journal pasó de 119 a
135 filas y los trades persistidos de 81 a 85. Tras reanudar y solicitar Q,
el cierre registró 90 trades observados y recuperación OK, salida 0. Base nueva
conservada: `data/order_flow/dom_runs/dom_2jhxb56k/flow.sqlite3`.
La prueba visual con color y cierre automático también terminó correctamente,
con otra base nueva; no se reutilizó ninguna base anterior. Ctrl-C/cancelación
y restauración termios se comprobaron con tests offline y PTY sintético.

No cambia scanner, Analysis Pipeline, consenso, contratos de decisión, reglas,
Order Flow, Order Book, Live Store o SQLite. No hay IPC, web, daemon o acciones
financieras. Las pruebas live son exclusivamente públicas y no usan fixtures.

La sesión prolongada ETH/HYPE de `dom_rcn_ujuo` se revisó posteriormente sin
modificar su base. El [diagnóstico TAPE GAP](../DOM_TAPE_GAP_DIAGNOSIS.md) documenta
la caducidad de IDs a 1200 s, los reinicios de segmento y la diferencia entre
acumulados de sesión y `obs` de ventanas. El pulido visual no corrige esa lógica.

Verificación del pulido: **285 tests, OK en 24,963 s**, con seis pruebas nuevas
de porcentaje, ayuda/ingestión, ancho útil y estados; los datos internos de las
vistas permanecen intactos. La base `dom_rcn_ujuo` conserva su SHA-256 original.
