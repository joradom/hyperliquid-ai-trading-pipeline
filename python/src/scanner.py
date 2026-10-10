"""Live read-only Market State -> offline pre-analysis orchestration, in memory."""
from __future__ import annotations

import argparse
import copy
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Callable, Iterable

from .decision_snapshot import build_decision_snapshot
from .hl_client import HyperliquidAPIError
from .market_state import build_market_state
from .pre_analysis import POLICY, build_pre_analysis
from .research_config import ResearchConfig, load_config

SCHEMA_VERSION = "1.1"
MAX_CONCURRENT_CAPTURES = 3


def normalize_symbols(symbols: Iterable[str]) -> list[str]:
    """Validate the whole batch before network I/O; deduplicate in input order."""
    if isinstance(symbols, (str, bytes)):
        raise ValueError("symbols must be a list of market symbols")
    normalized = []
    for symbol in symbols:
        if not isinstance(symbol, str) or not re.fullmatch(r"[A-Za-z0-9]+", symbol):
            raise ValueError(f"invalid symbol: {symbol!r}; expected ASCII letters/digits")
        symbol = symbol.upper()
        if symbol not in normalized:
            normalized.append(symbol)
    if not normalized:
        raise ValueError("at least one symbol is required")
    return normalized


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def scan_symbols(symbols: Iterable[str], *,
                 market_builder: Callable[[str], dict[str, Any]] | None = None,
                 clock_ms: Callable[[], int] | None = None,
                 timer: Callable[[], float] | None = None,
                 configuration: ResearchConfig | None = None) -> dict[str, Any]:
    """Capture up to three symbols concurrently; screen each on its worker.

    ANALYZE rows immediately build a decision snapshot at the same evaluation
    clock, passing the exact market and pre-analysis objects. Injectable
    builder/clocks allow offline tests; no persistence or account API is called.
    Every successful row includes the unchanged, complete pre-analysis result.
    """
    symbols = normalize_symbols(symbols)
    if configuration is not None and not isinstance(configuration, ResearchConfig):
        raise ValueError("configuration must be ResearchConfig")
    policy_kwargs = {"policy": {**POLICY, **configuration.scanner}} if configuration is not None else {}
    market_builder = market_builder or build_market_state
    clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)
    timer = timer or time.perf_counter
    started = timer()
    started_at = _iso(clock_ms())
    def scan_one(symbol: str) -> dict[str, Any]:
        row_started = timer()
        capture_seconds = pre_seconds = decision_seconds = 0.0
        stage = "capture"
        stage_started = row_started
        market = None
        analysis = None
        decision = None
        error = None
        try:
            market = market_builder(symbol)
            capture_seconds = timer() - stage_started
            stage = "pre_analysis"
            stage_started = timer()
            as_of_ms = clock_ms()
            analysis = build_pre_analysis(market, as_of_ms=as_of_ms, **policy_kwargs)
            # A builder returning another asset must never classify this symbol.
            if analysis["symbol"] != symbol:
                raise ValueError(f"market symbol mismatch: requested {symbol}, received {analysis['symbol']}")
            pre_seconds = timer() - stage_started
            if analysis["classification"] == "ANALYZE":
                stage = "decision_snapshot"
                stage_started = timer()
                decision = build_decision_snapshot(market, as_of_ms=as_of_ms, pre_analysis=analysis, **policy_kwargs)
                decision_seconds = timer() - stage_started
        except (HyperliquidAPIError, OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError) as exc:
            duration = timer() - stage_started
            if stage == "capture": capture_seconds = duration
            elif stage == "pre_analysis": pre_seconds = duration
            else: decision_seconds = duration
            if stage != "decision_snapshot": analysis = None
            decision = None
            error = {"stage": stage, "type": type(exc).__name__, "message": str(exc)}
        return {
            "symbol": symbol,
            "status": "error" if error else "ok",
            "classification": analysis["classification"] if analysis else "SKIP",
            "reasons": copy.deepcopy(analysis["reasons"]) if analysis else [f"{stage}_failed"],
            "market_timestamp_utc": market.get("timestamp_utc") if isinstance(market, dict) else None,
            "market_quality": copy.deepcopy(market.get("data_quality")) if isinstance(market, dict) else None,
            "pre_analysis": analysis,
            "decision_snapshot": decision,
            "timing": {"capture_seconds": round(capture_seconds, 6), "pre_analysis_seconds": round(pre_seconds, 6),
                       "decision_snapshot_seconds": round(decision_seconds, 6),
                       "total_seconds": round(timer() - row_started, 6)},
            "error": error,
        }
    # Each worker owns its market and evaluates it immediately after capture.
    # map yields in input order, even if captures finish in another order.
    with ThreadPoolExecutor(max_workers=min(MAX_CONCURRENT_CAPTURES, len(symbols))) as pool:
        results = list(pool.map(scan_one, symbols))
    completed_at = _iso(clock_ms())
    total_seconds = timer() - started
    errors = sum(row["status"] == "error" for row in results)
    return {
        "kind": "scanner", "schema_version": SCHEMA_VERSION, "read_only": True,
        "status": "failed" if errors == len(results) else "partial_failure" if errors else "ok",
        "started_at_utc": started_at, "completed_at_utc": completed_at,
        "total_seconds": round(total_seconds, 6),
        "capture_seconds_total": round(sum(row["timing"]["capture_seconds"] for row in results), 6),
        "pre_analysis_seconds_total": round(sum(row["timing"]["pre_analysis_seconds"] for row in results), 6),
        "decision_snapshot_seconds_total": round(sum(row["timing"]["decision_snapshot_seconds"] for row in results), 6),
        "symbols": symbols,
        "counts": {label: sum(row["classification"] == label for row in results) for label in ("SKIP", "WATCH", "ANALYZE")},
        "results": results,
    }


