"""Build, persist, and verify read-only Hyperliquid Market State snapshots."""
from __future__ import annotations

import argparse, copy, json, math, sys, time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .hl_client import HyperliquidAPIError, HyperliquidInfoClient
from .indicators import calculate, contains_only_finite_numbers
from .integrity import attach_checksum, canonical_checksum_payload, verify_checksum
from .orderbook import market_state_book

SCHEMA_VERSION = "1.1"
INTERVAL_MS = {"5m": 300_000, "15m": 900_000, "1h": 3_600_000, "4h": 14_400_000}
DEFAULT_CANDLES = 200
STALENESS_TOLERANCES_MS = {"market_context": 30_000, "orderbook": 15_000, "funding": 5_400_000, "open_interest": 30_000, "ohlcv_multiplier": 2}


def _number(value: Any) -> float | None:
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError):
        return None


def _display_number(value: Any) -> float | None:
    value = _number(value)
    return round(value, 12) if value is not None else None


def _iso(ms: int | None) -> str | None:
    if ms is None:
        return None
    try:
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def _issue(issues: list[dict[str, str]], code: str, scope: str, detail: str) -> None:
    issues.append({"code": code, "scope": scope, "detail": detail})


def _freshness(source_ms: int | None, reference_ms: int, tolerance_ms: int) -> dict[str, Any]:
    if source_ms is None:
        return {"status": "unknown", "age_ms": None, "tolerance_ms": tolerance_ms}
    age = max(0, reference_ms - source_ms)
    return {"status": "fresh" if age <= tolerance_ms else "stale", "age_ms": age, "tolerance_ms": tolerance_ms}


def _capture(captured_ms: int, source_ms: int | None, reference_ms: int, tolerance_ms: int) -> dict[str, Any]:
    return {"captured_at_utc": _iso(captured_ms), "captured_at_origin": "r102_local_clock_after_response",
            "source_timestamp_utc": _iso(source_ms), "source_timestamp_origin": "hyperliquid" if source_ms is not None else None,
            "freshness": _freshness(source_ms, reference_ms, tolerance_ms)}


def _mark_freshness(meta: dict[str, Any], issues: list[dict[str, str]], scope: str) -> None:
    freshness = meta["freshness"]
    if freshness["status"] == "stale":
        _issue(issues, "stale_data", scope, f"age {freshness['age_ms']} ms exceeds tolerance {freshness['tolerance_ms']} ms")
    elif freshness["status"] == "unknown":
        _issue(issues, "unknown_freshness", scope, "Hyperliquid did not provide a source timestamp")


def _normalize_candles(raw: list[dict[str, Any]], interval_ms: int, now_ms: int, issues: list[dict[str, str]], scope: str) -> list[dict[str, Any]]:
    output, seen = [], set()
    try:
        ordered = sorted(raw, key=lambda item: int(item.get("t", 0)))
    except (AttributeError, TypeError, ValueError):
        _issue(issues, "malformed_response", scope, "candle response contains a non-object item")
        return []
    for candle in ordered:
        try:
            start = int(candle.get("t")); close_time = int(candle.get("T", start + interval_ms - 1))
        except (AttributeError, TypeError, ValueError):
            _issue(issues, "invalid_timestamp", scope, "candle has a non-integer timestamp"); continue
        if start in seen:
            _issue(issues, "duplicate_timestamp", scope, f"duplicate candle at {start}"); continue
        seen.add(start)
        if close_time > now_ms:
            continue  # Expected API behavior: indicators use closed candles only.
        try:
            item = {"open_time_ms": start, "close_time_ms": close_time, "open": float(candle["o"]),
                    "high": float(candle["h"]), "low": float(candle["l"]), "close": float(candle["c"]),
                    "volume": float(candle["v"]), "trade_count": int(candle["n"]) if candle.get("n") is not None else None}
        except (KeyError, TypeError, ValueError):
            _issue(issues, "invalid_candle", scope, f"unusable OHLCV candle at {start}"); continue
        if not contains_only_finite_numbers(item):
            _issue(issues, "invalid_candle", scope, f"non-finite OHLCV candle at {start}"); continue
        output.append(item)
    for left, right in zip(output, output[1:]):
        delta = right["open_time_ms"] - left["open_time_ms"]
        if delta != interval_ms:
            _issue(issues, "timestamp_gap", scope, f"expected {interval_ms} ms, observed {delta} ms")
    return output


def _book(raw: dict[str, Any], issues: list[dict[str, str]]) -> dict[str, Any]:
    return market_state_book(raw, issues, number=_number, display_number=_display_number, iso=_iso)


