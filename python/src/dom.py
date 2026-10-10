"""Foreground public READ-ONLY terminal DOM. Rendering owns no market maths."""
from __future__ import annotations

import argparse
import asyncio
import math
import os
import sys
import tempfile
import time
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .flow_store import FlowStore
from .live_store import LiveSnapshot, LiveStore, freeze, recovery_check
from .stream_types import symbol
from .ws_client import PublicWSClient

DATA_ROOT = Path(__file__).resolve().parents[2] / "data/order_flow"
MISSING = "—"
MAX_RENDER_WIDTH = 136


def number(value, *, compact=False, places=4):
    """Presentation formatting only; never changes a metric or its validity."""
    if value is None:
        return MISSING
    try:
        value = Decimal(str(value))
        if not value.is_finite():
            return MISSING
        if compact:
            for limit, suffix in ((Decimal("1e9"), "G"), (Decimal("1e6"), "M"), (Decimal("1e3"), "k")):
                if abs(value) >= limit:
                    return f"{value / limit:.2f}".rstrip("0").rstrip(".") + suffix
        if places is not None and value and abs(value) < Decimal(10) ** -places:
            return format(value, ".4g")
        text = format(value, "f" if places is None else f".{places}f")
        text = text.rstrip("0").rstrip(".") if "." in text else text
        return format(value, ".7g") if len(text) > 16 else text
    except (InvalidOperation, ValueError, TypeError):
        return MISSING


def age(value):
    return MISSING if value is None else f"{value / 1000:.1f}s"


def obi_percent(value):
    """Display a supplied ratio as percent; leave the immutable input untouched."""
    if value is None:
        return MISSING
    amount = Decimal(str(value))
    if not amount.is_finite():
        return MISSING
    text = f"{amount * 100:.2f}"
    return ("0.00" if Decimal(text) == 0 else text) + "%"


@dataclass(frozen=True)
class DOMFrame:
    symbol: str
    asset_index: int
    asset_count: int
    levels: int
    as_of_ms: int
    flow: LiveSnapshot | None
    l2: LiveSnapshot | None
    transport: Mapping
    paused: bool = False
    keyboard_enabled: bool = True
    show_help: bool = False


class Controls:
    def __init__(self, symbols, levels=10):
        self.symbols = tuple(symbols)
        self.index = 0
        self.levels = levels
        self.paused = False
        self.show_help = False
        self.revision = 0
        self.changed = asyncio.Event()
        self.quit = asyncio.Event()

    @property
    def selected(self):
        return self.symbols[self.index]

    def key(self, key):
        if key.lower() == "q" or key == "\x04":
            self.quit.set()
        elif key.lower() == "p":
            self.paused = not self.paused
        elif key in ("+", "="):
            self.levels = min(20, self.levels + 1)
        elif key == "-":
            self.levels = max(1, self.levels - 1)
        elif key == "\t":
            self.index = (self.index + 1) % len(self.symbols)
        elif key.lower() == "h" or key == "?":
            self.show_help = not self.show_help
        else:
            return
        self.revision += 1
        self.changed.set()


class TerminalKeys:
    """POSIX cbreak input, retaining Ctrl-C signals; restored on every exit."""
    def __init__(self, stream=None):
        self.stream = stream or sys.stdin
        self.queue = asyncio.Queue(maxsize=64)
        self.enabled = False
        self.fd = self.loop = self.original = None

    def __enter__(self):
        if os.name != "posix" or not self.stream.isatty():
            return self
        import termios
        import tty
        self.fd = self.stream.fileno()
        self.loop = asyncio.get_running_loop()
        self.original = termios.tcgetattr(self.fd)
        try:
            tty.setcbreak(self.fd)
            self.loop.add_reader(self.fd, self._read)
            self.enabled = True
        except (OSError, ValueError, NotImplementedError):
            termios.tcsetattr(self.fd, termios.TCSADRAIN, self.original)
            self.original = None
        return self

    def _read(self):
        try:
            data = os.read(self.fd, 64)
        except OSError:
            data = b""
        if not data:
            self.loop.remove_reader(self.fd)
            if not self.queue.full():
                self.queue.put_nowait("q")
            return
        for key in data.decode("utf-8", errors="ignore"):
            if not self.queue.full():
                self.queue.put_nowait(key)

    async def get(self):
        return await self.queue.get()

    def __exit__(self, *exc):
        if self.original is not None:
            import termios
            try:
                self.loop.remove_reader(self.fd)
            finally:
                termios.tcsetattr(self.fd, termios.TCSADRAIN, self.original)
        self.enabled = False