def format_report(report: dict[str, Any]) -> str:
    lines = []
    for row in report["results"]:
        quality = (row["market_quality"] or {}).get("status", "unavailable")
        timing = row["timing"]
        lines.append(f"{row['symbol']} {row['classification']} | reasons={','.join(row['reasons'])} | quality={quality}"
                     f" | capture={timing['capture_seconds']:.3f}s pre={timing['pre_analysis_seconds']:.3f}s"
                     f" | decision={timing['decision_snapshot_seconds']:.3f}s"
                     f" | decision_snapshot={'ready' if row['decision_snapshot'] is not None else 'none'}")
        if row["error"]:
            error = row["error"]
            lines.append(f"  error[{error['stage']}]: {error['type']}: {error['message']}")
        else:
            for issue in row["pre_analysis"]["quality"]["blockers"]:
                lines.append(f"  blocker: {issue['code']} | {issue['scope']} | {issue['detail']}")
    lines.append(f"Total capture/pre-analysis/decision-snapshot: {report['total_seconds']:.3f}s | status={report['status']}"
                 f" | SKIP={report['counts']['SKIP']} WATCH={report['counts']['WATCH']} ANALYZE={report['counts']['ANALYZE']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("symbols", nargs="+", help="perpetual market symbols, e.g. HYPE ETH SOL")
    parser.add_argument("--json", action="store_true", help="emit one structured JSON report on stdout")
    parser.add_argument("--config", help="research YAML file (default: config/research.yaml)")
    parser.add_argument("--profile", help="research profile (default: YAML default_profile)")
    args = parser.parse_args(argv)
    try:
        symbols = normalize_symbols(args.symbols)
        configuration = load_config(args.config, args.profile)
    except ValueError as exc:
        parser.error(str(exc))
    report = scan_symbols(symbols, configuration=configuration)
    if args.json:
        print(json.dumps(report, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False))
    else:
        print(format_report(report))
    return 0 if report["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
