"""Offline, deterministic screening of existing Market State v1.1 snapshots."""
from __future__ import annotations

import argparse
import copy
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .integrity import attach_checksum, verify_checksum

SCHEMA_VERSION = "1.0"
RULES_VERSION = "1.0"
TIMEFRAMES = {"5m": 300_000, "15m": 900_000, "1h": 3_600_000, "4h": 14_400_000}
# Screening heuristics, not calibrated trading or execution recommendations.
POLICY = {
    "min_candles": 120,
    "max_context_capture_age_ms": 30_000,
    "max_orderbook_age_ms": 15_000,
    "max_funding_age_ms": 5_400_000,
    "ohlcv_age_multiplier": 2,
    "max_future_skew_ms": 5_000,
    "min_day_notional_volume_usd": 1_000_000,
    "max_spread_bps": 10,
    "min_atr_15m_pct": 0.1,
    "max_atr_15m_pct": 5,
    "min_relative_volume": 1.2,
}
EXPECTED_UNKNOWN_SCOPES = {"market_context", "derivatives.open_interest"}


def _get(value: Any, *keys: str) -> Any:
    for key in keys:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def _number(value: Any) -> float | None:
    # Input is normalized JSON; numeric strings and booleans are not numbers.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return float(value) if math.isfinite(value) else None
    except OverflowError:
        return None


def _utc_ms(value: Any) -> int | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.utcoffset() is None:
            return None
        return int(parsed.timestamp() * 1000)
    except (ValueError, OverflowError, OSError):
        return None


