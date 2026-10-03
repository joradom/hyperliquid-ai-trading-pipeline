# Arquitectura

El proyecto separa lo que puede comprobarse con reglas de lo que requiere
interpretación. Python consulta el mercado, normaliza sus respuestas y calcula
indicadores reproducibles. Un filtrado determinista decide qué merece revisión.
El modelo recibe un resumen validado y devuelve una tesis condicional
estructurada. Una revalidación breve comprueba después si una propuesta caducada
sigue siendo compatible con datos actuales.

Analysis Pipeline es el orquestador de todo ese recorrido, no una etapa de
ejecución posterior al modelo. Scanner orquesta captura, preanálisis y
construcción de Decision Snapshot. El siguiente esquema representa el flujo de
datos dentro de esas capas:

```text
Hyperliquid /info
    │
    ▼
Market State
    │
    ▼
Pre-analysis (dentro de Scanner)
    │
    ▼
Scanner
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
                         ChatGPT/Codex
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

Trade State ── consulta de cuenta independiente

MetaMask Agent Wallet ── interacción separada con Hyperliquid
                         sin conexión automática al informe
```

## Responsabilidades

| Capa | Responsabilidad |
|---|---|
| Adquisición | Market State consulta contexto, velas, libro y funding; Trade State consulta una cuenta aparte |
| Cálculo determinista | Python normaliza, calcula indicadores, registra tiempos y verifica integridad |
| Screening | Pre-analysis clasifica como `SKIP`, `WATCH` o `ANALYZE`; Scanner coordina mercados concurrentes |
| Contrato de entrada | Decision Snapshot resume datos, reglas, unidades, limitaciones y caducidad |
| Análisis con IA | Codex interpreta señales, formula tesis y declara condiciones de entrada e invalidación |
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
| Decision Snapshot | schema y derivación 1.0 |
| Analysis Pipeline | schema 1.2 |
| Respuesta de análisis | schema 2.0 |
| Contrato de compatibilidad | 1.0 |
| Fast Revalidation | 1.0 |

Las versiones forman parte del contrato. Los campos, enums, unidades, reglas de
procedencia y restricciones de validación se detallan en
[los componentes](components/). Los campos narrativos del modelo no se convierten
en código ni se analizan para extraer umbrales. Los predicados estructurados
tienen un vocabulario cerrado; los requisitos que no pueden expresarse o
justificarse quedan en `unresolved_conditions`.

## Aislamiento del modelo

Python entrega únicamente el JSON compacto por stdin, con instrucciones fijas.
El modelo no necesita descubrir APIs, abrir el repositorio ni leer snapshots
de disco. La invocación usa `codex exec`, modo read-only, sesión efímera,
schema de salida y herramientas/MCP desactivados, con configuración por proceso.

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
