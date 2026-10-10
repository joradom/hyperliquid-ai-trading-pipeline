# Guía de uso

Esta guía permite probar el flujo de investigación: obtener datos, filtrarlos y,
si cumplen los requisitos, solicitar una revisión a un modelo de IA. Ningún
resultado del pipeline autoriza operaciones. Lee antes el
[DISCLAIMER](../DISCLAIMER.md) y las [medidas de seguridad](SECURITY.md).

## Requisitos y entorno Python

Se necesita Python 3.10 o posterior por la sintaxis utilizada en los módulos.
La capa Python utiliza la biblioteca estándar; `python/requirements.txt` no
declara dependencias de terceros. Las capturas en directo necesitan acceso HTTPS
a la API oficial de Hyperliquid en mainnet.

Los comandos siguientes se ejecutan desde el directorio `python/`:

```bash
cd python
```

Si ya existe el entorno virtual `.venv`, puede activarse con el comando utilizado
en la documentación original:

```bash
source .venv/bin/activate
```

La activación es opcional para estos módulos; ese comando no crea el entorno.
No se incluye un instalador ni una preparación automática de credenciales.

Analysis Pipeline necesita una CLI `codex` compatible, disponible en PATH, con
autenticación y acceso al modelo configurado ya disponibles. El pipeline no
instala la CLI ni inicia sesión. Las revisiones pueden consumir tokens.
MetaMask Agent Wallet no es un requisito para Market State, Scanner o Analysis
Pipeline: su [capa de interacción](external-tools/METAMASK_AGENT_WALLET.md) es
independiente.

## Cuenta de consulta: configuración local o entorno

La dirección no está codificada en los módulos Python. Las consultas de cuenta
resuelven el valor en este orden:

1. `--user` en la CLI o `user` explícito en `build_trade_state`.
2. Variable de entorno `HYPERLIQUID_ACCOUNT_ADDRESS`.
3. Campo `hyperliquid_account_address` de `config.local.json`, en la raíz del
   proyecto, independientemente del directorio desde el que se ejecute.

Para configurar una instalación local, copiar
[`config.sample.json`](../config.sample.json) como `config.local.json` y
sustituir `<PUBLIC_ACCOUNT_ADDRESS>` por la cuenta elegida. El placeholder no es
una dirección válida. `config.local.json` queda excluido de Git; el `.sample`
puede publicarse. Como alternativa, definir `HYPERLIQUID_ACCOUNT_ADDRESS` en el
entorno, sin guardar su valor en archivos públicos.

Sin una cuenta válida, las consultas de cuenta terminan con un error antes de
acceder a la red. `hl_api.py market` y `src.trade_state --verify` no necesitan
esta configuración. Market State, Scanner y Analysis Pipeline tampoco la leen.

Todo el contenido de `data/snapshots/` queda excluido de Git, incluidos archivos
nuevos y subdirectorios. Los archivos locales siguen disponibles para análisis.

## Flujo básico

Para descubrir e investigar sin indicar símbolos, desde `python/`:

```bash
python3 -B -m src.research_pipeline --profile default --top-n 10 --limit 3 --max-workers 3 --json
```

[Research Pipeline](components/RESEARCH_PIPELINE.md) conecta el screener con
una sola captura del scanner y las revisiones elegibles, conservando los
informes internos, ranking y errores parciales. No depende de Night Guard.

1. Market State consulta y normaliza datos, calcula indicadores y guarda una
   instantánea cuando se usa su CLI.
2. Pre-analysis evalúa una instantánea existente sin consultar la red.
3. Scanner captura mercados en directo y aplica ese filtrado en memoria.
   Solo prepara Decision Snapshot para `ANALYZE`.
4. Analysis Pipeline ejecuta el scanner, revisa los candidatos elegibles mediante
   Codex por defecto. El perfil experimental usa Codex y Agy en paralelo con consenso.
5. Si una respuesta válida `LONG`/`SHORT` caduca durante la revisión, realiza
   Fast Revalidation con dos consultas de solo lectura.

No es necesario ejecutar Market State primero para usar Scanner o Analysis
Pipeline: ambos capturan sus propios datos. No se reutilizan automáticamente
archivos antiguos como datos actuales.

Scanner y Analysis Pipeline admiten `--config` y `--profile` opcionales.
El YAML predeterminado es `python/config/research.yaml`: `default` conserva
1 millón USD de volumen mínimo, 10 bps de spread máximo y solo Codex;
`experimental` conserva 10 millones USD, 5 bps y consenso Codex+Agy.
Consulta [Configuración de investigación](components/RESEARCH_CONFIG.md).

## Market State

```bash
python -m src.market_state HYPE
python -m src.market_state ETH
```

