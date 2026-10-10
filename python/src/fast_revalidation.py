"""Minimal unsigned recapture and pure deterministic compatibility evaluation.

Only l2Book and metaAndAssetCtxs are fetched. Daemon capture workers never join
past the total deadline; a bounded global semaphore prevents unbounded growth
even if DNS/socket operations ignore their timeout. No retries or model calls.
"""
from __future__ import annotations

import hashlib
import copy
import math
import queue
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable

from .analysis_response import FIELD_PATHS, validate_response
from .decision_snapshot import validate_decision_snapshot
from .hl_client import CapturedResponse, HyperliquidInfoClient
from .integrity import attach_checksum, canonical_checksum_payload, verify_checksum
from .pre_analysis import TIMEFRAMES
from .orderbook import revalidation_book

DEFAULT_DEADLINE_SECONDS = 3.0
_CAPTURE_SLOTS = threading.BoundedSemaphore(6)


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _ms(value: str) -> int:
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if stamp.utcoffset() is None:
        raise ValueError("timestamp must include timezone")
    return int(stamp.timestamp() * 1000)


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (ValueError, OverflowError):
        return None


def _get(value: Any, path: str) -> Any:
    for key in path.split("."):
        value = value.get(key) if isinstance(value, dict) else None
    return value


def _digest(value: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_checksum_payload(value)).hexdigest()


def validate_deadline(value: float) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ValueError("revalidation deadline must be a positive finite number of seconds")


def capture_pair(symbol: str, *, deadline_seconds: float = DEFAULT_DEADLINE_SECONDS,
                 client: HyperliquidInfoClient | None = None,
                 clock_ms: Callable[[], int] | None = None,
                 timer: Callable[[], float] | None = None) -> dict[str, Any]:
    """Capture exactly two requests concurrently, with no wait for late workers.

    Also used by the isolated read-only latency benchmark. Responses stay local
    to this call; late completions cannot mutate any returned result/document.
    """
    validate_deadline(deadline_seconds)
    if not isinstance(symbol, str) or not symbol.isascii() or not symbol.isalnum():
        raise ValueError("invalid symbol")
    symbol = symbol.upper()
    clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)
    timer = timer or time.monotonic
    started, started_ms = timer(), clock_ms()
    expires = started + deadline_seconds
    client = client or HyperliquidInfoClient(timeout=deadline_seconds)
    mailbox: queue.Queue = queue.Queue(maxsize=2)
    results = {}

    def worker(name, call):
        begin, begin_ms = timer(), clock_ms()
        try:
            response = call()
            if not isinstance(response, CapturedResponse) or type(response.captured_at_ms) is not int:
                raise ValueError("missing captured response metadata")
            result = {"response": response, "error": None}
        except Exception as exc:
            # Public Info errors only; do not echo arbitrary exception payloads.
            timeout = isinstance(exc, TimeoutError) or isinstance(exc.__cause__, TimeoutError)
            result = {"response": None, "error": {"code": "API_TIMEOUT" if timeout else "API_ERROR",
                                                    "type": type(exc).__name__}}
        finally:
            elapsed = timer() - begin
            _CAPTURE_SLOTS.release()
        result.update(request_started_at_ms=begin_ms, duration_seconds=elapsed)
        mailbox.put_nowait((name, result))

    calls = {"l2Book": lambda: client.l2_book(symbol, n_sig_figs=5),
             "metaAndAssetCtxs": client.meta_and_asset_contexts}
    for name, call in calls.items():
        if timer() >= expires or not _CAPTURE_SLOTS.acquire(blocking=False):
            results[name] = {"response": None, "error": {"code": "CAPTURE_CAPACITY_UNAVAILABLE"},
                             "duration_seconds": 0.0, "request_started_at_ms": clock_ms()}
            continue
        try:
            threading.Thread(target=worker, args=(name, call), daemon=True, name=f"r102-info-{name}").start()
        except Exception:
            _CAPTURE_SLOTS.release()
            results[name] = {"response": None, "error": {"code": "CAPTURE_START_FAILED"},
                             "duration_seconds": 0.0, "request_started_at_ms": clock_ms()}
    while len(results) < 2:
        remaining = expires - timer()
        if remaining <= 0:
            break
        try:
            name, result = mailbox.get(timeout=remaining)
            results[name] = result
        except queue.Empty:
            break
    elapsed = timer() - started
    deadline_exceeded = elapsed >= deadline_seconds
    for name in calls:
        if name not in results:
            results[name] = {"response": None, "error": {"code": "DEADLINE_EXCEEDED"},
                             "duration_seconds": None, "request_started_at_ms": None}
    return {"symbol": symbol, "started_at_ms": started_ms, "completed_at_ms": clock_ms(),
            "total_seconds": elapsed, "deadline_seconds": deadline_seconds,
            "deadline_exceeded": deadline_exceeded, "sources": results}


