# R102 — Auditoría y estabilización FASE 0

Revisión del código local, 2026-10-08. Alcance: estabilizar tests y validación de
modelos, documentar dependencias y diseñar una ampliación. Ningún WebSocket,
indicador nuevo, filtro nuevo ni ejecución financiera se ha implementado.

## Resultado de estabilización

Estado inicial reproducido: 155 tests, 20 fallos y 2 errores.

| Grupo | Diagnóstico | Corrección |
|---|---|---|
| Analysis Pipeline | Mocks de una sola CLI y un proceso por candidato; esperaba JSONL de Codex también para Agy; barreras y clocks anteriores a la concurrencia anidada | Formatos separados, mismo payload, dos procesos por candidato, límites de seis procesos, orden e inmutabilidad |
| Pre-analysis | Tests con volumen mínimo 1 M y spread 10 bps | Fronteras de la política actual: 10 M y 5 bps; sin cambiar POLICY |
| Analysis Response | Regla de spread copiaba 10, incompatible con la procedencia actual del snapshot | Copiar el máximo del snapshot, no inventar un umbral |
| Fast Revalidation | Fixture de liquidez de 1 M y mocks Codex-only del pipeline | Leer el límite original y simular ambas CLI; comprobar revalidación concurrente |

Defectos reales corregidos en el pipeline:

- Agy no validaba schema ni procedencia individual: ahora ambos resultados
  pasan por `validate_response(..., snapshot=...)` antes de votar.
- Un contrato Codex inválido podía quedar oculto si Agy vetaba: ahora se valida
  antes de consolidar, también para NO_TRADE/WATCH del otro proveedor.
- El parser Agy extraía el primer objeto desde prosa y descartaba el resto:
  ahora exige JSON completo, sin duplicados, NaN, texto extra o múltiples respuestas.
- El fallback de errores usaba versión `2.0.0` y objetos donde el schema pide
  arrays. Ahora es un WATCH válido, explícitamente marcado como sustituto;
  nunca se registra como respuesta válida del proveedor.
- Los errores individuales se conservan con tipo/código y sin contenido de
  excepciones, stdout, stderr o comandos. Una fila afectada produce error y CLI 1.
- Se retiró autoaprobación de Agy y se usan sus flags locales de sandbox, plan
  y desactivación de expansión de skills. No equivale al aislamiento de Codex.

No se cambia la matriz de consenso, confidence de acuerdo, procedencia del
contrato direccional de Codex, filtros, umbrales o caducidad.

Verificación final: **162 tests, OK**, con respuestas simuladas, sin inferencias
ni solicitudes financieras. Comando desde `python/`:

```bash
python3 -B -m unittest discover -s tests
```

Pruebas adicionales: 16 combinaciones de voto, cada proveedor fallando o con
contrato inválido aunque el otro vete, formatos Agy, errores confidenciales,
fronteras de liquidez/spread y edades exactas con ±1 ms. Los tests existentes
de Market State, Trade State y demás componentes siguen pasando.

## Responsabilidades y dependencias reales

| Módulo | Responsabilidad / dependencias | REST / nuevas capturas |
|---|---|---|
| `hl_client.py` | Único transporte de estos módulos; `urllib.request`, respuestas `CapturedResponse` y errores API | `_post` envía POST a `/info`; métodos descritos abajo |
| `market_state.py` | Captura por activo, normaliza OHLCV/L2/contexto/funding, usa `indicators.calculate` e `integrity` | 7 consultas por activo: contexto global, 4 velas, libro, funding |
| `indicators.py` | Cálculo puro de EMA, RSI Wilder, MACD, Bollinger, ATR absoluto y porcentual, volumen relativo | Ninguna; reutiliza velas cerradas normalizadas |
| `trade_state.py` | Consulta de cuenta independiente, posiciones/órdenes/protección/fills, reconstrucción limitada, checksum | 5 consultas, incluso con filtro de símbolo; no entra en el análisis de mercado |
| `pre_analysis.py` | Transformación pura con as_of explícito; verifica integridad/calidad/edades, aplica POLICY, clasifica y firma el documento con checksum | Ninguna; consume Market State existente |
| `scanner.py` | Orquesta hasta 3 capturas por lote, evalúa cada activo inmediatamente, aísla errores y conserva orden | Llama a Market State; llama a preanálisis y crea Decision Snapshot solo para ANALYZE |
| `market_screener.py` | Descubre y ordena universo; volumen 24h, aproximación nocional por velas, aceleración y otros rankings | 1 contexto global + K consultas 1h; K <= top_n, por defecto 50, aunque limit sea 10 |
| `decision_snapshot.py` | Proyección pura cerrada, derivaciones/rangos/estadísticas, calidad, caducidad y enlaces SHA-256 | Ninguna; verifica Market State y vuelve a evaluar preanálisis para comparar el recibido |
| `analysis_pipeline.py` | Invoca scanner una vez, gates de vigencia, dos revisores por candidato, valida individualmente y consolida | Nuevas capturas del scanner; IA reutiliza su Decision Snapshot; revalidación solo para consenso direccional caducado |
| `fast_revalidation.py` | Gates originales, dos capturas concurrentes acotadas, normalización estricta y evaluación pura de predicados originales | 2 consultas por propuesta revalidada; no consulta velas, historial, cuenta ni modelos |

