"""Discover public markets, scan once and review candidates in memory; no execution."""
from __future__ import annotations

import argparse
import json
import math
import time
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, Callable

from .analysis_pipeline import format_report as format_analysis, run_pipeline
from .fast_revalidation import DEFAULT_DEADLINE_SECONDS, validate_deadline
from .market_screener import screen_market
from .market_context import MarketContextProvider
from .market_state import market_capture_weight
from .research_config import ResearchConfig, SORT_MODES, load_config
from .scanner import normalize_symbols, scan_symbols

SCHEMA_VERSION = "1.0"
SELECTION_METRICS = (
    "price", "price_change_pct_24h", "volume_24h_usd", "oi_usd", "funding_rate",
    "funding_annualized_pct", "volume_today_usd", "volume_prev_24h_usd",
    "vol_delta_pct_24h", "vol_oi_ratio",
)


def _iso(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _finite(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def _json_safe(value, issues, path="screener"):
    """Keep all fields; nonfinite numbers become explicit nulls for strict JSON."""
    if type(value) in (int, float) and not _finite(value):
        issues.append({"stage": "screener", "code": "nonfinite_number", "path": path})
        return None
    if isinstance(value, dict):
        return {key: _json_safe(item, issues, f"{path}.{key}") for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item, issues, f"{path}[{index}]") for index, item in enumerate(value)]
    return value


def _selection_quality(report, issues):
    if (not isinstance(report, dict) or report.get("kind") != "market_screener"
            or report.get("schema_version") != "1.0" or report.get("read_only") is not True
            or report.get("status") not in ("ok", "partial_failure", "failed")
            or not isinstance(report.get("symbols"), list) or not isinstance(report.get("results"), list)):
        raise ValueError("invalid market_screener report")
    rows = report["results"]
    if len(rows) != len(report["symbols"]) or any(not isinstance(row, dict) for row in rows):
        raise ValueError("screener rows and symbols must match")
    if [row.get("symbol") for row in rows] != report["symbols"]:
        raise ValueError("screener ranking and symbols must match")
    runnable, quality = [], []
    for rank, row in enumerate(rows, 1):
        symbol = row["symbol"]
        optional_history = ("volume_prev_24h_usd", "vol_delta_pct_24h") if (
            report.get("parameters", {}).get("sort_by") in ("volume", "oi", "px_change", "momentum")
            and row.get("history_status") == "not_requested" and not row.get("error")
        ) else ()
        unavailable = [key for key in SELECTION_METRICS
                       if key not in optional_history and not _finite(row.get(key))]
        if row.get("error"):
            issues.append({"stage": "screener", "code": "asset_selection_error", "rank": rank,
                           "symbol": symbol, "detail": row["error"]})
        if unavailable:
            issues.append({"stage": "screener", "code": "unavailable_metrics", "rank": rank,
                           "symbol": symbol, "fields": unavailable})
        try:
            normalized = normalize_symbols([symbol])[0]
        except ValueError:
            normalized = None
            issues.append({"stage": "screener", "code": "unsupported_symbol", "rank": rank, "symbol": symbol})
        if normalized is not None:
            if normalized in runnable:
                issues.append({"stage": "screener", "code": "duplicate_symbol", "rank": rank, "symbol": symbol})
            else:
                runnable.append(normalized)
        quality.append({"rank": rank, "symbol": symbol, "pipeline_symbol": normalized,
                        "status": "degraded" if row.get("error") or unavailable or normalized is None else "ok",
                        "unavailable_metrics": unavailable})
    return runnable, quality


def run_research(*, configuration: ResearchConfig | None = None,
                 top_n: int | None = None, limit: int | None = None, sort_by: str | None = None,
                 min_volume: int | float | None = None, max_workers: int | None = None,
                 screener: Callable[..., dict[str, Any]] | None = None,
                 pipeline: Callable[..., dict[str, Any]] | None = None,
                 scanner: Callable[..., dict[str, Any]] | None = None,
                 runner: Callable[..., Any] | None = None, revalidator: Callable[..., dict[str, Any]] | None = None,
                 clock_ms: Callable[[], int] | None = None, timer: Callable[[], float] | None = None,
                 revalidation_deadline_seconds: float = DEFAULT_DEADLINE_SECONDS,
                 context_provider: MarketContextProvider | None = None,
                 incremental: bool | None = None, max_admitted: int | None = None,
                 admission_wait: Callable[[float], None] | None = None) -> dict[str, Any]:
    """Overrides > selected profile > defaults; APIs do not read configuration files.

    The existing analysis pipeline invokes capture_scan once and owns all gates,
    snapshots, model calls and revalidation. Discovery never supplies Market State.
    Full internal reports stay in memory; only nonfinite discovery numbers are
    changed to null, with their original field paths recorded in selection issues.
    """
    if configuration is not None and not isinstance(configuration, ResearchConfig):
        raise ValueError("configuration must be ResearchConfig")
    settings = configuration or ResearchConfig()
    overrides = {name: value for name, value in (
        ("top_n", top_n), ("limit", limit), ("sort_by", sort_by),
        ("min_volume", min_volume), ("max_workers", max_workers)) if value is not None}
    if incremental is not None and type(incremental) is not bool:
        raise ValueError("incremental must be bool or None")
    if max_admitted is not None and (type(max_admitted) is not int or not 1 <= max_admitted <= 3):
        raise ValueError("max_admitted must be between 1 and 3")
    if admission_wait is not None and not callable(admission_wait):
        raise ValueError("admission_wait must be callable")
    if pipeline is not None and (incremental is not None or max_admitted is not None or admission_wait is not None):
        raise ValueError("incremental options require the native analysis pipeline")
    parameters = replace(settings.screener, **overrides).parameters
    validate_deadline(revalidation_deadline_seconds)
    clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)
    timer = timer or time.perf_counter
    started = timer()
    report = {"kind": "research_pipeline", "schema_version": SCHEMA_VERSION, "read_only": True,
              "execution_authorized": False, "profile": settings.profile, "parameters": parameters,
              "status": "ok", "outcome": "completed", "started_at_utc": _iso(clock_ms()),
              "completed_at_utc": None, "selected_symbols": [], "pipeline_symbols": [],
              "screener": None, "scanner": None, "analysis": None,
              "selection_quality": {"status": "ok", "assets": [], "issues": []}, "errors": [],
              "timing": {"screener_seconds": 0.0, "analysis_pipeline_seconds": 0.0, "total_seconds": 0.0}}
    issues = report["selection_quality"]["issues"]
    stage = "screener"
    stage_started = timer()

    provider = context_provider or MarketContextProvider(rest_config=settings.rest)

    def capture_scan(symbols, **kwargs):
        if report["scanner"] is not None:
            raise ValueError("research pipeline must invoke scanner only once")
        if scanner is None:
            kwargs["context_provider"] = provider
        captured = (scanner or scan_symbols)(symbols, **kwargs)
        report["scanner"] = captured
        return captured

    try:
        selection = (screener or screen_market)(**parameters, clock_ms=clock_ms, timer=timer,
            **({"context_provider": provider,
                "scanner_headroom": market_capture_weight() if scanner is None and pipeline is None else 0}
               if screener is None else {}))
        report["screener"] = _json_safe(selection, issues)
        runnable, quality = _selection_quality(report["screener"], issues)
        report["selected_symbols"] = list(report["screener"]["symbols"])
        report["pipeline_symbols"] = runnable
        report["selection_quality"]["assets"] = quality
        if report["screener"]["status"] == "failed":
            raise ValueError("market discovery failed")
        report["timing"]["screener_seconds"] = round(timer() - stage_started, 6)
        if not runnable:
            if report["selected_symbols"]:
                raise ValueError("no selected symbol is supported by scanner")
            report["outcome"] = "no_candidates"
        else:
            stage = "analysis_pipeline"
            stage_started = timer()
            optional = {name: value for name, value in (("runner", runner), ("revalidator", revalidator)) if value is not None}
            if pipeline is None:
                if incremental is not None or (
                        getattr(scanner or scan_symbols, "supports_incremental_review", False) is True):
                    optional["incremental"] = incremental if incremental is not None else True
                if max_admitted is not None:
                    optional["max_admitted"] = max_admitted
                if admission_wait is not None:
                    optional["admission_wait"] = admission_wait
            analysis = (pipeline or run_pipeline)(runnable, configuration=settings, scanner=capture_scan,
                clock_ms=clock_ms, timer=timer, revalidation_deadline_seconds=revalidation_deadline_seconds, **optional)
            report["analysis"] = analysis
            if (not isinstance(analysis, dict) or analysis.get("kind") != "analysis_pipeline"
                    or analysis.get("schema_version") not in ("1.2", "1.3")
                    or analysis.get("read_only") is not True or analysis.get("execution_authorized") is not False
                    or analysis.get("status") not in ("ok", "partial_failure", "failed")):
                raise ValueError("invalid analysis_pipeline report")
            report["status"] = analysis["status"]
            report["outcome"] = "analysis_failed" if analysis["status"] == "failed" else "completed"
            report["timing"]["analysis_pipeline_seconds"] = round(timer() - stage_started, 6)
        if report["status"] == "ok" and (issues or report["screener"]["status"] == "partial_failure"
                or (report["scanner"] is not None and report["scanner"].get("status") == "partial_failure")):
            report["status"] = "partial_failure"
    except Exception as exc:
        report["status"] = "failed"
        report["outcome"] = "selection_failed" if stage == "screener" else "analysis_failed"
        report["errors"].append({"stage": stage, "type": type(exc).__name__, "message": str(exc)})
        key = "screener_seconds" if stage == "screener" else "analysis_pipeline_seconds"
        report["timing"][key] = round(timer() - stage_started, 6)
    report["selection_quality"]["status"] = (
        "failed" if report["outcome"] == "selection_failed" else "degraded" if issues or
        report["screener"]["status"] == "partial_failure" else "ok")
    report["completed_at_utc"] = _iso(clock_ms())
    report["timing"]["total_seconds"] = round(timer() - started, 6)
    return report