La salida legible resume precios, funding, OI, número de velas, calidad, checksum
y ruta guardada. La CLI escribe el JSON completo en `../data/snapshots/`.
Market State no tiene una opción `--json`; los datos JSON están en el archivo.

Para comprobar únicamente el checksum de un archivo existente:

```bash
python3 -m src.market_state --verify ../data/snapshots/SNAPSHOT.json
```

`SNAPSHOT.json` es un nombre de ejemplo: hay que usar un archivo real. Esta
comprobación de integridad no certifica la actualidad ni la autenticidad del
origen. Consulta [Market State](components/MARKET_STATE.md).

## Pre-analysis y Decision Snapshot sin red

Estos comandos necesitan archivos existentes; los nombres son ejemplos:

```bash
python3 -B -m src.pre_analysis ../data/snapshots/SNAPSHOT_HYPE_market_state.json
python3 -B -m src.pre_analysis HYPE.json ETH.json --as-of 2026-10-01T19:36:28Z
python3 -B -m src.decision_snapshot ../data/snapshots/SNAPSHOT_HYPE_market_state.json
python3 -B -m src.decision_snapshot --schema
```

`--as-of` permite reproducir una evaluación histórica, no actualizar sus datos.
La salida de ambos módulos es JSON compacto en stdout, sin guardar archivos.
La excepción `--allow-watch-for-benchmark` de Decision Snapshot es solo para
benchmark: no permite que el pipeline operativo revise `WATCH`.
Consulta [Pre-analysis](components/PRE_ANALYSIS.md) y
[Decision Snapshot](components/DECISION_SNAPSHOT.md).

## Market Screener: descubrimiento y aceleración de volumen

```bash
# Top 10 activos con mayor aceleración de volumen en 24h
python3 -B -m src.market_screener

# Top 5 por volumen nocional absoluto en formato JSON
python3 -B -m src.market_screener --limit 5 --sort-by volume --json

# Encadenamiento con el Scanner mediante tubería
python3 -B -m src.market_screener --limit 3 --symbols-only | xargs python3 -B -m src.scanner
```

`market_screener` extrae el universo completo de perpetuos en una sola llamada de red y
descarga en paralelo las velas horarias de las últimas 48h para comparar el volumen de las
últimas 24h con el periodo precedente (`VOL_DELTA`), filtrando pares con bajo volumen.
`--symbols-only` emite únicamente los tickers separados por espacio para encadenar con
`src.scanner` o `src.analysis_pipeline`. Consulta [Market Screener](components/MARKET_SCREENER.md).

## Scanner: salida legible y JSON

```bash
python3 -B -m src.scanner HYPE ETH SOL
python3 -B -m src.scanner HYPE ETH SOL --json
```

La salida legible muestra la clasificación, motivos, calidad, tiempos y si se
preparó una instantánea de decisión. La salida `--json` es un único objeto
compacto con `results`, `counts`, errores y tiempos. Conserva `pre_analysis`
completo y `decision_snapshot` cuando corresponde; no conserva todas las velas
ni niveles L2. No invoca modelos ni escribe instantáneas.

Salida 0 significa procesamiento sin fallos de orquestación, aunque todos los
mercados sean `SKIP`. Salida 1 indica al menos un fallo aislado; salida 2,
entrada inválida antes de consultar la red. Consulta
[Scanner](components/SCANNER.md).

## Analysis Pipeline: revisión con IA

```bash
python3 -B -m src.analysis_pipeline HYPE ETH SOL
python3 -B -m src.analysis_pipeline HYPE ETH SOL --json
python3 -B -m src.analysis_pipeline HYPE ETH SOL --json --revalidation-deadline-seconds 3
```

Solo envía al modelo candidatos `ANALYZE` elegibles, sin bloqueos y vigentes.
La salida legible muestra clasificación, motivos, si se invocó Codex, el estado
de ambos modelos y tiempos;
decisión, confianza y tesis solo aparecen tras una respuesta válida.
`--json` incluye el contrato completo de compatibilidad, revisión y revalidación.

Las respuestas que caducan durante la revisión se conservan como históricas con
`requires_revalidation=true`. Solo las direccionales `LONG`/`SHORT` activan
Fast Revalidation. `execution_authorized=false` permanece siempre.
Salida 0 indica orquestación sin errores, incluidos candidatos no elegibles o
caducados; 1, errores aislados; 2, argumentos inválidos antes de capturar.

Consulta [Analysis Pipeline](components/ANALYSIS_PIPELINE.md),
[Fast Revalidation](components/FAST_REVALIDATION.md) y
[ChatGPT/Codex CLI](external-tools/CHATGPT_CLI.md).

## Cómo interpretar los estados

