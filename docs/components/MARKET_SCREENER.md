# Market Screener (Descubrimiento y Aceleración de Volumen)

El componente `market_screener` resuelve la fase previa al escáner de microestructura:
identifica en memoria los activos más activos de Hyperliquid y calcula la aceleración
real de volumen a 24 horas (`VOL_DELTA`), filtrando pares ilíquidos antes de lanzar
consultas profundas de orderbook y velas multitemporalidad.

## Responsabilidad y Principios

1. **Eficiencia de red**: Obtiene todo el universo de perpetuos de Hyperliquid en una
   sola llamada (`metaAndAssetCtxs`).
2. **Cálculo de Aceleración de Volumen**: Hyperliquid no ofrece el volumen de ayer en su
   resumen; `market_screener` descarga en paralelo (concurrencia controlada) las velas
   horarias de las últimas 48 horas para comparar el volumen nocional de las últimas 24h
   con el de las 24h precedentes:
   $$\text{VOL\_DELTA} = \left(\frac{\text{Volumen Hoy}}{\text{Volumen Ayer}} - 1\right) \times 100$$
3. **Cero dependencias externas**: Implementado con la biblioteca estándar de Python
   mediante `src.hl_client.HyperliquidInfoClient` (`urllib.request`).
4. **Composabilidad**: Soporta salida de tabla legible para humanos, salida estructurada
   JSON canónica para integraciones y flag `--symbols-only` para encadenar directamente
   con `src.scanner` y `src.analysis_pipeline`.

## Métricas calculadas

* `price`: Precio de marca (`markPx`).
* `price_change_pct_24h`: Variación porcentual de precio en las últimas 24 horas respecto a `prevDayPx`.
* `volume_24h_usd`: Volumen nocional en USD de las últimas 24 horas (`dayNtlVlm`).
* `volume_prev_24h_usd`: Volumen nocional en USD acumulado entre las 48h y 24h previas.
* `vol_delta_pct_24h`: Aceleración o caída porcentual del volumen actual frente al anterior.
* `oi_usd`: Interés abierto nocional en USD (`openInterest * markPx`).
* `vol_oi_ratio`: Ratio de rotación de capital (`volume_24h_usd / oi_usd`).
* `funding_rate` y `funding_annualized_pct`: Tasa de financiación instantánea y aproximada anualizada.

## Criterios de ordenación (`--sort-by`)

* `vol_delta` (por defecto): Prioriza los activos con mayor aceleración relativa de volumen.
* `volume`: Ordena por volumen nocional absoluto en 24h.
* `oi`: Ordena por mayor interés abierto en USD.
* `px_change`: Ordena por mayor variación absoluta de precio en 24h.
* `momentum`: Ordena por la fórmula compuesta $|\Delta P_{24h}| \times \sqrt{\text{Volumen}_{24h}}$.

## Ejemplos de uso

```bash
# 1. Tabla resumen de los 10 pares con mayor aceleración de volumen
python3 -m src.market_screener

# 2. Top 5 por volumen nocional absoluto
python3 -m src.market_screener --limit 5 --sort-by volume

# 3. Salida JSON estructurada
python3 -m src.market_screener --limit 5 --json

# 4. Encadenamiento directo con el Scanner de microestructura
python3 -m src.market_screener --limit 3 --symbols-only | xargs python3 -m src.scanner

# 5. Encadenamiento con Analysis Pipeline (revisión con IA)
python3 -m src.market_screener --limit 3 --symbols-only | xargs python3 -m src.analysis_pipeline
```
