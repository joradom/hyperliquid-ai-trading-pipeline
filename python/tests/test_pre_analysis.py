import copy
import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from src.integrity import attach_checksum, verify_checksum
from src.market_state import build_market_state
from src.pre_analysis import POLICY, build_pre_analysis, main
from test_market_state import FakeClient, NOW


class PreAnalysisTests(unittest.TestCase):
    def snapshot(self, trend="up"):
        snapshot = build_market_state("HYPE", FakeClient(), candle_count=120, clock_ms=lambda: NOW)
        snapshot["market_context"]["day_notional_volume"] = 10_000_000
        snapshot["orderbook"]["best_bid"] = 100
        snapshot["orderbook"]["best_ask"] = 100.01
        for frame in snapshot["timeframes"].values():
            frame["indicators"]["ema"] = {"21": 103, "50": 102, "120": 101} if trend == "up" else {"21": 101, "50": 102, "120": 103}
            frame["indicators"]["volume_relative_20"] = 1.3
            frame["indicators"]["atr_14_pct"] = 0.5
        attach_checksum(snapshot)
        return snapshot

    def screen(self, snapshot, offset=1000, resign=True):
        if resign:
            attach_checksum(snapshot)
        return build_pre_analysis(snapshot, as_of_ms=NOW + offset)

    def codes(self, result):
        return [item["code"] for item in result["quality"]["blockers"]]

    def test_analyze_up_and_down_with_expected_unknown_freshness(self):
        for trend in ("up", "down"):
            with self.subTest(trend=trend):
                result = self.screen(self.snapshot(trend))
                self.assertEqual(result["classification"], "ANALYZE")
                self.assertTrue(result["quality"]["warnings"])
                self.assertFalse(result["quality"]["blockers"])
                self.assertTrue(verify_checksum(result))
                self.assertTrue(all("candles" not in frame for frame in result["features"]["timeframes"].values()))

    def test_deterministic_and_does_not_mutate_input_or_policy(self):
        snapshot = self.snapshot()
        before, policy_before = copy.deepcopy(snapshot), copy.deepcopy(POLICY)
        first = build_pre_analysis(snapshot, as_of_ms=NOW + 1000)
        second = build_pre_analysis(snapshot, as_of_ms=NOW + 1000)
        self.assertEqual(first, second)
        self.assertEqual(snapshot, before)
        self.assertEqual(first["source_checksum"], snapshot["integrity"]["checksum"])
        first["policy"]["min_candles"] = 0
        self.assertEqual(POLICY, policy_before)

    def test_watch_for_no_activity_or_conflicting_trends(self):
        snapshot = self.snapshot()
        for frame in snapshot["timeframes"].values():
            frame["indicators"]["volume_relative_20"] = 1
        result = self.screen(snapshot)
        self.assertEqual(result["classification"], "WATCH")
        self.assertIn("relative_volume_below_threshold", result["reasons"])
        snapshot = self.snapshot()
        snapshot["timeframes"]["4h"]["indicators"]["ema"] = {"21": 101, "50": 102, "120": 103}
        self.assertEqual(self.screen(snapshot)["classification"], "WATCH")

    def test_mixed_4h_allowed_and_activity_on_either_fast_timeframe(self):
        snapshot = self.snapshot()
        snapshot["timeframes"]["4h"]["indicators"]["ema"] = {"21": 102, "50": 101, "120": 103}
        snapshot["timeframes"]["5m"]["indicators"]["volume_relative_20"] = 0.9
        snapshot["timeframes"]["15m"]["indicators"]["volume_relative_20"] = 1.2
        self.assertEqual(self.screen(snapshot)["classification"], "ANALYZE")

    def test_liquidity_spread_and_volatility_gates(self):
        for field in ("volume", "spread", "low_atr", "high_atr"):
            snapshot = self.snapshot()
            if field == "volume": snapshot["market_context"]["day_notional_volume"] = 999_999
            if field == "spread": snapshot["orderbook"]["best_ask"] = 101
            if field == "low_atr": snapshot["timeframes"]["15m"]["indicators"]["atr_14_pct"] = 0.099
            if field == "high_atr": snapshot["timeframes"]["15m"]["indicators"]["atr_14_pct"] = 5.001
            with self.subTest(field=field):
                result = self.screen(snapshot)
                self.assertEqual(result["classification"], "SKIP")
                self.assertIn("screening_condition_failed", self.codes(result))

    def test_inclusive_liquidity_volatility_and_activity_boundaries(self):
        for atr in (0.1, 5):
            snapshot = self.snapshot()
            snapshot["market_context"]["day_notional_volume"] = 1_000_000
            snapshot["timeframes"]["15m"]["indicators"]["atr_14_pct"] = atr
            self.assertEqual(self.screen(snapshot)["classification"], "ANALYZE")

    def test_spread_boundary_and_blocker_precedence(self):
        snapshot = self.snapshot()
        snapshot["orderbook"]["best_bid"] = 1999
        snapshot["orderbook"]["best_ask"] = 2001
        self.assertEqual(self.screen(snapshot)["classification"], "ANALYZE")
        snapshot["orderbook"]["best_ask"] = 2001.001
        self.assertEqual(self.screen(snapshot)["classification"], "SKIP")

    def test_funding_and_each_candle_timestamp_checked_independently(self):
        for scope in ("funding", "5m", "15m", "1h", "4h"):
            snapshot = self.snapshot()
            if scope == "funding":
                snapshot["capture"]["sources"][scope]["source_timestamp_utc"] = "2020-01-01T00:00:00Z"
            else:
                snapshot["timeframes"][scope]["last_close_utc"] = "2020-01-01T00:00:00Z"
            with self.subTest(scope=scope):
                self.assertEqual(self.screen(snapshot)["classification"], "SKIP")

    def test_stored_freshness_is_recomputed_at_consumption(self):
        result = self.screen(self.snapshot(), offset=60_000)
        self.assertEqual(result["classification"], "SKIP")
        self.assertIn("unusable_timestamp", self.codes(result))
        self.assertFalse(result["quality"]["ages"]["orderbook"]["usable"])

    def test_source_timestamp_future_and_missing(self):
        snapshot = self.snapshot()
        snapshot["capture"]["sources"]["orderbook"]["source_timestamp_utc"] = None
        self.assertEqual(self.screen(snapshot)["classification"], "SKIP")
        result = self.screen(self.snapshot(), offset=-10_000)
        self.assertEqual(result["classification"], "SKIP")
        self.assertIn("unusable_timestamp", self.codes(result))

    def test_checksum_missing_tampered_and_nonfinite(self):
        for mode in ("missing", "tampered", "nonfinite"):
            snapshot = self.snapshot()
            if mode == "missing": snapshot.pop("integrity")
            if mode == "tampered": snapshot["price"]["mark"] = 99
            if mode == "nonfinite": snapshot["price"]["mark"] = float("nan")
            result = self.screen(snapshot, resign=False)
            self.assertEqual(result["classification"], "SKIP")
            self.assertIn("invalid_checksum", self.codes(result))
            self.assertTrue(verify_checksum(result))

    def test_source_issues_block_except_expected_unknowns(self):
        for code, scope in (("timestamp_gap", "timeframes.5m"), ("endpoint_unavailable", "derivatives.funding_history"), ("unknown_freshness", "orderbook")):
            snapshot = self.snapshot()
            snapshot["data_quality"]["issues"].append({"code": code, "scope": scope})
            self.assertEqual(self.screen(snapshot)["classification"], "SKIP")

    def test_malformed_missing_numeric_and_insufficient_fields(self):
        snapshots = []
        for key in ("timeframes", "capture", "data_quality", "orderbook"):
            snapshot = self.snapshot(); snapshot[key] = []
            snapshots.append(snapshot)
        snapshot = self.snapshot(); snapshot["timeframes"]["1h"]["indicators"]["ema"]["21"] = None
        snapshots.append(snapshot)
        snapshot = self.snapshot(); snapshot["timeframes"]["5m"]["candle_count"] = 119
        snapshots.append(snapshot)
        snapshot = self.snapshot(); snapshot["orderbook"]["best_bid"] = 101
        snapshots.append(snapshot)
        for snapshot in snapshots:
            result = self.screen(snapshot)
            self.assertEqual(result["classification"], "SKIP")
            self.assertTrue(verify_checksum(result))

    def test_schema_and_validation_errors(self):
        snapshot = self.snapshot(); snapshot["schema_version"] = "1.0"
        self.assertIn("unsupported_schema", self.codes(self.screen(snapshot)))
        snapshot = self.snapshot(); snapshot["data_quality"]["validation_errors"] = ["bad"]
        self.assertEqual(self.screen(snapshot)["classification"], "SKIP")

    def test_cli_batch_is_offline_and_stdout_only(self):
        snapshots = [self.snapshot(), self.snapshot("down")]
        snapshots[1]["symbol"] = "ETH"; attach_checksum(snapshots[1])
        stdout = io.StringIO()
        with patch.object(Path, "open", side_effect=[io.StringIO(json.dumps(s)) for s in snapshots]), \
             patch("urllib.request.urlopen", side_effect=AssertionError("network forbidden")), \
             patch.object(Path, "write_text", side_effect=AssertionError("writes forbidden")), \
             redirect_stdout(stdout):
            self.assertEqual(main(["hype.json", "eth.json", "--as-of", "2033-05-18T03:33:21Z"]), 0)
        results = json.loads(stdout.getvalue())
        self.assertEqual([r["symbol"] for r in results], ["HYPE", "ETH"])
        self.assertEqual([r["classification"] for r in results], ["ANALYZE", "ANALYZE"])

    def test_invalid_arguments_and_cli_input(self):
        with self.assertRaises(ValueError): build_pre_analysis([], as_of_ms=NOW)
        with self.assertRaises(ValueError): build_pre_analysis({}, as_of_ms=True)
        with patch.object(Path, "open", return_value=io.StringIO("not json")), redirect_stderr(io.StringIO()):
            self.assertEqual(main(["bad.json"]), 1)


if __name__ == "__main__":
    unittest.main()
