# ChatGPT/Codex CLI

## Papel en el proyecto

El componente implementado invoca **Codex CLI**, mediante `codex exec`.
El nombre de este documento agrupa la relación con ChatGPT/Codex; el proyecto
no incluye un ejecutable propio llamado ChatGPT CLI ni automatiza la interfaz
gráfica de ChatGPT.

La CLI permite integrar la revisión en un proceso Python con stdin/stdout y
una respuesta estructurada. Su modo no interactivo admite flujos por scripts
y schemas de salida. Consulta el
[modo no interactivo oficial](https://learn.chatgpt.com/docs/non-interactive-mode).

## Qué hace Python

- Captura datos de Hyperliquid.
- Normaliza respuestas y calcula indicadores.
- Valida estructura, integridad, antigüedad y calidad.
- Aplica reglas deterministas de filtrado.
- Construye snapshots y controla su plazo de validez.
- Valida la respuesta y reevalúa la compatibilidad cuando corresponde.

## Qué hace el modelo IA

- Interpreta el conjunto de señales del snapshot recibido.
- Formula una tesis.
- Identifica condiciones de entrada.
- Identifica condiciones de invalidación y limitaciones.
- Devuelve una respuesta estructurada.

El modelo no calcula una nueva captura, no busca información adicional ni
comprueba por sí mismo condiciones futuras. Python no extrae reglas ejecutables
de su texto narrativo. La respuesta expresa propuestas, no órdenes.

## Por qué una CLI

Permite automatización desde Python sin depender de una interfaz gráfica:
las instrucciones fijas se pasan como argumento, los datos por stdin y los
eventos se recogen por stdout. `--json` produce un flujo JSONL de eventos;
`--output-schema` solicita la estructura de la respuesta final. El pipeline
extrae y valida localmente esa respuesta.

La CLI también permite limitar herramientas y acceso al entorno. La razón para
usarla aquí es integrar y acotar esa revisión concreta; no se afirma que sea
la única arquitectura posible para análisis con IA.

## Configuración concreta respaldada por el proyecto

La fuente es
[`analysis_pipeline.py`](../../python/src/analysis_pipeline.py), función
`codex_command()`, y la
[referencia de Analysis Pipeline](../components/ANALYSIS_PIPELINE.md).

| Ajuste | Valor del proyecto |
|---|---|
| Modelo | `gpt-6.1-sol` |
| Esfuerzo | `low` |
| Proceso | `codex exec`, sin shell, stdin/stdout/stderr mediante tuberías |
| Concurrencia | Como máximo tres revisiones independientes, sin reintentos |
| Timeout | 180 segundos |
| Directorio | `/tmp` |
| Permisos | Sandbox `read-only`, `approval_policy="never"`, `default_permissions=":read-only"` |
| Sesión | `--ephemeral` |
| Configuración | `--ignore-user-config`, `--ignore-rules`, `--strict-config`, ajustes por proceso |
| Datos | Snapshot JSON en memoria; schema estático en `python/src/analysis_response.schema.json` |
| Acceso adicional | Web, MCP, herramientas, extensiones y lectura de instrucciones del proyecto desactivados mediante ajustes |

La autenticación debe existir previamente. No hay alternativa automática de
modelo, permisos o flags si la CLI no es compatible. El stderr de la CLI no
se publica en la salida para evitar filtrar información de autenticación.

Solo se admite un turno completado con respuesta válida. Eventos de herramientas
inesperados, JSON ambiguo, contratos inválidos, timeout o salida no nula son
errores de esa revisión y no cancelan las demás.

## Límites del aislamiento y de la respuesta

Read-only no significa que toda lectura del sistema sea imposible. El aislamiento
depende de los flags efectivos, la CLI y las políticas del entorno.
`--ephemeral` no promete eliminar cachés, telemetría ni mantenimiento interno
de autenticación.

`LONG`/`SHORT` son propuestas condicionales y `confidence` es cualitativa.
Siempre se conserva `execution_authorized=false`.
Consulta [Seguridad](../SECURITY.md) antes de ampliar permisos o integrar
ejecución.
