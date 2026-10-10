# Order Book compartido — FASE 3

Núcleo puro READ-ONLY en [`orderbook.py`](../../python/src/orderbook.py).
Normaliza snapshots L2 y comparte BBO, midpoint, spread, profundidad y OBI.
No realiza I/O, consulta cuentas, firma, envía órdenes ni persiste libros.
Market State sigue en schema 1.1 y Fast Revalidation conserva su contrato 1.0.

## Integración y APIs

```text
WS → stream_types._book → normalize_ws_book → Book inmutable
    → PublicWSClient.books / recepción / epoch / conflicto
    → LiveStore.sync_ws → último libro en RAM
        ├─ read(): vista FASE 2 intacta, incluido l2_book
        └─ read_l2(): vista L2 adicional profundamente inmutable

REST → Market State._book → market_state_book → contrato legacy 1.1
REST → Fast Revalidation._normalize_book → revalidation_book → observaciones legacy
```

- `normalize_ws_book(raw, now_ms, symbols)` reutiliza los validadores de campos
  de `stream_types`, sus clases `Book`/`Level` y `StreamValidationError`. Conserva
  tipos, imports y errores. El import local evita dependencia circular durante
  la inicialización; los validadores no hacen I/O.
- `market_state_book(raw, issues, number=..., display_number=..., iso=...)`
  recibe las conversiones originales de Market State, añade las mismas
  incidencias y devuelve exactamente el documento anterior.
- `revalidation_book(raw, symbol, number=...)` recibe la conversión original de
  Fast Revalidation y conserva errores, floats y orden de validación.
- `bbo`, `midpoint`, `spread_absolute`, `spread_pct`, `spread_bps`, `obi` son
  funciones compartidas. Las funciones escalares conservan el orden aritmético
  del consumidor; `midpoint(..., overflow_safe=True)` divide antes de sumar.
  `obi` es la fórmula legacy, sin imponer nueva validación a los adaptadores.
- `calculate_book(Book | None)` produce métricas desacopladas con validez
  estructural y espacial. No certifica frescura: para ello se usa `l2_view`.
- `l2_view(...)` añade identidad, procedencia, precisión, recepción, épocas,
  edades y calidad temporal. Todos sus tiempos son argumentos explícitos.
- `LiveStore.read_l2(network, coin, as_of_ms=...)` envuelve esa vista en
  `LiveSnapshot`, con mappings y tuplas inmutables. `to_dict()` devuelve una
  copia mutable independiente. Un activo desconocido produce `KeyError`.

`LiveStore.read(...)` y `read_all()` conservan sus campos, valores y formatos,
incluidas las cadenas decimales de los niveles. La CLI de captura mantiene su
salida anterior; la vista nueva se consume por la API Python. `read_l2` sin
`as_of_ms` usa el reloj local actual, aunque el motor de flujo no haya avanzado.
No muta Order Flow ni renueva un timestamp de libro.

## Compatibilidad de normalización

Las diferencias previas son deliberadamente preservadas:

| Aspecto | WS | Market State 1.1 | Fast Revalidation |
|---|---|---|---|
| Aritmética/representación | Decimal, Book/Level | float; precio/spread/midpoint redondeados a 12 decimales | float sin redondeo de presentación |
| Símbolo | obligatorio, validado y suscrito | no comprueba coin en `_book` | coin opcional; si existe debe coincidir |
| Tiempo | entero positivo, límite y desfase futuro ≤5 s | conversión `int` tolerante | tipo exacto int; frescura evaluada después |
| Niveles | exactamente dos lados, 1–20 por lado | admite lados ausentes y registra incidencias | exactamente dos lados no vacíos, sin nuevo máximo |
| Ordenación | estricta; rechaza precios repetidos | no impone ordenación | monotónica; permite precios repetidos |
| Tamaños | positivos | conversión tolerante, null tratado como cero en profundidad legacy | permite cero por nivel; total positivo por lado |
| BBO bloqueado | rechaza bid=ask | conserva spread cero | admite bid=ask |
| Cuenta de órdenes n | entero positivo obligatorio | conserva el campo recibido | no lo utiliza |
| Campo WS spread | valida y descarta | ignora campos adicionales | ignora campos adicionales |

