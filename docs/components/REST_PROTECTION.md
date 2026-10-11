# Protección REST y contexto temporal

R102 regula las consultas públicas de `/info` en `hl_client`; no añade firmas,
wallets ni operaciones. La fórmula de `vol_delta` conserva sus dos bloques de
24 velas horarias cerradas. La presentación de sus ventanas queda pendiente.

## Presupuesto y alcance

`RESTCoordinator` comparte una ventana móvil ponderada entre los clientes de
un mismo origen API y proceso. Los defaults son 900 unidades/60s y hasta tres
peticiones HTTP en vuelo. Se permite utilizar el presupuesto disponible sin un espaciado fijo por petición.
La ventana, los compromisos y la concurrencia se comprueban bajo lock antes de
enviar; la espera y el transporte ocurren fuera del lock.

La [documentación oficial de límites](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/rate-limits-and-user-limits)
establece 1200 unidades REST/minuto por IP, base 2 para L2 y base 20 para
contexto, velas y funding. El recargo depende de elementos devueltos: bloques
de 60 para velas y de 20 para funding. R102 redondea hacia arriba por prudencia;
la página no explicita el redondeo de bloques incompletos.

Para velas se reserva una cota del rango solicitado, incluyendo margen para
extremos y limitando a 5000. Funding por rango reserva hasta 500 elementos;
userFills reserva hasta 2000. Estos límites de respuestas se describen en la
[Info API](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint).
Al recibir una respuesta válida, el contador se ajusta a la longitud real. Un intento fallido conserva su reserva.
Una respuesta inesperadamente mayor genera deuda que bloquea nuevas admisiones;
no puede deshacer una petición ya enviada ni garantiza una cuota externa.

Una captura completa hace siete peticiones y tiene base **122**:
20 de contexto + 80 de cuatro timeframes + 2 de L2 + 20 de funding. Con unas
204 velas por timeframe y 72 registros de funding, el estimado conservador es
**142**. La reserva preventiva puede ser mayor por el límite de funding.

El límite es por proceso, no por IP real. Dos terminales, otros dispositivos,
reinicios y programas externos no comparten este estado. Para dos procesos
cooperantes se puede repartir manualmente el presupuesto o serializar las
investigaciones. No se añade coordinación entre procesos ni almacenamiento.

## Errores, retry y plazos

`HyperliquidAPIError` mantiene tipo, texto HTTP y causa; añade `status_code`,
`request_type`, `retry_after_seconds`, `attempts` y `code`. No registra payloads
ni cabeceras. Los códigos locales distinguen deadline, presupuesto y espera de
retry agotados. Las formas de informes de los consumidores se conservan.

Solo HTTP 429 se reintenta en esta fase. `Retry-After` admite segundos o fecha
HTTP. El cooldown se comparte; cada retraso respeta el mayor de Retry-After y
backoff exponencial (1s inicial, 8s máximo), más jitter positivo de hasta 0,25s.
No se acorta una espera requerida para hacerla caber en el deadline. Cada intento
vuelve a reservar peso y libera el slot antes de backoff. Máximo tres intentos,
10s acumulados de regulación/backoff y 20s totales por petición. Los errores de
red, timeouts, JSON y otros HTTP no se reintentan automáticamente.

Market State aplica un deadline total de 25s, ajusta el timeout al tiempo restante
y deja de iniciar consultas al vencerlo. Un fallo de contexto aborta la captura;
los fallos posteriores conservan el snapshot degradado y sus incidencias.
Los gates existentes siguen decidiendo si los datos son utilizables.

Fast Revalidation conserva dos consultas nuevas, un intento por endpoint y
3s por defecto. No usa el proveedor de contexto. Los trabajadores reciben el
plazo restante; los resultados tardíos no se aceptan ni modifican el informe.
El mecanismo existente de threads daemon y capacidad acotada se mantiene.

El timeout de `urllib` no cancela de forma dura todas las fases DNS/socket.
R102 acota las esperas propias, comprueba el plazo tras el transporte y rechaza
resultados tardíos; Market State puede tardar en retornar si el transporte ignora
su timeout. Fast Revalidation conserva su retorno acotado independiente.

## Contexto temporal

`MarketContextProvider` conserva la respuesta completa de `metaAndAssetCtxs`
solo en memoria y devuelve copias. Una actualización concurrente usa una sola
petición. El TTL por defecto es 10s; se comprueba con reloj monotónico y edad del
timestamp local original. No se inventa un timestamp de origen del exchange ni
se renueva `captured_at_ms` al reutilizar. Si el refresco falla, no se sirve el
contexto caducado como actual.

Research comparte el proveedor entre descubrimiento y el scanner predeterminado.
Scanner independiente crea uno por ejecución. Market State y screener admiten
`context_provider` opcional; los builders/scanners/screeners inyectados conservan
sus argumentos originales y no reciben una dependencia nueva automáticamente.
El proveedor no aparece en JSON. No se reutilizan libros ni velas entre etapas.

El ahorro es condicional: cada contexto individual evitado ahorra una petición
y 20 unidades, pero cada refresco por TTL añade una consulta nueva. Un screener
vol_delta largo normalmente obliga a refrescar antes del scanner. La frescura
se comprueba otra vez en preanálisis; un contexto válido al inicio puede caducar
antes de la evaluación. No se cambian gates ni clasificaciones para eludirlo.

