"""Versioned, offline market decision contract for model review; no execution."""
from __future__ import annotations

import argparse
import copy
import json
import math
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from statistics import fmean
from typing import Any

from .integrity import attach_checksum, verify_checksum
from .pre_analysis import POLICY, TIMEFRAMES, build_pre_analysis

SCHEMA_VERSION = "1.0"
DERIVATION_VERSION = "1.0"


def _object(properties: dict[str, Any]) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


def _array(items: dict[str, Any]) -> dict[str, Any]:
    return {"type": "array", "items": items}


NUMBER = {"type": ["number", "null"]}
STRING = {"type": "string"}
NULLABLE_STRING = {"type": ["string", "null"]}
BOOL = {"type": "boolean"}
INTEGER = {"type": "integer", "minimum": 0}
TIMESTAMP = {"type": "string", "format": "date-time"}
CHECKSUM = {"type": "string", "pattern": "^[0-9a-f]{64}$"}
ISSUE = _object({"code": STRING, "scope": STRING, "detail": STRING})
INDICATORS = _object({
    "ema": _object({p: NUMBER for p in ("8", "21", "50", "120")}),
    "price_distance_to_ema_pct": _object({p: NUMBER for p in ("8", "21", "50", "120")}),
    "rsi_14": NUMBER,
    "macd": _object({p: NUMBER for p in ("line", "signal_9", "histogram")}),
    "bollinger_20_2": _object({p: NUMBER for p in ("middle", "upper", "lower")}),
    "atr_14": NUMBER, "atr_14_pct": NUMBER, "volume_average_20": NUMBER, "volume_relative_20": NUMBER,
})
CANDLE = _object({
    "open_time_ms": INTEGER, "close_time_ms": INTEGER,
    **{p: NUMBER for p in ("open", "high", "low", "close", "volume")},
    "trade_count": {"type": ["integer", "null"], "minimum": 0},
})
CANDLE["type"] = ["object", "null"]
FRAME = _object({
    "candle_count": INTEGER, "last_close_utc": NULLABLE_STRING,
    "trend": {"enum": ["up", "down", "mixed"]}, "latest_candle": CANDLE,
    "indicators": INDICATORS,
    "range_last_20_closed": _object({"sample_count": INTEGER, "high": NUMBER, "low": NUMBER}),
})
AGE = _object({
    "age_ms": {"type": ["integer", "null"]}, "tolerance_ms": INTEGER, "basis": STRING, "usable": BOOL,
})
AGE_SCOPES = ["snapshot", "market_context_and_prices", "open_interest", "orderbook", "funding"] + [f"timeframes.{t}" for t in TIMEFRAMES]
CHECK = _object({"rule": STRING, "observed": {}, "threshold": {}, "passed": BOOL})
POLICY_SCHEMA = _object({
    **{p: INTEGER for p in ("min_candles", "max_context_capture_age_ms", "max_orderbook_age_ms", "max_funding_age_ms",
                           "ohlcv_age_multiplier", "max_future_skew_ms", "min_day_notional_volume_usd", "max_spread_bps")},
    **{p: {"type": "number"} for p in ("min_atr_15m_pct", "max_atr_15m_pct", "min_relative_volume")},
})
DECISION_SNAPSHOT_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "R102 Decision Snapshot v1.0",
    **_object({
        "kind": {"const": "decision_snapshot"}, "schema_version": {"const": SCHEMA_VERSION},
        "derivation_version": {"const": DERIVATION_VERSION}, "symbol": {"type": "string", "pattern": "^[A-Z0-9]+$"},
        "as_of_utc": TIMESTAMP, "market_timestamp_utc": TIMESTAMP,
        "valid_until_utc": TIMESTAMP, "source_market_checksum": CHECKSUM,
        "source_pre_analysis_checksum": CHECKSUM, "read_only": {"const": True},
        "source": _object({"venue": STRING, "api": STRING, "endpoint": STRING, "read_only": {"const": True}, "request_types": _array(STRING)}),
        "pre_analysis": _object({
            "schema_version": {"const": "1.0"}, "rules_version": {"const": "1.0"},
            "classification": {"enum": ["SKIP", "WATCH", "ANALYZE"]},
            "reasons": _array(STRING), "policy": POLICY_SCHEMA, "checks": _array(CHECK),
        }),
        "benchmark_override": _object({"allow_watch_requested": BOOL, "applied": BOOL, "rules_and_thresholds_changed": {"const": False}}),
        "quality": _object({
            "market_status": {"enum": ["ok", "degraded", "invalid"]},
            "source_issues": _array(ISSUE), "source_validation_errors": _array(STRING),
            "blockers": _array(ISSUE), "warnings": _array(ISSUE),
            "ages": _object({scope: AGE for scope in AGE_SCOPES}),
        }),
        "price": _object({p: NUMBER for p in ("mark", "oracle", "mid", "previous_day")}),
        "market_context": _object({
            "max_leverage": NUMBER, "size_decimals": NUMBER, "day_notional_volume": NUMBER,
            "premium": NUMBER, "impact_prices": _array(NUMBER),
        }),
        "timeframes": _object({t: FRAME for t in TIMEFRAMES}),
        "derivatives": _object({
            "open_interest": NUMBER,
            "funding": _object({
                "current_rate": NUMBER, "history_72h_count": INTEGER,
                "history_72h_min": NUMBER, "history_72h_max": NUMBER, "history_72h_mean": NUMBER,
            }),
        }),
        "orderbook": _object({
            "timestamp_ms": {"type": ["integer", "null"]}, "timestamp_utc": NULLABLE_STRING,
            **{p: NUMBER for p in ("best_bid", "best_ask", "midpoint", "spread_absolute", "spread_pct_of_mid", "spread_bps")},
            "depth": _object({"bid_total_size": NUMBER, "ask_total_size": NUMBER, "levels_per_side": _array(INTEGER)}),
            "imbalance": _object({"value": NUMBER, "formula": STRING}),
        }),
        "account_constraints": {"type": "null"},
        "semantics": _object({"units": _object({p: STRING for p in ("prices", "percentages", "basis_points", "funding_rates", "volume_and_sizes", "open_interest")}),
                              "indicator_method": STRING, "range_method": STRING, "funding_method": STRING, "trend_method": STRING}),
        "limitations": _array(STRING),
        "decision_contract": _object({
            "eligible_for_model_review": BOOL, "routing_reason": STRING,
            "allowed_decisions": _array({"enum": ["NO_TRADE", "WATCH", "LONG", "SHORT"]}),
            "required_response_fields": _array(STRING), "confidence_semantics": STRING,
            "on_quality_block": {"const": "NO_TRADE"}, "on_ineligible": {"const": "do_not_invoke_model"},
            "analysis_scope": STRING, "execution_authorized": {"const": False},
        }),
        "integrity": _object({"algorithm": {"const": "sha256"}, "canonicalization": STRING, "scope": STRING, "checksum": CHECKSUM}),
    }),
}