La calidad estricta de las métricas nuevas no se aplica a los contratos REST.
En particular, no se corrigen silenciosamente libros incompletos o tamaños
inválidos del adaptador Market State. Una normalización Decimal única cambiaría
redondeos, fronteras y checksums. Se conserva además su fórmula de midpoint
`(ask+bid)/2`; revalidación usa `bid/2+ask/2`.

## Métricas, unidades y precisión

Para precio p (quote por base) y tamaño q (base):

- BBO: primer bid y primer ask; midpoint=(ask+bid)/2.
- Spread absoluto=ask−bid; porcentaje=spread/midpoint×100;
  bps=spread/midpoint×10000.
- Profundidad acumulada por lado desde el mejor precio hacia fuera:
  base=Σq; quote=Σ(p·q). Cada nivel incluye nocional y ambos acumulados.
- OBI legacy=(bid_base−ask_base)/(bid_base+ask_base), sobre **todos los niveles
  devueltos**, sin afirmar que representan el libro entero.
- Bandas como fracciones de midpoint: `0.001`, `0.0025`, `0.005`, `0.01`,
  correspondientes a ±0,10 %, ±0,25 %, ±0,50 % y ±1,00 %.

En cada banda se incluyen bids con `mid*(1-b) ≤ p ≤ mid` y asks con
`mid ≤ p ≤ mid*(1+b)`. Los límites son inclusivos. La banda registra profundidad
base/quote observada por lado y calcula OBI en **base**, sin mezclar nocionales.

Las métricas nuevas usan un contexto Decimal local de precisión 192, sin alterar
el contexto global. Se serializan como cadenas canónicas, incluida notación
exponencial: `1e2` representa 100. Precio/volumen/ratios no se convierten a float.
`precision` registra nSigFigs=5 de la suscripción actual, mantissa=null, precisión
aritmética y máximo de niveles. `units` distingue base, quote, quote_per_base y
ratio; no presupone la moneda quote de un mercado nuevo.

## Cobertura y OBI observado/válido

La cobertura describe el rango de precios del snapshot **agregado devuelto**,
no liquidez oculta, órdenes individuales ni continuidad histórica del exchange.
No se infiere que un lado está completo por tener menos de 20 filas.

| Cobertura por lado | Evidencia |
|---|---|
| `complete` | bid más externo ≤ límite inferior, o ask más externo ≥ límite superior, en un snapshot estructuralmente válido y ordenado |
| `partial` | hay niveles dentro de la banda, pero el rango visible no alcanza su límite externo |
| `unknown` | falta un libro utilizable o no hay niveles dentro de la banda ni evidencia suficiente de completitud |

La banda conjunta es `complete` solo si ambos lados lo son; si alguno es
`unknown`, la banda es `unknown`; en los demás casos es `partial`. Se conservan
límites, extremos visibles, conteos y motivos por lado. La agregación nSigFigs
limita la interpretación: complete no demuestra cobertura a precios sin agregar.

`obi_observed` refleja exclusivamente tamaños visibles dentro de la banda.
`obi_valid` requiere estructura válida, ambos lados complete y denominador
positivo. En `l2_view` requiere además calidad temporal usable. Cobertura
parcial/desconocida conserva el observado y deja válido=null con motivo.
Si no hay liquidez visible en la banda, ambos son null y aparece
`no_visible_liquidity_in_band`, incluso si el BBO permite demostrar que la banda
está vacía. Libro vacío/cruzado/repetido o cantidades inválidas producen métricas
no utilizables y motivos estructurales, sin convertir ausencias en cero.

