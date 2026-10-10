# Analysis Pipeline

## Qué es

El orquestador que captura y filtra mercados, envía al modelo solo candidatos
elegibles y valida su respuesta. Coordina también la revalidación cuando una
propuesta direccional caduca durante la revisión.

## Para qué sirve

Automatiza una revisión de investigación con contratos explícitos, límites de
acceso y errores aislados por activo. No ejecuta operaciones ni invoca `mm`.

## Entrada

Símbolos de perpetuos y CLI Codex compatible, con autenticación previa.
Agy solo es necesario cuando el perfil seleccionado lo incluye.
Scanner construye las observaciones y el contrato en memoria.

## Salida

Informe con schema 1.2 original por defecto o 1.3 con perfiles alternativos,
respuesta de análisis v2.0 cuando es válida y
`fast_revalidation` cuando corresponde. Siempre mantiene
`execution_authorized=false`. La salida puede ser legible o JSON.

## Referencia técnica

**Versión: v1.2 por defecto / v1.3 con perfiles alternativos.** Los ejemplos se ejecutan desde `python/`. Fuentes:
[`analysis_pipeline.py`](../../python/src/analysis_pipeline.py),
[`analysis_response.py`](../../python/src/analysis_response.py) y
[`analysis_response.schema.json`](../../python/src/analysis_response.schema.json).
La especificación completa se conserva a continuación.

Orquestación predeterminada de solo lectura: `scanner v1.1 -> decision_snapshot v1 -> Codex`.
El perfil `experimental` conserva `Codex+Agy -> consenso`.
Opciones, contratos y tiempos: [Configuración de investigación](RESEARCH_CONFIG.md).
No integra mm, wallets, órdenes, firmas ni ejecución. No añade datos ni modifica
`pre_analysis` o las reglas de calidad/actualidad.
Las respuestas `LONG`/`SHORT` caducadas utilizan
[fast_revalidation v1](FAST_REVALIDATION.md), con dos nuevas capturas concurrentes
de solo lectura y ninguna llamada adicional al LLM.

## CLI

Desde `python/`:

```bash
python3 -B -m src.analysis_pipeline HYPE ETH SOL
python3 -B -m src.analysis_pipeline HYPE ETH SOL --json
python3 -B -m src.analysis_pipeline HYPE ETH SOL --profile experimental --json
python3 -B -m src.analysis_pipeline HYPE --config config/research.yaml --profile default
python3 -B -m unittest discover -s tests -v
```

Requiere las CLI seleccionadas compatibles en PATH con autenticación ya disponible.
El pipeline no instala, configura ni inicia sesión. Solo intenta iniciar la CLI
cuando existe un resultado `ANALYZE` elegible, lo que puede consumir tokens.
Sin candidatos elegibles, no inicia procesos de ninguno de los modelos.
Las pruebas usan simulaciones y nunca utilizan autenticación, acceso a la red
ni llamadas reales a modelos.

## Flujo y comprobaciones

1. Una única llamada a `scan_symbols` realiza hasta tres capturas simultáneas y
   devuelve los resultados en el orden solicitado. Conserva sus objetos
   `pre_analysis` y `decision_snapshot` en memoria sin reconstruirlos. Solo se
   realiza una nueva captura mínima cuando una respuesta `LONG`/`SHORT` válida
   caduca durante la revisión.
2. `SKIP`/`WATCH` terminan aquí, aunque una fila contenga accidentalmente una
   instantánea.
3. `ANALYZE` requiere una fila sin errores, un contrato válido con checksum
   intacto, símbolo y checksum de `pre_analysis` coincidentes,
   `classification=ANALYZE` en ambos contratos,
   `eligible_for_model_review=true`, cero bloqueos y ninguna excepción de
   benchmark. `quality=degraded` y sus advertencias no bloquean por sí solos.
4. La validez se comprueba inicialmente y otra vez tras serializar y preparar el
   comando, justo antes de iniciar el proceso. Se conserva la condición exacta
   `now <= valid_until_utc`; no se cambian `as_of`, tolerancias ni caducidad.
