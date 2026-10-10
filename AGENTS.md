# R102: instrucciones de desarrollo

R102 es un pipeline modular de investigación de mercados de Hyperliquid,
desarrollado principalmente en Python. Trabajar en este repositorio; no usar
copias antiguas ni configurar agentes globalmente como parte de un cambio local.

## Arquitectura real

- La entrada sin símbolos es `research_pipeline.run_research`: llama a
  `market_screener.screen_market` y después a `analysis_pipeline.run_pipeline`.
- Analysis Pipeline invoca una sola vez `scanner.scan_symbols`. Cada trabajador
  del scanner construye `market_state`, ejecuta `pre_analysis` y genera
  `decision_snapshot` solo para `ANALYZE`. El pipeline reutiliza esos objetos
  para la revisión de IA; `SKIP` y `WATCH` no invocan modelos.
- No encadenar scanner y pipeline pasando solo símbolos: vuelve a capturarlos.
  Mantener el adaptador en memoria de Research Pipeline. Las recapturas
  explícitas de Fast Revalidation tienen otro propósito y se conservan.
- L2 compartido, WebSocket, Order Flow/TAPE y DOM son capas independientes;
  el scanner continúa con REST y no necesita un capturador WS.

## Límites de los cambios

- Conservar interfaces CLI, contratos JSON, versiones y consumidores existentes.
  Revisar compatibilidad antes de añadir campos o cambiar su significado.
- Configurar investigación con `python/config/research.yaml` y perfiles.
  Mantener precedencia CLI → perfil → defaults y compatibilidad con YAML antiguos.
  Seleccionar proveedores soportados mediante configuración; Agy es opcional,
  Codex es el predeterminado y el consenso está desactivado por defecto.
- Investigación read-only por defecto. No utilizar wallets, firmar, transferir
  ni crear/modificar/cancelar órdenes sin autorización explícita para esa acción.
  `LONG`, `SHORT` y `VALID` nunca constituyen autorización de ejecución.
- Mantener Linux/Kubuntu y la biblioteca estándar donde sea suficiente.
  WebSocket y DOM tienen dependencias opcionales propias; no hacerlas obligatorias
  para scanner o análisis. No introducir funcionalidades/refactorizaciones ajenas.
- Examinar `git status` y los cambios locales antes de editar. No descartarlos,
  sobrescribirlos ni eliminar archivos sin revisar referencias y dependencias.
- No publicar configuraciones privadas, credenciales, direcciones personales,
  SQLite, logs ni datos de sesiones/captura. Respetar `.gitignore`.

## Privacy and information disclosure

Esta política es obligatoria para el código y los documentos generados por
agentes. Publicar únicamente la información necesaria para comprender,
ejecutar y reproducir el software.

- Nunca incluir nombres reales, datos personales, direcciones reales de wallets,
  credenciales, tokens, claves API o identificadores privados en archivos
  destinados a publicación.
- Nunca publicar rutas absolutas del equipo del desarrollador, nombres de
  usuario del sistema, directorios personales o estructuras privadas del disco.
  Usar rutas relativas al repositorio, variables como `$HOME` o placeholders
  genéricos cuando sean necesarios.
- No copiar literalmente salidas de terminal, logs o trazas sin comprobar y
  anonimizar previamente su contenido.
- No incluir información sensible en documentación, ejemplos, tests, fixtures,
  benchmarks, informes, mensajes de commit o pull requests.
- No divulgar detalles privados innecesarios en errores, comentarios ni archivos
  generados. Aplicar la misma revisión a los resultados que produzcan los agentes.
- Conservar identificadores técnicos legítimos y datos sintéticos inequívocos;
  no renombrar módulos, contratos o rutas funcionales para ocultar falsos positivos.
  Los archivos privados se excluyen completos, no se publican como evidencia.

Antes de cualquier publicación, aplicar la clasificación y el procedimiento de
[la regla de privacidad](.agents/rules/privacy.md). `.gitignore` y los controles
automáticos ayudan, pero no sustituyen la revisión del contenido.

## Validación y referencias

Desde `python/`: `python3 -B -m unittest discover -s tests`. Ejecutar los tests
relevantes tras cambios; para consolidación ejecutar la suite completa,
`git diff --check` y ayudas `--help` de las CLI afectadas. Usar datos y modelos
simulados; las ayudas no deben iniciar capturas, wallets ni inferencias.
No usar `python/test_jurado.py` como test del proyecto: es un experimento local.
No hacer commit/push ni modificar rama/remoto sin autorización.

La [regla local](.agents/rules/trading_guardrails.md) define los límites operativos.
La [skill de investigación](.agents/skills/r102-hyperliquid-pipeline/SKILL.md)
contiene procedimientos de mercado, no reglas generales de desarrollo.
Consultar [Arquitectura](docs/ARCHITECTURE.md), [Uso](docs/USAGE.md),
[Research Pipeline](docs/components/RESEARCH_PIPELINE.md),
[Configuración](docs/components/RESEARCH_CONFIG.md) y [Seguridad](docs/SECURITY.md).
