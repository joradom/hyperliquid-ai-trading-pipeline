import copy
import io
import json
import unittest
from threading import Barrier, Event, Lock
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from src.decision_snapshot import build_decision_snapshot, validate_decision_snapshot
from src.hl_client import HyperliquidAPIError
from src.integrity import attach_checksum, verify_checksum
from src.market_state import build_market_state
from src.pre_analysis import POLICY, build_pre_analysis
from src.scanner import MAX_CONCURRENT_CAPTURES, format_report, main, normalize_symbols, scan_symbols
from test_market_state import FakeClient, NOW


class ScannerTests(unittest.TestCase):
    def market(self, symbol, classification="ANALYZE"):
        market = build_market_state(symbol, FakeClient(), candle_count=120, clock_ms=lambda: NOW)
        market["market_context"]["day_notional_volume"] = 10_000_000 if classification != "SKIP" else 0
        market["orderbook"]["best_bid"] = 100
        market["orderbook"]["best_ask"] = 100.01
        for frame in market["timeframes"].values():
            frame["indicators"]["ema"] = {"8": 104, "21": 103, "50": 102, "120": 101}
            frame["indicators"]["atr_14_pct"] = 0.5
            frame["indicators"]["volume_relative_20"] = 1 if classification == "WATCH" else 1.3
        attach_checksum(market)
        return market

    def scan(self, symbols, builder=None, **kwargs):
        return scan_symbols(symbols, market_builder=builder or self.market, clock_ms=lambda: NOW + 1000, **kwargs)

    def test_all_classifications_equal_the_offline_scanner(self):
        markets = {symbol: self.market(symbol, label) for symbol, label in (("HYPE", "WATCH"), ("ETH", "ANALYZE"), ("SOL", "SKIP"))}
        before = copy.deepcopy(markets)
        policy = copy.deepcopy(POLICY)
        report = self.scan(["HYPE", "ETH", "SOL"], lambda symbol: markets[symbol])
        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["counts"], {"SKIP": 1, "WATCH": 1, "ANALYZE": 1})
        for row in report["results"]:
            expected = build_pre_analysis(markets[row["symbol"]], as_of_ms=NOW + 1000)
            self.assertEqual(row["pre_analysis"], expected)
            self.assertEqual(row["classification"], expected["classification"])
            self.assertEqual(row["reasons"], expected["reasons"])
            self.assertTrue(verify_checksum(row["pre_analysis"]))
            self.assertNotIn("timeframes", row)  # no full market series retained in report
        self.assertEqual(markets, before)
        self.assertEqual(POLICY, policy)

    def test_capture_error_isolated_and_later_symbols_still_run(self):
        calls = []
        def builder(symbol):
            calls.append(symbol)
            if symbol == "ETH": raise HyperliquidAPIError("HTTP 429: rate limited")
            return self.market(symbol)
        report = self.scan(["HYPE", "ETH", "SOL"], builder)
        self.assertCountEqual(calls, ["HYPE", "ETH", "SOL"])
        self.assertEqual([r["symbol"] for r in report["results"]], ["HYPE", "ETH", "SOL"])
        self.assertEqual(report["status"], "partial_failure")
        failed = report["results"][1]
        self.assertEqual(failed["classification"], "SKIP")
        self.assertEqual(failed["reasons"], ["capture_failed"])
        self.assertIsNone(failed["pre_analysis"])
        self.assertEqual(failed["error"]["message"], "HTTP 429: rate limited")
        self.assertEqual(failed["error"]["stage"], "capture")
        self.assertEqual(report["results"][2]["classification"], "ANALYZE")

    def test_all_capture_failures_report_failed_not_empty_market(self):
        def builder(symbol): raise HyperliquidAPIError(f"unknown perpetual market: {symbol}")
        report = self.scan(["UNKNOWN", "OTHER"], builder)
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["counts"]["SKIP"], 2)
        self.assertTrue(all(row["market_quality"] is None for row in report["results"]))

    def test_pre_analysis_failure_isolated(self):
        market = self.market("HYPE")
        with patch("src.scanner.build_pre_analysis", side_effect=ValueError("bad input")):
            report = self.scan(["HYPE"], lambda _: market)
        failed = report["results"][0]
        self.assertEqual(failed["reasons"], ["pre_analysis_failed"])
        self.assertEqual(failed["market_timestamp_utc"], market["timestamp_utc"])
        self.assertIsNone(failed["pre_analysis"])

    def test_wrong_symbol_never_classifies_requested_asset(self):
        report = self.scan(["HYPE"], lambda _: self.market("ETH"))
        self.assertEqual(report["results"][0]["classification"], "SKIP")
        self.assertIn("symbol mismatch", report["results"][0]["error"]["message"])

    def test_quality_degradation_and_staleness_preserved(self):
        market = self.market("HYPE")
        report = scan_symbols(["HYPE"], market_builder=lambda _: market, clock_ms=lambda: NOW + 60_000)
        row = report["results"][0]
        self.assertEqual(row["market_quality"]["status"], "degraded")
        self.assertEqual(row["classification"], "SKIP")
        self.assertTrue(any(issue["code"] == "unusable_timestamp" for issue in row["pre_analysis"]["quality"]["blockers"]))
        self.assertEqual(report["status"], "ok")  # screening SKIP is not an orchestration error

    def test_evaluation_happens_immediately_per_symbol(self):
        events = []
        def builder(symbol):
            events.append("capture:" + symbol)
            return self.market(symbol)
        def pre(market, **kwargs):
            events.append("pre:" + market["symbol"])
            return build_pre_analysis(market, **kwargs)
        with patch("src.scanner.build_pre_analysis", side_effect=pre): self.scan(["HYPE", "ETH"], builder)
        for symbol in ("HYPE", "ETH"):
            self.assertLess(events.index("capture:" + symbol), events.index("pre:" + symbol))

    def test_three_concurrent_captures_and_limit_for_larger_batches(self):
        self.assertEqual(MAX_CONCURRENT_CAPTURES, 3)
        barrier = Barrier(3)
        lock = Lock()
        active = peak = 0
        def builder(symbol):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            try:
                # Sequential or fewer than three workers cannot pass this barrier.
                barrier.wait(timeout=5)
                return self.market(symbol)
            finally:
                with lock: active -= 1
        symbols = ["HYPE", "ETH", "SOL", "BTC", "DOGE", "AVAX"]
        report = self.scan(symbols, builder)
        self.assertEqual(peak, 3)
        self.assertEqual(active, 0)
        self.assertEqual(report["status"], "ok")
        self.assertEqual([r["symbol"] for r in report["results"]], symbols)

    def test_out_of_order_completion_preserves_output_and_does_not_delay_pre(self):
        later_screened = Event()
        captures = Barrier(3)
        events = []
        def builder(symbol):
            captures.wait(timeout=5)
            if symbol == "HYPE":
                self.assertTrue(later_screened.wait(timeout=5))
            events.append("capture:" + symbol)
            return self.market(symbol)
        def pre(market, **kwargs):
            result = build_pre_analysis(market, **kwargs)
            events.append("pre:" + market["symbol"])
            if market["symbol"] == "SOL": later_screened.set()
            return result
        with patch("src.scanner.build_pre_analysis", side_effect=pre):
            report = self.scan(["HYPE", "ETH", "SOL"], builder)
        self.assertLess(events.index("pre:SOL"), events.index("capture:HYPE"))
        self.assertEqual([r["symbol"] for r in report["results"]], ["HYPE", "ETH", "SOL"])
        self.assertEqual(report["status"], "ok")

    def test_concurrent_error_does_not_cancel_other_captures(self):
        captures = Barrier(3)
        def builder(symbol):
            captures.wait(timeout=5)
            if symbol == "ETH": raise HyperliquidAPIError("HTTP 500: server error")
            return self.market(symbol)
        report = self.scan(["HYPE", "ETH", "SOL"], builder)
        self.assertEqual(report["status"], "partial_failure")
        self.assertEqual([r["classification"] for r in report["results"]], ["ANALYZE", "SKIP", "ANALYZE"])
        self.assertEqual(report["results"][1]["error"]["message"], "HTTP 500: server error")

    def test_monotonic_durations_and_totals(self):
        # start, row-start, capture-end, pre-start/end, decision-start/end,
        # row-end, batch-end
        ticks = iter([0.0, 0.1, 2.1, 2.2, 2.5, 2.6, 2.8, 2.9, 3.0])
        report = self.scan(["HYPE"], timer=lambda: next(ticks))
        self.assertEqual(report["results"][0]["timing"], {"capture_seconds": 2.0, "pre_analysis_seconds": 0.3,
                                                            "decision_snapshot_seconds": 0.2, "total_seconds": 2.8})
        self.assertEqual(report["total_seconds"], 3.0)
        self.assertEqual(report["capture_seconds_total"], 2.0)
        self.assertEqual(report["pre_analysis_seconds_total"], 0.3)
        self.assertEqual(report["decision_snapshot_seconds_total"], 0.2)

    def test_case_normalization_deduplication_and_full_batch_validation(self):
        self.assertEqual(normalize_symbols(["hype", "ETH", "HYPE", "sol"]), ["HYPE", "ETH", "SOL"])
        calls = []
        def builder(symbol): calls.append(symbol); return self.market(symbol)
        report = self.scan(["hype", "HYPE", "eth"], builder)
        self.assertEqual(report["symbols"], ["HYPE", "ETH"])
        self.assertCountEqual(calls, ["HYPE", "ETH"])
        for invalid in ([], "HYPE", ["HYPE", "SOL;bad"], [""], [None], [" HYPE"], ["ΣOL"]):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError): self.scan(invalid, builder)
        self.assertCountEqual(calls, ["HYPE", "ETH"])

    def test_default_builder_uses_existing_memory_layer_without_writes(self):
        market = self.market("HYPE")
        with patch("src.scanner.build_market_state", return_value=market) as builder, \
             patch("src.market_state.save_snapshot", side_effect=AssertionError("save forbidden")), \
             patch.object(Path, "write_text", side_effect=AssertionError("write forbidden")), \
             patch("builtins.open", side_effect=AssertionError("file I/O forbidden")), \
             patch("subprocess.run", side_effect=AssertionError("execution forbidden")):
            report = scan_symbols(["HYPE"], clock_ms=lambda: NOW + 1000)
        builder.assert_called_once_with("HYPE")
        self.assertEqual(report["results"][0]["classification"], "ANALYZE")

    def test_cli_json_stdout_and_exit_codes(self):
        for label in ("ANALYZE", "SKIP"):
            report = self.scan(["HYPE"], lambda _: self.market("HYPE", label))
            out = io.StringIO()
            with patch("src.scanner.scan_symbols", return_value=report), redirect_stdout(out):
                self.assertEqual(main(["HYPE", "--json"]), 0)
            self.assertEqual(json.loads(out.getvalue()), report)
            self.assertEqual(out.getvalue().count("\n"), 1)
        def fail(_): raise HyperliquidAPIError("timeout")
        failed = self.scan(["HYPE"], fail)
        with patch("src.scanner.scan_symbols", return_value=failed), redirect_stdout(io.StringIO()):
            self.assertEqual(main(["HYPE", "--json"]), 1)

    def test_cli_text_includes_reasons_blockers_and_total(self):
        report = self.scan(["HYPE"], lambda _: self.market("HYPE", "SKIP"))
        out = io.StringIO()
        with patch("src.scanner.scan_symbols", return_value=report), redirect_stdout(out):
            self.assertEqual(main(["HYPE"]), 0)
        self.assertEqual(out.getvalue(), format_report(report) + "\n")
        self.assertIn("HYPE SKIP", out.getvalue())
        self.assertIn("daily_liquidity", out.getvalue())
        self.assertIn("Total capture/pre-analysis/decision-snapshot:", out.getvalue())

    def test_only_analyze_generates_snapshot_with_exact_in_memory_objects(self):
        markets = {symbol: self.market(symbol, label) for symbol, label in (("HYPE", "ANALYZE"), ("ETH", "WATCH"), ("SOL", "SKIP"))}
        before = copy.deepcopy(markets)
        with patch("src.scanner.build_decision_snapshot", wraps=build_decision_snapshot) as builder:
            report = self.scan(["HYPE", "ETH", "SOL"], lambda symbol: markets[symbol])
        builder.assert_called_once()
        args, kwargs = builder.call_args
        hype, eth, sol = report["results"]
        self.assertIs(args[0], markets["HYPE"])
        self.assertIs(kwargs["pre_analysis"], hype["pre_analysis"])
        self.assertEqual(kwargs["as_of_ms"], NOW + 1000)
        self.assertNotIn("allow_watch_for_benchmark", kwargs)
        self.assertEqual(validate_decision_snapshot(hype["decision_snapshot"]), [])
        self.assertEqual(hype["decision_snapshot"]["source_market_checksum"], markets["HYPE"]["integrity"]["checksum"])
        self.assertEqual(hype["decision_snapshot"]["source_pre_analysis_checksum"], hype["pre_analysis"]["integrity"]["checksum"])
        self.assertEqual(hype["decision_snapshot"]["as_of_utc"], hype["pre_analysis"]["as_of_utc"])
        for row in (eth, sol):
            self.assertIsNone(row["decision_snapshot"])
            self.assertEqual(row["timing"]["decision_snapshot_seconds"], 0)
        self.assertEqual(markets, before)

    def test_degraded_unknown_source_times_do_not_block_usable_analyze(self):
        row = self.scan(["HYPE"])["results"][0]
        self.assertEqual(row["market_quality"]["status"], "degraded")
        self.assertEqual(row["classification"], "ANALYZE")
        decision = row["decision_snapshot"]
        self.assertIsNotNone(decision)
        self.assertEqual(decision["quality"]["market_status"], "degraded")
        self.assertEqual(decision["quality"]["blockers"], [])
        self.assertTrue(decision["decision_contract"]["eligible_for_model_review"])
        self.assertTrue(any(issue["code"] == "unknown_freshness" for issue in decision["quality"]["warnings"]))

    def test_stale_analyze_candidate_never_generates_snapshot(self):
        market = self.market("HYPE")
        with patch("src.scanner.build_decision_snapshot", side_effect=AssertionError("stale snapshot must not be built")) as builder:
            report = scan_symbols(["HYPE"], market_builder=lambda _: market, clock_ms=lambda: NOW + 60_000)
        builder.assert_not_called()
        self.assertEqual(report["results"][0]["classification"], "SKIP")
        self.assertIsNone(report["results"][0]["decision_snapshot"])

    def test_decision_build_failure_keeps_classification_and_is_isolated(self):
        def decision(market, **kwargs):
            if market["symbol"] == "ETH": raise ValueError("inconsistent source data")
            return build_decision_snapshot(market, **kwargs)
        with patch("src.scanner.build_decision_snapshot", side_effect=decision):
            report = self.scan(["HYPE", "ETH", "SOL"])
        self.assertEqual(report["status"], "partial_failure")
        hype, eth, sol = report["results"]
        self.assertEqual(eth["classification"], "ANALYZE")
        self.assertIsNotNone(eth["pre_analysis"])
        self.assertIsNone(eth["decision_snapshot"])
        self.assertEqual(eth["error"]["stage"], "decision_snapshot")
        self.assertEqual(eth["error"]["message"], "inconsistent source data")
        self.assertEqual(eth["reasons"], eth["pre_analysis"]["reasons"])
        self.assertTrue(hype["decision_snapshot"] and sol["decision_snapshot"])

    def test_snapshot_built_before_waiting_for_slow_first_symbol(self):
        decision_ready = Event()
        captures = Barrier(3)
        def builder(symbol):
            captures.wait(timeout=5)
            if symbol == "HYPE": self.assertTrue(decision_ready.wait(timeout=5))
            return self.market(symbol)
        def decision(market, **kwargs):
            result = build_decision_snapshot(market, **kwargs)
            if market["symbol"] == "SOL": decision_ready.set()
            return result
        with patch("src.scanner.build_decision_snapshot", side_effect=decision):
            report = self.scan(["HYPE", "ETH", "SOL"], builder)
        self.assertEqual(report["status"], "ok")
        self.assertEqual([r["symbol"] for r in report["results"]], ["HYPE", "ETH", "SOL"])
        self.assertTrue(all(r["decision_snapshot"] is not None for r in report["results"]))

    def test_shared_evaluation_clock_preserves_expiry_without_reclocking(self):
        # Only batch start, one per-symbol evaluation, and batch completion read
        # the clock. A later batch completion cannot rewrite the snapshot age.
        wall_clock = iter([NOW, NOW + 1000, NOW + 60_000])
        report = scan_symbols(["HYPE"], market_builder=self.market, clock_ms=lambda: next(wall_clock))
        row = report["results"][0]
        self.assertEqual(row["decision_snapshot"]["as_of_utc"], row["pre_analysis"]["as_of_utc"])
        self.assertEqual(row["decision_snapshot"]["quality"]["ages"], row["pre_analysis"]["quality"]["ages"])
        self.assertLess(row["decision_snapshot"]["valid_until_utc"], report["completed_at_utc"])

    def test_cli_invalid_symbols_exit_before_capture(self):
        with patch("src.scanner.scan_symbols") as scanner, redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            main(["HYPE", "bad;symbol"])
        self.assertEqual(error.exception.code, 2)
        scanner.assert_not_called()


if __name__ == "__main__":
    unittest.main()
