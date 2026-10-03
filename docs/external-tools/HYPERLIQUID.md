# Hyperliquid

Hyperliquid es el mercado de perpetuos utilizado en esta investigación. La capa
Python consulta su API HTTP pública oficial en mainnet, mediante solicitudes
`POST` sin firma a `https://api.hyperliquid.xyz/info`.
La [Info API oficial](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint)
describe los tipos de consulta.

## Peticiones utilizadas

| Capa | Tipos de petición |
|---|---|
| Market State | `metaAndAssetCtxs`, `candleSnapshot`, `l2Book`, `fundingHistory` |
| Trade State | `clearinghouseState`, `frontendOpenOrders`, `userFills`, `userFunding`, `metaAndAssetCtxs` |
| Fast Revalidation | `l2Book`, `metaAndAssetCtxs` |
| Cliente `hl_api.py` | Consultas públicas de cuenta y mercado a `/info` |

Scanner coordina las consultas de Market State. Analysis Pipeline añade la
revisión del modelo; Pre-analysis y Decision Snapshot no acceden a la red.

## Separación de ejecución

`/info` se utiliza para lectura. El cliente Python no firma ni llama a
`/exchange`, reservado a operaciones de negociación. Estas capas no cambian
órdenes ni posiciones, aunque las CLI de Market State y Trade State guarden
archivos JSON locales.

La interacción mediante MetaMask Agent Wallet es
[una capa externa separada](METAMASK_AGENT_WALLET.md). Una futura integración
de ejecución no forma parte del pipeline actual y requeriría controles propios.
Los datos de mercado y sus checksums no autorizan una operación.