| Estado | Significado |
|---|---|
| `SKIP` | No supera el filtrado por datos inutilizables o condiciones de mercado fuera de los límites; hay que consultar los motivos |
| `WATCH` | En el scanner, supera las comprobaciones básicas pero falta confluencia; en la respuesta de IA, expresa espera o necesidad de confirmación |
| `ANALYZE` | Candidato para revisión por el modelo, sujeto además a integridad, calidad y plazo |
| `NO_TRADE` | Decisión del modelo de no proponer una operación; un error de infraestructura no se convierte automáticamente en esta decisión |
| `LONG` | Propuesta condicional de exposición alcista, sin orden ni permiso de ejecución |
| `SHORT` | Propuesta condicional de exposición bajista, sin orden ni permiso de ejecución |
| `VALID` | La revalidación confirma compatibilidad con todas las condiciones obligatorias evaluables en ese momento; no autoriza operar |
| `INVALID` | Evidencia actual y utilizable demuestra el incumplimiento de una condición básica u obligatoria |
| `INDETERMINATE` | No puede establecerse la compatibilidad por datos, cobertura, procedencia, transporte o plazo insuficientes |

`WATCH` puede proceder de etapas distintas. `ANALYZE` no significa `LONG` ni
`SHORT`. La confianza low/medium/high es cualitativa, no una probabilidad
calibrada de éxito.

## Tests

Desde `python/`:

```bash
python3 -B -m unittest discover -s tests -v
```

La suite utiliza respuestas simuladas y relojes controlados; no requiere mainnet,
credenciales, autenticación real ni llamadas al modelo. Cubre indicadores,
normalización, integridad, filtrado, contratos, concurrencia, errores y
revalidación. `-B` evita escribir bytecode. No hace falta ejecutar operaciones
financieras para validar estas capas.

## Qué es de solo lectura

| Capa | Acceso externo | Escrituras locales propias |
|---|---|---|
| `hl_api.py` | Consultas públicas a `/info` | Salida por stdout |
| Market State / Trade State | Consultas sin firma a `/info` | Sus CLI guardan JSON de instantáneas |
| Pre-analysis / Decision Snapshot | Ninguno | Salida por stdout |
| Scanner | Consultas de mercado a `/info` | Salida por stdout, sin instantáneas en disco |
| Analysis Pipeline | Captura de mercado y revisión por proveedores seleccionados | El pipeline no guarda archivos; las CLI externas pueden mantener estado interno |
| Fast Revalidation | `l2Book` y `metaAndAssetCtxs` | Resultado en memoria |

Solo lectura se refiere al estado financiero: guardar un JSON local no modifica
una wallet ni una posición. MetaMask Agent Wallet tiene capacidades de firma y
ejecución externas a este flujo y no debe tratarse como una herramienta
exclusivamente de lectura.

## Qué falta y limitaciones actuales

No hay ejecución automática integrada, sizing, selección de apalancamiento,
colocación de SL/TP ni consumidor operativo de la revalidación.
`account_constraints=null` impide concluir la seguridad de una cuenta desde el
contrato de mercado. Trade State existe como consulta separada y no se incorpora
a la revisión del modelo.

En el perfil experimental, el consenso conserva `NO_TRADE` si cualquiera veta, `WATCH` si cualquiera espera
o si hay desacuerdo direccional, y `LONG`/`SHORT` solo si ambos coinciden.
El contrato estructurado utilizado después continúa siendo el de Codex.
Los errores de un proveedor se indican por separado y degradan su voto a un
`WATCH` de seguridad; el informe devuelve estado de error y salida CLI 1.
Una coincidencia de votos produce confianza `high` por la heurística existente,
no una probabilidad de éxito.

La CLI del pipeline ya contiene un scanner. Si se usa un scanner externo como
prefiltro y se pasan después sus símbolos al pipeline, habrá nuevas capturas.
Para el recorrido habitual basta `screener → analysis_pipeline`; no existe
todavía una entrada CLI para reutilizar un informe de scanner. Véanse la
[auditoría FASE 0](PHASE0_AUDIT.md) y el
[plan WS/Order Flow/DOM](WEBSOCKET_ORDER_FLOW_PLAN.md).

Los umbrales son heurísticas iniciales; no acreditan rentabilidad. Los lotes no
son observaciones atómicas. Los datos pueden caducar antes del envío, durante
la revisión o antes de un uso posterior. Contexto/OI carecen de marca temporal
del mercado y se utiliza una aproximación basada en recepción local con
advertencias. La revalidación no obtiene nuevas velas ni demuestra persistencia,
ausencia de cruces durante el razonamiento o calidad de ejecución futura.

