# Trade State

## Qué es

Una fotografía de consulta de una cuenta: márgenes, posiciones, órdenes abiertas
e historial reciente. Es independiente del análisis de mercado con IA.

## Para qué sirve

Permite inspeccionar exposición y protección declarada de posiciones y reconstruir
operaciones cerradas cuando el historial lo permite. No coloca ni modifica
órdenes, ni se incorpora automáticamente a `account_constraints`.

## Entrada

Una dirección pública mediante `--user` y, opcionalmente, `--symbol`.
La dirección no requiere una clave privada. Si se omite `--user`, se utiliza
`HYPERLIQUID_ACCOUNT_ADDRESS` o el campo `hyperliquid_account_address` de
`config.local.json`. No hay una dirección predeterminada codificada.
La API `build_trade_state` aplica la misma prioridad a su argumento `user`.
Sin un valor válido, falla antes de consultar la red. Consulta la
[configuración local](../USAGE.md#cuenta-de-consulta-configuración-local-o-entorno).

## Salida

Un documento con schema 1.0, `account_address`, `account`, `positions`,
`open_orders`, `risk`, `recent_history`, `capture`, `data_quality` e
`integrity`. La CLI guarda JSON y muestra un resumen. La presencia de una
dirección o historial puede revelar datos personales; no publicar esa salida
sin revisarla.

## Referencia técnica

Los ejemplos se ejecutan desde `python/`. Los comandos de captura sin
`--user` que se conservan a continuación usan el entorno o la configuración local;
para una consulta deliberada hay que indicar la cuenta elegida mediante ese
argumento. `--verify` solo lee un archivo local y verifica su checksum.

Fuente: [`trade_state.py`](../../python/src/trade_state.py) y
[`hl_client.py`](../../python/src/hl_client.py). Consulta
[Seguridad](../SECURITY.md).

## Trade State v1

`src.trade_state` es una instantánea independiente de cuenta de solo lectura,
construida con el mismo transporte `/info` sin firma. Consulta
`clearinghouseState`, `frontendOpenOrders`, `userFills`, `userFunding` y
`metaAndAssetCtxs`.

```bash
python3 -m src.trade_state
python3 -m src.trade_state --symbol HYPE
python3 -m src.trade_state --verify ../data/snapshots/SNAPSHOT_trade_state.json
```

Las órdenes protectoras solo se asocian cuando el símbolo y el lado opuesto
coinciden con la posición y la API informa explícitamente de `reduceOnly=true`,
`isTrigger=true`, un precio de activación distinto de cero y un tipo de orden
reconocible Stop o Take Profit. En v1, `protection_complete` exige que tanto SL
como TP cubran el 100% del tamaño de la posición. Las órdenes reduce-only no
clasificables permanecen como órdenes abiertas ordinarias.

Las operaciones cerradas recientes se reconstruyen como episodios de ejecuciones
que parten de posición cero y terminan en posición cero. El PnL neto es
`sum(closedPnl) - USDC fees + signed userFunding` ocurrido entre las ejecuciones
de apertura y cierre. Las inversiones de posición y los historiales truncados
antes de la ejecución de apertura no pueden reconstruirse de forma fiable y no
se fuerzan a un resultado de operación cerrada. La instantánea usa el mecanismo
SHA-256 determinista compartido descrito en la [arquitectura](../ARCHITECTURE.md) y no contiene capacidad
de firma ni ejecución.
