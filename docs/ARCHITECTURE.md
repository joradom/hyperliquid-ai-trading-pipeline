# Arquitectura

El proyecto separa lo que puede comprobarse con reglas de lo que requiere
interpretación. Python consulta el mercado, normaliza sus respuestas y calcula
indicadores reproducibles. Un filtrado determinista decide qué merece revisión.
El modelo recibe un resumen validado y devuelve una tesis condicional
estructurada. Una revalidación breve comprueba después si una propuesta caducada
sigue siendo compatible con datos actuales.

Research Pipeline es la entrada que descubre candidatos y conserva los informes.
Llama a Market Screener y después a Analysis Pipeline. Este último invoca una
sola vez Scanner y coordina las revisiones; no es una etapa posterior al modelo.
Scanner orquesta captura, preanálisis y construcción de Decision Snapshot:

```text
Research Pipeline
    ├─ Market Screener → símbolos ordenados y métricas de selección
    └─ Analysis Pipeline
           └─ Scanner: Market State → Pre-analysis
 ┌──┴─────────┬───────────────┐
SKIP        WATCH          ANALYZE
                              │
                              ▼
                       Decision Snapshot
                              │
                 validación y envío por
                      Analysis Pipeline
                              │
                              ▼
                 proveedor(es) según perfil
           Codex por defecto / Codex+Agy con consenso
                              │
                              ▼
                  respuesta JSON validada
                     NO_TRADE / WATCH
                       LONG / SHORT
                              │
             LONG/SHORT con snapshot caducado
                              │
                              ▼
                      Fast Revalidation
               VALID / INVALID / INDETERMINATE
                              │
                              ▼
                       informe de análisis
                 conservado por Research Pipeline

Trade State ── consulta de cuenta independiente

MetaMask Agent Wallet ── interacción separada con Hyperliquid
                         sin conexión automática al informe
```

## Responsabilidades

| Capa | Responsabilidad |
|---|---|
| Descubrimiento | Market Screener filtra el universo por volumen y calcula la aceleración 24h (`VOL_DELTA`) |
| Investigación completa | Research Pipeline conecta descubrimiento y análisis y conserva los informes internos sin recapturar el scanner |
| Adquisición | Market State consulta contexto, velas, libro y funding; Trade State consulta una cuenta aparte |
| Cálculo determinista | Python normaliza, calcula indicadores, registra tiempos y verifica integridad |
| Screening | Pre-analysis clasifica como `SKIP`, `WATCH` o `ANALYZE`; Scanner coordina mercados concurrentes |
| Contrato de entrada | Decision Snapshot resume datos, reglas, unidades, limitaciones y caducidad |
| Análisis con IA | Codex por defecto; Codex+Agy cuando se selecciona consenso, conservando el contrato direccional de Codex |
| Orquestación y validación | Analysis Pipeline limita el envío y valida la respuesta del modelo |
| Revalidación | Fast Revalidation evalúa compatibilidad mediante dos nuevas consultas, sin otra llamada al modelo |
| Futura ejecución | Una capa aún no integrada necesitaría controles propios de cuenta, riesgo, permisos y validez |

La herramienta externa MetaMask Agent Wallet permite interacción con Hyperliquid,
pero el código del pipeline no invoca `mm`, no firma ni envía órdenes. Su uso
se documenta [por separado](external-tools/METAMASK_AGENT_WALLET.md).

## Freshness: cuándo son utilizables los datos

Una respuesta recién recibida no demuestra que sus datos sean actuales. Market
State distingue el tiempo de origen suministrado por Hyperliquid de la recepción
local. Si no hay tiempo de origen, la actualidad es `unknown`.
Contexto y OI conservan esa limitación.

Pre-analysis recalcula las antigüedades con su hora de evaluación: L2, 15 s;
funding histórico, 90 min; velas cerradas, dos veces su intervalo; inicio de
instantánea y recepción local de contexto/OI, 30 s. Admite hasta 5 s de desfase
hacia el futuro. Las dos advertencias esperadas de contexto/OI no bloquean por
sí solas si el resto de las comprobaciones pasa.

Decision Snapshot fija `valid_until_utc` según la primera caducidad de esas
dependencias. Generarlo no renueva los datos. El pipeline comprueba el plazo
justo antes del envío; conserva una revisión que caduque mientras el modelo
razona como histórica, con `requires_revalidation=true`.

Fast Revalidation vuelve a consultar solo `l2Book` y `metaAndAssetCtxs`.
Reutiliza reglas y dependencias originales; llegar al siguiente cierre de una
vela relevante sin verificarla puede producir `INDETERMINATE`. Solo `VALID`
lleva un nuevo plazo, que el consumidor debe comprobar al usarlo.

## Checksums e integridad

Las instantáneas y contratos utilizan SHA-256 sobre JSON UTF-8 canónico:
claves ordenadas, separadores compactos y exclusión del campo superior
`integrity`. Los checksums enlazan mercado, preanálisis, instantánea de decisión,
respuesta y contrato de compatibilidad.

Detectan cambios y permiten comprobar coherencia entre documentos. No son firmas
del mercado ni prueba de autenticidad criptográfica del origen. En revalidación,
el hash del Market State original es una referencia enlazada: su cuerpo no se
conserva para volver a verificarlo allí.

## Contratos JSON y versiones