def get_decision_snapshot_schema() -> dict[str, Any]:
    """Return a detached JSON Schema for external consumers, without file I/O."""
    return copy.deepcopy(DECISION_SNAPSHOT_SCHEMA)


def _schema_errors(value: Any, schema: dict[str, Any], path: str = "$", errors: list[str] | None = None) -> list[str]:
    """Validate the subset used by our own schema, not arbitrary JSON Schemas."""
    errors = errors if errors is not None else []
    if "const" in schema and (value != schema["const"] or type(value) is not type(schema["const"])):
        errors.append(f"{path}: invalid constant")
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: invalid enum")
    types = schema.get("type", [])
    types = [types] if isinstance(types, str) else types
    matches = {"null": value is None, "object": isinstance(value, dict), "array": isinstance(value, list),
               "string": isinstance(value, str), "boolean": isinstance(value, bool),
               "integer": type(value) is int, "number": type(value) in (int, float)}
    if types and not any(matches[t] for t in types):
        errors.append(f"{path}: invalid type")
        return errors
    if type(value) in (int, float):
        try: finite = math.isfinite(value)
        except OverflowError: finite = False
        if not finite: errors.append(f"{path}: non-finite number")
        if "minimum" in schema and value < schema["minimum"]: errors.append(f"{path}: below minimum")
    if isinstance(value, str):
        if "pattern" in schema and not re.fullmatch(schema["pattern"], value): errors.append(f"{path}: invalid pattern")
        if schema.get("format") == "date-time" and _timestamp_ms(value) is None: errors.append(f"{path}: invalid timestamp")
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in value: errors.append(f"{path}.{key}: required")
        for key, item in value.items():
            if key in properties: _schema_errors(item, properties[key], f"{path}.{key}", errors)
            elif schema.get("additionalProperties") is False: errors.append(f"{path}.{key}: unexpected")
            else: _schema_errors(item, {}, f"{path}.{key}", errors)
    if isinstance(value, list):
        for index, item in enumerate(value): _schema_errors(item, schema.get("items", {}), f"{path}[{index}]", errors)
    return errors