def validate_snapshot(snapshot: dict[str, Any]) -> list[str]:
    errors = []
    if snapshot.get("schema_version") != SCHEMA_VERSION: errors.append("invalid schema_version")
    if not snapshot.get("symbol") or not snapshot.get("timestamp_utc"): errors.append("missing identity fields")
    if set(snapshot.get("timeframes", {})) != set(INTERVAL_MS): errors.append("required timeframes are absent")
    if not contains_only_finite_numbers(snapshot): errors.append("snapshot contains NaN or infinity")
    book = snapshot.get("orderbook", {})
    if book.get("best_bid") is not None and book.get("best_ask") is not None and book["best_bid"] > book["best_ask"]: errors.append("crossed order book")
    if "integrity" in snapshot and not verify_checksum(snapshot): errors.append("checksum mismatch")
    return errors


def build_market_state(symbol: str, client: HyperliquidInfoClient | None = None, candle_count: int = DEFAULT_CANDLES,
                       clock_ms: Callable[[], int] | None = None) -> dict[str, Any]:
    symbol = symbol.upper()
    if not symbol.isalnum(): raise ValueError("symbol must be alphanumeric")
    if candle_count < 1: raise ValueError("candle_count must be positive")
    client = client or HyperliquidInfoClient(); clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)
    started = clock_ms(); issues = []
    market = client.market_context(symbol); asset, context = market.value
    context_capture = _capture(market.captured_at_ms, None, started, STALENESS_TOLERANCES_MS["market_context"])
    _mark_freshness(context_capture, issues, "market_context")
    timeframes, timeframe_captures = {}, {}
    for interval, duration in INTERVAL_MS.items():
        scope = f"timeframes.{interval}"
        try:
            response = client.candles(symbol, interval, started - duration * (candle_count + 3), started)
            candles = _normalize_candles(response.value, duration, started, issues, scope)[-candle_count:]; captured = response.captured_at_ms
        except HyperliquidAPIError as exc:
            candles, captured = [], clock_ms(); _issue(issues, "endpoint_unavailable", scope, str(exc))
        if len(candles) < candle_count: _issue(issues, "incomplete_timeframe", scope, f"received {len(candles)} of {candle_count} requested closed candles")
        if len(candles) < 120: _issue(issues, "insufficient_candles", scope, f"EMA120 requires 120 candles; received {len(candles)}")
        legacy = [{"t": c["open_time_ms"], "o": c["open"], "h": c["high"], "l": c["low"], "c": c["close"], "v": c["volume"]} for c in candles]
        source_ms = candles[-1]["close_time_ms"] if candles else None
        meta = _capture(captured, source_ms, started, duration * STALENESS_TOLERANCES_MS["ohlcv_multiplier"])
        _mark_freshness(meta, issues, scope); timeframe_captures[interval] = meta
        timeframes[interval] = {"interval_ms": duration, "candle_count": len(candles),
                                "first_open_utc": _iso(candles[0]["open_time_ms"]) if candles else None,
                                "last_close_utc": _iso(source_ms), "latest_candle": candles[-1] if candles else None,
                                "candles": candles, "indicators": calculate(legacy) if candles else None}
    try:
        response = client.l2_book(symbol); orderbook = _book(response.value, issues); book_captured = response.captured_at_ms
    except HyperliquidAPIError as exc:
        _issue(issues, "endpoint_unavailable", "orderbook", str(exc)); orderbook, book_captured = _book({}, issues), clock_ms()
    book_meta = _capture(book_captured, orderbook["timestamp_ms"], started, STALENESS_TOLERANCES_MS["orderbook"]); _mark_freshness(book_meta, issues, "orderbook")
    try:
        response = client.funding_history(symbol, started - 72 * 3_600_000, started); history, funding_captured = response.value, response.captured_at_ms
    except HyperliquidAPIError as exc:
        history, funding_captured = [], clock_ms(); _issue(issues, "endpoint_unavailable", "derivatives.funding_history", str(exc))
    funding_source = None
    if history and isinstance(history[-1], dict):
        try: funding_source = int(history[-1].get("time"))
        except (TypeError, ValueError): _issue(issues, "invalid_timestamp", "derivatives.funding", "latest funding timestamp is invalid")
    funding_meta = _capture(funding_captured, funding_source, started, STALENESS_TOLERANCES_MS["funding"]); _mark_freshness(funding_meta, issues, "derivatives.funding")
    oi_meta = _capture(market.captured_at_ms, None, started, STALENESS_TOLERANCES_MS["open_interest"]); _mark_freshness(oi_meta, issues, "derivatives.open_interest")
    prices = {name: _display_number(context.get(key)) for name, key in {"mark":"markPx", "oracle":"oraclePx", "mid":"midPx", "previous_day":"prevDayPx"}.items()}
    for name, value in prices.items():
        if value is None: _issue(issues, "missing_field", f"price.{name}", "field absent or null in metaAndAssetCtxs")
    funding = _number(context.get("funding")); oi = _number(context.get("openInterest"))
    if funding is None: _issue(issues, "missing_field", "derivatives.funding.current_rate", "field absent or null in metaAndAssetCtxs")
    if oi is None: _issue(issues, "missing_field", "derivatives.open_interest", "field absent or null in metaAndAssetCtxs")
    snapshot = {"schema_version": SCHEMA_VERSION, "symbol": symbol, "timestamp_utc": _iso(started),
        "source": {"venue":"Hyperliquid mainnet", "api":"official HTTP Info API", "read_only":True,
                   "endpoint":"https://api.hyperliquid.xyz/info", "request_types":["metaAndAssetCtxs","candleSnapshot","l2Book","fundingHistory"]},
        "capture": {"snapshot_started_at_utc":_iso(started), "snapshot_completed_at_utc":_iso(clock_ms()),
                    "timestamps_note":"source timestamps are from Hyperliquid; captured_at timestamps are from the local R102 clock after each response",
                    "sources":{"market_context_and_prices":context_capture, "ohlcv":timeframe_captures, "orderbook":book_meta,
                               "funding":funding_meta, "open_interest":oi_meta}},
        "price": prices, "market_context":{"max_leverage":asset.get("maxLeverage"), "size_decimals":asset.get("szDecimals"),
            "day_notional_volume":_number(context.get("dayNtlVlm")), "premium":_number(context.get("premium")),
            "impact_prices":[_number(v) for v in context.get("impactPxs", [])]},
        "timeframes":timeframes, "derivatives":{"funding":{"current_rate":funding,"history_72h":history},"open_interest":oi},
        "orderbook":orderbook, "data_quality":{"status":"ok" if not issues else "degraded", "issues":issues,
        "staleness_tolerances_ms":copy.deepcopy(STALENESS_TOLERANCES_MS)}}
    snapshot["data_quality"]["validation_errors"] = validate_snapshot(snapshot)
    if snapshot["data_quality"]["validation_errors"]: snapshot["data_quality"]["status"] = "invalid"
    attach_checksum(snapshot); return snapshot