| Documento | Versión documentada |
|---|---|
| Market State | schema 1.1 |
| Trade State | schema 1.0 |
| Pre-analysis | schema y reglas 1.0 |
| Scanner | schema 1.1 |
| Research Pipeline | schema 1.0 |
| Decision Snapshot | schema y derivación 1.0 |
| Analysis Pipeline | schema 1.2 original / 1.3 con configuraciones alternativas |
| Respuesta de análisis | schema 2.0 |
| Contrato de compatibilidad | 1.0 |
| Fast Revalidation | 1.0 |
| Diagnóstico WebSocket público (standalone) | schema 1.0 |
| Order Flow / Live Store standalone | schema y cálculo 1.0; SQLite schema 1 |
| Vista adicional Order Book L2 | schema y cálculo 1.0; Decimal, cobertura/frescura por snapshot |

Las versiones forman parte del contrato. Los campos, enums, unidades, reglas de
procedencia y restricciones de validación se detallan en
[los componentes](components/). Los campos narrativos del modelo no se convierten
en código ni se analizan para extraer umbrales. Los predicados estructurados
tienen un vocabulario cerrado; los requisitos que no pueden expresarse o
justificarse quedan en `unresolved_conditions`.

## Aislamiento del modelo

Codex recibe únicamente el JSON compacto por stdin, con instrucciones fijas.
El modelo no necesita descubrir APIs, abrir el repositorio ni leer snapshots
de disco. La invocación usa `codex exec`, modo read-only, sesión efímera,
schema de salida y herramientas/MCP desactivados, con configuración por proceso.

Cuando está seleccionado, Agy recibe el mismo JSON como argumento de `--print`, usa `gemini-3.8-flash-low`,
salida JSON, schema estático, sandbox, modo plan y expansión de skills desactivada.
No utiliza autoaprobación de herramientas. Sus opciones no garantizan el mismo
aislamiento que Codex; no se ha certificado esa equivalencia con una inferencia
real en FASE 0. En consenso, ambos resultados se validan individualmente contra
schema y procedencia del snapshot antes de consolidar. Los informes 1.3 conservan
respuestas y estados separados en `model_review.providers`; el perfil original
mantiene los campos de 1.2 sin esa extensión.

Se aceptan solo una respuesta JSON válida y un turno completado; acciones de
herramientas inesperadas, errores de proceso o contratos inválidos hacen fallar
esa revisión. Esta defensa no deshace una acción ya realizada. El aislamiento
depende también de la CLI y de los permisos reales del entorno; read-only no
significa denegación absoluta de lectura.
Consulta [ChatGPT/Codex CLI](external-tools/CHATGPT_CLI.md) y
[Analysis Pipeline](components/ANALYSIS_PIPELINE.md).

## Análisis y ejecución

`LONG`/`SHORT` son propuestas condicionales. `complete=true` declara
cobertura del contrato, no validez de mercado. `VALID` confirma compatibilidad
limitada a las condiciones evaluadas, no autorización.
`execution_authorized=false` permanece siempre.

Market State y Trade State realizan lecturas sin firma y sus CLI guardan
instantáneas locales. Pre-analysis y Decision Snapshot trabajan sin red.
Scanner y Fast Revalidation usan lecturas de mercado; Analysis Pipeline añade
la revisión del modelo. Ninguna de estas capas modifica posiciones o wallets.

La ejecución futura tendría que diseñarse por separado. Este trabajo documental
no añade un puente de ejecución, políticas nuevas ni cambios de comportamiento.
Consulta las [medidas de seguridad](SECURITY.md).

## Auditoría y evolución propuesta

La [auditoría FASE 0](PHASE0_AUDIT.md) documenta las llamadas REST reales,
recapturas y responsabilidades de cada módulo. El screener descubre símbolos:
no suministra un Market State reutilizable. Encadenar un scanner externo con
la CLI del pipeline vuelve a capturar los símbolos seleccionados.
Research Pipeline evita ese encadenamiento: utiliza el adaptador de scanner
en memoria de Analysis Pipeline. Su configuración está en
[research.yaml y perfiles](components/RESEARCH_CONFIG.md); la API y el contrato
completos están en [Research Pipeline](components/RESEARCH_PIPELINE.md).

[WebSocket, Order Flow y DOM](WEBSOCKET_ORDER_FLOW_PLAN.md) es una propuesta
por etapas. FASE 1 implementa solo el [cliente WS público](components/WEBSOCKET_CLIENT.md)
y tipos de eventos normalizados: una conexión compartida, trades/l2Book, ACK,
heartbeat/reconexión, deduplicación, límites y diagnóstico de continuidad.
No se conecta a las capas REST ni cambia sus cálculos o contratos. FASE 2 añade
[Order Flow](components/ORDER_FLOW.md), journal/checkpoints SQLite y vistas
inmutables independientes. Reutiliza trades normalizados y epochs de FASE 1;
los cálculos firmados reales siguen null mientras side sea unverified.
Persistencia local y lectura financiera se distinguen: no hay ejecución.
FASE 3 implementa [Order Book compartido](components/ORDERBOOK.md): funciones
puras, adaptadores que conservan validación/float/redondeo legacy y la vista
adicional `LiveStore.read_l2`. Market State 1.1 conserva campos y checksums;
revalidación conserva gates, límites, errores y deadline. Compartir cálculos
no sustituye capturas REST por WS ni modifica Pre-analysis o Decision Snapshot.
SQLite sigue guardando solo flujo, sin historial L2. FASE 4 añade el
[DOM foreground](components/DOM.md), que consume vistas inmutables con controles
de presentación y Rich opcional; no modifica motores ni conecta el scanner/IA.
Indicadores y nuevos filtros siguen pendientes de autorización.
