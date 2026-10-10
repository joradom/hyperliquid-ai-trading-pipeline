---
name: r102-hyperliquid-pipeline
description: >-
  Use for R102 Hyperliquid market discovery, scanner screening and read-only AI
  research with research_pipeline or analysis_pipeline. Account queries require
  an explicit separate request; this skill does not authorize wallet or trading actions.
---

# R102: investigación de mercados

Usar las API del proyecto y su configuración de investigación. Las instrucciones
de desarrollo están en [AGENTS.md](../../../AGENTS.md); esta skill describe
procedimientos de mercado, no una política de ejecución ni un supervisor.

Aplicar [Privacy and information disclosure](../../../AGENTS.md#privacy-and-information-disclosure)
y [la revisión previa a publicación](../../rules/privacy.md) al preparar informes,
ejemplos o resultados para compartir. Una consulta de cuenta autorizada no
autoriza publicar datos reales de esa cuenta ni copiar trazas sin anonimizar.

## Entrada principal

Desde el directorio `python/` del repositorio:

```bash
python3 -B -m src.research_pipeline --profile default --top-n 10 --limit 3 --max-workers 3 --json
```

Research Pipeline descubre candidatos con Market Screener y llama a Analysis
Pipeline, que ejecuta el scanner una vez. El scanner captura Market State,
preanaliza y construye Decision Snapshots para ANALYZE; la revisión reutiliza
esos objetos. No ejecutar primero un scanner y pasar después solo sus símbolos
a Analysis Pipeline: ese encadenamiento provoca recapturas.

Para una lista explícita de símbolos, usar `src.analysis_pipeline` directamente.
Para revisar únicamente el filtro determinista, usar `src.scanner`.

## Configuración, resultados y límites

- `--config` y `--profile` seleccionan `python/config/research.yaml`.
  CLI → perfil → defaults. El perfil original usa 1 millón USD/10 bps,
  solo Codex y sin consenso; el experimental usa 10 millones USD/5 bps,
  Codex+Agy y consenso. Agy no es necesario en el perfil original.
- SKIP/WATCH no invocan modelos. ANALYZE solo se revisa si supera los gates de
  calidad, política, integridad y caducidad. LONG/SHORT son propuestas
  condicionales; `execution_authorized=false` nunca autoriza una orden.
- La salida conserva ranking, errores y datos ausentes. Código CLI 1 puede
  acompañar un informe parcial utilizable; leer `status`, incidencias y
  contratos internos. No interpretar null como cero.
- Las consultas del screener también consumen red para su pool `top_n`.
  Empezar con cargas pequeñas; no prometer cuotas, latencias o porcentajes de
  descarte a partir de mediciones históricas. Una pausa fija no garantiza
  evitar HTTP 429. No lanzar reintentos indefinidos.
- No consultar cuentas, usar wallets, firmar o modificar órdenes por inferir
  una señal. Las consultas de cuenta son una operación separada y requieren
  una solicitud explícita. No hay gestión de posiciones automática en esta skill.

Leer [comandos](references/commands.md) para elegir CLI. Los contratos y campos
están en [Research Pipeline](../../../docs/components/RESEARCH_PIPELINE.md),
[Configuración](../../../docs/components/RESEARCH_CONFIG.md) y
[Analysis Pipeline](../../../docs/components/ANALYSIS_PIPELINE.md).
El helper `scripts/run_pipeline.sh` delega en Research Pipeline; no agrega
etapas ni reglas de clasificación.