def evaluate_predicate(rule: dict[str, Any], observed: float | None, snapshot: dict[str, Any]) -> dict[str, Any]:
    """Evaluate the eleven v2 operators only; no threshold/text interpretation.

    OI uses fractional change. v2's provenance validator still rejects OI rules
    without an existing authorized policy; this evaluator does not create one.
    """
    number = _number(observed)
    predicate, threshold = rule["predicate_type"], rule["threshold"]
    if number is None:
        return {"observed": None, "predicate_result": None, "passed": None}
    if predicate.startswith("oi_change_"):
        original = _number(_get(snapshot, FIELD_PATHS["open_interest"]))
        if original is None or original <= 0:
            return {"observed": number, "predicate_result": None, "passed": None}
        number = (number - original) / original
    elif predicate == "max_price_displacement_atr":
        original = _number(_get(snapshot, FIELD_PATHS[rule["baseline_field"]]))
        atr = _number(_get(snapshot, FIELD_PATHS[rule["atr_field"]]))
        if original is None or atr is None or atr <= 0:
            return {"observed": number, "predicate_result": None, "passed": None}
        number = abs(number - original) / atr
    if not math.isfinite(number):
        return {"observed": None, "predicate_result": None, "passed": None}
    if predicate == "price_above":
        truth = number > threshold
    elif predicate == "price_below":
        truth = number < threshold
    elif predicate == "price_in_range":
        truth = rule["range"]["lower"] <= number <= rule["range"]["upper"]
    elif predicate in {"spread_lte", "funding_lte", "oi_change_lte", "imbalance_lte", "max_price_displacement_atr"}:
        truth = number <= threshold
    elif predicate in {"funding_gte", "oi_change_gte", "imbalance_gte"}:
        truth = number >= threshold
    else:
        return {"observed": number, "predicate_result": None, "passed": None}
    return {"observed": number, "predicate_result": truth,
            "passed": not truth if rule["purpose"] == "invalidation" else truth}


def _original_gate(row: dict[str, Any]) -> None:
    snapshot, pre, review = row["decision_snapshot"], row["pre_analysis"], row["model_review"]
    if validate_decision_snapshot(snapshot) or not verify_checksum(pre):
        raise ValueError("original schema/checksum invalid")
    if (row["symbol"] != snapshot["symbol"] or pre["symbol"] != snapshot["symbol"]
            or snapshot["source_pre_analysis_checksum"] != pre["integrity"]["checksum"]
            or snapshot["source_market_checksum"] != pre["source_checksum"]
            or snapshot["as_of_utc"] != pre["as_of_utc"]):
        raise ValueError("original checksum/identity links invalid")
    if (pre["read_only"] is not True or row["classification"] != "ANALYZE" or pre["classification"] != "ANALYZE"
            or snapshot["pre_analysis"]["classification"] != "ANALYZE"
            or snapshot["decision_contract"]["eligible_for_model_review"] is not True
            or snapshot["quality"]["blockers"] or pre["quality"]["blockers"]
            or snapshot["benchmark_override"]["applied"] or row.get("error")):
        raise ValueError("original proposal ineligible")
    if snapshot["pre_analysis"] != {key: pre[key] for key in snapshot["pre_analysis"]}:
        raise ValueError("original pre_analysis projection mismatch")
    response = review["response"]
    validate_response(response, snapshot=snapshot)
    if (response["decision"] not in {"LONG", "SHORT"} or review["status"] != "completed"
            or review["invoked"] is not True or review["execution_authorized"] is not False
            or review["requires_revalidation"] is not True or review["expired_at_completion"] is not True):
        raise ValueError("review is not a directional proposal requiring revalidation")
    sent, completed = _ms(review["sent_at_utc"]), _ms(review["completed_at_utc"])
    if (sent > _ms(snapshot["valid_until_utc"]) or completed <= _ms(snapshot["valid_until_utc"]) or completed < sent
            or _ms(snapshot["as_of_utc"]) - sent > snapshot["pre_analysis"]["policy"]["max_future_skew_ms"]):
        raise ValueError("invalid original dispatch/completion times")


def _normalize_book(raw: Any, symbol: str) -> dict[str, Any]:
    return revalidation_book(raw, symbol, number=_number)


