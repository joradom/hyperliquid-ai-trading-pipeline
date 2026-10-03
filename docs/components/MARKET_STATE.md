# Market State

## Qué es

Una fotografía normalizada de un mercado perpetuo: precios, velas cerradas,
indicadores, libro de órdenes y derivados. Ayuda a distinguir los valores
observados de sus tiempos de captura y problemas de calidad.

## Para qué sirve

Ofrece datos reproducibles para el filtrado posterior, sin firmar ni operar.
El cálculo es determinista a partir de las observaciones capturadas.

## Entrada

Un símbolo como HYPE o ETH y respuestas de la Info API. El valor por defecto
es `DEFAULT_CANDLES=200` por marco, con 5m/15m/1h/4h. La CLI acepta
`--candles` y `--output-dir`.

## Salida

Un documento Market State con schema 1.1, series cerradas completas, indicadores,
`capture`, `data_quality` e `integrity`. La CLI guarda el JSON en
`data/snapshots/` y muestra un resumen; la API `build_market_state` devuelve
el objeto en memoria.

## Referencia técnica

El primer bloque incluye `cd python` y parte de la raíz del proyecto;
los demás comandos se ejecutan desde `python/`. Fuente:
[`market_state.py`](../../python/src/market_state.py),
[`indicators.py`](../../python/src/indicators.py),
[`hl_client.py`](../../python/src/hl_client.py) e
[`integrity.py`](../../python/src/integrity.py).

## Market State v1

El paquete `src` añade una instantánea de mercado normalizada y estrictamente
de solo lectura. Usa únicamente cuatro tipos de solicitud de la Info API:
`metaAndAssetCtxs`, `candleSnapshot`, `l2Book` y `fundingHistory`.
Las velas abiertas se excluyen de los cálculos de indicadores.

```bash
cd python
python -m src.market_state HYPE
python -m src.market_state ETH
python -m unittest discover -s tests
```

Las instantáneas se escriben en `data/snapshots/`. EMA y MACD usan medias
exponenciales inicializadas con SMA; RSI y ATR usan suavizado de Wilder;
Bollinger Bands usa la desviación estándar poblacional de 20 velas. El
desequilibrio del libro de órdenes se calcula sobre todos los niveles devueltos
por `l2Book` como `(bid_size - ask_size) / (bid_size + ask_size)`.
Cada marco temporal conserva la serie completa de velas cerradas normalizadas
utilizada en los cálculos, de modo que cada indicador pueda reproducirse
únicamente a partir del JSON.

El paquete no contiene código de claves privadas, autenticación, firma, wallets,
`/exchange`, órdenes, apalancamiento ni cambios de posiciones. Los valores
ausentes de la API permanecen en `null` y se describen en `data_quality.issues`.


## Metadatos de robustez de Market State v1.1

El schema 1.1 amplía v1: los campos de mercado existentes permanecen y los nuevos
bloques de nivel superior son `capture` e `integrity`.
`capture.sources` registra la hora local inmediatamente después de cada
respuesta (`captured_at_origin` identifica el reloj del proyecto), separada de las
marcas temporales proporcionadas por Hyperliquid. Una marca temporal local nunca
se etiqueta como una marca temporal de origen.

La actualidad de los datos es `fresh`, `stale` o `unknown`. Es `unknown`
cuando Hyperliquid no proporciona una marca temporal de origen, aunque la
respuesta acabe de llegar. Las tolerancias centralizadas en
`STALENESS_TOLERANCES_MS` son: 15 segundos para L2, 30 segundos para contexto
de mercado y open interest, 90 minutos para funding horario y dos veces el
intervalo de vela para OHLCV. Contexto y open interest permanecen actualmente
en `unknown` porque `metaAndAssetCtxs` no fecha esos campos. Las fuentes
obsoletas y desconocidas también se reflejan en `data_quality.issues`.

`integrity.checksum` es SHA-256 sobre JSON UTF-8 compacto determinista con claves
ordenadas. Se excluye todo el campo de nivel superior `integrity` para evitar
recursión. Para verificar una instantánea guardada sin realizar solicitudes de red:

```bash
python3 -m src.market_state --verify ../data/snapshots/SNAPSHOT.json
```

Los precios de mercado, bid, ask, punto medio y diferencial absoluto siguen siendo
números JSON, pero se redondean a 12 decimales para suprimir artefactos de
representación binaria. Cálculos, velas, valores de indicadores, funding y open
interest conservan su precisión float original.

La suite unitaria usa respuestas simuladas deterministas de la Info API para
casos normales, ausentes, null, insuficientes, con huecos, abiertos, obsoletos,
libros vacíos/de un solo lado y errores de endpoint. Las pruebas de transporte
simulan errores HTTP, timeouts, JSON malformado y estructuras de API malformadas,
por lo que las pruebas de robustez no requieren mainnet ni credenciales.