def _timestamp_ms(value: Any) -> int | None:
    if not isinstance(value, str): return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return int(parsed.timestamp() * 1000) if parsed.utcoffset() is not None else None
    except (ValueError, OverflowError, OSError): return None


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _require_checksum(value: Any, name: str) -> None:
    try: valid = isinstance(value, dict) and verify_checksum(value)
    except (TypeError, ValueError, OverflowError): valid = False
    if not valid: raise ValueError(f"{name}: missing or invalid checksum")


def _project(raw: Any, schema: dict[str, Any]) -> Any:
    """Select only contract fields; absent nullable leaves remain null."""
    if "properties" in schema:
        if raw is None and "null" in schema.get("type", []): return None
        if raw is not None and not isinstance(raw, dict): raise ValueError("source field must be an object")
        return {key: _project((raw or {}).get(key), child) for key, child in schema["properties"].items()}
    return copy.deepcopy(raw)


def build_decision_snapshot(market_state: dict[str, Any], *, as_of_ms: int,
                            pre_analysis: dict[str, Any] | None = None,
                            allow_watch_for_benchmark: bool = False) -> dict[str, Any]:
    """Pure transform of Market State v1.1, with an explicit evaluation clock.

    An optional pre-analysis must exactly match the result recomputed at this
    clock from this market. No market/account collection, model call or writes.
    """
    if type(allow_watch_for_benchmark) is not bool: raise ValueError("allow_watch_for_benchmark must be boolean")
    _require_checksum(market_state, "market_state")
    if market_state.get("schema_version") != "1.1": raise ValueError("requires Market State v1.1")
    for key in ("source", "capture", "timeframes", "price", "market_context", "derivatives", "orderbook", "data_quality"):
        if not isinstance(market_state.get(key), dict): raise ValueError(f"{key} must be an object")
    computed = build_pre_analysis(market_state, as_of_ms=as_of_ms)
    if pre_analysis is not None:
        _require_checksum(pre_analysis, "pre_analysis")
        if pre_analysis != computed: raise ValueError("pre_analysis does not match market, rules, or evaluation time")
    pre = computed
    source = market_state.get("source")
    if not isinstance(source, dict) or source.get("read_only") is not True: raise ValueError("requires a read-only source")
    quality = copy.deepcopy(pre["quality"])
    frames = {}
    for interval in TIMEFRAMES:
        raw = market_state.get("timeframes", {}).get(interval)
        if not isinstance(raw, dict): raise ValueError(f"missing timeframe: {interval}")
        candles = raw.get("candles")
        if not isinstance(candles, list) or len(candles) != raw.get("candle_count"):
            raise ValueError(f"{interval}: candle_count does not match candles")
        tail = candles[-20:]
        for candle in tail:
            if _schema_errors(candle, CANDLE) or candle is None:
                raise ValueError(f"{interval}: malformed range candle")
            high, low = candle["high"], candle["low"]
            if high is None or low is None or not 0 < low <= high:
                raise ValueError(f"{interval}: invalid range prices")
            if any(candle[p] is None or not low <= candle[p] <= high for p in ("open", "close")) or candle["volume"] is None or candle["volume"] < 0:
                raise ValueError(f"{interval}: inconsistent OHLCV candle")
            if candle["close_time_ms"] > as_of_ms:
                raise ValueError(f"{interval}: open/future candle in closed series")
        if raw.get("latest_candle") != (candles[-1] if candles else None):
            raise ValueError(f"{interval}: latest_candle does not match series")
        if candles and _timestamp_ms(raw.get("last_close_utc")) != candles[-1]["close_time_ms"]:
            raise ValueError(f"{interval}: last_close_utc does not match series")
        frames[interval] = {
            "candle_count": len(candles), "last_close_utc": raw.get("last_close_utc"),
            "trend": pre["features"]["timeframes"][interval]["trend"],
            "latest_candle": _project(raw.get("latest_candle"), CANDLE),
            "indicators": _project(raw.get("indicators"), INDICATORS),
            "range_last_20_closed": {"sample_count": len(tail), "high": max(c["high"] for c in tail) if tail else None,
                                     "low": min(c["low"] for c in tail) if tail else None},
        }
    derivatives = market_state.get("derivatives")
    if not isinstance(derivatives, dict) or not isinstance(derivatives.get("funding"), dict):
        raise ValueError("derivatives.funding must be an object")
    history = derivatives["funding"].get("history_72h")
    if not isinstance(history, list): raise ValueError("funding history must be a list")
    rates = []
    for row in history:
        try:
            if not isinstance(row, dict) or isinstance(row.get("fundingRate"), bool): raise ValueError()
            rate = float(row["fundingRate"])
            if not math.isfinite(rate): raise ValueError()
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise ValueError("malformed funding rate in history") from exc
        rates.append(rate)
    expiries = [as_of_ms + age["tolerance_ms"] - age["age_ms"] for age in quality["ages"].values() if age["age_ms"] is not None]
    applied = allow_watch_for_benchmark and pre["classification"] == "WATCH" and not quality["blockers"]
    eligible = not quality["blockers"] and (pre["classification"] == "ANALYZE" or applied)
    result = {
        "kind": "decision_snapshot", "schema_version": SCHEMA_VERSION, "derivation_version": DERIVATION_VERSION,
        "symbol": market_state.get("symbol"), "as_of_utc": pre["as_of_utc"],
        "market_timestamp_utc": market_state.get("timestamp_utc"), "valid_until_utc": _iso(min(expiries)) if expiries else pre["as_of_utc"],
        "source_market_checksum": market_state["integrity"]["checksum"], "source_pre_analysis_checksum": pre["integrity"]["checksum"],
        "read_only": True, "source": _project(source, DECISION_SNAPSHOT_SCHEMA["properties"]["source"]),
        "pre_analysis": {key: copy.deepcopy(pre[key]) for key in ("schema_version", "rules_version", "classification", "reasons", "policy", "checks")},
        "benchmark_override": {"allow_watch_requested": allow_watch_for_benchmark, "applied": applied, "rules_and_thresholds_changed": False},
        "quality": {"market_status": market_state.get("data_quality", {}).get("status"),
                    "source_issues": _project_issues(market_state.get("data_quality", {}).get("issues")),
                    "source_validation_errors": copy.deepcopy(market_state.get("data_quality", {}).get("validation_errors")), **quality},
        "price": _project(market_state.get("price"), DECISION_SNAPSHOT_SCHEMA["properties"]["price"]),
        "market_context": _project(market_state.get("market_context"), DECISION_SNAPSHOT_SCHEMA["properties"]["market_context"]),
        "timeframes": frames,
        "derivatives": {"open_interest": derivatives.get("open_interest"), "funding": {
            "current_rate": derivatives["funding"].get("current_rate"), "history_72h_count": len(rates),
            "history_72h_min": min(rates) if rates else None, "history_72h_max": max(rates) if rates else None,
            "history_72h_mean": fmean(rates) if rates else None}},
        "orderbook": _project({**market_state.get("orderbook", {}), "spread_bps": pre["features"]["spread_bps"]}, DECISION_SNAPSHOT_SCHEMA["properties"]["orderbook"]),
        "account_constraints": None,
        "semantics": {
            "units": {"prices": "source quote-price units", "percentages": "percent, not fraction", "basis_points": "1 bps = 0.01 percent",
                      "funding_rates": "source fractional rate per funding interval, not percent or annualized",
                      "volume_and_sizes": "source asset-size units; day_notional_volume is USD", "open_interest": "source-native quantity, not USD notional"},
            "indicator_method": "EMA/MACD SMA-seeded; RSI/ATR Wilder; Bollinger20 population stddev; indicators use closed candles",
            "range_method": "min low / max high of last up to 20 closed candles; not validated support/resistance",
            "funding_method": "unweighted min/max/arithmetic mean of returned fundingRate rows; 72h requested, completeness not certified",
            "trend_method": "up: EMA21>EMA50>EMA120; down: reverse; otherwise mixed",
        },
        "limitations": [
            "Context/prices and current OI/funding have no exchange timestamp; local receipt is only an age proxy",
            "L2 and OI are single observations; no persistence, OI changes, tape/CVD or liquidation evidence",
            "Full OHLCV, funding rows and L2 levels omitted; no slopes, slippage or validated support/resistance supplied",
            "No account state, fee schedule or risk budget; account_constraints=null does not mean flat or zero balance",
            "Confidence is heuristic, not a calibrated probability; replay at as_of_utc is not a live signal",
        ],
        "decision_contract": {
            "eligible_for_model_review": eligible,
            "routing_reason": "quality_or_screening_block" if quality["blockers"] else "benchmark_watch_override" if applied else "pre_analysis_analyze" if eligible else "pre_analysis_watch",
            "allowed_decisions": ["NO_TRADE", "WATCH", "LONG", "SHORT"],
            "required_response_fields": ["decision", "confidence", "thesis", "entry_conditions", "invalidation", "limiting_data"],
            "confidence_semantics": "qualitative or explicitly heuristic; never calibrated success probability",
            "on_quality_block": "NO_TRADE", "on_ineligible": "do_not_invoke_model",
            "analysis_scope": "Market direction/setup only, as of as_of_utc; verify time<=valid_until_utc before live review; future entry conditions are unobserved",
            "execution_authorized": False,
        },
    }
    # Report every omitted numeric value explicitly, including optional indicators.
    for path in _null_numeric_paths(result, DECISION_SNAPSHOT_SCHEMA):
        result["quality"]["warnings"].append({"code": "missing_numeric_value", "scope": path, "detail": "unavailable; do not infer zero"})
    attach_checksum(result)
    errors = validate_decision_snapshot(result)
    if errors: raise ValueError("invalid decision snapshot: " + "; ".join(errors))
    return result


