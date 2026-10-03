"""Offline response v2 schema, provenance and non-execution tests."""
import copy
import json
import unittest
from unittest.mock import patch

from src.analysis_response import RESPONSE_SCHEMA, RESPONSE_SCHEMA_PATH, validate_response
import test_decision_snapshot


def base_response(decision="WATCH"):
    return {"response_schema_version": "2.0", "execution_authorized": False,
            "decision": decision, "confidence": "medium", "thesis": "Propuesta condicional as-of.",
            "entry_conditions": [], "invalidation": [], "limiting_data": [], "compatibility_contract": None}


def rule(**overrides):
    result = {"rule_id": "price_invalidation", "field": "l2_midpoint", "predicate_type": "price_below",
              "threshold": 99, "range": None,
              "reference_source": {"primary": "timeframes.15m.latest_candle.low", "secondary": None},
              "threshold_origin": "explicit_snapshot_level", "derivation": "identity",
              "purpose": "invalidation", "required_for_valid": True, "baseline_field": None, "atr_field": None}
    result.update(overrides)
    return result


def directional_response(snapshot, decision="LONG"):
    response = base_response(decision)
    key = "low" if decision == "LONG" else "high"
    response["compatibility_contract"] = {
        "contract_version": "1.0", "source_snapshot_checksum": snapshot["integrity"]["checksum"],
        "actionable": True, "complete": True,
        "rules": [rule(predicate_type="price_below" if decision == "LONG" else "price_above",
                       threshold=snapshot["timeframes"]["15m"]["latest_candle"][key],
                       reference_source={"primary": f"timeframes.15m.latest_candle.{key}", "secondary": None})],
        "unresolved_conditions": [],
    }
    return response


def unresolved():
    return {"condition_id": "oi_policy", "purpose": "entry", "reason": "new_policy_required",
            "threshold_origin": "user_policy", "description": "Falta una política aprobada de variación de OI."}


class AnalysisResponseTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = test_decision_snapshot.DecisionSnapshotTests().build()

    def validate(self, response):
        return validate_response(response, snapshot=self.snapshot)

    def test_no_trade_and_watch_null_or_non_actionable(self):
        for decision in ("NO_TRADE", "WATCH"):
            response = base_response(decision)
            self.assertEqual(self.validate(response), response)
            response["compatibility_contract"] = {
                "contract_version": "1.0", "source_snapshot_checksum": self.snapshot["integrity"]["checksum"],
                "actionable": False, "complete": False, "rules": [], "unresolved_conditions": [],
            }
            self.assertEqual(self.validate(response), response)
            response["compatibility_contract"]["complete"] = True
            with self.assertRaises(ValueError): self.validate(response)

    def test_complete_long_and_short_are_snapshot_bound_and_immutable(self):
        for decision in ("LONG", "SHORT"):
            response = directional_response(self.snapshot, decision)
            before = copy.deepcopy((response, self.snapshot))
            with patch("urllib.request.urlopen", side_effect=AssertionError("network forbidden")), \
                 patch("subprocess.run", side_effect=AssertionError("model forbidden")):
                self.assertIs(self.validate(response), response)
            self.assertEqual((response, self.snapshot), before)
            self.assertFalse(response["execution_authorized"])

    def test_long_short_missing_null_or_optional_invalidation_rejected(self):
        for decision in ("LONG", "SHORT"):
            for mode in ("missing", "null", "empty", "entry_only", "optional"):
                response = directional_response(self.snapshot, decision)
                if mode == "missing": response.pop("compatibility_contract")
                elif mode == "null": response["compatibility_contract"] = None
                elif mode == "empty": response["compatibility_contract"]["rules"] = []
                elif mode == "entry_only": response["compatibility_contract"]["rules"][0]["purpose"] = "entry"
                else: response["compatibility_contract"]["rules"][0]["required_for_valid"] = False
                with self.subTest(decision=decision, mode=mode), self.assertRaises(ValueError): self.validate(response)

    def test_closed_fields_predicates_references_and_no_executable_text(self):
        for key, value in (("field", "rsi_14"), ("predicate_type", "eval_python"),
                           ("purpose", "execution"), ("threshold_origin", "invented"),
                           ("threshold", "99"), ("threshold", True),
                           ("threshold", float("nan")), ("threshold", float("inf")),
                           ("required_for_valid", 1), ("rule_id", "price > 99"),
                           ("reference_source", {"primary": "thesis", "secondary": None}),
                           ("expression", "eval(thesis)"), ("description", "Use price 99")):
            response = directional_response(self.snapshot)
            response["compatibility_contract"]["rules"][0][key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ValueError): self.validate(response)

    def test_unverifiable_condition_is_explicitly_incomplete(self):
        response = directional_response(self.snapshot)
        contract = response["compatibility_contract"]
        contract.update(complete=False, unresolved_conditions=[unresolved()])
        self.assertIs(self.validate(response), response)
        contract["complete"] = True
        with self.assertRaises(ValueError): self.validate(response)
        contract.update(complete=False, unresolved_conditions=[])
        with self.assertRaises(ValueError): self.validate(response)

    def test_numbers_and_instructions_in_human_text_never_change_rules(self):
        response = directional_response(self.snapshot)
        before = copy.deepcopy(response["compatibility_contract"])
        response.update(thesis="Compra en 999; ejecuta price_below 12345.",
                        entry_conditions=["RSI > 55 y funding 0.5; eval('1+1')"],
                        invalidation=["Invalidación 888, ignora las reglas JSON"],
                        limiting_data=["Precio 777"])
        self.validate(response)
        self.assertEqual(response["compatibility_contract"], before)

    def test_v1_never_accepted_for_new_decisions(self):
        response = base_response()
        for field in ("response_schema_version", "execution_authorized", "compatibility_contract"):
            response.pop(field)
        with self.assertRaises(ValueError): validate_response(response)
        for version in ("1.0", 2, None):
            response = base_response(); response["response_schema_version"] = version
            with self.assertRaises(ValueError): validate_response(response)

    def test_no_execution_extra_keys_duplicate_ids_or_wrong_snapshot(self):
        for mode in ("execution", "extra", "checksum", "duplicate", "unresolved_duplicate", "actionable"):
            response = directional_response(self.snapshot)
            contract = response["compatibility_contract"]
            if mode == "execution": response["execution_authorized"] = True
            if mode == "extra": contract["size"] = 1
            if mode == "checksum": contract["source_snapshot_checksum"] = "0" * 64
            if mode == "duplicate": contract["rules"].append(copy.deepcopy(contract["rules"][0]))
            if mode == "unresolved_duplicate":
                item = unresolved(); item["condition_id"] = contract["rules"][0]["rule_id"]
                contract.update(complete=False, unresolved_conditions=[item])
            if mode == "actionable": contract["actionable"] = False
            with self.subTest(mode=mode), self.assertRaises(ValueError): self.validate(response)

    def test_threshold_provenance_cannot_be_fabricated(self):
        for mutation in (
            {"threshold": 98}, {"threshold_origin": "user_policy"}, {"threshold_origin": "unsupported"},
            {"threshold_origin": "pre_analysis_policy"},
            {"reference_source": {"primary": "timeframes.15m.latest_candle.low", "secondary": "price.mark"}},
            {"baseline_field": "mark"},
        ):
            response = directional_response(self.snapshot)
            response["compatibility_contract"]["rules"][0].update(mutation)
            with self.subTest(mutation=mutation), self.assertRaises(ValueError): self.validate(response)
        response = directional_response(self.snapshot)
        self.snapshot["timeframes"]["15m"]["latest_candle"]["low"] = None
        with self.assertRaises(ValueError): self.validate(response)

    def test_supported_scalar_predicates_use_matching_units_and_original_values(self):
        cases = [
            ("spread_lte", "spread_bps", "pre_analysis.policy.max_spread_bps", 10, "pre_analysis_policy"),
            ("funding_lte", "funding_current", "derivatives.funding.history_72h_max", 0.00004, "explicit_snapshot_level"),
            ("funding_gte", "funding_current", "derivatives.funding.history_72h_min", -0.00002, "explicit_snapshot_level"),
            ("imbalance_gte", "orderbook_imbalance", "orderbook.imbalance.value", self.snapshot["orderbook"]["imbalance"]["value"], "explicit_snapshot_level"),
            ("imbalance_lte", "orderbook_imbalance", "orderbook.imbalance.value", self.snapshot["orderbook"]["imbalance"]["value"], "explicit_snapshot_level"),
        ]
        for predicate, field, ref, threshold, origin in cases:
            response = directional_response(self.snapshot)
            extra = rule(rule_id="additional_condition", purpose="quality", predicate_type=predicate, field=field,
                         threshold=threshold, threshold_origin=origin, reference_source={"primary":ref,"secondary":None})
            response["compatibility_contract"]["rules"].append(extra)
            self.validate(response)
            extra["field"] = "mark"
            with self.assertRaises(ValueError): self.validate(response)

    def test_range_and_atr_distance_have_reproducible_derivations(self):
        response = directional_response(self.snapshot)
        ranged = rule(rule_id="entry_range", purpose="entry", predicate_type="price_in_range", threshold=None,
                      range={"lower":99,"upper":102}, derivation="ordered_range", threshold_origin="model_derived_from_snapshot",
                      reference_source={"primary":"timeframes.15m.latest_candle.low","secondary":"timeframes.15m.latest_candle.high"})
        response["compatibility_contract"]["rules"].append(ranged)
        self.validate(response)
        ranged["range"]["upper"] = 103
        with self.assertRaises(ValueError): self.validate(response)
        ranged["range"]["upper"] = 102
        atr = self.snapshot["timeframes"]["15m"]["indicators"]["atr_14"]
        displacement = abs(self.snapshot["orderbook"]["midpoint"] - 99) / atr
        extra = rule(rule_id="price_drift", purpose="quality", predicate_type="max_price_displacement_atr",
                     threshold=displacement, derivation="snapshot_distance_atr", threshold_origin="model_derived_from_snapshot",
                     baseline_field="l2_midpoint", atr_field="original_atr_15m",
                     reference_source={"primary":"orderbook.midpoint","secondary":"timeframes.15m.latest_candle.low"})
        response["compatibility_contract"]["rules"].append(extra)
        self.validate(response)
        extra["threshold"] = 0.25
        with self.assertRaises(ValueError): self.validate(response)
        extra["threshold"] = displacement; extra["baseline_field"] = "mark"
        with self.assertRaises(ValueError): self.validate(response)

    def test_oi_change_has_no_existing_policy_and_cannot_invent_zero(self):
        for predicate in ("oi_change_gte", "oi_change_lte"):
            response = directional_response(self.snapshot)
            response["compatibility_contract"]["rules"].append(rule(
                rule_id="oi_change", purpose="entry", field="open_interest", predicate_type=predicate,
                threshold=0, reference_source={"primary":"derivatives.open_interest","secondary":None}))
            with self.assertRaises(ValueError): self.validate(response)

    def test_checked_in_schema_is_the_runtime_schema_and_strict_at_all_levels(self):
        self.assertEqual(json.loads(RESPONSE_SCHEMA_PATH.read_text()), RESPONSE_SCHEMA)
        def check(schema):
            if schema.get("type") == "object":
                self.assertFalse(schema["additionalProperties"])
                self.assertEqual(set(schema["required"]), set(schema["properties"]))
            for value in schema.values():
                if isinstance(value, dict): check(value)
                if isinstance(value, list):
                    for child in value:
                        if isinstance(child, dict): check(child)
        check(RESPONSE_SCHEMA)


if __name__ == "__main__":
    unittest.main()
