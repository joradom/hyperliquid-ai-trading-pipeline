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
from .hl_client import HyperliquidAPIError, reservation_scope_for
from .market_context import MarketContextProvider
from .market_state import build_market_state, market_capture_weight
from .pre_analysis import EXPECTED_UNKNOWN_SCOPES, POLICY, build_pre_analysis
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


def capture_warnings(row: dict[str, Any]) -> list[dict[str, Any]]:
    """Acquisition degradation, excluding explicitly tolerated source warnings.

    Keep reasons in their existing market_quality/pre_analysis JSON locations.
    Legitimate deterministic SKIP/WATCH classifications are not failures.
    """
    quality = row.get("market_quality") or {}
    issues = [issue for issue in quality.get("issues", [])
              if not (issue.get("code") == "unknown_freshness"
                      and issue.get("scope") in EXPECTED_UNKNOWN_SCOPES)]
    if quality.get("validation_errors") or quality.get("status") == "invalid":
        issues.append({"code": "source_validation_errors", "scope": "data_quality",
                       "detail": "capture failed data validation"})
    for issue in (row.get("pre_analysis") or {}).get("quality", {}).get("blockers", []):
        if issue.get("code") == "unusable_timestamp":
            issues.append(issue)
    return issues


def scan_symbols(symbols: Iterable[str], *,
                 market_builder: Callable[[str], dict[str, Any]] | None = None,
                 clock_ms: Callable[[], int] | None = None,
                 timer: Callable[[], float] | None = None,
                 configuration: ResearchConfig | None = None,
                 context_provider: MarketContextProvider | None = None,
                 capture_admission: Callable[..., Any] | None = None,
                 on_result: Callable[[int, dict[str, Any]], None] | None = None) -> dict[str, Any]:
    """Capture up to three symbols concurrently; screen each on its worker.

    ANALYZE rows immediately build a decision snapshot at the same evaluation
    clock, passing the exact market and pre-analysis objects. Injectable
    builder/clocks allow offline tests; no persistence or account API is called.
    Every successful row includes the unchanged, complete pre-analysis result.
    """
    symbols = normalize_symbols(symbols)
    if configuration is not None and not isinstance(configuration, ResearchConfig):
        raise ValueError("configuration must be ResearchConfig")
    if capture_admission is not None and not callable(capture_admission):
        raise ValueError("capture_admission must be callable")
    if on_result is not None and not callable(on_result):
        raise ValueError("on_result must be callable")
    policy_kwargs = {"policy": {**POLICY, **configuration.scanner}} if configuration is not None else {}
    reserve_rest = None
    if market_builder is None:
        settings = configuration or ResearchConfig()
        provider = context_provider or MarketContextProvider(rest_config=settings.rest)
        reserve_rest = lambda: reservation_scope_for(provider.client, market_capture_weight())
        market_builder = lambda symbol: build_market_state(symbol, context_provider=provider,
            deadline_seconds=settings.rest.capture_deadline_seconds, clock_ms=clock_ms)
    clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)
    timer = timer or time.perf_counter
    started = timer()
    started_at = _iso(clock_ms())
    def capture_one(symbol: str) -> dict[str, Any]:
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
    def scan_one(index_symbol):
        index, symbol = index_symbol
        row = None
        def capture_and_deliver():
            nonlocal row
            if reserve_rest is None:
                row = capture_one(symbol)
            else:
                admission_started = timer()
                try:
                    with reserve_rest():
                        row = capture_one(symbol)
                except HyperliquidAPIError as exc:
                    # Deliver rejection while the review lease is still owned;
                    # otherwise an incremental consumer would lose this row.
                    row = {"symbol": symbol, "status": "error", "classification": "SKIP",
                           "reasons": ["capture_admission_failed"], "market_timestamp_utc": None,
                           "market_quality": None, "pre_analysis": None, "decision_snapshot": None,
                           "timing": {"capture_seconds": 0.0, "pre_analysis_seconds": 0.0,
                                      "decision_snapshot_seconds": 0.0,
                                      "total_seconds": round(timer() - admission_started, 6)},
                           "error": {"stage": "capture_admission", "type": type(exc).__name__,
                                     "message": "capture admission failed: " + exc.code}}
            if on_result is not None:
                try:
                    on_result(index, row)
                except Exception as exc:
                    row["status"] = "error"
                    row["error"] = {"stage": "review_handoff", "type": type(exc).__name__,
                                    "message": "incremental review handoff failed"}
            return row
        if capture_admission is None and reserve_rest is None:
            return capture_and_deliver()
        waiting_started = timer()
        try:
            # Waiting for review capacity precedes capture clocks and deadline.
            if capture_admission is None:
                return capture_and_deliver()
            with capture_admission(index, symbol):
                return capture_and_deliver()
        except Exception as exc:
            if row is not None:
                row["status"] = "error"
                row["error"] = {"stage": "capture_admission", "type": type(exc).__name__, "message": ("capture admission failed: " + exc.code) if isinstance(exc, HyperliquidAPIError) else "capture admission failed"}
                return row
            return {"symbol": symbol, "status": "error", "classification": "SKIP",
                    "reasons": ["capture_admission_failed"], "market_timestamp_utc": None,
                    "market_quality": None, "pre_analysis": None, "decision_snapshot": None,
                    "timing": {"capture_seconds": 0.0, "pre_analysis_seconds": 0.0,
                               "decision_snapshot_seconds": 0.0, "total_seconds": round(timer() - waiting_started, 6)},
                    "error": {"stage": "capture_admission", "type": type(exc).__name__, "message": ("capture admission failed: " + exc.code) if isinstance(exc, HyperliquidAPIError) else "capture admission failed"}}

    # Each worker owns its market and evaluates it immediately after capture.
    # map yields in input order, even if captures finish in another order.
    with ThreadPoolExecutor(max_workers=min(MAX_CONCURRENT_CAPTURES, len(symbols))) as pool:
        results = list(pool.map(scan_one, enumerate(symbols)))
    completed_at = _iso(clock_ms())
    total_seconds = timer() - started
    errors = sum(row["status"] == "error" for row in results)
    degraded = any(capture_warnings(row) for row in results)
    return {
        "kind": "scanner", "schema_version": SCHEMA_VERSION, "read_only": True,
        "status": "failed" if errors == len(results) else "partial_failure" if errors or degraded else "ok",
        "started_at_utc": started_at, "completed_at_utc": completed_at,
        "total_seconds": round(total_seconds, 6),
        "capture_seconds_total": round(sum(row["timing"]["capture_seconds"] for row in results), 6),
        "pre_analysis_seconds_total": round(sum(row["timing"]["pre_analysis_seconds"] for row in results), 6),
        "decision_snapshot_seconds_total": round(sum(row["timing"]["decision_snapshot_seconds"] for row in results), 6),
        "symbols": symbols,
        "counts": {label: sum(row["classification"] == label for row in results) for label in ("SKIP", "WATCH", "ANALYZE")},
        "results": results,
    }


# Explicit capability; mocks/legacy callables do not opt in by accepting **kwargs.
scan_symbols.supports_incremental_review = True


def format_report(report: dict[str, Any]) -> str:
    lines = []
    for row in report["results"]:
        quality = (row["market_quality"] or {}).get("status", "unavailable")
        timing = row["timing"]
        lines.append(f"{row['symbol']} {row['classification']} | reasons={','.join(row['reasons'])} | quality={quality}"
                     f" | capture={timing['capture_seconds']:.3f}s pre={timing['pre_analysis_seconds']:.3f}s"
                     f" | decision={timing['decision_snapshot_seconds']:.3f}s"
                     f" | decision_snapshot={'ready' if row['decision_snapshot'] is not None else 'none'}")
        for issue in capture_warnings(row):
            lines.append(f"  warning[capture_degraded]: {issue['code']} | {issue.get('scope', 'capture')}"
                         f" | {issue.get('detail', '')}")
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