def _project_issues(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list): raise ValueError("source issues must be a list")
    return [_project(item, ISSUE) for item in raw]


def _null_numeric_paths(value: Any, schema: dict[str, Any], path: str = "$") -> list[str]:
    if value is None and "number" in schema.get("type", []): return [path]
    paths = []
    if isinstance(value, dict):
        for key, child in schema.get("properties", {}).items(): paths.extend(_null_numeric_paths(value.get(key), child, f"{path}.{key}"))
    if isinstance(value, list):
        for i, item in enumerate(value): paths.extend(_null_numeric_paths(item, schema.get("items", {}), f"{path}[{i}]"))
    return paths


def validate_decision_snapshot(snapshot: Any) -> list[str]:
    """Validate v1 structure, checksum and routing consistency without I/O."""
    errors = _schema_errors(snapshot, DECISION_SNAPSHOT_SCHEMA)
    if errors: return errors
    try: valid = verify_checksum(snapshot)
    except (TypeError, ValueError, OverflowError): valid = False
    if not valid: errors.append("checksum mismatch")
    pre, quality, override = snapshot["pre_analysis"], snapshot["quality"], snapshot["benchmark_override"]
    applied = override["allow_watch_requested"] and pre["classification"] == "WATCH" and not quality["blockers"]
    eligible = not quality["blockers"] and (pre["classification"] == "ANALYZE" or applied)
    if override["applied"] != applied: errors.append("inconsistent benchmark override")
    if snapshot["decision_contract"]["eligible_for_model_review"] != eligible: errors.append("inconsistent model eligibility")
    if pre["policy"] != POLICY: errors.append("policy does not match pre_analysis v1")
    if eligible and not all(age["usable"] for age in quality["ages"].values()): errors.append("eligible snapshot has unusable timestamps")
    routing = "quality_or_screening_block" if quality["blockers"] else "benchmark_watch_override" if applied else "pre_analysis_analyze" if eligible else "pre_analysis_watch"
    if snapshot["decision_contract"]["routing_reason"] != routing: errors.append("inconsistent routing reason")
    as_of_ms = _timestamp_ms(snapshot["as_of_utc"])
    expiries = [as_of_ms + age["tolerance_ms"] - age["age_ms"] for age in quality["ages"].values() if age["age_ms"] is not None]
    if snapshot["valid_until_utc"] != (_iso(min(expiries)) if expiries else snapshot["as_of_utc"]): errors.append("inconsistent validity deadline")
    return errors