def _normalize_context(raw: Any, symbol: str) -> dict[str, Any]:
    if not isinstance(raw, (list, tuple)) or len(raw) != 2 or not isinstance(raw[0], dict) or not isinstance(raw[1], list):
        raise ValueError("malformed context response")
    universe, contexts = raw[0].get("universe"), raw[1]
    if not isinstance(universe, list) or len(universe) != len(contexts):
        raise ValueError("context/universe mismatch")
    matches = [index for index, asset in enumerate(universe) if isinstance(asset, dict) and asset.get("name") == symbol]
    if len(matches) != 1 or not isinstance(contexts[matches[0]], dict):
        raise ValueError("missing/ambiguous context identity")
    raw = contexts[matches[0]]
    fields = {"mark": "markPx", "context_mid": "midPx", "oracle": "oraclePx", "premium": "premium",
              "daily_notional_volume": "dayNtlVlm", "funding_current": "funding", "open_interest": "openInterest"}
    result = {field: _number(raw.get(key)) for field, key in fields.items()}
    impacts = raw.get("impactPxs")
    result["impact_prices"] = [_number(value) for value in impacts] if isinstance(impacts, list) else None
    return result


def _document(row: dict[str, Any], started_ms: int) -> dict[str, Any]:
    return {"kind": "fast_revalidation", "schema_version": "1.0", "rules_version": "1.0",
            "symbol": row.get("symbol"), "decision": _get(row, "model_review.response.decision"),
            "read_only": True, "execution_authorized": False, "status": "INDETERMINATE",
            "started_at_utc": _iso(started_ms), "completed_at_utc": None, "evaluated_at_utc": None,
            "valid_until_utc": None, "source": {}, "capture": {"request_types": [], "sources": {}},
            "observations": {}, "dependencies": {}, "checks": [], "reasons": [], "warnings": [], "timing": {}}


def _reason(doc, code, scope, effect="INDETERMINATE", rule_id=None):
    doc["reasons"].append({"code": code, "scope": scope, "effect": effect, "rule_id": rule_id})


def revalidation_failure(row: dict[str, Any], code: str, *, now_ms: int) -> dict[str, Any]:
    doc = _document(row, now_ms)
    _reason(doc, code, "fast_revalidation")
    doc.update(completed_at_utc=_iso(now_ms), evaluated_at_utc=_iso(now_ms))
    attach_checksum(doc)
    return doc