5. El JSON compacto se pasa por stdin a Codex y, si está seleccionado, como argumento de `--print` a Agy, con instrucciones fijas.
   No se crean archivos temporales, instantáneas ni salidas en disco. El único
   archivo pasado a `--output-schema` es el schema estático versionado del
   proyecto, no datos de mercado. La CLI lo carga; el modelo no necesita
   inspeccionar el proyecto.
6. Las revisiones de candidatos `ANALYZE` se ejecutan concurrentemente en un
   grupo de como máximo **3** trabajadores, sin reintentos. `SKIP`/`WATCH`
   nunca entran en la cola. Cada trabajador valida su candidato y genera su
   propios comandos y payload; por defecto se lanza solo Codex. Con consenso se lanzan dos procesos en paralelo (hasta seis procesos para tres candidatos). Cada llamada a subproceso tiene tuberías y contexto
   de sesión independientes. Los resultados conservan el orden de los símbolos
   aunque los procesos terminen en otro orden. Con más de tres candidatos, los
   siguientes esperan en una cola: si uno caduca antes de obtener un trabajador
   o durante la preparación, no se envía (`snapshot_expired`). No se vuelve a
   capturar ni se amplía la ventana. Un error, timeout o JSON inválido no cancela
   otras revisiones.

El modelo revisa la instantánea **en el momento indicado por as_of_utc**. Si
caduca durante la revisión, la respuesta **se conserva**, con
`expired_at_completion=true` y `requires_revalidation=true`; la salida legible
advierte de que es un análisis histórico, referido al momento de la observación,
que requiere revalidación antes de cualquier propuesta operativa. No se descarta
ni se convierte artificialmente en `NO_TRADE`. Si termina dentro del plazo,
`requires_revalidation=false` describe solo esta comprobación temporal, **no**
permiso para operar: `execution_authorized=false` siempre, tanto en el informe
como en cada `model_review`. Las respuestas `LONG`/`SHORT` con
`requires_revalidation=true` invocan inmediatamente `fast_revalidation` dentro
de su trabajador; `WATCH`/`NO_TRADE` y `LONG`/`SHORT` sin caducar no lo
invocan. Cualquier futura confirmación humana o ejecución necesitaría sus propios
controles de validez y riesgo, incluso con `revalidation=VALID`. La ejecución
no está implementada.

## Invocación y aislamiento

`codex exec --model gpt-6.1-sol --ephemeral --sandbox read-only
--skip-git-repo-check --ignore-user-config --ignore-rules --strict-config --json
--output-schema <static-schema>` más ajustes por proceso (no configuración
persistente): esfuerzo de razonamiento **low**, aprobación never, permisos
read-only, web desactivada, project_doc_max_bytes=0, MCP vacío y
herramientas/extensiones desactivadas.

