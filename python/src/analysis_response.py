"""Analysis response v2: closed data contract and snapshot-bound validation.

No market I/O, predicate evaluation, text interpretation or execution. An
invalidation predicate describes a trigger: true would invalidate the proposal.
Entry/quality predicates describe requirements: true would pass that requirement.
"""
from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any

RESPONSE_SCHEMA_VERSION = "2.0"
RESPONSE_SCHEMA_PATH = Path(__file__).with_name("analysis_response.schema.json").resolve()
# Load the static schema once, not during a review or from model-controlled data.
RESPONSE_SCHEMA = json.loads(RESPONSE_SCHEMA_PATH.read_text(encoding="utf-8"))
BASE_RESPONSE_FIELDS = frozenset({"decision", "confidence", "thesis", "entry_conditions", "invalidation", "limiting_data"})
RESPONSE_FIELDS = BASE_RESPONSE_FIELDS | {"response_schema_version", "execution_authorized", "compatibility_contract"}

PRICE_FIELDS = {"l2_midpoint", "best_bid", "best_ask", "mark", "context_mid", "oracle"}
FIELD_PATHS = {
    "l2_midpoint": "orderbook.midpoint", "best_bid": "orderbook.best_bid", "best_ask": "orderbook.best_ask",
    "spread_bps": "orderbook.spread_bps", "mark": "price.mark", "context_mid": "price.mid", "oracle": "price.oracle",
    "premium": "market_context.premium", "daily_notional_volume": "market_context.day_notional_volume",
    "funding_current": "derivatives.funding.current_rate", "open_interest": "derivatives.open_interest",
    "orderbook_imbalance": "orderbook.imbalance.value",
    "original_atr_5m": "timeframes.5m.indicators.atr_14", "original_atr_15m": "timeframes.15m.indicators.atr_14",
}
PRICE_REFERENCES = {FIELD_PATHS[f] for f in PRICE_FIELDS}
for _frame in ("5m", "15m", "1h", "4h"):
    PRICE_REFERENCES.update(f"timeframes.{_frame}.latest_candle.{key}" for key in ("open", "high", "low", "close"))
    PRICE_REFERENCES.update(f"timeframes.{_frame}.range_last_20_closed.{key}" for key in ("low", "high"))
    PRICE_REFERENCES.update(f"timeframes.{_frame}.indicators.ema.{period}" for period in ("8", "21", "50", "120"))
    PRICE_REFERENCES.update(f"timeframes.{_frame}.indicators.bollinger_20_2.{key}" for key in ("lower", "middle", "upper"))


def _schema_validate(value: Any, schema: dict[str, Any], path: str = "$") -> None:
    """Validate the exact JSON Schema subset used by the checked-in v2 schema."""
    if "$ref" in schema:
        _schema_validate(value, RESPONSE_SCHEMA["$defs"][schema["$ref"].removeprefix("#/$defs/")], path)
        return
    if "anyOf" in schema:
        for branch in schema["anyOf"]:
            try:
                _schema_validate(value, branch, path)
                return
            except ValueError:
                pass
        raise ValueError(f"{path}: does not match any allowed shape")
    types = schema.get("type", [])
    types = [types] if isinstance(types, str) else types
    matches = {"null": value is None, "object": type(value) is dict, "array": type(value) is list,
               "string": type(value) is str, "boolean": type(value) is bool,
               "number": type(value) in (int, float)}
    if types and not any(matches[t] for t in types):
        raise ValueError(f"{path}: invalid type")
    if "const" in schema and (type(value) is not type(schema["const"]) or value != schema["const"]):
        raise ValueError(f"{path}: invalid constant")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(f"{path}: unsupported value")
    if type(value) in (int, float):
        try:
            finite = math.isfinite(value)
        except OverflowError:
            finite = False
        if not finite:
            raise ValueError(f"{path}: nonfinite number")
    if type(value) is str:
        if len(value) < schema.get("minLength", 0) or ("pattern" in schema and not re.search(schema["pattern"], value)):
            raise ValueError(f"{path}: invalid string")
    if type(value) is dict:
        properties = schema.get("properties", {})
        if not set(schema.get("required", [])).issubset(value):
            raise ValueError(f"{path}: missing required fields")
        if schema.get("additionalProperties") is False and set(value) - properties.keys():
            raise ValueError(f"{path}: unexpected fields")
        for key, item in value.items():
            _schema_validate(item, properties.get(key, {}), f"{path}.{key}")
    if type(value) is list:
        for index, item in enumerate(value):
            _schema_validate(item, schema.get("items", {}), f"{path}[{index}]")


def _source_number(snapshot: dict[str, Any], path: str) -> float:
    value: Any = snapshot
    for key in path.split("."):
        value = value.get(key) if isinstance(value, dict) else None
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError(f"unavailable numeric snapshot reference: {path}")
    return value