def render_frame(frame: DOMFrame, width: int, height: int):
    """Consume frozen views; scaling bars is purely visual, not OBI/depth maths."""
    from rich.console import Group
    from rich.constrain import Constrain
    from rich.table import Table
    from rich.text import Text

    width = min(width, MAX_RENDER_WIDTH)
    def compose(*items):
        return Constrain(Group(*items), width=width)

    flow = frame.flow.data if frame.flow else {}
    l2 = frame.l2.data if frame.l2 else {}
    metrics = l2.get("metrics", {})
    bbo = metrics.get("bbo") or {}
    asset = frame.transport.get("assets", {}).get(frame.symbol, {})
    freshness = l2.get("freshness", {})
    reasons = l2.get("quality", {}).get("reasons", ())
    book_status = ("WARMUP" if not l2 or "missing_book" in reasons else
                   "STALE" if any("stale" in reason for reason in reasons) else
                   "FUTURE" if any("future" in reason for reason in reasons) else
                   "FRESH" if l2.get("quality", {}).get("usable") else "DEGRADED")
    tape_status = "GAP" if (asset.get("continuity") == "gap_detected" or
                              "historical_gaps" in flow.get("quality", {}).get("reasons", ())) else "NO KNOWN GAP"
    verified = flow.get("side_semantics") == "verified_synthetic_aggressor"
    side_label = "VERIFIED SYNTHETIC" if verified else "UNVERIFIED"
    stamp = datetime.fromtimestamp(frame.as_of_ms / 1000, timezone.utc).strftime("%H:%M:%S")
    paused = "PAUSED · " if frame.paused else ""
    color = "yellow" if frame.paused or book_status != "FRESH" else "dim"
    def line(text, style=""):
        return Text(text, style=style, no_wrap=True, overflow="ellipsis")

    if frame.show_help:
        help_rows = [line(f"AYUDA · {frame.symbol} · READ-ONLY · H/? volver", "bold"),
                     line("READY: ventana utilizable, según el motor.", "green"),
                     line("PARTIAL / obs: sólo valores observados; cobertura insuficiente.", "yellow"),
                     line("WARMUP: todavía falta duración continua para la ventana."),
                     line("GAP: interrupción registrada; WS LIVE no borra el historial.", "yellow"),
                     line("UNVERIFIED: side A/B sin verificar; Delta/CVD no disponibles.", "dim"),
                     line("C / P / U: banda complete / partial / unknown."),
                     line("OBI %: ratio original ×100; obs observado, val válido, — ausente."),
                     line("base: cantidad; quote: nocional; Σ: acumulado; e: epoch WS."),
                     line("k/M/G: miles/millones/miles de millones. Precios sin redondeo extra."),
                     line("Ayuda no pausa captura; P sólo congela presentación.")]
        ws_reasons = asset.get("gap_reasons", {})
        flow_reasons = flow.get("gap_reasons", {})
        if ws_reasons:
            help_rows.append(line("GAP WS: " + ", ".join(f"{key}={value}" for key, value in ws_reasons.items()), "dim"))
        if flow_reasons:
            help_rows.append(line("GAP flujo: " + ", ".join(f"{key}={value}" for key, value in flow_reasons.items()), "dim"))
        help_rows.append(line("Q salir · P pausa · +/- niveles · Tab activo · H/? volver", "dim"))
        return compose(*(help_rows[:max(1, height-1)] + [help_rows[-1]])) if height < len(help_rows) else compose(*help_rows)

    if width < 42 or height < 14:
        rows = [line(f"{paused}{frame.symbol} WS {str(frame.transport.get('state', 'disconnected')).upper()} e{frame.transport.get('connection_epoch', 0)}", "bold cyan"),
                line(f"L2 {book_status} · TAPE {tape_status}", color),
                line(side_label + (" · PAUSED" if frame.paused else ""), "yellow"),
                line(f"Bid {number(bbo.get('best_bid'), places=None)} Ask {number(bbo.get('best_ask'), places=None)}"),
                line(f"Mid {number(bbo.get('midpoint'), places=None)} Spr {number(bbo.get('spread_bps'), places=2)}bps"),
                line(f"Obs trades {number(flow.get('observed_total', {}).get('unique_trades'))}"),
                line(f"OBI obs {obi_percent(metrics.get('obi_legacy', {}).get('observed'))} val {obi_percent(metrics.get('obi_legacy', {}).get('valid'))}"),
                line("Q · P · +/- · Tab · H ayuda", "dim")]
        # Essential states/controls survive even very small terminals.
        return compose(*(rows[:max(1, height-1)] + [rows[-1]])) if height < len(rows) else compose(*rows)

    full = width >= 110 and height >= 30
    reserve = 19 if full else 13
    visible = min(frame.levels, max(1, height - reserve))
    items = [line(f"{paused}R102 DOM · {frame.symbol} [{frame.asset_index+1}/{frame.asset_count}] · READ-ONLY · {stamp} UTC", "bold"),
             line(f"WS {str(frame.transport.get('state', 'disconnected')).upper()} e{frame.transport.get('connection_epoch', 0)} · L2 {book_status} ex {age(freshness.get('exchange', {}).get('age_ms'))} rc {age(freshness.get('receipt', {}).get('age_ms'))} · TAPE {tape_status}", color),
             line(f"Bid {number(bbo.get('best_bid'), places=None)}   Ask {number(bbo.get('best_ask'), places=None)}   Mid {number(bbo.get('midpoint'), places=None)}", "bold"),
             line(f"[{side_label}] · Spread {number(bbo.get('spread_absolute'), places=None)} quote · {number(bbo.get('spread_bps'), places=2)} bps", "dim")]
    if width < 80:
        items[1] = line(f"WS {str(frame.transport.get('state', 'disconnected')).upper()} e{frame.transport.get('connection_epoch', 0)} · L2 {book_status} · TAPE {tape_status}", color)
    depth = metrics.get("depth") or {}
    bids = depth.get("bids", {}).get("levels", ())[:visible]
    asks = depth.get("asks", {}).get("levels", ())[:visible]
    maximum = max((Decimal(row["size_base"]) for row in (*bids, *asks)), default=Decimal(0))
    bar_width = 8 if width >= 80 else 4
    def bar(row, style):
        count = round(float(Decimal(row["size_base"]) / maximum) * bar_width) if row and maximum else 0
        return Text("█" * count, style=style)
    table = Table(expand=False, box=None, padding=(0, 1), show_edge=False, header_style="dim")
    columns = ["Bid px", "base", "Σ base"]
    if full:
        columns += ["quote"]
        if width >= 128:
            columns += ["Σ quote"]
    columns += ["vol"]
    for title in columns:
        table.add_column(title, justify="right", no_wrap=True, style="green" if title == "Bid px" else "")
    for title in ("Ask px", *columns[1:]):
        table.add_column(title, justify="right", no_wrap=True, style="red" if title == "Ask px" else "")
    def cells(row, style):
        if row is None:
            return [MISSING] * len(columns)
        values = [number(row["price"], places=None), number(row["size_base"], compact=True), number(row["cumulative_base"], compact=True)]
        if full:
            values.append(number(row["notional_quote"], compact=True, places=2))
            if width >= 128:
                values.append(number(row["cumulative_quote"], compact=True, places=2))
        return values + [bar(row, style)]
    for index in range(max(len(bids), len(asks), 1)):
        table.add_row(*cells(bids[index] if index < len(bids) else None, "dim green"),
                      *cells(asks[index] if index < len(asks) else None, "dim red"))
    items.append(table)
    legacy = metrics.get("obi_legacy", {})
    items.append(line(f"OBI todos los niveles · obs {obi_percent(legacy.get('observed'))} · válido {obi_percent(legacy.get('valid'))} · sin señal", "dim"))
    bands = metrics.get("bands", {})
    if full:
        obi_table = Table(box=None, padding=(0, 1), expand=True)
        for title in ("Banda ±", "Cobertura", "OBI observado", "OBI válido"):
            obi_table.add_column(title)
        for key, label in (("0.001", "0.10%"), ("0.0025", "0.25%"), ("0.005", "0.50%"), ("0.01", "1.00%")):
            row = bands.get(key, {})
            coverage = str(row.get("coverage", "unknown")).upper()
            obi_table.add_row(label, Text(coverage, style="green" if coverage == "COMPLETE" else "yellow"), obi_percent(row.get("obi_observed")), obi_percent(row.get("obi_valid")))
        items.append(obi_table)
    else:
        entries = []
        for key, label in (("0.001", ".10"), ("0.0025", ".25"), ("0.005", ".50"), ("0.01", "1.0")):
            row = bands.get(key, {})
            coverage = {"complete": "C", "partial": "P", "unknown": "U"}.get(row.get("coverage"), "U")
            if width < 80:
                entries.append(f"{label}% {coverage} val:{obi_percent(row.get('obi_valid'))}")
            else:
                entries.append(f"{label}% {coverage} obs:{obi_percent(row.get('obi_observed'))} val:{obi_percent(row.get('obi_valid'))}")
        items += [line(" | ".join(entries[:2]), "dim"), line(" | ".join(entries[2:]) + "  C/P/U", "dim")]
    totals = flow.get("observed_total", {})
    items.append(line(f"Observado · trades {number(totals.get('unique_trades'))} · base {number(totals.get('volume_base'), compact=True)} · quote {number(totals.get('volume_quote'), compact=True, places=2)}"))
    windows = Table(box=None, padding=(0, 1), expand=False, show_header=full)
    for title in ("Flujo", "Estado", "Trades", "base", "quote", "Delta / CVD base"):
        windows.add_column(title, no_wrap=True)
    for name in ("1m", "5m", "15m"):
        row = flow.get("windows", {}).get(name, {})
        reasons = row.get("quality_reasons", ())
        status = ("STALE" if "stream_stale" in reasons else "WARMUP" if "warmup" in reasons else
                  "GAP" if "disconnected" in reasons else "READY" if row.get("ready") else "PARTIAL")
        partial = row.get("observed_partial", {})
        label = "" if row.get("ready") else "obs "
        display = row if row.get("ready") else partial
        signed = verified and row.get("signed_available")
        delta = f"{number(row.get('delta_base'))} / {number(row.get('cvd_window_base'))}" if signed else "UNVERIFIED" if not verified else MISSING
        status_label = status + (" PARTIAL" if not row.get("ready") and status != "PARTIAL" else "")
        windows.add_row(name, Text(status_label, style="green" if status == "READY" else "yellow"),
                        label + number(display.get("trades")), number(display.get("volume_base"), compact=True),
                        number(display.get("volume_quote"), compact=True, places=2), Text(delta, style="dim"))
    items.append(windows)
    if verified:
        cvd = flow.get("cvd", {})
        items.append(line(f"CVD segmento · base {number(cvd.get('base') if cvd.get('available') else None)} · quote {number(cvd.get('quote') if cvd.get('available') else None)}"))
    controls = "Q salir · P pausa · +/- niveles · Tab activo"
    if frame.paused:
        controls = "P reanudar · ingesta activa · Q · +/- · Tab"
    if not frame.keyboard_enabled:
        controls = "Entrada no interactiva · Ctrl-C salir · --seconds para cierre automático"
    elif width < 80:
        controls = "P seguir · ingesta activa · Q +/- Tab" if frame.paused else "Q salir · P pausa · +/- · Tab"
    help_hint = " · H/?" if frame.keyboard_enabled else ""
    items.append(line(controls + f" · {visible}/{frame.levels} niveles" + help_hint, "dim"))
    return compose(*items)