## Configuración y pruebas

Ver [Configuración](RESEARCH_CONFIG.md#protección-rest). No hay import-time I/O,
dependencias adicionales, caché persistente, telemetría privada ni servicios.

Para tests se inyectan `RESTCoordinator(timer=..., wait=...)`, transporte, reloj
UTC y RNG en el cliente. El cliente y el coordinador deben compartir el reloj
monotónico. También se admiten reloj/espera directos al crear el primer cliente
de un origen aislado. Cambiar esos relojes en un coordinador compartido existente
se rechaza. `request_scope` es local al thread y una anidación nunca alarga el
plazo ni aumenta los intentos. Los clientes simulados antiguos no necesitan
implementar esa capacidad; el consumidor comprueba el plazo antes y después.

Los tests de protección, contexto e integración son offline: relojes y espera
simulados, transporte sintético y sincronización mediante eventos. No requieren
API real, credenciales ni modelos.


## Estados y admisión por petición completa

La admisión calcula cuándo vencen suficientes reservas para liberar el peso
completo solicitado, combinándolo con cooldown y compromisos pendientes. Si ese instante
alcanza o supera el deadline y no hay compromisos/respuestas capaces de liberar
capacidad antes, rechaza sin esperar ni iniciar HTTP. La capacidad
HTTP sigue siendo dinámica: no se supone cuándo terminará una petición en vuelo.

Scanner conserva clasificaciones y motivos en sus ubicaciones JSON existentes.
Las incidencias de adquisición, validación o timestamps inutilizables elevan su
estado agregado a `partial_failure`, incluso sin una excepción de fila. Las
advertencias de origen temporal desconocido expresamente toleradas por
preanálisis no convierten un SKIP/WATCH legítimo en un fallo. Capturas degradadas
con datos parciales no producen `failed` automáticamente; ese estado se conserva
para fallos de todas las filas.

Analysis y Research propagan esta degradación. Un ANALYZE cuyo snapshot caducó
antes de enviar también produce informe parcial y conserva `snapshot_expired`
como motivo de no invocación. La CLI humana muestra advertencias separadas de
captura degradada y snapshot caducado; JSON conserva la forma y los motivos en
`market_quality`, `pre_analysis` y `model_review`. La revisión nativa ahora recibe cada snapshot al terminar su fila. Una reserva
antes de capturar limita a tres recorridos captura/revisión y evita acumular
snapshots esperando IA. Los callables antiguos no adheridos mantienen su
comportamiento por lote. Ver [Analysis Pipeline](ANALYSIS_PIPELINE.md#protocolo-incremental-opcional).


## Admisión conjunta de captura y asignación de etapas

La política distingue consumo de ventana, compromisos futuros y margen de etapa.
`reservation` reserva una captura completa: 163 unidades conservadoras con 200
velas por timeframe y funding hasta 500 elementos. Cada petición convierte crédito
propio en consumo de ventana. Una respuesta válida ajusta solo el consumo efectivo;
no devuelve el mismo importe a otro compromiso. Al terminar o fallar la captura,
se liberan únicamente los créditos aún no utilizados. Las reservas anidadas del
scanner y Market State reutilizan el mismo compromiso, sin duplicarlo.

Scanner espera REST dentro de la reserva previa de revisión y antes de iniciar
los relojes de captura. Market State independiente también reserva antes de su
deadline. `admission_wait_seconds=180` acota esta espera previa; no amplía los
20s de petición, los 25s de captura ni ningún gate de frescura. Los fallos de
admisión conservan la fila y el código en el informe incremental, sin iniciar IA.
Una reserva estima el primer intento; fallos de red, reintentos o cambios de coste
pueden consumir más y siguen limitados por ventana/deadline. No promete datos
completos ante fallos de transporte.

Research nativo protege temporalmente el coste de una captura mientras descubre
candidatos. El screener no puede consumir ese margen, que se libera al salir de
la etapa, incluso ante excepciones. No se inicia el scanner durante descubrimiento
ni se recapturan símbolos. El screener reserva contexto y cada consulta de velas antes de
empezar el plazo HTTP; su espera previa comparte un límite global de 180s por
etapa. Los trabajadores REST efectivos no superan la concurrencia HTTP configurada.
Si el presupuesto no se recupera a tiempo, el error permanece en el resumen del
pool anterior a `limit`; no se sustituye por ausencia legítima de histórico.

Los clientes/builder inyectados antiguos no necesitan implementar reservas.
Los perfiles YAML anteriores conservan defaults; el nuevo campo de configuración
es opcional. Las formas JSON, versiones, ranking, prompts e indicadores se mantienen.
No hay persistencia ni coordinación entre procesos. Fast Revalidation no reserva
una captura grande ni espera fuera de sus 3s: mantiene consultas nuevas y un
intento por endpoint en el mismo presupuesto compartido.

Los tests `test_rest_admission.py` y `test_market_screener.py` verifican recuperación
entre ventanas, contabilidad sin crédito duplicado, limpieza y errores visibles.
La simulación con 50 candidatos, diez activos y runner de 20s registra cada admisión:
consumo + compromisos + margen no superan 900; todos los endpoints completan bajo
los plazos de captura. El tiempo total puede abarcar varias ventanas y respuestas
IA caducadas durante la revisión se siguen tratando como históricas.