`hl_api.py` es una CLI separada ya existente con su propio transporte
`post_info` basado en urllib; no importa `hl_client.py`. Hay por tanto una
duplicación REST legacy fuera de los diez módulos auditados. Utiliza `/info`
para cuenta/spot, fills, órdenes históricas, funding y portfolio. No se ha
creado otro transporte ni se ha refactorizado esa CLI. No se ha ejecutado
Trade State ni accedido a configuración de cuentas durante esta auditoría.

## Solicitudes HTTP exactas

Todas las siguientes pasan por `HyperliquidInfoClient._post` a la URL oficial
por defecto `https://api.hyperliquid.xyz/info`:

| Consumidor | Método del cliente | Request type |
|---|---|---|
| Market State | `market_context` → `meta_and_asset_contexts` | `metaAndAssetCtxs` (universo entero, selecciona un activo después) |
| Market State | `candles` en 5m/15m/1h/4h | `candleSnapshot` (4 peticiones) |
| Market State | `l2_book` | `l2Book`, nSigFigs=5 |
| Market State | `funding_history` | `fundingHistory`, ventana solicitada 72h |
| Screener | `meta_and_asset_contexts`, `candles` | `metaAndAssetCtxs`, `candleSnapshot` 1h por candidato |
| Trade State | `clearinghouse_state`, `frontend_open_orders`, `user_fills`, `user_funding`, `meta_and_asset_contexts` | `clearinghouseState`, `frontendOpenOrders`, `userFills`, `userFunding`, `metaAndAssetCtxs` |
| Fast Revalidation | `l2_book`, `meta_and_asset_contexts` | `l2Book`, `metaAndAssetCtxs` |

El constructor del cliente admite URL inyectada cuyo sufijo sea `/info`; no
valida estrictamente el hostname. Es un límite actual a considerar antes de
un despliegue con configuraciones externas. En esta fase no se modifica esa API.

## Redundancias y límites encontrados

1. Dentro de un lote de scanner, cada Market State pide nuevamente el contexto
   global. Para N activos son N respuestas del mismo universo, no una compartida.
2. El screener ya descargó contexto global y 1h para K candidatos. El scanner
   vuelve a pedir contexto y 1h de los seleccionados; no recibe esos datos y
   solicita unas 200 velas, frente a unas 49 del screener. No son capturas
   intercambiables sin comprobar cobertura, as_of, esquema y frescura.
3. `analysis_pipeline <symbols>` inicia su propio scanner. Un prefiltro CLI
   `scanner ... | ... | analysis_pipeline ...` provoca 7 consultas adicionales
   por cada símbolo enviado. Con N escaneados, M enviados y R revalidados:
   consultas = `1 + K + 7N + 7M + 2R`. Sin scanner externo: `1 + K + 7N + 2R`.
   Son conteos de peticiones, no pesos de rate limit ni una captura atómica.
4. Decision Snapshot vuelve a calcular preanálisis al recibirlo para verificar
   correspondencia exacta. Duplica trabajo CPU, no red; es una defensa de
   procedencia deliberada. No se elimina ni se fusiona con scanner.
5. Pre-analysis calcula tendencia/spread desde observaciones; scanner no
   redefine reglas. Sus responsabilidades son clasificación y orquestación,
   respectivamente. No hay dos clasificadores de negocio.