def build_pre_analysis(snapshot: dict[str, Any], *, as_of_ms: int) -> dict[str, Any]:
    """Screen without I/O or input mutation; identical input/time gives identical JSON.

    SKIP means unusable data or unsuitable screening conditions. WATCH means valid
    conditions without the v1 confluence. ANALYZE means eligible for model review,
    never an instruction to open a position. Account state is deliberately omitted.
    """
    if not isinstance(snapshot, dict):
        raise ValueError("market snapshot must be a JSON object")
    if isinstance(as_of_ms, bool) or not isinstance(as_of_ms, int):
        raise ValueError("as_of_ms must be integer epoch milliseconds")
    try:
        as_of_utc = datetime.fromtimestamp(as_of_ms / 1000, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    except (ValueError, OverflowError, OSError) as exc:
        raise ValueError("as_of_ms is out of range") from exc

    blockers: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    checks: list[dict[str, Any]] = []

    def block(code: str, scope: str, detail: str) -> None:
        blockers.append({"code": code, "scope": scope, "detail": detail})

    def check(rule: str, observed: Any, passed: bool, threshold: Any) -> None:
        checks.append({"rule": rule, "observed": observed, "threshold": threshold, "passed": passed})

    try:
        checksum_ok = verify_checksum(snapshot)
    except (TypeError, ValueError, OverflowError):
        checksum_ok = False
    check("source_checksum", checksum_ok, checksum_ok, True)
    if not checksum_ok:
        block("invalid_checksum", "integrity", "source checksum absent or invalid")
    if snapshot.get("schema_version") != "1.1":
        block("unsupported_schema", "schema_version", "requires Market State v1.1")
    if not isinstance(snapshot.get("symbol"), str) or not snapshot["symbol"].isalnum():
        block("invalid_symbol", "symbol", "symbol must be alphanumeric")
    if _get(snapshot, "source", "read_only") is not True:
        block("invalid_source", "source.read_only", "requires a read-only snapshot")

    quality = snapshot.get("data_quality")
    if not isinstance(quality, dict) or quality.get("status") not in ("ok", "degraded"):
        block("invalid_quality", "data_quality", "quality status must be ok or degraded")
    issues = _get(snapshot, "data_quality", "issues")
    if not isinstance(issues, list):
        block("invalid_quality", "data_quality.issues", "issues must be a list")
        issues = []
    for issue in issues:
        if not isinstance(issue, dict):
            block("invalid_issue", "data_quality.issues", "issue must be an object")
        elif issue.get("code") == "unknown_freshness" and isinstance(issue.get("scope"), str) and issue["scope"] in EXPECTED_UNKNOWN_SCOPES:
            warnings.append({"code": issue["code"], "scope": issue["scope"], "detail": str(issue.get("detail", ""))})
        else:
            block("source_issue", str(issue.get("scope", "data_quality")), str(issue.get("code", "unknown issue")))
    errors = _get(snapshot, "data_quality", "validation_errors")
    if not isinstance(errors, list) or errors:
        block("source_validation_errors", "data_quality.validation_errors", "validation errors absent or nonempty")

    ages: dict[str, Any] = {}

    def freshness(scope: str, timestamp: Any, tolerance: int, basis: str) -> None:
        stamp = _utc_ms(timestamp)
        age = as_of_ms - stamp if stamp is not None else None
        passed = age is not None and -POLICY["max_future_skew_ms"] <= age <= tolerance
        ages[scope] = {"age_ms": age, "tolerance_ms": tolerance, "basis": basis, "usable": passed}
        if not passed:
            block("unusable_timestamp", scope, "timestamp missing, invalid, too far in future, or stale at evaluation")

    freshness("snapshot", snapshot.get("timestamp_utc"), POLICY["max_context_capture_age_ms"], "local_snapshot_start")
    sources = _get(snapshot, "capture", "sources")
    # Fresh response receipt is a bounded proxy, never proof of exchange freshness.
    for name in ("market_context_and_prices", "open_interest"):
        freshness(name, _get(sources, name, "captured_at_utc"), POLICY["max_context_capture_age_ms"], "local_response_receipt")
        warnings.append({"code": "source_time_unavailable", "scope": name,
                         "detail": "local response receipt bounds age; exchange freshness is unknown"})
    for name, tolerance in (("orderbook", POLICY["max_orderbook_age_ms"]), ("funding", POLICY["max_funding_age_ms"])):
        freshness(name, _get(sources, name, "source_timestamp_utc"), tolerance, "exchange_source_time")

    features: dict[str, Any] = {}
    for interval, duration in TIMEFRAMES.items():
        frame = _get(snapshot, "timeframes", interval)
        freshness(f"timeframes.{interval}", _get(frame, "last_close_utc"), duration * POLICY["ohlcv_age_multiplier"], "closed_candle_time")
        count = _number(_get(frame, "candle_count"))
        if count is None or count < POLICY["min_candles"]:
            block("insufficient_candles", f"timeframes.{interval}", "at least 120 closed candles required")
        indicator = _get(frame, "indicators")
        ema = {period: _number(_get(indicator, "ema", period)) for period in ("21", "50", "120")}
        rvol = _number(_get(indicator, "volume_relative_20"))
        atr = _number(_get(indicator, "atr_14_pct"))
        if any(v is None or v <= 0 for v in ema.values()) or rvol is None or rvol < 0 or atr is None or atr < 0:
            block("invalid_indicators", f"timeframes.{interval}", "requires positive EMA21/50/120 and nonnegative relative volume/ATR")
        trend = "mixed"
        if all(v is not None and v > 0 for v in ema.values()):
            if ema["21"] > ema["50"] > ema["120"]:
                trend = "up"
            elif ema["21"] < ema["50"] < ema["120"]:
                trend = "down"
        features[interval] = {"trend": trend, "ema": ema, "relative_volume_20": rvol, "atr_14_pct": atr}

    mark = _number(_get(snapshot, "price", "mark"))
    bid = _number(_get(snapshot, "orderbook", "best_bid"))
    ask = _number(_get(snapshot, "orderbook", "best_ask"))
    valid_book = bid is not None and ask is not None and 0 < bid <= ask
    if mark is None or mark <= 0 or not valid_book:
        block("invalid_prices", "price/orderbook", "requires positive mark and uncrossed positive bid/ask")
    midpoint = bid / 2 + ask / 2 if valid_book else None
    spread_bps = (ask - bid) / midpoint * 10_000 if midpoint else None
    for side in ("bid_total_size", "ask_total_size"):
        depth = _number(_get(snapshot, "orderbook", "depth", side))
        if depth is None or depth <= 0:
            block("invalid_depth", f"orderbook.depth.{side}", "requires positive depth on both sides")
    volume = _number(_get(snapshot, "market_context", "day_notional_volume"))
    liquidity_ok = volume is not None and volume >= POLICY["min_day_notional_volume_usd"]
    spread_ok = spread_bps is not None and spread_bps <= POLICY["max_spread_bps"]
    atr15 = features["15m"]["atr_14_pct"]
    volatility_ok = atr15 is not None and POLICY["min_atr_15m_pct"] <= atr15 <= POLICY["max_atr_15m_pct"]
    for rule, observed, passed, threshold in (
        ("daily_liquidity", volume, liquidity_ok, {"min_usd": POLICY["min_day_notional_volume_usd"]}),
        ("spread", spread_bps, spread_ok, {"max_bps": POLICY["max_spread_bps"]}),
        ("volatility_15m", atr15, volatility_ok, {"min_pct": POLICY["min_atr_15m_pct"], "max_pct": POLICY["max_atr_15m_pct"]}),
    ):
        check(rule, observed, passed, threshold)
        if not passed:
            block("screening_condition_failed", rule, "value outside v1 screening limits")

    trend15, trend1h, trend4h = (features[t]["trend"] for t in ("15m", "1h", "4h"))
    aligned = trend15 in ("up", "down") and trend15 == trend1h and trend4h in (trend15, "mixed")
    activity = any(features[t]["relative_volume_20"] is not None and features[t]["relative_volume_20"] >= POLICY["min_relative_volume"] for t in ("5m", "15m"))
    check("trend_confluence", {"15m": trend15, "1h": trend1h, "4h": trend4h}, aligned,
          "15m and 1h aligned; 4h aligned or mixed")
    check("relative_volume", {t: features[t]["relative_volume_20"] for t in ("5m", "15m")}, activity,
          {"min_on_either_timeframe": POLICY["min_relative_volume"]})
    classification = "SKIP" if blockers else "ANALYZE" if aligned and activity else "WATCH"
    reasons = (["blocking_checks"] if blockers else ["trend_and_activity_confluence"] if classification == "ANALYZE" else
               [code for code, passed in (("trend_confluence_absent", aligned), ("relative_volume_below_threshold", activity)) if not passed])
    result = {
        "schema_version": SCHEMA_VERSION, "kind": "pre_analysis", "rules_version": RULES_VERSION,
        "symbol": snapshot.get("symbol") if isinstance(snapshot.get("symbol"), str) else None,
        "as_of_utc": as_of_utc, "market_timestamp_utc": snapshot.get("timestamp_utc") if isinstance(snapshot.get("timestamp_utc"), str) else None,
        "source_checksum": _get(snapshot, "integrity", "checksum") if checksum_ok else None,
        "classification": classification, "reasons": reasons, "policy": copy.deepcopy(POLICY),
        "quality": {"blockers": blockers, "warnings": warnings, "ages": ages}, "checks": checks,
        "features": {"mark": mark, "day_notional_volume_usd": volume, "spread_bps": spread_bps, "timeframes": features},
        "read_only": True,
    }
    attach_checksum(result)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshots", type=Path, nargs="+", help="existing Market State JSON files")
    parser.add_argument("--as-of", help="timezone-aware ISO timestamp; defaults to current UTC time")
    args = parser.parse_args(argv)
    as_of_ms = _utc_ms(args.as_of) if args.as_of else int(datetime.now(timezone.utc).timestamp() * 1000)
    if as_of_ms is None:
        parser.error("--as-of must be a timezone-aware ISO timestamp")
    try:
        results = []
        for path in args.snapshots:
            with path.open(encoding="utf-8") as handle:
                results.append(build_pre_analysis(json.load(handle), as_of_ms=as_of_ms))
        print(json.dumps(results, sort_keys=True, indent=2, allow_nan=False))
    except (OSError, ValueError, TypeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