def evaluate_capture(row: dict[str, Any], captured: dict[str, Any], *, as_of_ms: int,
                     doc: dict[str, Any] | None = None) -> dict[str, Any]:
    """Pure evaluation of an already collected pair; no I/O or input mutation."""
    doc = doc if doc is not None else _document(row, captured["started_at_ms"])
    try:
        _original_gate(row)
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError, OSError):
        return revalidation_failure(row, "ORIGINAL_PROVENANCE_OR_CONTRACT_INVALID", now_ms=as_of_ms)
    snapshot, response = row["decision_snapshot"], row["model_review"]["response"]
    policy, contract = snapshot["pre_analysis"]["policy"], response["compatibility_contract"]
    doc["source"] = {"decision_snapshot_checksum": snapshot["integrity"]["checksum"],
                     "pre_analysis_checksum": snapshot["source_pre_analysis_checksum"],
                     "source_market_checksum": snapshot["source_market_checksum"],
                     "model_response_checksum": _digest(response), "compatibility_contract_checksum": _digest(contract),
                     "original_valid_until_utc": snapshot["valid_until_utc"]}
    doc["coverage"] = {"complete": contract["complete"], "unresolved_conditions": copy.deepcopy(contract["unresolved_conditions"])}
    if not contract["complete"]:
        _reason(doc, "CONTRACT_INCOMPLETE", "compatibility_contract")
    if contract["unresolved_conditions"]:
        _reason(doc, "UNRESOLVED_CONDITIONS", "compatibility_contract")
    expiries = [captured["started_at_ms"] + policy["max_context_capture_age_ms"]]
    transport_failed = captured["deadline_exceeded"]
    if transport_failed:
        _reason(doc, "DEADLINE_EXCEEDED", "capture")
    book_ok = context_ok = False

    def freshness(name, stamp, tolerance, basis, required=True):
        age = as_of_ms - stamp if type(stamp) is int else None
        usable = age is not None and -policy["max_future_skew_ms"] <= age <= tolerance
        meta = {"timestamp_utc": _iso(stamp) if type(stamp) is int else None, "age_ms": age,
                "tolerance_ms": tolerance, "basis": basis, "usable": usable,
                "required_for_valid": required or doc["dependencies"].get(name, {}).get("required_for_valid", False)}
        doc["dependencies"][name] = meta
        if usable and required:
            expiries.append(stamp + tolerance)
        elif not usable and required:
            _reason(doc, "L2_STALE" if name == "orderbook" else "DEPENDENCY_UNUSABLE", name)
        return usable

    doc["capture"]["request_types"] = ["l2Book", "metaAndAssetCtxs"]
    for name, result in captured["sources"].items():
        packet = result["response"]
        doc["capture"]["sources"][name] = {
            "request_started_at_utc": _iso(result["request_started_at_ms"]) if type(result["request_started_at_ms"]) is int else None,
            "captured_at_utc": _iso(packet.captured_at_ms) if packet else None,
            "duration_seconds": result["duration_seconds"], "error": result["error"]}
        if result["error"]:
            transport_failed = True
            _reason(doc, result["error"]["code"], name)
            continue
        try:
            if name == "l2Book":
                book = _normalize_book(packet.value, snapshot["symbol"])
                doc["observations"].update(book)
                book_ok = freshness("orderbook", book["source_timestamp_ms"], policy["max_orderbook_age_ms"], "exchange_source_time")
            else:
                context = _normalize_context(packet.value, snapshot["symbol"])
                doc["observations"].update(context)
                context_ok = freshness("market_context_and_prices", packet.captured_at_ms,
                                       policy["max_context_capture_age_ms"], "local_response_receipt")
                freshness("open_interest", packet.captured_at_ms, policy["max_context_capture_age_ms"], "local_response_receipt")
                doc["warnings"].append({"code": "SOURCE_TIME_UNAVAILABLE", "scope": "context/prices/OI/funding",
                                        "detail": "Local receipt bounds age; exchange freshness is unknown."})
                for field in ("mark", "context_mid", "oracle"):
                    if context[field] is None or context[field] <= 0:
                        context_ok = False
                        _reason(doc, "MISSING_OR_INVALID_PRICE", field)
                if context["daily_notional_volume"] is None or context["daily_notional_volume"] < 0:
                    context_ok = False
                    _reason(doc, "MISSING_REQUIRED_FIELD", "daily_notional_volume")
                for field in ("funding_current", "open_interest", "premium"):
                    if context[field] is None:
                        doc["warnings"].append({"code": "OPTIONAL_FIELD_UNAVAILABLE", "scope": field})
        except (ValueError, TypeError, KeyError, OverflowError, OSError):
            _reason(doc, "MALFORMED_L2" if name == "l2Book" else "MALFORMED_CONTEXT", name)

    def check(rule_id, observed, expected, passed, code, scope):
        doc["checks"].append({"rule_id": rule_id, "observed": observed, "expected": expected,
                              "required_for_valid": True, "outcome": "UNKNOWN" if passed is None else "PASS" if passed else "FAIL"})
        if passed is None:
            _reason(doc, "REQUIRED_RULE_UNEVALUABLE", scope, rule_id=rule_id)
        elif not passed:
            _reason(doc, code, scope, "INVALID", rule_id)

    observations = doc["observations"]
    spread, volume = observations.get("spread_bps"), observations.get("daily_notional_volume")
    check("base_spread", spread, policy["max_spread_bps"], spread <= policy["max_spread_bps"] if book_ok else None,
          "SPREAD_LIMIT_EXCEEDED", "orderbook")
    check("base_liquidity", volume, policy["min_day_notional_volume_usd"],
          volume >= policy["min_day_notional_volume_usd"] if context_ok else None,
          "DAILY_LIQUIDITY_BELOW_MINIMUM", "market_context")

    # Original screening used all four closed frames. No new candles/indicators
    # are available: crossing ANY of their next closes makes reuse indeterminate.
    frame_ok = {}
    for frame, duration in TIMEFRAMES.items():
        last = snapshot["timeframes"][frame]["latest_candle"]["close_time_ms"]
        frame_ok[frame] = freshness(f"timeframes.{frame}", last, duration * policy["ohlcv_age_multiplier"], "original_closed_candle_time")
        next_close = last + duration
        expiries.append(next_close - 1)
        if as_of_ms >= next_close:
            frame_ok[frame] = False
            _reason(doc, "NEW_CLOSED_CANDLE_UNVERIFIED", f"timeframes.{frame}")
        doc["dependencies"][f"timeframes.{frame}"]["next_close_utc"] = _iso(next_close)
        doc["dependencies"][f"timeframes.{frame}"]["reusable"] = frame_ok[frame]

    for rule in contract["rules"]:
        field, predicate = rule["field"], rule["predicate_type"]
        usable = book_ok if field in {"l2_midpoint", "best_bid", "best_ask", "spread_bps", "orderbook_imbalance"} else context_ok
        for path in rule["reference_source"].values():
            if path and path.startswith("timeframes."):
                usable = usable and frame_ok[path.split(".")[1]]
            if path and path.startswith("derivatives.funding.history_"):
                age = snapshot["quality"]["ages"]["funding"]
                stamp = _ms(snapshot["as_of_utc"]) - age["age_ms"]
                usable = freshness("original_funding_history", stamp, policy["max_funding_age_ms"],
                                   "original_exchange_source_time", required=rule["required_for_valid"]) and usable
        if rule["atr_field"]:
            usable = usable and frame_ok["5m" if rule["atr_field"] == "original_atr_5m" else "15m"]
        value = observations.get(field)
        if predicate.startswith("oi_change_") and value is not None and value < 0:
            usable = False
        evaluated = evaluate_predicate(rule, value if usable else None, snapshot)
        passed = evaluated["passed"]
        item = {"rule_id": rule["rule_id"], "field": field, "predicate_type": predicate,
                "purpose": rule["purpose"], "required_for_valid": rule["required_for_valid"],
                "expected": rule["range"] if rule["range"] is not None else rule["threshold"],
                **evaluated, "outcome": "UNKNOWN" if passed is None else "PASS" if passed else "FAIL"}
        doc["checks"].append(item)
        if rule["required_for_valid"]:
            if passed is None:
                _reason(doc, "REQUIRED_RULE_UNEVALUABLE", field, rule_id=rule["rule_id"])
            elif not passed:
                _reason(doc, "INVALIDATION_TRIGGERED" if rule["purpose"] == "invalidation" else "REQUIRED_CONDITION_FAILED",
                        field, "INVALID", rule["rule_id"])
    if transport_failed:
        doc["status"] = "INDETERMINATE"
    elif any(reason["effect"] == "INVALID" for reason in doc["reasons"]):
        doc["status"] = "INVALID"
    elif doc["reasons"]:
        doc["status"] = "INDETERMINATE"
    else:
        doc["status"] = "VALID"
        doc["valid_until_utc"] = _iso(min(expiries))
    doc.update(completed_at_utc=_iso(as_of_ms), evaluated_at_utc=_iso(as_of_ms))
    doc["timing"].update(capture_seconds=captured["total_seconds"], deadline_seconds=captured["deadline_seconds"])
    attach_checksum(doc)
    return doc


