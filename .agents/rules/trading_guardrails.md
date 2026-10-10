---
description: Límites operativos del pipeline de investigación read-only de R102
globs: ["python/**", ".agents/**"]
---

# R102: límites de investigación

Esta regla se lee desde [AGENTS.md](../../AGENTS.md). No presupone que todos
los agentes interpreten automáticamente este directorio o su frontmatter.

- El pipeline principal consulta mercado y produce propuestas condicionales;
  no gestiona posiciones ni modifica órdenes. `execution_authorized=false`
  debe mantenerse en los contratos que lo incluyen.
- No usar wallets ni firmar/transmitir operaciones sin una autorización explícita
  para la acción concreta. Una señal, una condición de Break-Even o una revisión
  `LONG`/`SHORT`/`VALID` no concede ese permiso.
- No iniciar supervisores periódicos ni importar procedimientos de ejecución
  externa al realizar investigación o desarrollo. No existe una cadencia de
  trading obligatoria en el pipeline principal.
- Utilizar `research_pipeline` para descubrir y revisar en una sola ejecución.
  No pasar resultados del scanner al pipeline como una nueva lista de símbolos:
  se perderían sus snapshots y se repetirían capturas.
- Respetar calidad, checksum, política efectiva y caducidad. No modificar
  clasificaciones o umbrales para forzar una revisión. Los fallos y datos ausentes
  deben seguir visibles en el informe.
- `top_n`, `limit` y `max_workers` controlan carga de descubrimiento; el scanner
  añade sus propias consultas REST. No tratar una pausa fija ni un tamaño de
  lote histórico como garantía frente a rate limits. Evitar pruebas intensivas
  durante mantenimiento y no iniciar reintentos indefinidos.

Referencias: [Research Pipeline](../../docs/components/RESEARCH_PIPELINE.md),
[Seguridad](../../docs/SECURITY.md) y [Configuración](../../docs/components/RESEARCH_CONFIG.md).
