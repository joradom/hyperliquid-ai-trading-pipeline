# Configuración de scanner y proveedores

Los comandos existentes funcionan sin opciones nuevas. Desde `python/`:

```bash
python3 -B -m src.scanner HYPE ETH SOL --json
python3 -B -m src.analysis_pipeline HYPE ETH SOL --json
python3 -B -m src.scanner HYPE ETH SOL --profile experimental --json
python3 -B -m src.analysis_pipeline HYPE ETH SOL --profile experimental --json
python3 -B -m src.analysis_pipeline HYPE --config config/research.yaml --profile default --json
python3 -B -m src.research_pipeline --profile default --limit 3 --json
```

La CLI carga [research.yaml](../../python/config/research.yaml), con ruta basada
en la ubicación del módulo y no en el directorio actual. `--config` selecciona
otro archivo; `--profile` selecciona un perfil de ese archivo. Sin `--profile`
se utiliza `default_profile`. No se leen configuraciones de cuentas ni de trading.

| Perfil | Volumen diario mínimo USD | Spread máximo bps | Proveedores | Consenso | Pipeline JSON |
| --- | ---: | ---: | --- | --- | --- |
| `default` | 1.000.000 | 10 | Codex | Desactivado | 1.2 original |
| `experimental` | 10.000.000 | 5 | Codex y Agy | Activado | 1.3 |

Cada perfil declara `scanner.min_day_notional_volume_usd`,
`scanner.max_spread_bps`, `ai.providers` y `ai.consensus`. Los umbrales son
enteros positivos. Se admite un proveedor (`[codex]` o `[agy]`) sin consenso,
o ambos con consenso. El perfil experimental conserva la política de consenso
existente: NO_TRADE veta, WATCH implica espera y LONG/SHORT requieren acuerdo;
el contrato direccional procede de Codex.

El bloque opcional `screener` configura `top_n`, `limit`, `sort_by`,
`min_volume` y `max_workers`. Los YAML anteriores y bloques parciales siguen
siendo válidos; el volumen omitido hereda el mínimo del scanner del perfil.
La nueva CLI `research_pipeline` permite sobrescribir esas opciones por llamada.
El comando independiente `market_screener` conserva sus valores originales.
Detalles: [Research Pipeline](RESEARCH_PIPELINE.md).

El cargador usa únicamente la biblioteca estándar y admite un subconjunto
deliberadamente pequeño de YAML: mapas con sangría de dos espacios, enteros,
booleanos `true/false`, nombres simples, nombres entre comillas y listas inline.
No admite anchors, aliases, tags, mapas inline ni listas de bloques. Rechaza
claves duplicadas, campos desconocidos, perfiles ausentes y combinaciones
incoherentes antes de consultar mercado o invocar modelos. Valida todos los
perfiles, incluidos los no seleccionados.

Las API `scan_symbols` y `run_pipeline` admiten `configuration=ResearchConfig(...)`.
Sin ese argumento usan los valores originales sin leer archivos; para consumir
YAML desde Python se pasa el resultado de `load_config(path, profile)`. La
configuración es inmutable y se pasa por llamada: perfiles concurrentes no
modifican POLICY ni se contaminan. Se externalizan volumen, spread,
selección de proveedores, parámetros del descubrimiento y protección REST.
Indicadores, confluencia y límites de frescura conservan sus cálculos. Decision Snapshot mantiene su forma v1.0
y registra la política efectiva; su validación comprueba que los checks usen
esos umbrales. El pipeline rechaza una fila cuya política no corresponda a la
configuración seleccionada.

## Proveedores y compatibilidad

Solo se preparan e invocan las CLI seleccionadas, para ANALYZE elegible. El
perfil predeterminado no necesita Agy: devuelve la respuesta validada de Codex
sin consolidarla ni alterar tesis o confianza. No se instalan CLI ni se inicia
sesión. Un proveedor seleccionado ausente produce un error aislado por activo;
en consenso conserva el fallback WATCH y los vetos NO_TRADE existentes. No hay
fallback automático a otro proveedor para una revisión individual.

El perfil predeterminado mantiene los campos originales del informe 1.2,
incluidos los objetos de fila y model_review, sin añadir providers o campos
de Agy. Cualquier combinación distinta de los valores originales usa 1.3,
mantiene los campos históricos y añade perfil, modelos, consenso, intentos por
proveedor y diagnóstico individual. `--profile default` devuelve 1.2 también.
La respuesta del modelo sigue en versión 2.0; scanner conserva versión 1.1.

## Tiempos

`timing.codex_exec_seconds` y `codex_exec_seconds_total` corresponden solo a
Codex; incluyen proceso y validación individual. En 1.3, los equivalentes
`agy_exec_seconds` y `agy_exec_seconds_total` corresponden solo a Agy.
`consolidation_seconds` y `consolidation_seconds_total` miden la consolidación
y su validación; son cero sin consenso. `model_review.providers.*.seconds`
registra el mismo tiempo individual por proveedor, incluso cuando falla.

`codex_review_wall_seconds` mide desde el comienzo de la primera invocación de
Codex hasta el final de la última. En 1.3, `model_review_seconds` por fila y
`model_review_wall_seconds` global incluyen la espera de todos los proveedores
y la consolidación, hasta terminar la revisión y antes de su revalidación.
Los tiempos globales pueden incluir esperas entre candidatos. Los procesos
concurrentes se solapan: las sumas por proveedor no equivalen al tiempo real.
La revalidación mantiene sus tiempos separados y `total_seconds` incluye el
flujo completo.

Tests: `test_research_config.py`, `test_pipeline_profiles.py` y las suites
existentes de scanner, pipeline, preanálisis, contratos y revalidación. Todos
simulan red y modelos; no usan cuentas ni operaciones.


## Protección REST

Cada perfil admite un bloque opcional `rest`. Los YAML anteriores conservan los
valores de `RESTConfig`: presupuesto 900/60s, concurrencia HTTP 3, tres intentos,
espera acumulada de regulación/backoff por petición 10s, deadline de petición
20s, captura Market State 25s y TTL de contexto 10s. La espera previa de admisión
REST se limita a 180s; ocurre antes de los deadlines de petición/captura. Todos los campos son
enteros positivos; se rechazan presupuestos superiores a 1200 y ventanas
inferiores a 60s. No se amplían las tolerancias de calidad del preanálisis.

Ejemplo del bloque dentro de un perfil existente:

```yaml
    rest:
      budget: 900
      window_seconds: 60
      max_inflight: 3
      max_attempts: 3
      max_wait_seconds: 10
      admission_wait_seconds: 180
      request_deadline_seconds: 20
      capture_deadline_seconds: 25
      context_ttl_seconds: 10
      backoff_initial_seconds: 1
      backoff_max_seconds: 8
```

La API admite `ResearchConfig(rest=RESTConfig(...))`; el cliente independiente
admite `rest_config=RESTConfig(...)`. La configuración explícita prevalece sobre
los defaults. No se añaden flags REST a las CLI en esta fase: `--config` y
`--profile` siguen seleccionando el perfil. Dos políticas REST incompatibles
para el mismo servicio dentro de un proceso se rechazan; un cliente nuevo no
reinicia el presupuesto. La configuración REST no cambia la versión del
informe ni el proveedor de IA. Detalles: [Protección REST](REST_PROTECTION.md).
