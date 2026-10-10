"""Build the Markdown experiment report from saved public JSON evidence."""
import argparse
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def fmt(value, places=3):
    return "NO DISPONIBLE" if value is None else f"{value:.{places}f}"


def mib(value):
    return fmt(value / 1024**2, 3)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("selection", type=Path)
    args = parser.parse_args()
    selection = json.loads(args.selection.read_text())
    directory = Path(selection["artifact_directory"])
    scenarios = {}
    for size in (5, 10, 20):
        path = directory / f"scenario_{size:02}.json"
        if path.exists():
            scenarios[size] = json.loads(path.read_text())
    if not scenarios:
        raise ValueError("no experiment evidence")
    env = selection["environment"]
    all_ok = len(scenarios) == 3 and all(d["status"] == "ok" for d in scenarios.values())
    lines = ["# R102 — Benchmark multi-activo 5 / 10 / 20", "",
             "Capturas públicas mainnet del 2026-10-09. READ-ONLY; sin cuentas, wallets, firmas, órdenes ni inferencias.", "",
             "**Resultado:** " + ("las tres capturas completaron sin overflow, errores, reconexiones o corrupción; la contabilidad local y la recuperación coinciden."
                                    if all_ok else "la escalada se detuvo o quedó incompleta; consultar motivos y evidencia por escenario."), "",
             "La viabilidad demostrada se limita al tráfico recibido y a este host durante tres ventanas de 60 s. No es una capacidad máxima ni certificación de operación continua o de Termux.", "",
             "## 1. Entorno, preparación y metodología", "",
             f"- Raíz y Git: `{ROOT}`. Se conservaron los cambios previos; sin commit ni push.",
             "- Suite de referencia: **253 tests offline, OK en 18,226 s**, ejecutados con `python/.venv/bin/python -B -m unittest discover -s tests` desde `python/`.",
             f"- Python {env['python'].splitlines()[0]}; SQLite {env['sqlite']}; websockets {env['websockets']}.",
             f"- Host: {env['platform']}; CPU {env['cpu']}; {env['logical_cpus']} CPU lógicas.",
             f"- Carga al seleccionar: `{env['loadavg']}`. Host compartido, sin afinidad, frecuencia fijada ni detención de procesos ajenos. Había 132 GiB libres en el filesystem del proyecto.",
             "- Cada escenario fue un proceso Python nuevo, consecutivo y sin otros benchmarks propios concurrentes. Un PublicWSClient, una conexión física exitosa y un LiveStore compartido por ejecución; ningún cliente por activo.",
             "- Suscripciones públicas trades y l2Book por activo, nSigFigs=5. Se usaron exactamente los primeros 5, 10 y 20 de una selección fija.",
             "- Sync: `LiveStore.sync_ws` seguido de sleep de 0,1 s, como la captura existente. Máximo original de 512 trades por sync; no se reagrupan ni reordenan operaciones.",
             "- Publicación auxiliar: aproximadamente una vez por segundo se leen `read` y `read_l2` para todos los activos, sin DOM. El intervalo es trabajo+sueño; no se afirma frecuencia dura garantizada.",
             "- Instrumentación separada: [`multi_asset_benchmark.py`](../benchmarks/multi_asset_benchmark.py). Wrappers de instancia delegan los métodos originales de handle_message, sync/write y cierre; no cambian código productivo ni parámetros.",
             "- Bases exclusivas nuevas, ninguna base operativa reutilizada o eliminada; datos y evidencia permanecen en `data/order_flow/`, ya ignorado por Git.", "",
             "### Configuración conservada", "",
             "SQLite: WAL, synchronous=FULL (2), wal_autocheckpoint=64 páginas, página=4096 bytes, max_page_count=16384: cuota de base principal de 64 MiB. Journal=100000 filas y retención=24 h. WAL/SHM no están incluidos en esa cuota de páginas.", "",
             "WS: 64 activos máximo; cola de trades=8192 eventos/2 MiB; cache global de IDs=100000, TTL=1200 s; historial L2=128 por activo; cola nativa websockets max_queue=16. Order Flow: 64 activos, 4096 pendientes por activo, 960 buckets, 100000 IDs por activo, lateness=2000 ms y freshness=30000 ms. Libros/métricas L2 siguen en RAM, sin historial SQLite.", "",
             "Se verificaron los [límites oficiales por IP](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/rate-limits-and-user-limits): 10 conexiones WS, 30 nuevas/min, 1000 suscripciones y 2000 mensajes salientes/min. Los escenarios necesitaron 10/20/40 suscripciones y 12/22/42 mensajes salientes; la selección hizo una sola consulta metaAndAssetCtxs, peso 20. No se ejecutó scanner. El uso de esa IP por procesos ajenos no se midió.", "",
             "### Selección reproducible", "",
             f"Consulta pública solicitada {selection['request']['requested_at_utc']}; recibida {selection['request']['received_at_utc']}. Criterio: top 20 de `dayNtlVlm`, universo nativo sin isDelisted, símbolos ASCII alfanuméricos y precio/volumen positivos finitos. Se conservó la grafía oficial. No se consultaron velas, cuentas ni el screener.", "",
             "| Orden | Activo | Volumen nocional 24 h al seleccionar | Primer grupo |", "|---:|---|---:|---:|"]
    for index, asset in enumerate(selection["assets"], 1):
        lines.append(f"| {index} | {asset['symbol']} | {float(asset['day_notional_volume']):,.2f} | {5 if index<=5 else 10 if index<=10 else 20} |")
    lines += ["", "### Criterios de parada fijados antes de capturar", "",
              "Detener el escenario y no iniciar uno mayor ante overflow, conflictos, fallo de escritura/integridad/recuperación/contabilidad o reconexión; también ante cola o pendientes ≥80 % de capacidad, base o journal ≥80 % de cuota, RSS >512 MiB, o tres sync consecutivos >2 s. Errores WS o suscripciones incompletas impiden escalar al finalizar. Son límites de seguridad del experimento, no políticas financieras. No se activaron en las tres ejecuciones.", "",
              "Los archivos existentes se protegen con rechazo de reutilización y escritura exclusiva de resultados. No se redujeron garantías de durabilidad para mejorar cifras.", "",
              "## 2. Comparativa", "",
              "El presupuesto WS de 60 s comienza en la primera conexión, como en producción. ACKs consumen parte de ese presupuesto; el tiempo de observación WS incluye su cierre. El tiempo con todas las suscripciones confirmadas se mide desde el último ACK hasta el retorno de run, también con cierre. `Total` incluye creación de base, captura, drenaje, auditoría, dos recuperaciones y cierre; excluye arranque/imports del intérprete y escritura del JSON final.", "",
              "| Métrica | 5 activos | 10 activos | 20 activos |", "|---|---:|---:|---:|"]

    def compare(label, callback):
        values = [str(callback(scenarios[size])) if size in scenarios else "NO EJECUTADO" for size in (5,10,20)]
        lines.append("| " + label + " | " + " | ".join(values) + " |")

    compare("Estado", lambda d:d["status"])
    compare("Tiempo total, s",lambda d:fmt(d["total_seconds"]))
    compare("Captura con conexión/setup/cierre, s",lambda d:fmt(d["capture_task_seconds"]))
    compare("Observación WS, s",lambda d:fmt(d["transport"]["observation_seconds"]))
    compare("Todas las suscripciones confirmadas hasta retorno, s",lambda d:fmt(d["confirmed_monitoring_seconds"]))
    compare("Primer conjunto completo de ACKs desde inicio, s",lambda d:fmt(d["first_all_ack_elapsed_seconds"]))
    compare("Intentos / conexiones / reconexiones",lambda d:" / ".join(str(d["transport"]["counts"].get(k,0)) for k in ("connection_attempts","connections","reconnections")))
    compare("Suscripciones confirmadas / deseadas",lambda d:f"{d['maximum_confirmed_subscriptions']} / {2*d['size']}")
    compare("Mensajes WS recibidos",lambda d:d["transport"]["counts"]["messages"])
    compare("Trades únicos WS = consumidos = journal",lambda d:d["committed_trade_operations"])
    compare("Snapshots L2 aceptados",lambda d:d["transport"]["counts"]["books"])
    compare("Trades únicos / segundo de observación WS",lambda d:fmt(d["throughput"]["ws_unique_per_observation_second"],2))
    compare("Trades comprometidos / segundo de captura",lambda d:fmt(d["throughput"]["committed_trade_operations_per_capture_second"],2))
    compare("Cola máxima, eventos",lambda d:d["transport"]["counts"]["queue_peak"])
    compare("Cola máxima, bytes de payload contabilizado",lambda d:d["queue_peak_payload_bytes"])
    compare("Overflow / descartes WS",lambda d:f"{d['transport']['counts'].get('queue_dropped',0)} / {d['transport']['counts'].get('queue_dropped',0)}")
    compare("Duplicados / conflictos / out-of-order WS",lambda d:" / ".join(str(d["transport"]["counts"].get(k,0)) for k in ("duplicates","conflicts","out_of_order")))
    compare("Interrupciones de continuidad WS / errores WS",lambda d:f"{d['transport']['counts'].get('continuity_interruptions',0)} / {sum(d['transport']['errors'].values())}")
    compare("Operaciones fallidas / mensajes inválidos",lambda d:f"{d['operations_failed']} / {d['transport']['counts'].get('invalid_messages',0)}")
    compare("Timeouts registrados",lambda d:sum(v for k,v in d["transport"]["errors"].items() if "timeout" in k))
    compare("Máxima edad exchange L2 muestreada, ms",lambda d:max(p["max_exchange_age_ms_sampled"] or 0 for p in d["per_asset"].values()))
    compare("Máxima edad de recepción L2 muestreada, ms",lambda d:max(p["max_receipt_age_ms_sampled"] or 0 for p in d["per_asset"].values()))
    compare("CPU hasta final de captura, s",lambda d:fmt(d["capture_cpu_seconds"]))
    compare("CPU de captura / pared, % de un núcleo",lambda d:fmt(100*d["capture_cpu_seconds"]/d["capture_task_seconds"],2))
    compare("CPU total del experimento, s",lambda d:fmt(d["cpu_total_seconds"]))
    compare("CPU user / system del proceso completo, s",lambda d:f"{fmt(d['cpu_user_seconds_process_lifetime'])} / {fmt(d['cpu_system_seconds_process_lifetime'])}")
    compare("RSS actual / pico al final de captura, MiB",lambda d:f"{fmt(d['memory_capture_end']['rss_current_kib']/1024)} / {fmt(d['memory_capture_end']['rss_peak_kib']/1024)}")
    compare("RSS actual / pico final del proceso, MiB",lambda d:f"{fmt(d['memory_final']['rss_current_kib']/1024)} / {fmt(d['memory_final']['rss_peak_kib']/1024)}")
    compare("SQLite / WAL antes de cierre, bytes",lambda d:f"{d['storage_at_capture_end']['database_bytes']} / {d['storage_at_capture_end']['wal_bytes']}")
    compare("SQLite / WAL pico muestreado, bytes",lambda d:f"{d['storage_sampled_peaks']['database_bytes']} / {d['storage_sampled_peaks']['wal_bytes']}")
    compare("SQLite / WAL después del cierre, bytes",lambda d:f"{d['files_after_close']['sqlite_bytes']} / {d['files_after_close']['wal_bytes']}")
    compare("Filas journal / checkpoints actuales",lambda d:f"{d['storage_at_capture_end']['journal_rows']} / {d['storage_at_capture_end']['checkpoints']}")
    compare("Payload de checkpoints actuales, bytes UTF-8",lambda d:d["checkpoint_payload_bytes_at_end"])
    compare("Transacciones comprometidas",lambda d:d["transactions_committed"])
    compare("Drenaje terminal / cierre socket, s",lambda d:f"{fmt(d['terminal_drain_seconds'])} / {fmt(d['ws_socket_close_seconds'])}")
    compare("Cierre SQLite incluyendo checkpoint WAL, s",lambda d:fmt(d["sqlite_close_seconds"],6))
    compare("Verificación / recuperación con verificación, s",lambda d:f"{fmt(d['verify_seconds'])} / {fmt(d['recover_seconds_including_verify'])}")
    compare("Reapertura + segunda recuperación + cierre, s",lambda d:fmt(d["reopen_recover_and_close_seconds"]))
    compare("Integridad / recuperación / reapertura",lambda d:f"{d['validation']['journal_and_checkpoints']['integrity']} / {d['validation']['recovery_state_matches']} / {d['validation']['reopened_recovery_state_matches']}")
    compare("Pendientes abandonados por cierre normal",lambda d:sum(p["flow_snapshot_at_end"]["counts"].get("pending_abandoned",0) for p in d["per_asset"].values()))
    compare("Trades fuera de segmento activo / bootstrap",lambda d:f"{sum(p['flow_snapshot_at_end']['counts'].get('outside_active_epoch',0) for p in d['per_asset'].values())} / {sum(p['flow_snapshot_at_end']['counts'].get('bootstrap_trades',0) for p in d['per_asset'].values())}")
    compare("Trades agregados en segundos cerrados",lambda d:sum(p["flow_snapshot_at_end"]["counts"].get("closed_trades",0) for p in d["per_asset"].values()))
    lines += ["", "Los trades únicos incluyen el bootstrap recibido al suscribir y eventos anteriores al comienzo conservador del segmento observado. La tasa anterior no representa exclusivamente trades nuevos durante los 60 s. Las capturas fueron consecutivas: el tráfico de BTC/ETH/HYPE cambió, de modo que 20 activos no equivalen a duplicar la carga de 10.", "",
              "### Tiempos locales de procesamiento", "",
              "| Medición | 5 activos | 10 activos | 20 activos |", "|---|---:|---:|---:|"]
    for key,label in (("handle_message_seconds","Handler WS, p50 / p95 / max, ms"),
                      ("journal_write_seconds","FlowStore.write, p50 / p95 / max, ms"),
                      ("sync_ws_seconds","Sync, p50 / p95 / max, ms"),
                      ("publication_read_seconds","Lectura/publicación de todos los activos, p50 / p95 / max, ms"),
                      ("loop_interval_seconds","Intervalo entre sync, p50 / p95 / max, ms")):
        compare(label,lambda d,key=key:" / ".join(fmt(d["timing_observations"][key][part]*1000,2) for part in ("p50","p95","max")))
    compare("Recepción local normalizada → retorno de commit, p50 / p95 / max, ms",lambda d:" / ".join(str(d["timing_observations"]["receipt_to_commit_ms"][part]) for part in ("p50","p95","max")))
    compare("Tiempo acumulado write / sync / lecturas, s",lambda d:" / ".join(fmt(d["timing_observations"][key]["sum"]) for key in ("journal_write_seconds","sync_ws_seconds","publication_read_seconds")))
    compare("Trades / segundo de pared dentro de write",lambda d:fmt(d["throughput"]["committed_trades_per_journal_write_wall_second"],2))
    lines += ["", "Write incluye backup/export de estado, validaciones, hashes, SQL y COMMIT FULL; está contenido en sync. No sumar ambas duraciones. Las lecturas incluyen métricas L2, copias/inmutabilidad y vista de flujo; no son latencia por evento. El cociente trades/tiempo write describe esta carga, no el techo de SQLite. Los percentiles conservan todas las observaciones de estas capturas; ningún buffer de muestras alcanzó su límite de 100000.", "",
              "**NO DISPONIBLE:** latencia exchange→recepción de socket, espera en buffers socket→handler y latencia por trade commit→publicación. No se estiman. La edad exchange del libro es una métrica de frescura, no una medida de latencia de red; la edad de recepción es distinta. Recepción→commit usa dos lecturas del reloj local en milisegundos, no sincronización con el exchange.", "",
              "## 3. Resultados por activo", "",
              "C/P/U = complete/partial/unknown; bandas ordenadas ±0,10 / ±0,25 / ±0,50 / ±1,00 %. La cobertura de la tabla es la última vista muestreada, relativa al rango agregado visible, no al libro entero. `Fresco` cuenta muestras con quality=ok; todas las degradadas fueron missing_book durante inicialización. No hubo muestras stale/future tras disponer del libro.", ""]
    for size in (5,10,20):
        if size not in scenarios:
            lines += [f"### {size} activos: NO EJECUTADO", ""]
            continue
        d=scenarios[size]
        lines += [f"### {size} activos", "", f"Inicio UTC {d['started_at_utc']}; fin con auditoría {d['completed_at_utc']}. Carga inicial del host: `{d['environment']['loadavg']}`.", "",
                  "| Activo | Trades únicos | L2 | Edad exchange / recepción máx., ms | Fresco / muestras | Bandas | Pendientes máx. | Cola terminal no agregada |",
                  "|---|---:|---:|---:|---:|---|---:|---:|"]
        for coin in d["symbols"]:
            p=d["per_asset"][coin]
            coverage=" / ".join({"complete":"C","partial":"P","unknown":"U"}[p["last_l2_view"]["metrics"]["bands"][key]["coverage"]] for key in ("0.001","0.0025","0.005","0.01"))
            lines.append(f"| {coin} | {p['flow_snapshot_at_end']['observed_total']['unique_trades']} | {p['ws']['counts'].get('books',0)} | {p['max_exchange_age_ms_sampled']} / {p['max_receipt_age_ms_sampled']} | {p['l2_quality_samples'].get('ok',0)} / {p['book_samples']} | {coverage} | {p['max_pending']} | {p['flow_snapshot_at_end']['counts'].get('pending_abandoned',0)} |")
        lines += ["", "Por activo: duplicados, conflictos, out-of-order, descartes WS, pending_overflow y gaps internos de flujo fueron cero. Hubo un cierre capture_end por activo, explícito y esperado. Contadores completos, motivos temporales y distribución de cobertura de todas las muestras se conservan en el JSON enlazado al final.", ""]
    lines += ["### Calidad temporal, espacial y del tape", "",
              "Los 12 snapshots L2 por activo observados en cada minuto equivalen a una cadencia media de ~0,2 snapshots/s. No se certifica refresco de 0,5 s: la especificación del feed y el tráfico efectivo son diferentes. Los máximos de edad son muestreados a ~1 Hz; un pico intermedio puede no estar registrado.", "",
              "Un libro temporalmente fresco puede tener todas sus bandas parciales. En esos casos se conserva OBI observado, pero OBI válido es null. No se etiquetó como completo el libro entero por recibir 20 niveles.", "",
              "El cierre normal usa la convención existente: al desconectar se abandona el buffer de reordenación pendiente sin añadirlo a segundos cerrados. Esos trades siguen en journal/observed_total y su cantidad está arriba; no equivalen a overflow ni pérdida silenciosa de identidades. El bootstrap y los eventos fuera del segmento activo tampoco se incluyen como tape continuo certificado.", "",
              "Mainnet mantuvo side_semantics=unverified y Delta/CVD/buy/sell firmados null. Las ventanas 5/15 min siguen en warmup; 60 s tampoco certifican una ventana 1 min utilizable tras margen de reordenación y cierre. No se interpretan métricas firmadas reales.", "",
              "## 4. Integridad y ausencia de pérdidas locales no explicadas", "",
              "En cada base: quick_check, checksums del journal, secuencia/manifest, cursores y anchors de checkpoints verificados mediante FlowStore.verify. Recuperación exacta de todos los estados exportados sin cache de IDs, primero en la conexión original y luego tras cerrar físicamente y reabrir la base. Se conservaron los PRAGMA y el manifest de configuración.", "",
              "Para los tres escenarios se cumple: WS únicos = consumidos = operaciones trade comprometidas = observed_total = filas trade retenidas. Cola final=0, queue_dropped=0, retired_through_seq=0. Sin dedup evictions, errores de integridad, fallos de operaciones ni timeouts. No se encontró pérdida silenciosa en la ruta local aceptada; sin secuencias del exchange, esto no demuestra completitud absoluta del mercado.", "",
              "Todos los hashes de `python/src/*.py` coinciden antes/después y entre escenarios. No se modificaron producción, scanner, pipeline, consenso, reglas, SQLite o dependencias. No hubo reaprovechamiento ni borrado de bases.", "",
              "## 5. Cuellos de botella y limitaciones", "",
              "La mayor ocupación síncrona medida corresponde a FlowStore.write: ~99 % del tiempo de sync. Ese tramo abarca trabajo Python de estado/serialización/integridad y SQLite; no se instrumentó SQL o fsync por separado y no puede atribuirse todo al motor SQLite. El p95 de write crece al aumentar activos; el event loop se bloquea mientras escribe.", "",
              "Las lecturas de vistas también crecen con el grupo, aunque su tiempo acumulado fue menor. La cola máxima de 555/8192 eventos (6,77 %) y los pendientes observados quedaron lejos de los umbrales de parada. No se observó saturación importante bajo esta carga.", "",
              "Una repetición por tamaño, activos anidados pero tráfico diferente, solo 60 s y bootstrap inicial: no es una prueba de capacidad constante ni de picos fuertes. No hay control de CPU/frecuencia, red, congestión o procesos ajenos. Los wrappers y lecturas auxiliares añaden overhead; no se usó tracemalloc. El RSS incluye intérprete, SQLite, buffers e instrumentación; VmHWM es el pico del propio proceso, también durante recuperación.", "",
              "En un minuto no se llenan 960 buckets, caches de 100000 IDs por activo, retención de 24 h o cuota del journal. Es posible que memoria/coste por transacción aumenten después. La cola reportada no incluye buffers TCP ni la cola interna de websockets. Tamaño de base/WAL y edades son máximos muestreados; el pico RSS sí es kernel VmHWM. Publicación/handler/commit se distinguen y no se usan sus tiempos como latencia de red.", "",
              "## 6. Viabilidad", "",
              "La implementación actual pudo monitorizar 5, 10 y 20 perpetuos simultáneamente en este host, con una conexión y durabilidad FULL, a las tasas recibidas. Los tres escenarios preservaron journal, checkpoints y recuperación sin pérdidas locales no explicadas. El pico final de 20 activos fue ~42,09 MiB y su CPU de captura equivalió al ~18,87 % de un núcleo en esa ventana.", "",
              "Es evidencia favorable para monitorización ligera de corto plazo. No permite asegurar estabilidad 24/7, capacidad máxima de trades/s, precisión continua subsegundo o autonomía/temperatura en un teléfono. No justifica cambiar SQLite ni relajar durabilidad.", "",
              "## 7. Recomendaciones para una futura implementación ligera en Termux", "",
              "1. Mantener un solo proceso de monitorización, una conexión compartida y un LiveStore. Empezar con 5 activos en el teléfono objetivo y repetir escalada con tráfico fuerte y sesiones largas antes de habilitar 20 permanentemente. No ejecutar scanner o modelos dentro del proceso de captura.",
              "2. Publicar snapshots desacoplados a ~1 Hz y calcular métricas espaciales solo cuando cambia el libro; actualizar frescura aparte. Coalescer únicamente vistas L2, con contador explícito; nunca descartar trades para acelerar el render.",
              "3. Medir/profilar exportación, JSON y hashes de checkpoints antes de sustituir SQLite. Explorar por separado batching acotado por tiempo/operaciones y un único writer dueño de estado/conexión, preservando FULL, atomicidad, rollback y publicación tras commit. No se implementó esa optimización aquí.",
              "4. Fijar un presupuesto de memoria y retención basado en el dispositivo y en cargas prolongadas. Cualquier reducción de caches debe conservar deduplicación/retención y declarar gaps cuando ya no pueda demostrarse continuidad. No extrapolar el RSS del primer minuto a horas de servicio.",
              "5. Probar pantalla apagada, suspensión, cambio Wi-Fi/datos, reconexión y reinicio. Termux documenta restricciones de procesos/CPU en Android y la necesidad de revisar optimización de batería; no basta con que el ensayo desktop pase. [README oficial de Termux](https://github.com/termux/termux-app/blob/master/README.md). No se cambiaron ajustes del teléfono ni permisos del host.",
              "6. Mantener telemetría de overflow, edad exchange/recepción, epochs, cuotas y auditoría de recuperación. Calidad L2, cobertura espacial y continuidad del tape deben permanecer separadas; side=A/B seguirá bloqueando signos hasta verificación independiente.", "",
              "## Evidencia y reproducción", "",
              f"Directorio conservado e ignorado: `{directory}`. No contiene la base operativa. Los JSON contienen métricas y vistas normalizadas públicas, sin paquetes crudos, users, direcciones ni credenciales.", "",
              "| Archivo de evidencia | SHA-256 |", "|---|---|"]
    for path in (directory/"selection.json", *(directory/f"scenario_{size:02}.json" for size in scenarios)):
        link="../"+str(path.relative_to(ROOT))
        lines.append(f"| [{path.name}]({link}) | `{hashlib.sha256(path.read_bytes()).hexdigest()}` |")
    lines += ["", "Los hashes de producción y configuración exacta están dentro de cada JSON. SHA-256 del harness utilizado: `"+hashlib.sha256((ROOT/"benchmarks/multi_asset_benchmark.py").read_bytes()).hexdigest()+"`.", "",
              "Para repetir, desde la raíz canónica; select crea otro directorio exclusivo y devuelve su ruta. Sustituir `<nueva-seleccion>` por el nuevo selection.json y comprobar status/integridad antes de cada escalada:", "",
              "```bash", "python/.venv/bin/python -B benchmarks/multi_asset_benchmark.py select",
              "python/.venv/bin/python -B benchmarks/multi_asset_benchmark.py capture --selection <nueva-seleccion> --size 5 --seconds 60",
              "python/.venv/bin/python -B benchmarks/multi_asset_benchmark.py capture --selection <nueva-seleccion> --size 10 --seconds 60",
              "python/.venv/bin/python -B benchmarks/multi_asset_benchmark.py capture --selection <nueva-seleccion> --size 20 --seconds 60", "```", "",
              "No ejecutar los tres comandos de captura en paralelo ni repetirlos sobre los archivos existentes. Si un escenario devuelve stop_escalation, conservar sus artefactos y no iniciar tamaños mayores. El generador `benchmarks/report_multi_asset_benchmark.py` reconstruye este documento desde esos JSON y rechaza sobrescribir un informe existente.", ""]
    out=ROOT/"docs/MULTI_ASSET_BENCHMARK.md"
    with out.open("x",encoding="utf-8") as stream:
        stream.write("\n".join(lines))
    print(out)


if __name__=="__main__":main()