def save_snapshot(snapshot: dict[str, Any], directory: Path) -> Path:
    if not verify_checksum(snapshot): attach_checksum(snapshot)
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.fromisoformat(snapshot["timestamp_utc"].replace("Z", "+00:00")).strftime("%Y%m%d_%H%M%S")
    path = directory / f"{stamp}_{snapshot['symbol']}_market_state.json"
    path.write_text(json.dumps(snapshot, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8"); return path


def load_and_verify(path: Path) -> bool:
    with path.open(encoding="utf-8") as handle: return verify_checksum(json.load(handle))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__); parser.add_argument("symbol", nargs="?")
    parser.add_argument("--candles", type=int, default=DEFAULT_CANDLES); parser.add_argument("--output-dir", type=Path, default=Path(__file__).resolve().parents[2] / "data" / "snapshots")
    parser.add_argument("--verify", type=Path, metavar="SNAPSHOT"); return parser


def main() -> int:
    args = _parser().parse_args()
    if args.verify:
        try: valid = load_and_verify(args.verify)
        except (OSError, json.JSONDecodeError) as exc: print(f"error: {exc}", file=sys.stderr); return 1
        print(f"checksum={'valid' if valid else 'INVALID'}: {args.verify}"); return 0 if valid else 1
    if not args.symbol: print("error: symbol is required unless --verify is used", file=sys.stderr); return 2
    try:
        snapshot = build_market_state(args.symbol, candle_count=args.candles)
        if snapshot["data_quality"]["validation_errors"]: raise ValueError("; ".join(snapshot["data_quality"]["validation_errors"]))
        path = save_snapshot(snapshot, args.output_dir)
    except (HyperliquidAPIError, ValueError, OSError) as exc: print(f"error: {exc}", file=sys.stderr); return 1
    print(f"{snapshot['symbol']} Market State v{SCHEMA_VERSION} | {snapshot['timestamp_utc']}")
    print(f"mark={snapshot['price']['mark']} bid={snapshot['orderbook']['best_bid']} ask={snapshot['orderbook']['best_ask']} funding={snapshot['derivatives']['funding']['current_rate']} OI={snapshot['derivatives']['open_interest']}")
    print("candles: " + ", ".join(f"{k}={v['candle_count']}" for k,v in snapshot["timeframes"].items()))
    print(f"quality={snapshot['data_quality']['status']} issues={len(snapshot['data_quality']['issues'])} checksum={snapshot['integrity']['checksum']}")
    print(f"saved: {path}"); return 0


if __name__ == "__main__": raise SystemExit(main())
