# Seguridad

El proyecto es experimental. La separación actual entre análisis y ejecución
reduce el alcance del pipeline, pero no convierte el conjunto en un sistema
seguro para operar con fondos. Lee el [DISCLAIMER](../DISCLAIMER.md).

## Datos que no deben publicarse

No guardar seed phrases, mnemonics, private keys, API keys, tokens ni credenciales
en el repositorio, documentación, ejemplos, registros, capturas de pantalla o
snapshots compartidos. No copiar archivos de sesión de herramientas externas.
Usar placeholders sin valor real, como `<PUBLIC_ACCOUNT_ADDRESS>`.

Una dirección pública de wallet no es una clave secreta y no permite firmar por
sí sola. Aun así puede identificar a una persona y vincular sus saldos u
operaciones. Debe eliminarse de la versión pública cuando no sea necesaria.

Antes de publicar, revisar documentación y enlaces, código, configuraciones,
snapshots, logs y cualquier historial que se vaya a compartir. Las exclusiones
de `python/.gitignore` incluyen `.venv/`, `.env`, logs y configuraciones
locales, pero no sustituyen esa revisión ni eliminan contenido ya compartido.

`hl_api.py` y `src/trade_state.py` no contienen una dirección personal
predeterminada. Resuelven la cuenta mediante un valor explícito,
`HYPERLIQUID_ACCOUNT_ADDRESS` o `config.local.json`. El archivo público
`config.sample.json` contiene solo un placeholder. La configuración real se
mantiene local y no debe compartirse.

El `.gitignore` de la raíz excluye `config.local.json` y todo
`data/snapshots/`, incluidos sus subdirectorios, además de las copias compiladas
de Python. Estas exclusiones no retiran archivos que ya estuvieran versionados
ni limpian un historial publicado. Los snapshots pueden contener direcciones
e historial de cuentas aunque el código no tenga una cuenta codificada.

## Separar lectura y ejecución

Los módulos Python consultan `/info` sin firma. El pipeline no llama a
`/exchange`, no utiliza `mm`, no accede a wallets y no modifica órdenes,
apalancamiento ni posiciones. Las escrituras de Market State y Trade State
son archivos JSON locales.

MetaMask Agent Wallet es una herramienta externa con capacidades de firma y
cambio de estado. Mantenerla separada del proceso que interpreta señales.
`LONG`, `SHORT`, `VALID` o `complete=true` no deben convertirse
automáticamente en una operación. El contrato mantiene
`execution_authorized=false`.

## Permisos de agentes y acceso al sistema

Limitar los permisos al mínimo necesario. Evitar que el modelo explore
innecesariamente el sistema, lea credenciales, descubra APIs o ejecute comandos.
La revisión Codex recibe datos por stdin y usa un schema estático; desactiva
herramientas, web, MCP y extensiones por proceso, con cwd `/tmp` y sandbox
read-only.

En perfiles que lo seleccionan, Agy recibe el mismo snapshot como argumento de
`--print` y usa el mismo schema. El perfil predeterminado utiliza solo Codex.
Sus flags locales son sandbox, modo plan y desactivación de expansión de skills;
no se autoaprueban herramientas. No se afirma que estos flags desactiven todas
las herramientas/MCP ni que equivalgan al aislamiento Codex. No se realizó una
inferencia real para certificarlos en FASE 0. Si se activa consenso, ambos
resultados deben superar validación individual de estructura y procedencia
antes de consolidar.
Los errores del proveedor se registran solo con tipo/código y no imprimen
stderr, comandos completos ni contenido arbitrario de excepciones.

Estos ajustes concretos están en
[Analysis Pipeline](components/ANALYSIS_PIPELINE.md). No equivalen a una
denegación absoluta de lectura ni a un contenedor completamente aislado.
La configuración efectiva de la CLI y las políticas del entorno siguen siendo
parte de la protección. `--ephemeral` evita persistir el registro de sesión,
pero no garantiza ausencia de cachés, telemetría o mantenimiento interno de
autenticación.

La validación de eventos rechaza acciones inesperadas; no puede deshacerlas.
No asumir que una instrucción del prompt sustituye una restricción de permisos.

## Integridad, actualidad y origen

Los checksums verifican integridad del contenido y coherencia entre documentos,
no autenticidad criptográfica del origen. Un documento coherente puede ser
antiguo o contener datos incorrectos. Comprobar también versiones, calidad,
bloqueos, procedencia y `valid_until_utc` con el reloj del consumidor.

Los tiempos locales no son tiempos del mercado. Contexto/OI conservan
advertencias de origen temporal desconocido. La revalidación tiene cobertura
limitada y no demuestra ausencia de cruces de precio, persistencia de condiciones
o seguridad de ejecución futura.

## Desarrollo y cambios de estado

No utilizar fondos relevantes durante desarrollo. Las pruebas de estas capas
son simuladas y no requieren fondos, credenciales ni operaciones reales.

Revisar cuidadosamente cualquier código que firme, transfiera, cree o cancele
órdenes, cambie apalancamiento o modifique posiciones. Una futura capa de
ejecución necesitaría controles propios de cuenta, límites, permisos,
confirmación y validez; esas garantías no están implementadas por el informe
de análisis.

La configuración de trabajo indicada para MetaMask es `server-wallet + guard`.
Evita entregar directamente la mnemonic personal al agente, pero sigue
requiriendo proteger la autenticación y revisar las políticas efectivas.
BYOK exige gestionar material de firma en un entorno automatizado y aumenta
su exposición operativa. Consulta
[MetaMask Agent Wallet](external-tools/METAMASK_AGENT_WALLET.md).