class RichRenderer:
    """Only immutable frames cross into one presentation worker."""
    def __init__(self, console):
        self.console = console
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="r102-dom-render")
        self.live = None

    def _draw(self, frame):
        from rich.live import Live
        if self.live is None:
            self.live = Live(console=self.console, auto_refresh=False, screen=self.console.is_terminal,
                             vertical_overflow="crop", redirect_stdout=False, redirect_stderr=False)
            self.live.start()
        size = self.console.size
        self.live.update(render_frame(frame, size.width, size.height), refresh=True)

    async def present(self, frame):
        await asyncio.get_running_loop().run_in_executor(self.pool, self._draw, frame)

    async def close(self):
        def stop():
            if self.live is not None:
                self.live.stop()
        try:
            await asyncio.get_running_loop().run_in_executor(self.pool, stop)
        finally:
            self.pool.shutdown(wait=True)


class DOMSession:
    """Single foreground owner; independent ingress and presentation tasks."""
    def __init__(self, client, live, renderer, *, refresh=4, levels=10,
                 clock_ms=None, key_source=None, keyboard_enabled=True, poll_seconds=0.1):
        self.client, self.live, self.renderer = client, live, renderer
        self.controls = Controls(client.symbols, levels)
        self.refresh = refresh
        self.clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)
        self.key_source = key_source
        self.keyboard_enabled = keyboard_enabled
        self.poll_seconds = poll_seconds
        self.ingest_cycles = self.rendered_frames = 0
        self.final_report = None

    def frame(self):
        coin = self.controls.selected
        now = self.clock_ms()
        try:
            flow = self.live.read("mainnet", coin, as_of_ms=now)
            l2 = self.live.read_l2("mainnet", coin, as_of_ms=now)
        except KeyError:
            flow = l2 = None
        return DOMFrame(coin, self.controls.index, len(self.client.symbols), self.controls.levels,
                        now, flow, l2, freeze(self.client.summary()), self.controls.paused, self.keyboard_enabled,
                        self.controls.show_help)

    async def _ingest(self, ws_task):
        while not ws_task.done():
            self.live.sync_ws(self.client, now_ms=self.clock_ms())
            self.ingest_cycles += 1
            await asyncio.sleep(self.poll_seconds)

    async def _present(self):
        previous = None
        revision = -1
        while not self.controls.quit.is_set():
            if not self.controls.paused or self.controls.revision != revision:
                if self.controls.paused and previous is not None and previous.symbol == self.controls.selected:
                    frame = replace(previous, paused=True, levels=self.controls.levels, show_help=self.controls.show_help)
                else:
                    frame = self.frame()
                presented_revision = self.controls.revision
                await self.renderer.present(frame)
                previous, revision = frame, presented_revision
                self.rendered_frames += 1
            self.controls.changed.clear()
            try:
                await asyncio.wait_for(self.controls.changed.wait(), 1 / self.refresh)
            except asyncio.TimeoutError:
                pass

    async def _keys(self):
        while not self.controls.quit.is_set():
            self.controls.key(await self.key_source())

    async def run(self, seconds=None):
        ws_task = asyncio.create_task(self.client.run(seconds), name="dom-ws")
        tasks = [asyncio.create_task(self._ingest(ws_task), name="dom-ingest"),
                 asyncio.create_task(self._present(), name="dom-present"),
                 asyncio.create_task(self.controls.quit.wait(), name="dom-stop")]
        if self.key_source is not None:
            tasks.append(asyncio.create_task(self._keys(), name="dom-keys"))
        ingestion_failed = False
        try:
            done, _ = await asyncio.wait([ws_task, *tasks], return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                if not task.cancelled() and task.exception() is not None:
                    ingestion_failed = task.get_name() == "dom-ingest"
                    raise task.exception()
        finally:
            self.client.stop()
            self.controls.quit.set()
            for task in tasks:
                task.cancel()
            try:
                await asyncio.gather(ws_task, *tasks, return_exceptions=True)
                if not ingestion_failed:
                    while self.client.trade_queue:
                        self.live.sync_ws(self.client, now_ms=self.clock_ms())
                    self.live.sync_ws(self.client, now_ms=self.clock_ms())
                transport = self.client.summary()
                check = recovery_check(self.live.journal, self.live.engine)
                if not check["journal_rows"]:
                    # Assets can exist before the first ACK/transaction. There
                    # is no durable market state to compare on an early quit.
                    check["state_matches"] = None
                self.final_report = {"read_only": True, "execution_authorized": False,
                    "transport": transport, "recovery": check, "remaining_trade_queue": len(self.client.trade_queue),
                    "ingest_cycles": self.ingest_cycles, "rendered_frames": self.rendered_frames,
                    "observed_trades": {coin: self.live.read("mainnet", coin)["observed_total"]["unique_trades"]
                                        for coin in self.client.symbols if ("mainnet", coin) in self.live.engine.assets}}
            finally:
                try:
                    await self.renderer.close()
                finally:
                    self.live.journal.close()
        return self.final_report


def fresh_database(requested=None):
    """Reserve a new file; never reuse or overwrite an operational database."""
    if requested is None:
        parent = DATA_ROOT / "dom_runs"
        parent.mkdir(parents=True, exist_ok=True)
        path = Path(tempfile.mkdtemp(prefix="dom_", dir=parent)) / "flow.sqlite3"
    else:
        path = Path(requested).resolve()
        if not path.is_relative_to(DATA_ROOT.resolve()):
            raise ValueError("--db must be a new path inside data/order_flow")
        path.parent.mkdir(parents=True, exist_ok=True)
    if any(Path(str(path) + suffix).exists() for suffix in ("", "-wal", "-shm")):
        raise ValueError("refusing to reuse an existing database or WAL/SHM")
    with path.open("x"):
        pass
    return path


async def foreground(args, console):
    path = fresh_database(args.db)
    console.print(f"R102 DOM READ-ONLY · SQLite nueva: {path}", style="dim", markup=False)
    with FlowStore(path) as journal:
        live = LiveStore(journal)
        client = PublicWSClient(args.symbols)
        renderer = RichRenderer(console)
        with TerminalKeys() as keyboard:
            session = DOMSession(client, live, renderer, refresh=args.refresh, levels=args.levels,
                                 key_source=keyboard.get if keyboard.enabled else None,
                                 keyboard_enabled=keyboard.enabled)
            report = await session.run(args.seconds)
    recovered = report["recovery"]["state_matches"]
    console.print(f"DOM cerrado · {sum(v or 0 for v in report['observed_trades'].values())} trades observados · "
                  f"recuperación {'SIN DATOS' if recovered is None else 'OK' if recovered else 'ERROR'} · {path}", markup=False)
    healthy = (recovered is not False and not report["remaining_trade_queue"]
               and not report["transport"]["errors"] and not report["transport"]["counts"]["queue_dropped"])
    return 0 if healthy else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("symbols", nargs="+")
    parser.add_argument("--levels", type=int, default=10, help="visible levels per side, 1–20")
    parser.add_argument("--refresh", type=float, default=4, help="visual target Hz, 0.5–20")
    parser.add_argument("--seconds", type=float, help="optional short public capture budget")
    parser.add_argument("--no-color", action="store_true")
    parser.add_argument("--db", type=Path, help="new database path inside ignored data/order_flow")
    args = parser.parse_args(argv)
    try:
        args.symbols = list(dict.fromkeys(symbol(value) for value in args.symbols))
        if not 1 <= len(args.symbols) <= 64:
            raise ValueError("requires 1–64 distinct symbols")
        if not 1 <= args.levels <= 20:
            raise ValueError("levels must be 1–20")
        if not math.isfinite(args.refresh) or not 0.5 <= args.refresh <= 20:
            raise ValueError("refresh must be 0.5–20 Hz")
        if args.seconds is not None and (not math.isfinite(args.seconds) or args.seconds <= 0):
            raise ValueError("seconds must be positive and finite")
    except ValueError as exc:
        parser.error(str(exc))
    try:
        from rich.console import Console
    except ImportError:
        print("DOM requires optional presentation dependencies: .venv/bin/python -m pip install -r requirements-dom.txt", file=sys.stderr)
        return 2
    console = Console(no_color=args.no_color)
    if console.is_terminal and console.is_dumb_terminal:
        print("DOM interactivo requiere un terminal ANSI. TERM=dumb no permite dibujar la pantalla; usa un terminal compatible (por ejemplo TERM=xterm-256color).", file=sys.stderr)
        return 2
    try:
        return asyncio.run(foreground(args, console))
    except KeyboardInterrupt:
        console.print("DOM cerrado por Ctrl-C.", markup=False)
        return 130
    except (OSError, ValueError, RuntimeError) as exc:
        console.print(f"DOM detenido: {type(exc).__name__}. La base nueva se conserva para inspección.", markup=False)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