Las consultas de cuenta utilizan un valor explícito, el entorno o la
configuración local. Antes de una publicación completa deben revisarse los
archivos que se van a compartir, según [Seguridad](SECURITY.md); `.gitignore`
no elimina contenido que ya se hubiese publicado.

## WebSocket público standalone — FASE 1

Desde `python/`, con la dependencia opcional `requirements-ws.txt` disponible:

```bash
python3 -B -m src.ws_client ETH HYPE --seconds 60
python3 -B -m src.ws_client ETH HYPE --seconds 60 --json
python3 -B -m src.ws_client ETH HYPE --continuous
```

Una conexión compartida recibe trades y snapshots l2Book, controla ACK,
heartbeat y reconexión. La CLI imprime solo estadísticas y edades, sin persistir
paquetes o direcciones; Ctrl+C cancela y cierra la conexión. No calcula todavía
Delta/CVD ni alimenta al pipeline. `side_semantics=unverified` conserva la
incertidumbre sobre agresión y `execution_authorized=false`.
Consultar [WebSocket Client](components/WEBSOCKET_CLIENT.md) para los límites,
las pruebas offline y la comparación reproducible A/B contra el libro anterior.

## Order Flow y recuperación local — FASE 2

Desde `python/`:

```bash
python3 -B -m src.live_store ETH HYPE --seconds 60 --json
python3 -B -m src.live_store --recover-only --json
python3 -B -m src.live_store --replay-trades 20000 --db ../data/order_flow/replay_new.sqlite3 --json
```

La carpeta `data/order_flow/` está ignorada por Git. Journal normalizado y
checkpoint se confirman en una misma transacción. Los reenvíos retenidos no
se suman de nuevo; desconexión/reinicio abre segmentos independientes. El
replay exige base nueva y utiliza exclusivamente fixtures synthetic con
semántica conocida. En mainnet, volumen/conteo observado sí están disponibles,
pero Delta/CVD y comprador/vendedor siguen null por `side_semantics=unverified`.
La recuperación muestra estado histórico `recovered_not_live`, no una señal
vigente. Ver [Order Flow](components/ORDER_FLOW.md) para definiciones, warmup,
fronteras, memoria, cuotas y resultados de aceptación. No alimenta al scanner.

## Order Book compartido — FASE 3

La API Python añade `live.read_l2("mainnet", "ETH", as_of_ms=...)`, que devuelve
otra vista inmutable con métricas, procedencia, precisión y calidad del libro.
`live` es la instancia `LiveStore` sincronizada mediante `sync_ws`; se mantienen
`read`, `read_all` y la salida CLI anteriores. Si se omite `as_of_ms`, se evalúa
con el reloj local actual. No hay nuevo comando de captura ni datos L2 en SQLite.

Ver [Order Book](components/ORDERBOOK.md) para OBI observado/válido, cobertura
por bandas, frescura independiente del flujo y compatibilidad de adaptadores.
Desde `python/`, la suite offline del entorno virtual se ejecuta con:

```bash
.venv/bin/python -B -m unittest discover -s tests
```

## DOM interactivo — FASE 4

El [DOM de FASE 4](components/DOM.md) se abre por separado desde `python/`:

```bash
.venv/bin/python -m pip install -r requirements-dom.txt
.venv/bin/python -m src.dom ETH
.venv/bin/python -m src.dom ETH HYPE SOL
```

Controles sin Enter: Q salir, P pausar solo pantalla, +/- niveles, Tab activo;
Ctrl-C cancela. Cada ejecución usa una base nueva bajo `data/order_flow/dom_runs/`.
Opciones: `--refresh 4`, `--levels 10`, `--no-color`, `--seconds 15` para prueba
corta pública. No invoca modelos ni ejecuta operaciones.

## Cliente de consulta hl_api.py

`hl_api.py` envía solicitudes `POST` sin firma exclusivamente al endpoint
`/info`. No contiene integración con wallets, claves privadas, credenciales,
código de firma ni llamadas al endpoint de negociación `/exchange`. La salida
se deriva únicamente de campos devueltos por Hyperliquid.

La dirección de ejemplo pública se representa como `<PUBLIC_ACCOUNT_ADDRESS>`.
Los comandos siguientes sin `--user` utilizan el entorno o `config.local.json`.


### Uso del cliente

```bash
source .venv/bin/activate
python hl_api.py account
python hl_api.py fills
python hl_api.py orders
python hl_api.py funding
python hl_api.py pnl
```

Usar `--json` para obtener la respuesta de la API sin modificar y
`--user 0x...` para consultar otra cuenta pública. `fills` y `orders` también
aceptan filtros `--oid` repetibles. Funding acepta `--start-time` y
`--end-time` como milisegundos desde epoch.

Consulta la [Info API oficial](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint).