def _validate_rule(rule: dict[str, Any], snapshot: dict[str, Any] | None) -> None:
    field, predicate = rule["field"], rule["predicate_type"]
    primary, secondary = rule["reference_source"]["primary"], rule["reference_source"]["secondary"]
    origin, derivation = rule["threshold_origin"], rule["derivation"]
    baseline, atr = rule["baseline_field"], rule["atr_field"]
    threshold, bounds = rule["threshold"], rule["range"]
    if origin in {"user_policy", "unsupported"}:
        raise ValueError("unavailable policy/unsupported conditions belong in unresolved_conditions")
    if predicate == "price_in_range":
        if field not in PRICE_FIELDS or threshold is not None or bounds is None or bounds["lower"] >= bounds["upper"]:
            raise ValueError("price_in_range requires ordered positive price bounds")
        if bounds["lower"] <= 0 or derivation != "ordered_range" or secondary not in PRICE_REFERENCES:
            raise ValueError("range must be derived from two explicit price references")
    elif threshold is None or bounds is not None:
        raise ValueError("scalar predicate requires threshold and null range")
    if predicate in {"price_above", "price_below", "price_in_range"}:
        if field not in PRICE_FIELDS or primary not in PRICE_REFERENCES:
            raise ValueError("price predicate requires price field and reference")
        if threshold is not None and threshold <= 0:
            raise ValueError("price threshold must be positive")
    elif predicate == "spread_lte":
        if field != "spread_bps" or primary != "pre_analysis.policy.max_spread_bps" or origin != "pre_analysis_policy":
            raise ValueError("spread limit must use the existing pre_analysis policy")
    elif predicate in {"funding_lte", "funding_gte"}:
        if field != "funding_current" or primary not in {
            "derivatives.funding.current_rate", "derivatives.funding.history_72h_min",
            "derivatives.funding.history_72h_max", "derivatives.funding.history_72h_mean",
        }:
            raise ValueError("funding predicate requires a funding snapshot reference")
    elif predicate in {"imbalance_gte", "imbalance_lte"}:
        if field != "orderbook_imbalance" or primary != "orderbook.imbalance.value" or not -1 <= threshold <= 1:
            raise ValueError("imbalance predicate requires original imbalance within [-1, 1]")
    elif predicate in {"oi_change_gte", "oi_change_lte"}:
        # R102 has a single OI observation and no OI-change policy. No invented
        # numeric delta (including zero) is accepted as a justified threshold.
        raise ValueError("OI-change thresholds require new policy: use unresolved_conditions")
    elif predicate == "max_price_displacement_atr":
        if (field not in PRICE_FIELDS or baseline != field or atr not in {"original_atr_5m", "original_atr_15m"}
                or primary != FIELD_PATHS[field] or secondary not in PRICE_REFERENCES
                or derivation != "snapshot_distance_atr" or origin != "model_derived_from_snapshot"
                or threshold <= 0 or rule["purpose"] == "invalidation"):
            raise ValueError("ATR displacement requires a positive snapshot-derived distance and matching price baseline")
    if predicate != "max_price_displacement_atr" and (baseline is not None or atr is not None):
        raise ValueError("baseline/ATR fields are only used for ATR displacement")
    if predicate not in {"price_in_range", "max_price_displacement_atr"}:
        if derivation != "identity" or secondary is not None:
            raise ValueError("scalar thresholds must copy one explicit snapshot value")
    if origin == "pre_analysis_policy" and predicate != "spread_lte":
        raise ValueError("no existing pre_analysis policy for this predicate")
    if predicate != "spread_lte" and origin not in {"explicit_snapshot_level", "model_derived_from_snapshot"}:
        raise ValueError("invalid threshold provenance")
    if origin == "explicit_snapshot_level" and derivation != "identity":
        raise ValueError("explicit snapshot levels require identity derivation")
    if rule["purpose"] == "invalidation" and rule["required_for_valid"] is not True:
        raise ValueError("invalidation triggers cannot be optional")
    if snapshot is None:
        return
    first = _source_number(snapshot, primary)
    if derivation == "ordered_range":
        expected = sorted((first, _source_number(snapshot, secondary)))
        actual = [bounds["lower"], bounds["upper"]]
        if actual != expected:
            raise ValueError("range does not match snapshot references")
    else:
        expected = first
        if derivation == "snapshot_distance_atr":
            atr_value = _source_number(snapshot, FIELD_PATHS[atr])
            if atr_value <= 0:
                raise ValueError("ATR reference must be positive")
            expected = abs(first - _source_number(snapshot, secondary)) / atr_value
        # Copied values are exact. Only arithmetic derivations admit roundoff;
        # this is never a market tolerance or a relaxation of screening policy.
        matches = (threshold == expected if derivation == "identity" else
                   math.isclose(threshold, expected, rel_tol=1e-12, abs_tol=1e-12))
        if not matches:
            raise ValueError("threshold does not match snapshot derivation")


def validate_response(response: Any, *, snapshot: dict[str, Any] | None = None) -> dict[str, Any]:
    """Validate v2 shape/semantics, optionally binding provenance to the input.

    The pipeline always supplies its exact snapshot. Structural-only use is for
    JSONL parsing/tests; it does not certify references or proposal completeness.
    Human strings and unresolved descriptions are never parsed or executed.
    """
    _schema_validate(response, RESPONSE_SCHEMA)
    contract = response["compatibility_contract"]
    directional = response["decision"] in {"LONG", "SHORT"}
    if contract is None:
        if directional:
            raise ValueError("LONG/SHORT requires compatibility_contract")
        return response
    if contract["actionable"] != directional:
        raise ValueError("contract actionable flag must match directional decision")
    rules, unresolved = contract["rules"], contract["unresolved_conditions"]
    if not directional:
        if rules or contract["complete"]:
            raise ValueError("NO_TRADE/WATCH contract must be non-actionable")
    else:
        if not any(r["purpose"] == "invalidation" and r["required_for_valid"] for r in rules):
            raise ValueError("LONG/SHORT requires a mandatory structured invalidation trigger")
        if contract["complete"] != (not unresolved):
            raise ValueError("complete must agree with unresolved_conditions")
    ids = [item["rule_id"] for item in rules] + [item["condition_id"] for item in unresolved]
    if len(ids) != len(set(ids)):
        raise ValueError("rule and unresolved condition identifiers must be unique")
    if snapshot is not None and contract["source_snapshot_checksum"] != snapshot["integrity"]["checksum"]:
        raise ValueError("compatibility contract belongs to a different snapshot")
    for rule in rules:
        _validate_rule(rule, snapshot)
    return response