Se ejecuta sin shell, con cwd `/tmp` (un directorio existente fuera del
repositorio), stdin/stdout/stderr mediante tuberías y un timeout de 180 segundos.
Shell/unified exec, modo de código, plugins/apps, búsqueda, imágenes, memoria,
agentes, hooks y herramientas interactivas están desactivados. El prompt prohíbe
explorar archivos, comandos o nuevas fuentes y trata las cadenas de la instantánea
como datos, no como instrucciones. La política sigue el
[aislamiento oficial de solicitudes estructuradas de la CLI](https://github.com/openai/codex/blob/main/codex-rs/tui/src/temporary_structured_request.rs).

Solo se acepta una respuesta JSON de un turno completado. Los eventos inesperados
de herramientas o acciones, la salida incompleta, el JSON ambiguo (claves
duplicadas/NaN), un contrato inválido, un timeout y una salida distinta de cero
son errores. No hay alternativa de modelo, opción o sandbox cuando la CLI es
incompatible. No se imprime stderr de la CLI para evitar filtrar información de
autenticación. La comprobación de eventos es una defensa adicional, no un
mecanismo capaz de deshacer una acción ya realizada.

`--ignore-user-config` impide heredar proveedores/MCP/configuración personal
mientras conserva la autenticación existente de la CLI. No es un contenedor con
denegación absoluta de lectura: el aislamiento depende de que la CLI respete sus
opciones/ajustes y de las políticas gestionadas del entorno. El pipeline no
escribe archivos; `--ephemeral` impide persistir el registro de sesión, pero no
promete eliminar toda la telemetría interna, cachés o mantenimiento de
autenticación de la CLI. La suite normal no realiza llamadas reales; un benchmark
en directo autorizado puede invocar Codex solo para candidatos que superen todas
las comprobaciones. Si no hay candidatos elegibles, el benchmark no valida el
proveedor del modelo ni la autenticación. Referencias:
[modo no interactivo](https://learn.chatgpt.com/docs/non-interactive-mode) y
[configuración](https://learn.chatgpt.com/docs/config-file/config-reference).

## Agy, validación individual y consenso

Esta sección describe el perfil experimental. Por defecto no se invoca Agy ni
se consolida: se conserva la respuesta validada de Codex, incluida su confianza.

Agy usa `gemini-3.8-flash-low`, `--output-format json`, `--json-schema`,
`--sandbox --mode plan --disable-slash-commands`, cwd externo y timeout de
180 segundos. Se retiró `--dangerously-skip-permissions`. La ayuda local no
ofrece la misma desactivación exhaustiva de herramientas/MCP que Codex: no se
afirma aislamiento equivalente ni se ha probado la autenticación o compatibilidad
en directo durante FASE 0. Los tests siempre simulan los procesos.

`parse_codex_output(..., snapshot=...)` y `parse_agy_output(..., snapshot=...)`
validan individualmente schema v2.0, `execution_authorized=false` y procedencia
del contrato, aunque la otra respuesta sea WATCH o NO_TRADE. Agy acepta un
envelope JSON (incluido pretty-print) o JSONL con exactamente un campo de resultado
`response`, cuyo valor es un objeto o una cadena JSON completa. Rechaza errores,
claves duplicadas, NaN, enums inválidos, texto extra y JSON extraído de prosa.
No se ocultan contratos inválidos detrás del voto del otro modelo.

La política de consenso existente se conserva:

| Codex / Agy | NO_TRADE | WATCH | LONG | SHORT |
|---|---|---|---|---|
| NO_TRADE | NO_TRADE | NO_TRADE | NO_TRADE | NO_TRADE |
| WATCH | NO_TRADE | WATCH | WATCH | WATCH |
| LONG | NO_TRADE | WATCH | LONG | WATCH |
| SHORT | NO_TRADE | WATCH | WATCH | SHORT |

La confianza es `high` si los votos coinciden y `low` si difieren, como antes;
es una heurística de acuerdo, no una probabilidad ni una mejora de calidad de datos.
Tesis combina ambas respuestas; entrada, invalidación y limiting_data conservan
los campos de Codex. Para propuestas direccionales se conserva únicamente su
compatibility_contract, sin fusionar reglas ni añadir filtros.

Cada fallo sustituye solo su voto por un WATCH de seguridad con listas vacías
válidas. Ese sustituto NO se registra como respuesta del modelo: el proveedor
queda con `status=error`, `response=null` y error de tipo/código sin contenido
de stderr, comando o excepción. La fila e informe quedan en estado de error
(salida CLI 1), pero conservan un resultado seguro WATCH o el NO_TRADE del modelo
válido. No se llama a revalidación para esos resultados no direccionales.

## Contrato de respuesta de análisis v2.0

Se conservan los seis campos legibles requeridos por `decision_snapshot` y se
añaden versionado, `execution_authorized=false` y `compatibility_contract`.
`decision_snapshot` no se modifica: sus `required_response_fields` forman el
contrato base; el pipeline exige exclusivamente v2 para nuevas revisiones. No
hay un lector de registros antiguos que requiera una alternativa v1. Schema:
`src/analysis_response.schema.json`; validación de estructura y procedencia:
`src/analysis_response.py`, sin dependencias externas.

```json
{
  "response_schema_version": "2.0",
  "execution_authorized": false,
  "decision": "WATCH",
  "confidence": "medium",
  "thesis": "Wait for setup confirmation.",
  "entry_conditions": ["Observable future conditions"],
  "invalidation": ["What invalidates the thesis"],
  "limiting_data": ["Missing data or relevant limitations"],
  "compatibility_contract": null
}
```

`decision`: NO_TRADE/WATCH/LONG/SHORT. `confidence`: low/medium/high, cualitativa
o heurística, **nunca una probabilidad calibrada**. La tesis no puede estar vacía;
los otros tres campos son arrays de cadenas no vacías (los arrays sí pueden estar
vacíos). No se permiten campos adicionales, números no finitos, booleanos usados
como números ni cadenas numéricas como umbrales. El JSON se valida localmente
además de `--output-schema`. `LONG`/`SHORT` no autorizan tamaños de operación,
apalancamiento ni operaciones: `account_constraints` permanece null.

### Contrato de compatibilidad 1.0

`NO_TRADE`/`WATCH` pueden llevar null o un contrato no accionable:
`actionable=false`, `complete=false` y reglas vacías. `LONG`/`SHORT` requieren
`actionable=true` y al menos una regla `purpose=invalidation` con
`required_for_valid=true`, incluso con `complete=false`. Sin una invalidación
defendible, el modelo debe elegir `WATCH`/`NO_TRADE`.

El contrato incluye `contract_version="1.0"`, `source_snapshot_checksum` (el
checksum exacto de la instantánea enviada), `actionable`, `complete`, `rules`
y `unresolved_conditions`. `complete=true` requiere
`unresolved_conditions` vacío; una propuesta direccional incompleta debe
explicar al menos una condición sin resolver. Los identificadores de reglas y
condiciones son valores snake_case únicos. Una respuesta inválida solo hace
fallar su revisión; una respuesta incompleta válida se conserva como propuesta
referida al momento de la observación.

Cada regla tiene `rule_id`, `field`, `predicate_type`, `threshold`, `range`,
`reference_source={primary,secondary}`, `threshold_origin`, `derivation`,
`purpose`, `required_for_valid`, `baseline_field` y `atr_field`. Todas las
claves son obligatorias; las que no aplican son null. Los únicos textos variables
de una regla son sus identificadores: campos, predicados, rutas, orígenes y
derivaciones son enums.

| Predicado | Campo permitido | Semántica para un futuro consumidor |
| --- | --- | --- |
| price_above / price_below | l2_midpoint, best_bid, best_ask, mark, context_mid, oracle | Comparación estricta > / < |
| price_in_range | Los mismos campos de precio | lower <= price <= upper |
| spread_lte | spread_bps | Diferencial <= límite original de pre_analysis |
| funding_lte / funding_gte | funding_current | Tasa fraccional <= / >= umbral |
| imbalance_lte / imbalance_gte | orderbook_imbalance | Desequilibrio <= / >= umbral, dentro de [-1,1] |
| oi_change_lte / oi_change_gte | open_interest | Cambio fraccional (nuevo-original)/original; reservado, sin política permitida actualmente |
| max_price_displacement_atr | Los mismos campos de precio | abs(nuevo-original del mismo campo)/ATR original <= umbral |

Los campos `premium`, `daily_notional_volume` y `original_atr_5m/15m` también
forman parte del enum cerrado solicitado. El vocabulario inicial no tiene
comparador de volumen/premium; no se fuerza `price_above` sobre unidades que no
son precios. Los valores de ATR originales se usan como valores auxiliares de
`atr_field`. Las condiciones que requieren otros operadores permanecen sin
resolver. No se añade ningún observable ni endpoint.

**purpose define la polaridad:** `entry`/`quality` son requisitos cuyo predicado
debe ser verdadero; `invalidation` es un desencadenante cuyo predicado debe ser
falso. Todas las invalidaciones son obligatorias. `required_for_valid=false` es
de diagnóstico y no puede representar una condición obligatoria. El desplazamiento
ATR solo permite `entry`/`quality` para evitar invertir su semántica. Esta
versión del contrato no realiza evaluación de mercado ni revalidación: la tabla
define el contrato para su futuro consumidor.

### Procedencia verificable de los umbrales

- `identity`: `threshold` copia exactamente `primary`; `secondary`,
  `baseline_field` y `atr_field` son null.
  `Origin=explicit_snapshot_level` o `model_derived_from_snapshot`.
  Solo `spread_lte` puede usar `pre_analysis_policy`, y debe copiar
  `pre_analysis.policy.max_spread_bps` sin cambios.
- `ordered_range`: `price_in_range` tiene `threshold=null` y
  `range={lower,upper}`, ordenado a partir de dos referencias explícitas de
  precio; `origin=model_derived_from_snapshot`.
- `snapshot_distance_atr`: `origin=model_derived_from_snapshot`; `primary`
  es el precio original correspondiente a `field` y `baseline_field=field`.
  `secondary` es una referencia explícita de precio y `atr_field` identifica
  un ATR original positivo. `threshold=abs(primary-secondary)/ATR`. Debe ser
  positivo: no se inventa ningún coeficiente ATR. Solo esta operación permite
  redondeo aritmético relativo/absoluto de 1e-12; las copias y los rangos se
  comprueban exactamente.

Las referencias permitidas son rutas cerradas a precios originales, la última
vela, extremos del rango de 20 velas, EMA/Bollinger, funding original/resumido,
desequilibrio, ATR y política de diferencial. El valor se comprueba contra la
instantánea exacta enviada y se rechazan las referencias null o sin valor
numérico. Los extremos del rango son observaciones, no soportes/resistencias
certificados. Copiar una observación de funding/desequilibrio demuestra su
procedencia, no su calibración como límite de riesgo. La selección de anclas y
la declaración `complete` siguen siendo afirmaciones del modelo, no hechos que
Python pueda inferir del texto.

`user_policy` y `unsupported` se aceptan como etiquetas de origen, pero no como
orígenes para reglas ejecutables en esta versión: no hay ninguna política externa
autorizada en la entrada. Los cambios de OI tampoco tienen un umbral actual en
el proyecto (ni siquiera cero); deben representarse como condiciones sin resolver, sin
inventar una nueva política.

Una condición sin resolver incluye `condition_id`, `purpose`, `reason`,
`threshold_origin` y una descripción legible no ejecutable. Motivos cerrados:
unsupported_predicate, new_policy_required, missing_snapshot_data,
temporal_evidence_required, ambiguous_condition. La confirmación de velas,
persistencia, registro de operaciones o nuevos límites deben permanecer aquí
cuando sean necesarios. No se extraen números ni instrucciones de `thesis`,
`entry_conditions`, `invalidation` o `description`. Python valida estructura,
coherencia y procedencia; no puede demostrar que el modelo haya declarado todas
las condiciones requeridas. `complete=true` nunca significa `VALID` ni ejecución.

El schema usa objetos cerrados, campos obligatorios, `$defs`/`$ref` y `anyOf`.
Las restricciones entre reglas (dirección, invalidación obligatoria, cobertura,
campos/unidades y procedencia) también se validan en Python antes de aceptar la
respuesta. Referencia del formato:
[Structured Outputs oficial](https://developers.openai.com/api/docs/guides/structured-outputs).

## Salida, auditoría y tiempos

JSON `kind=analysis_pipeline`, `schema_version=1.2`, `read_only=true`,
`execution_authorized=false`; conserva filas del scanner, motivos,
`pre_analysis` e instantáneas. Declara `model=gpt-6.1-sol`,
`reasoning_effort=low` y `max_concurrent_reviews=3`. Cada fila añade
`model_review` con `status`, `reason`, `invoked`, `response`, `sent_at_utc`,
`completed_at_utc`, `expired_at_completion`, `requires_revalidation`,
`execution_authorized=false` y `timing.codex_exec_seconds`. Cada fila añade
`fast_revalidation=null` o su documento completo con checksum y
`timing.fast_revalidation_seconds`. El informe añade `fast_revalidations`,
`fast_revalidation_seconds_total` y `fast_revalidation_deadline_seconds`
(3 segundos por defecto; configurable mediante `--revalidation-deadline-seconds`).
Los checksums existentes enlazan origen, preanálisis y revisión.

Con perfiles alternativos, el informe usa schema 1.3 y añade `profile`,
`consensus`, `models`, `provider_invocations`, `agy_invocations`,
`agy_exec_seconds_total`, `consolidation_seconds_total` y
`model_review_wall_seconds`. Cada fila añade `model_review.providers` con
`status`, `response`, `error` y `seconds` de cada proveedor, y tiempos de Agy,
consolidación y revisión conjunta. El perfil predeterminado conserva exactamente
los campos originales de 1.2, sin extensiones de proveedores.

`SKIP`/`WATCH` o gate fallido: `not_invoked`, respuesta null, proveedores vacíos en 1.3
y tiempos de modelo cero. Sin envío, las marcas de finalización son null.
Con respuestas válidas: `completed`. En consenso, un fallo individual produce
`error`, con fallback seguro etiquetado y error genérico de etapa `model_review`.
Sin consenso, un fallo produce `error` y respuesta null.
`codex_invocations` y `agy_invocations` cuentan intentos por candidato, incluidos
fallos de instalación/proceso; no miden tokens consumidos.

`capture_seconds_total`, `pre_analysis_seconds_total` y
`decision_snapshot_seconds_total` son sumas separadas del scanner; las capturas
concurrentes se solapan, por lo que no deben interpretarse como tiempo real
transcurrido. `scanner_seconds` es el tiempo real de captura/preanálisis/
instantánea. `timing.codex_exec_seconds` mide solo el proceso Codex y su validación;
su total es la suma entre activos. En 1.3, Agy tiene sus tiempos independientes
y `consolidation_seconds` mide únicamente consolidación y validación del consenso.
Las sumas se solapan: **no equivalen al tiempo real transcurrido**.
`codex_review_wall_seconds` va del inicio de la primera invocación Codex al final
de la última. `model_review_wall_seconds` en 1.3 abarca las revisiones de todos
los proveedores y la consolidación, antes de terminar sus revisiones y sin
añadir la revalidación posterior. Estos intervalos pueden incluir esperas entre
candidatos. Sin invocaciones los tiempos de modelo son cero; los gates siguen
incluidos en `total_seconds`. `total_seconds` incluye el flujo completo y
las comprobaciones antes del formato de stdout. El `timing.total_seconds`
original de cada fila conserva su significado en el scanner.

La salida legible muestra la clasificación de cada activo, motivos,
`codex_invoked`, tiempo individual y `requires_revalidation`;
en perfiles alternativos incluye estados y tiempos individuales de proveedores
y consolidación. La decisión/confianza/tesis corresponde al proveedor individual,
o al consenso si está activado. Para `LONG`/`SHORT` añade únicamente
`compatibility: complete=yes/no | rules=N | unresolved=N`; el contrato completo
solo aparece con `--json`. También muestra el tiempo real del scanner, el tiempo
real de revisión de Codex, estado/motivos y tiempo sumado de la revalidación
intentada, tiempo sumado de Codex, número de invocaciones y tiempo total.
Salida 0: orquestación sin errores (incluidos candidatos no elegibles/caducados).
Salida 1: errores aislados. Salida 2: argumentos inválidos, antes de capturar.

## Pruebas de concurrencia sin red

Barreras verifican dos modelos simultáneos por candidato y un máximo de tres
candidatos/seis procesos; los resultados mantienen el orden de símbolos aunque
termine antes otro activo. Los mocks devuelven el formato real de cada CLI.
Se prueban las 16 combinaciones del consenso, fallos individuales, validación
de ambos contratos incluso ante el veto del otro, confidencialidad de errores,
caducidad en cola y durante revisión, y revalidación dentro del trabajador sin
esperar a que terminen otros activos. SKIP/WATCH no invocan ningún modelo.
Los clocks virtuales prueban el solapamiento de duraciones individuales; el
tiempo de la revisión del candidato incluye coordinación de ambos procesos.