6. Normalización y métricas básicas L2 están repartidas entre `_book` de Market
   State y la normalización más estricta de Fast Revalidation. Pre-analysis
   recalcula spread desde BBO. La futura extracción a un módulo puro compartido
   necesita autorización y pruebas de equivalencia; no se hizo en FASE 0.
7. Screener no certifica continuidad de sus 48 velas; usa `v*c` como aproximación
   de nocional y puede mantener `status=ok` con errores por fila. Falta metadata
   granular de calidad/frescura/checksum. No debe tratarse como snapshot de decisión.
8. El universo usa nombres originales; scanner normaliza a mayúsculas y restringe
   símbolos alfanuméricos. Mercados con prefijo DEX o nombres con minúsculas
   especiales requerirán un catálogo canónico antes de ampliar cobertura.
9. La CLI `hl_api.py` mantiene un POST independiente del cliente usado por las
   capas nuevas. Una futura unificación necesitaría preservar sus errores,
   comandos y respuestas, más autorización; no afecta a las capturas del pipeline.

## Tiempo, calidad e integridad

Market State guarda inicio/final local y recepción por fuente. OHLCV usa el
cierre de la última vela cerrada; libro usa `time`; funding usa el último evento
devuelto. Contexto/OI no tienen timestamp de exchange y quedan `unknown`.
La etiqueta inicial de frescura se calcula respecto al inicio y recorta edades
negativas a cero: no reemplaza la validación firmada posterior con edades reales.

Pre-analysis recalcula edades con as_of: libro 15 s, contexto/OI e inicio 30 s,
funding 90 min, velas 2×intervalo, desfase futuro máximo 5 s, todos inclusivos.
Las dos incidencias esperadas de contexto/OI son advertencias; otras incidencias
de origen o checks incumplidos bloquean. Datos ausentes son null, no señales.

Decision Snapshot fija el mínimo de las caducidades. Pipeline comprueba gates
inicialmente y al despachar, conserva resultados históricos y solo revalida
consenso LONG/SHORT caducado. Fast Revalidation aplica los límites originales,
deadline global 3 s, semáforo acotado y workers daemon que no alteran resultados
después de devolverse. Un nuevo cierre de vela no verificado puede producir
INDETERMINATE. No afirma persistencia del libro o de condiciones temporales.

Market/Trade State, Pre-analysis, Decision Snapshot y resultado de revalidación
usan `integrity.py`: SHA-256 de JSON UTF-8 ordenado, compacto, sin el campo
superior integrity y sin NaN. Pipeline/scanner/screener no tienen checksum
superior propio. Revalidación calcula hashes de respuesta/contrato originales;
no son firmas ni certificación del exchange.

El schema de Decision Snapshot 1.0 es cerrado y exige Market State 1.1 y la
POLICY actual exacta. Los umbrales cambiaron previamente sin cambiar
rules_version 1.0: snapshots con la política anterior no son aceptados como
actuales. Esto se documenta, no se relajan los gates ni se renombran versiones
en FASE 0. La ampliación requiere una migración explícita, descrita en el plan.

## Git y alcance de los cambios

Se preservaron los cambios anteriores de `.gitignore`, screener, umbrales,
consenso dual y documentación. No hay commit, push, eliminación ni publicación.
Los archivos excluidos del experimento independiente, scripts y datos continúan
ignorados; no se han leído, modificado ni ejecutado durante esta fase.

Cambios FASE 0: validación/parsers/aislamiento local Agy y trazabilidad en
`analysis_pipeline.py`; ayuda CLI del screener; cuatro archivos de tests;
documentación existente de arquitectura, uso, preanálisis, pipeline y revalidación;
seguridad, este informe y el plan WS/Order Flow/DOM. Los campos nuevos del informe de
pipeline son aditivos; Decision Snapshot y respuesta v2.0 mantienen sus esquemas.

No se certificó una inferencia real de Agy con los flags corregidos. Esa prueba
de compatibilidad requiere una intervención separada autorizada. La siguiente
fase sigue pendiente de autorización expresa.

`python/test_jurado.py` es una prueba manual previa fuera de `tests/`: invoca
CLI reales al importarse, pide una respuesta de conexión predeterminada y usa
otro modelo Agy y autoaprobación. No se ejecutó ni se modificó. El comando de
suite certificado es `unittest discover -s tests`; un descubrimiento global
que importe ese archivo no tiene la misma garantía offline. Conviene aislarlo
o añadir un guard de ejecución en una limpieza posterior autorizada.
