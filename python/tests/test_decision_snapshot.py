import copy
import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from src.decision_snapshot import (
    build_decision_snapshot, get_decision_snapshot_schema, main,
    serialize_decision_snapshot, validate_decision_snapshot,
)
from src.integrity import attach_checksum, verify_checksum
from src.hl_client import CapturedResponse
from src.market_state import build_market_state
from src.pre_analysis import POLICY, build_pre_analysis
from test_market_state import FakeClient, NOW


class DecisionSnapshotTests(unittest.TestCase):
    def market(self):
        market = build_market_state("HYPE", FakeClient(), candle_count=120, clock_ms=lambda: NOW)
        market["market_context"]["day_notional_volume"] = 10_000_000
        market["orderbook"]["best_bid"] = 100
        market["orderbook"]["best_ask"] = 100.01
        market["orderbook"]["midpoint"] = 100.005
        market["orderbook"]["spread_absolute"] = 0.01
        market["orderbook"]["spread_pct_of_mid"] = 0.01 / 100.005 * 100
        for frame in market["timeframes"].values():
            frame["indicators"]["ema"] = {"8": 104, "21": 103, "50": 102, "120": 101}
            frame["indicators"]["volume_relative_20"] = 1.3
            frame["indicators"]["atr_14_pct"] = 0.5
        market["derivatives"]["funding"]["history_72h"] = [
            {"fundingRate": "-0.00002"}, {"fundingRate": "0.00001"}, {"fundingRate": "0.00004"},
        ]
        attach_checksum(market)
        return market

    def build(self, market=None, offset=1000, **kwargs):
        return build_decision_snapshot(market or self.market(), as_of_ms=NOW + offset, **kwargs)

    def test_analyze_contract_and_projection(self):
        market = self.market()
        result = self.build(market)
        self.assertEqual(result["kind"], "decision_snapshot")
        self.assertEqual(result["schema_version"], "1.0")
        self.assertEqual(validate_decision_snapshot(result), [])
        self.assertTrue(verify_checksum(result))
        self.assertTrue(result["decision_contract"]["eligible_for_model_review"])
        self.assertFalse(result["decision_contract"]["execution_authorized"])
        self.assertIsNone(result["account_constraints"])
        self.assertEqual(result["pre_analysis"]["policy"], POLICY)
        self.assertEqual(result["source_market_checksum"], market["integrity"]["checksum"])
        self.assertNotIn("levels", result["orderbook"])
        for interval, frame in result["timeframes"].items():
            self.assertNotIn("candles", frame)
            self.assertEqual(frame["indicators"], market["timeframes"][interval]["indicators"])
            self.assertEqual(frame["trend"], "up")

    def test_range_and_funding_summaries(self):
        market = self.market()
        result = self.build(market)
        for interval in result["timeframes"]:
            tail = market["timeframes"][interval]["candles"][-20:]
            summary = result["timeframes"][interval]["range_last_20_closed"]
            self.assertEqual(summary, {"sample_count": 20, "high": max(c["high"] for c in tail), "low": min(c["low"] for c in tail)})
        funding = result["derivatives"]["funding"]
        self.assertEqual(funding["history_72h_count"], 3)
        self.assertEqual(funding["history_72h_min"], -0.00002)
        self.assertEqual(funding["history_72h_max"], 0.00004)
        self.assertAlmostEqual(funding["history_72h_mean"], 0.00001)
        self.assertAlmostEqual(result["orderbook"]["spread_bps"], market["orderbook"]["spread_pct_of_mid"] * 100)

    def test_empty_funding_stays_null_and_explicit(self):
        market = self.market(); market["derivatives"]["funding"]["history_72h"] = []
        attach_checksum(market)
        result = self.build(market)
        self.assertEqual(result["derivatives"]["funding"]["history_72h_count"], 0)
        self.assertIsNone(result["derivatives"]["funding"]["history_72h_mean"])
        self.assertIn("missing_numeric_value", [i["code"] for i in result["quality"]["warnings"]])

    def test_existing_degraded_market_shapes_produce_ineligible_contracts(self):
        for mode in ("insufficient", "missing", "null", "funding_missing", "empty_book", "one_side", "gap", "old"):
            market = build_market_state("HYPE", FakeClient(mode), candle_count=120, clock_ms=lambda: NOW)
            with self.subTest(mode=mode):
                result = self.build(market)
                self.assertEqual(result["pre_analysis"]["classification"], "SKIP")
                self.assertFalse(result["decision_contract"]["eligible_for_model_review"])
                self.assertEqual(validate_decision_snapshot(result), [])

    def test_empty_candle_endpoint_retains_null_contract_and_blockers(self):
        client = FakeClient()
        client.candles = lambda *args: CapturedResponse([], NOW)
        market = build_market_state("HYPE", client, candle_count=120, clock_ms=lambda: NOW)
        result = self.build(market)
        self.assertEqual(result["pre_analysis"]["classification"], "SKIP")
        self.assertIsNone(result["timeframes"]["5m"]["latest_candle"])
        self.assertEqual(result["timeframes"]["5m"]["range_last_20_closed"]["sample_count"], 0)
        self.assertFalse(result["decision_contract"]["eligible_for_model_review"])

    def test_watch_default_and_benchmark_opt_in(self):
        market = self.market()
        market["timeframes"]["1h"]["indicators"]["ema"]["120"] = 104
        attach_checksum(market)
        normal = self.build(market)
        benchmark = self.build(market, allow_watch_for_benchmark=True)
        self.assertEqual(normal["pre_analysis"]["classification"], "WATCH")
        self.assertFalse(normal["decision_contract"]["eligible_for_model_review"])
        self.assertTrue(benchmark["decision_contract"]["eligible_for_model_review"])
        self.assertTrue(benchmark["benchmark_override"]["applied"])
        self.assertEqual(normal["pre_analysis"], benchmark["pre_analysis"])
        self.assertEqual(normal["source_pre_analysis_checksum"], benchmark["source_pre_analysis_checksum"])

    def test_skip_cannot_be_overridden_and_expiry_is_explicit(self):
        result = self.build(offset=60_000, allow_watch_for_benchmark=True)
        self.assertEqual(result["pre_analysis"]["classification"], "SKIP")
        self.assertFalse(result["decision_contract"]["eligible_for_model_review"])
        self.assertFalse(result["benchmark_override"]["applied"])
        self.assertEqual(result["decision_contract"]["on_quality_block"], "NO_TRADE")
        self.assertLess(result["valid_until_utc"], result["as_of_utc"])
        self.assertEqual(self.build()["valid_until_utc"], "2033-05-18T03:33:34.000Z")

    def test_matching_pre_analysis_accepted_mismatches_rejected(self):
        market = self.market()
        pre = build_pre_analysis(market, as_of_ms=NOW + 1000)
        self.assertEqual(self.build(market), self.build(market, pre_analysis=pre))
        for mode in ("time", "classification", "symbol", "policy", "checksum"):
            wrong = copy.deepcopy(pre)
            if mode == "time": wrong = build_pre_analysis(market, as_of_ms=NOW + 2000)
            if mode == "classification": wrong["classification"] = "SKIP"
            if mode == "symbol": wrong["symbol"] = "ETH"
            if mode == "policy": wrong["policy"]["max_spread_bps"] = 100
            if mode != "checksum": attach_checksum(wrong)
            else: wrong["integrity"]["checksum"] = "0" * 64
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                self.build(market, pre_analysis=wrong)

    def test_determinism_immutability_and_detached_schema(self):
        market = self.market(); pre = build_pre_analysis(market, as_of_ms=NOW + 1000)
        before = copy.deepcopy((market, pre, POLICY))
        first = self.build(market, pre_analysis=pre)
        second = self.build(market, pre_analysis=pre)
        self.assertEqual(serialize_decision_snapshot(first), serialize_decision_snapshot(second))
        self.assertEqual((market, pre, POLICY), before)
        first["timeframes"]["15m"]["indicators"]["ema"]["21"] = 0
        self.assertEqual((market, pre, POLICY), before)
        schema = get_decision_snapshot_schema(); schema["properties"].clear()
        self.assertTrue(get_decision_snapshot_schema()["properties"])

    def test_unavailable_optional_indicator_preserved_as_null(self):
        market = self.market(); market["timeframes"]["5m"]["indicators"]["macd"]["histogram"] = None
        attach_checksum(market)
        result = self.build(market)
        self.assertIsNone(result["timeframes"]["5m"]["indicators"]["macd"]["histogram"])
        self.assertTrue(any("histogram" in issue["scope"] for issue in result["quality"]["warnings"]))

    def test_invalid_sources_fail_closed(self):
        for mode in ("checksum", "schema", "nonfinite", "count", "open", "range", "funding", "latest", "shape", "last_close"):
            market = self.market()
            if mode == "checksum": market["price"]["mark"] = 99
            if mode == "schema": market["schema_version"] = "1.0"
            if mode == "nonfinite": market["price"]["mark"] = float("nan")
            if mode == "count": market["timeframes"]["5m"]["candle_count"] = 999
            if mode == "open": market["timeframes"]["5m"]["candles"][-1]["close_time_ms"] = NOW + 10_000
            if mode == "range": market["timeframes"]["5m"]["candles"][-1]["high"] = "102"
            if mode == "funding": market["derivatives"]["funding"]["history_72h"][0]["fundingRate"] = "NaN"
            if mode == "latest":
                market["timeframes"]["5m"]["latest_candle"] = copy.deepcopy(market["timeframes"]["5m"]["latest_candle"])
                market["timeframes"]["5m"]["latest_candle"]["close"] = 100
            if mode == "shape": market["timeframes"] = []
            if mode == "last_close": market["timeframes"]["5m"]["last_close_utc"] = "2033-05-18T03:33:19.000Z"
            if mode not in ("checksum", "nonfinite"): attach_checksum(market)
            with self.subTest(mode=mode), self.assertRaises(ValueError): self.build(market)

    def test_extra_source_fields_are_not_leaked(self):
        market = self.market()
        market["price"]["unrequested"] = 42
        market["timeframes"]["5m"]["indicators"]["unrequested"] = 42
        attach_checksum(market)
        self.assertNotIn("unrequested", serialize_decision_snapshot(self.build(market)))

    def test_schema_and_semantic_validation_detect_tampering(self):
        for mode in ("checksum", "field", "type", "eligibility", "override", "extra", "nonfinite", "policy", "expiry", "routing"):
            snapshot = self.build()
            if mode == "checksum": snapshot["price"]["mark"] = 42
            if mode == "field": snapshot.pop("semantics")
            if mode == "type": snapshot["price"]["mark"] = True
            if mode == "eligibility": snapshot["decision_contract"]["eligible_for_model_review"] = False
            if mode == "override": snapshot["benchmark_override"]["applied"] = True
            if mode == "extra": snapshot["unexpected"] = True
            if mode == "nonfinite": snapshot["price"]["mark"] = float("inf")
            if mode == "policy": snapshot["pre_analysis"]["policy"]["max_spread_bps"] = 100
            if mode == "expiry": snapshot["valid_until_utc"] = "2033-05-18T03:34:00.000Z"
            if mode == "routing": snapshot["decision_contract"]["routing_reason"] = "benchmark_watch_override"
            if mode not in ("checksum", "nonfinite"): attach_checksum(snapshot)
            with self.subTest(mode=mode):
                self.assertTrue(validate_decision_snapshot(snapshot))
                with self.assertRaises(ValueError): serialize_decision_snapshot(snapshot)

    def test_cli_batch_is_compact_offline_stdout_only(self):
        markets = [self.market(), self.market()]; markets[1]["symbol"] = "SOL"; attach_checksum(markets[1])
        out = io.StringIO()
        with patch.object(Path, "open", side_effect=[io.StringIO(json.dumps(m)) for m in markets]), \
             patch("urllib.request.urlopen", side_effect=AssertionError("network forbidden")), \
             patch("subprocess.run", side_effect=AssertionError("execution forbidden")), \
             patch.object(Path, "write_text", side_effect=AssertionError("write forbidden")), redirect_stdout(out):
            self.assertEqual(main(["hype.json", "sol.json", "--as-of", "2033-05-18T03:33:21Z"]), 0)
        results = json.loads(out.getvalue())
        self.assertEqual([s["symbol"] for s in results], ["HYPE", "SOL"])
        self.assertEqual(out.getvalue().count("\n"), 1)
        self.assertTrue(all(not validate_decision_snapshot(s) for s in results))

    def test_cli_schema_and_bad_input(self):
        out = io.StringIO()
        with redirect_stdout(out): self.assertEqual(main(["--schema"]), 0)
        self.assertEqual(json.loads(out.getvalue()), get_decision_snapshot_schema())
        with patch.object(Path, "open", return_value=io.StringIO("[]")), redirect_stderr(io.StringIO()):
            self.assertEqual(main(["bad.json"]), 1)
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            main(["snapshot.json", "--as-of", "2026-10-02T10:37:43"])


if __name__ == "__main__":
    unittest.main()