`obi_legacy.observed/valid` sigue la misma separación temporal, pero su alcance
es todos los niveles retornados; `entire_book_coverage` permanece unknown.
La calidad superior describe estructura/procedencia/frescura del snapshot;
cada banda añade su propia restricción espacial. Un snapshot `quality=ok` puede
tener bandas parciales con `obi_valid=null`.

## Recepción, frescura, conflictos y reconexión

Se registran tiempo exchange y recepción local del **último snapshot aceptado**,
época de ese libro y época actual. Se evalúan edades con `as_of_ms`, sin recortar
edades negativas. Los límites de metadata son inclusivos: 15000 ms de antigüedad
y 5000 ms de desfase futuro. No son nuevas reglas financieras ni gates.

- Un libro viejo, un duplicado, un conflicto, un trade o un pong no renuevan
  la recepción del libro conservado.
- Dos libros diferentes con el mismo timestamp degradan la vista L2 con
  `book_timestamp_conflict`; el observado permanece disponible. Un snapshot
  posterior válido elimina ese conflicto L2, sin borrar el gap del tape.
- Al establecer nueva conexión se vacían libro, recepción, época y conflicto.
  Si se conserva una referencia anterior, la discrepancia de épocas impide
  publicar OBI válido. Un snapshot fresco de la nueva conexión recupera L2,
  sin recuperar Delta/CVD, cobertura de trades ni historial anterior.
- Timestamp/recepción/época/precisión desconocidos, fuente futura o stale dejan
  `quality.usable=false` y OBI válido=null, con motivos explícitos. Cobertura
  espacial y valores observados no se borran por staleness.
- No se utiliza `transport_state`, heartbeat ni calidad global de Order Flow
  para certificar L2. Un snapshot reciente puede conservar frescura temporal
  durante una desconexión; `quality.usable` describe esa observación puntual,
  no demuestra disponibilidad actual del feed ni persistencia del libro.

Live Store publica libro y metadata solo después de que su sincronización haya
terminado sin error. Un fallo del journal mantiene la vista anterior. Esta
regla conserva el comportamiento transaccional existente; **SQLite no almacena
libros ni métricas L2**, y un reinicio sin nuevo snapshot devuelve missing_book.

## Verificación y límites de fase

Fixtures sintéticos de [`orderbook_legacy.json`](../../python/tests/fixtures/orderbook_legacy.json)
capturados antes de la extracción: 42 casos para salidas/errores de los tres
adaptadores; 10 checksums completos de Market State; 10 documentos completos
de Fast Revalidation evaluados con el módulo anterior, incluidos límites,
deadline, libros bloqueados/repetidos y errores. No se regeneran con el código
nuevo al ejecutar tests. La comparación canónica detecta también int vs float.

Tests nuevos en [`test_orderbook.py`](../../python/tests/test_orderbook.py) y
ampliaciones de Market State, Fast Revalidation, WS y Live Store comprueban
precisión, profundidad, bandas, cobertura insuficiente, calidad independiente,
libros antiguos, conflictos, reconexión, rollback e inmutabilidad.

Desde `python/`, con el entorno creado con Python 3.12.3 y websockets 10.4:

```bash
.venv/bin/python -B -m unittest discover -s tests
```

Verificación FASE 3: **253 tests, OK en 20,846 s**, los 220 existentes más
33 nuevos. Suite offline con fixtures y transportes simulados; sin conexión
real al exchange, wallets, cuentas, modelos ni servicios de ejecución.

No se modifica Pre-analysis, scanner, umbrales, Decision Snapshot, consenso,
SQLite ni cálculo de Order Flow. REST sigue capturando por REST: compartir
funciones no sustituye fuentes ni conecta el pipeline al WS. Side=A/B sigue
unverified. DOM, indicadores adicionales, contratos opt-in y benchmarks de
5/10/20 activos quedan fuera de esta fase.