def serialize_decision_snapshot(snapshot: dict[str, Any]) -> str:
    """Compact canonical UTF-8 JSON including integrity; size is len(text.encode())."""
    errors = validate_decision_snapshot(snapshot)
    if errors: raise ValueError("invalid decision snapshot: " + "; ".join(errors))
    return json.dumps(snapshot, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshots", type=Path, nargs="*", help="existing Market State v1.1 files")
    parser.add_argument("--as-of", help="timezone-aware ISO time; defaults to current UTC")
    parser.add_argument("--allow-watch-for-benchmark", action="store_true")
    parser.add_argument("--schema", action="store_true", help="print JSON Schema without reading snapshots")
    args = parser.parse_args(argv)
    if args.schema:
        if args.snapshots or args.as_of or args.allow_watch_for_benchmark: parser.error("--schema cannot be combined with snapshot options")
        print(json.dumps(get_decision_snapshot_schema(), sort_keys=True, separators=(",", ":")))
        return 0
    if not args.snapshots: parser.error("at least one market snapshot is required")
    as_of_ms = _timestamp_ms(args.as_of) if args.as_of else int(datetime.now(timezone.utc).timestamp() * 1000)
    if as_of_ms is None: parser.error("--as-of must be a timezone-aware ISO timestamp")
    try:
        results = []
        for path in args.snapshots:
            with path.open(encoding="utf-8") as handle: market = json.load(handle)
            results.append(build_decision_snapshot(market, as_of_ms=as_of_ms, allow_watch_for_benchmark=args.allow_watch_for_benchmark))
        print("[" + ",".join(serialize_decision_snapshot(result) for result in results) + "]")
    except (OSError, TypeError, ValueError, OverflowError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