def fast_revalidate(row: dict[str, Any], *, deadline_seconds: float = DEFAULT_DEADLINE_SECONDS,
                    client: HyperliquidInfoClient | None = None, clock_ms: Callable[[], int] | None = None,
                    timer: Callable[[], float] | None = None) -> dict[str, Any]:
    clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)
    timer = timer or time.monotonic
    validate_deadline(deadline_seconds)
    started, started_ms = timer(), clock_ms()
    doc = _document(row, started_ms)
    try:
        _original_gate(row)
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError, OSError):
        _reason(doc, "ORIGINAL_PROVENANCE_OR_CONTRACT_INVALID", "original_proposal")
    else:
        try:
            remaining = deadline_seconds - (timer() - started)
            if remaining <= 0:
                _reason(doc, "DEADLINE_EXCEEDED", "fast_revalidation")
            else:
                captured = capture_pair(row["symbol"], deadline_seconds=remaining, client=client, clock_ms=clock_ms, timer=timer)
                doc = evaluate_capture(row, captured, as_of_ms=clock_ms(), doc=doc)
        except Exception:
            _reason(doc, "REVALIDATION_ERROR", "fast_revalidation")
            doc.update(status="INDETERMINATE", valid_until_utc=None)
    elapsed, end_ms = timer() - started, clock_ms()
    if elapsed >= deadline_seconds:
        doc.update(status="INDETERMINATE", valid_until_utc=None)
        _reason(doc, "DEADLINE_EXCEEDED", "fast_revalidation")
    elif doc["status"] == "VALID" and end_ms > _ms(doc["valid_until_utc"]):
        doc.update(status="INDETERMINATE", valid_until_utc=None)
        _reason(doc, "RESULT_EXPIRED", "fast_revalidation")
    doc.update(completed_at_utc=_iso(end_ms))
    doc["evaluated_at_utc"] = doc["evaluated_at_utc"] or _iso(end_ms)
    doc["timing"].update(total_seconds=elapsed, deadline_seconds=deadline_seconds)
    attach_checksum(doc)
    return doc