def format_report(report):
    lines = [f"Research | profile={report['profile']} | status={report['status']} | outcome={report['outcome']}"]
    if isinstance(report["screener"], dict) and isinstance(report["screener"].get("results"), list):
        for rank, row in enumerate(report["screener"]["results"], 1):
            if not isinstance(row, dict):
                continue
            delta = row.get("vol_delta_pct_24h")
            value = f"{delta:+.2f}%" if _finite(delta) else "unavailable"
            lines.append(f"  #{rank} {row.get('symbol')} | vol_delta={value}")
    for issue in report["selection_quality"]["issues"]:
        lines.append(f"  selection issue: {issue['code']} | symbol={issue.get('symbol', 'n/a')}")
    if report["analysis"] is not None:
        try:
            lines.append(format_analysis(report["analysis"]))
        except (KeyError, TypeError, AttributeError):
            lines.append("  analysis report failed validation; see JSON and errors")
    for error in report["errors"]:
        lines.append(f"  error[{error['stage']}]: {error['type']}: {error['message']}")
    times = report["timing"]
    lines.append(f"Discovery={times['screener_seconds']:.3f}s | analysis={times['analysis_pipeline_seconds']:.3f}s"
                 f" | total={times['total_seconds']:.3f}s | execution_authorized=false")
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", help="research YAML file (default: config/research.yaml)")
    parser.add_argument("--profile", help="research profile (default: YAML default_profile)")
    parser.add_argument("--top-n", type=int, help="initial candidate pool by volume")
    parser.add_argument("--limit", type=int, help="maximum selected candidates")
    parser.add_argument("--sort-by", choices=SORT_MODES, help="final ranking criterion")
    parser.add_argument("--min-volume", type=float, help="minimum daily USD volume for discovery")
    parser.add_argument("--max-workers", type=int, help="discovery candle workers")
    parser.add_argument("--revalidation-deadline-seconds", type=float, default=DEFAULT_DEADLINE_SECONDS)
    parser.add_argument("--json", action="store_true", help="emit one complete research_pipeline v1.0 report")
    args = parser.parse_args(argv)
    try:
        settings = load_config(args.config, args.profile)
        report = run_research(configuration=settings, top_n=args.top_n, limit=args.limit, sort_by=args.sort_by,
            min_volume=args.min_volume, max_workers=args.max_workers,
            revalidation_deadline_seconds=args.revalidation_deadline_seconds)
    except ValueError as exc:
        parser.error(str(exc))
    print(json.dumps(report, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
          if args.json else format_report(report))
    return 0 if report["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
