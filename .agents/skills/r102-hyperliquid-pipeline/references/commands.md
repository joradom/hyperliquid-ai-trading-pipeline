# Comandos de investigación R102

Ejecutar desde `python/` del repositorio. Las ayudas `--help` no capturan
mercado ni invocan modelos.

## Descubrimiento y revisión sin símbolos

```bash
python3 -B -m src.research_pipeline --profile default --top-n 10 --limit 3 --max-workers 3 --json
python3 -B -m src.research_pipeline --profile experimental --limit 3 --json
python3 -B -m src.research_pipeline --config config/research.yaml --profile default --json
```

La misma API está disponible con `run_research(configuration=load_config(...))`.
Los overrides de selección son `--top-n`, `--limit`, `--sort-by`,
`--min-volume` y `--max-workers`. No iniciar capturas por separado para alimentar
después el pipeline con solo símbolos.

## Módulos independientes y símbolos explícitos

```bash
python3 -B -m src.market_screener --top-n 10 --limit 3 --json
python3 -B -m src.market_screener --top-n 10 --limit 3 --symbols-only
python3 -B -m src.scanner HYPE ETH SOL --profile default --json
python3 -B -m src.analysis_pipeline HYPE ETH SOL --profile default --json
```

Market Screener independiente conserva su mínimo original de 10 millones USD.
En Research Pipeline el mínimo se alinea con el perfil, salvo override explícito.
Analysis Pipeline ya contiene el scanner; genera sus propios datos y solo
invoca los proveedores configurados para ANALYZE elegible.

Research Pipeline devuelve schema 1.0, Scanner 1.1 y Analysis Pipeline 1.2
original o 1.3 alternativo. Conservar esos contratos. Una selección vacía normal
es `outcome=no_candidates`, sin scanner ni modelos; los datos degradados y
errores parciales permanecen visibles aunque la CLI termine con código 1.

El helper opcional, desde la raíz, acepta las opciones de Research Pipeline:

```bash
bash .agents/skills/r102-hyperliquid-pipeline/scripts/run_pipeline.sh --profile default --limit 3 --json
```

No utiliza lotes/pausas/gestión de posiciones propios. La antigua opción
`--batch-size` ya no es parte de este helper; seleccionar parámetros del
screener y conservar la concurrencia/gates de los módulos.

## Cuenta: consulta separada y explícita

Las consultas de cuenta no forman parte del descubrimiento ni del informe de
investigación. Si el usuario solicita expresamente esa consulta, seguir
[Trade State](../../../../docs/components/TRADE_STATE.md) y
[Seguridad](../../../../docs/SECURITY.md). SHA-256 es un checksum, no una firma.
Esta referencia no autoriza usar wallets, modificar órdenes, configurar
supervisores ni ejecutar operaciones.

Contratos completos y opciones:
[Research Pipeline](../../../../docs/components/RESEARCH_PIPELINE.md),
[Configuración](../../../../docs/components/RESEARCH_CONFIG.md).
