"""Discovery-to-review integration, with all HTTP and model processes simulated."""
import copy
import io
import json
import math
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

from src.analysis_pipeline import run_pipeline
from src.hl_client import HyperliquidAPIError
from src.integrity import verify_checksum
from src.market_screener import screen_market
from src.research_config import ResearchConfig, ScreenerConfig, load_config
from src.research_pipeline import format_report, main, run_research
from src.scanner import scan_symbols
from test_analysis_pipeline import RESPONSE, successful_runner, snapshot_input
from test_market_screener import FakeScreenerClient
from test_market_state import NOW
import test_scanner


class ResearchPipelineTests(unittest.TestCase):
    def setUp(self):
        self.network = patch("urllib.request.urlopen", side_effect=AssertionError("real HTTP forbidden")).start()
        self.process = patch("subprocess.run", side_effect=AssertionError("real models/wallets forbidden")).start()
        self.addCleanup(patch.stopall)

    def run_case(self, *, settings=None, labels=None, client=None, runner=None, **overrides):
        labels = labels or {}
        client = client or FakeScreenerClient()
        self.screener = Mock(side_effect=lambda **kwargs: screen_market(client=client, **kwargs))
        self.builder = Mock(side_effect=lambda symbol: test_scanner.ScannerTests().market(symbol, labels.get(symbol, "ANALYZE")))
        self.scanner = Mock(side_effect=lambda symbols, **kwargs: scan_symbols(symbols, market_builder=self.builder, **kwargs))
        self.pipeline = Mock(wraps=run_pipeline)
        self.runner = runner or Mock(side_effect=successful_runner)
        report = run_research(configuration=settings, screener=self.screener, pipeline=self.pipeline,
                              scanner=self.scanner, runner=self.runner, clock_ms=lambda: NOW + 1000, **overrides)
        self.network.assert_not_called()
        self.process.assert_not_called()
        return report

    def test_automatic_selection_preserves_original_ranking_metrics_and_reports(self):
        report = self.run_case(labels={"BTC": "WATCH", "ETH": "SKIP"})
        self.assertEqual(report["kind"], "research_pipeline")
        self.assertEqual(report["schema_version"], "1.0")
        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["selected_symbols"], ["FET", "BTC", "ETH"])
        self.assertEqual(report["pipeline_symbols"], report["selected_symbols"])
        self.assertEqual(report["screener"]["results"][0]["vol_delta_pct_24h"], 100)
        self.assertEqual(report["scanner"]["counts"], {"SKIP": 1, "WATCH": 1, "ANALYZE": 1})
        self.assertEqual(report["analysis"]["counts"], report["scanner"]["counts"])
        self.assertEqual(report["analysis"]["schema_version"], "1.2")
        self.assertEqual(report["analysis"]["results"][0]["model_review"]["response"], RESPONSE)
        self.assertEqual(self.pipeline.call_args.args[0], ["FET", "BTC", "ETH"])
        self.assertTrue(report["read_only"])
        self.assertFalse(report["execution_authorized"])
        self.assertNotIn("providers", report["analysis"]["results"][0]["model_review"])
        self.assertIn("#1 FET", format_report(report))
        self.assertIn("FET ANALYZE", format_report(report))
        json.dumps(report, allow_nan=False)

    def test_scanner_capture_is_single_and_snapshots_are_reused_by_identity(self):
        from src.decision_snapshot import serialize_decision_snapshot
        with patch("src.analysis_pipeline.serialize_decision_snapshot", wraps=serialize_decision_snapshot) as serialize:
            report = self.run_case(limit=2)
        self.screener.assert_called_once()
        self.pipeline.assert_called_once()
        self.scanner.assert_called_once()
        self.assertEqual(self.builder.call_count, 2)
        self.assertCountEqual([call.args[0] for call in self.builder.call_args_list], ["FET", "BTC"])
        self.assertEqual(self.runner.call_count, 2)
        for original, reviewed in zip(report["scanner"]["results"], report["analysis"]["results"]):
            self.assertIs(original["decision_snapshot"], reviewed["decision_snapshot"])
            self.assertIs(original["pre_analysis"], reviewed["pre_analysis"])
            self.assertTrue(verify_checksum(original["decision_snapshot"]))
            self.assertTrue(any(call.args[0] is original["decision_snapshot"] for call in serialize.call_args_list))
        payloads = [snapshot_input(call.args[0], call.kwargs) for call in self.runner.call_args_list]
        self.assertCountEqual([p["symbol"] for p in payloads], ["FET", "BTC"])
        self.assertTrue(all(p in [r["decision_snapshot"] for r in report["scanner"]["results"]] for p in payloads))

    def test_empty_selection_does_not_call_pipeline_scanner_or_models(self):
        report = self.run_case(min_volume=1_000_000_000)
        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["outcome"], "no_candidates")
        self.assertEqual(report["selected_symbols"], [])
        self.assertIsNone(report["scanner"])
        self.assertIsNone(report["analysis"])
        self.assertEqual(report["timing"]["analysis_pipeline_seconds"], 0)
        self.pipeline.assert_not_called()
        self.scanner.assert_not_called()
        self.runner.assert_not_called()

    def test_no_analyze_candidates_keep_all_classifications_without_models(self):
        report = self.run_case(labels={"FET": "WATCH", "BTC": "SKIP", "ETH": "WATCH"})
        self.scanner.assert_called_once()
        self.runner.assert_not_called()
        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["analysis"]["counts"], {"SKIP": 1, "WATCH": 2, "ANALYZE": 0})
        self.assertEqual(report["analysis"]["codex_invocations"], 0)
        self.assertTrue(all(row["decision_snapshot"] is None for row in report["scanner"]["results"]))

    def test_profile_and_explicit_overrides_have_correct_precedence(self):
        settings = ResearchConfig(screener=ScreenerConfig(top_n=3, limit=2, sort_by="oi", max_workers=2))
        report = self.run_case(settings=settings, limit=1, sort_by="vol_delta", min_volume=0)
        self.assertEqual(report["selected_symbols"], ["FET"])
        self.assertEqual(report["parameters"], {"top_n": 3, "limit": 1, "sort_by": "vol_delta", "min_volume": 0, "max_workers": 2})
        self.assertEqual(settings.screener.limit, 2)
        self.assertEqual(settings.screener.min_volume, 1_000_000)
        self.assertEqual(self.pipeline.call_args.kwargs["configuration"], settings)
        self.assertEqual(report["analysis"]["schema_version"], "1.2")

    def test_experimental_profile_keeps_strict_scanner_and_multimodel_contract(self):
        report = self.run_case(settings=load_config(profile="experimental"), limit=1)
        self.assertEqual(self.screener.call_args.kwargs["min_volume"], 10_000_000)
        self.assertEqual(report["scanner"]["results"][0]["pre_analysis"]["policy"]["max_spread_bps"], 5)
        self.assertEqual(report["analysis"]["schema_version"], "1.3")
        self.assertEqual({call.args[0][0] for call in self.runner.call_args_list}, {"codex", "agy"})

    def test_partial_screener_errors_do_not_prevent_capture_or_other_reviews(self):
        report = self.run_case(client=FakeScreenerClient(error_symbol="ETH"))
        self.assertEqual(report["screener"]["status"], "partial_failure")
        self.assertEqual(report["status"], "partial_failure")
        self.assertEqual(report["analysis"]["status"], "ok")
        self.assertEqual(report["selection_quality"]["status"], "degraded")
        self.scanner.assert_called_once()
        self.assertEqual(self.runner.call_count, 3)
        eth = next(row for row in report["screener"]["results"] if row["symbol"] == "ETH")
        self.assertIn("HyperliquidAPIError", eth["error"])
        self.assertIsNone(eth["vol_delta_pct_24h"])
        quality = next(row for row in report["selection_quality"]["assets"] if row["symbol"] == "ETH")
        self.assertIn("vol_delta_pct_24h", quality["unavailable_metrics"])
        self.assertTrue(any(issue["code"] == "asset_selection_error" for issue in report["selection_quality"]["issues"]))
        self.assertIn("vol_delta=unavailable", format_report(report))

    def test_default_discovery_and_scanner_share_provider_without_changing_json(self):
        from src.market_context import MarketContextProvider
        from test_market_state import FakeClient
        from test_rest_policy import Clock
        class CombinedClient(FakeScreenerClient):
            def __init__(self):
                super().__init__()
                self.market=FakeClient()
                self.meta_and_asset_contexts=Mock(wraps=self.meta_and_asset_contexts)
            def candles(self,*args): return self.market.candles(*args)
            def l2_book(self,*args): return self.market.l2_book(*args)
            def funding_history(self,*args): return self.market.funding_history(*args)
        clock=Clock(); client=CombinedClient()
        provider=MarketContextProvider(client,timer=clock,wait=clock.wait,clock_ms=lambda:NOW+1000)
        report=run_research(sort_by="volume",limit=2,context_provider=provider,
                            clock_ms=lambda:NOW+1000,runner=Mock(side_effect=successful_runner))
        client.meta_and_asset_contexts.assert_called_once()
        self.assertEqual(report["screener"]["status"],"ok")
        self.assertEqual(report["scanner"]["symbols"],["BTC","ETH"])
        self.assertTrue(all(row["market_quality"] is not None for row in report["scanner"]["results"]))
        self.assertFalse(report["execution_authorized"])
        json.dumps(report,allow_nan=False)
        self.network.assert_not_called()
        self.process.assert_not_called()

    def test_capture_degradation_propagates_to_research_json_and_text(self):
        from src.integrity import attach_checksum
        def builder(symbol):
            market=test_scanner.ScannerTests().market(symbol,"SKIP")
            if symbol=="BTC":
                market["data_quality"]["issues"].append({"code":"endpoint_unavailable",
                    "scope":"derivatives.funding_history","detail":"budget_wait_exceeded"})
                attach_checksum(market)
            return market
        def scanner(symbols,**kwargs):
            return scan_symbols(symbols,market_builder=builder,**kwargs)
        selection=screen_market(client=FakeScreenerClient(),clock_ms=lambda:NOW)
        runner=Mock(side_effect=AssertionError("SKIP must not invoke models"))
        report=run_research(screener=Mock(return_value=selection),scanner=scanner,
            runner=runner,clock_ms=lambda:NOW+1000)
        self.assertEqual(report["scanner"]["status"],"partial_failure")
        self.assertEqual(report["analysis"]["status"],"partial_failure")
        self.assertEqual(report["status"],"partial_failure")
        self.assertEqual(report["scanner"]["counts"],{"SKIP":3,"WATCH":0,"ANALYZE":0})
        self.assertIn("budget_wait_exceeded",format_report(report))
        self.assertIn("budget_wait_exceeded",json.dumps(report,allow_nan=False))
        runner.assert_not_called()

    def test_research_keeps_scanner_partial_when_injected_pipeline_reports_ok(self):
        captured={"status":"partial_failure"}
        def pipeline(symbols,scanner,**kwargs):
            scanner(symbols)
            return {"kind":"analysis_pipeline","schema_version":"1.2","read_only":True,
                    "execution_authorized":False,"status":"ok"}
        report=run_research(screener=Mock(return_value=screen_market(client=FakeScreenerClient())),
                           scanner=Mock(return_value=captured),pipeline=pipeline)
        self.assertEqual(report["status"],"partial_failure")
        self.assertEqual(report["analysis"]["status"],"ok")

    def test_context_rankings_do_not_degrade_for_omitted_history(self):
        for mode in ("volume", "oi", "px_change", "momentum"):
            with self.subTest(mode=mode):
                report = self.run_case(sort_by=mode)
                self.assertEqual(report["status"], "ok")
                self.assertEqual(report["selection_quality"]["issues"], [])
                self.scanner.assert_called_once()

    def test_discarded_screener_errors_degrade_research(self):
        report = self.run_case(client=FakeScreenerClient(error_symbol="ETH"), limit=1)
        self.assertEqual(report["selected_symbols"], ["FET"])
        self.assertEqual(report["status"], "partial_failure")
        self.assertEqual(report["analysis"]["status"], "ok")
        self.assertEqual(report["selection_quality"]["status"], "degraded")
        self.assertEqual(report["screener"]["errors"][0]["symbol"], "ETH")
        self.scanner.assert_called_once()

    def test_all_candle_errors_are_partial_if_scanner_and_analysis_succeed(self):
        selection = screen_market(client=FakeScreenerClient(), clock_ms=lambda: NOW)
        for row in selection["results"]:
            row.update(error="public candle endpoint unavailable", vol_delta_pct_24h=None, volume_prev_24h_usd=None)
        scan = test_scanner.ScannerTests().scan(selection["symbols"])
        report = run_research(screener=Mock(return_value=selection), scanner=Mock(return_value=scan),
                              runner=Mock(side_effect=successful_runner), clock_ms=lambda: NOW + 1000)
        self.assertEqual(report["status"], "partial_failure")
        self.assertEqual(report["analysis"]["status"], "ok")

    def test_missing_metrics_remain_null_and_are_explicitly_unavailable(self):
        client = FakeScreenerClient(candles_by_symbol={"FET": []})
        report = self.run_case(client=client)
        fet = next(row for row in report["screener"]["results"] if row["symbol"] == "FET")
        self.assertIsNone(fet["error"])
        self.assertIsNone(fet["vol_delta_pct_24h"])
        self.assertIsNone(fet["volume_prev_24h_usd"])
        self.assertEqual(report["status"], "partial_failure")

    def test_nonfinite_metrics_become_null_with_paths_without_mutating_input(self):
        selection = screen_market(client=FakeScreenerClient(), clock_ms=lambda: NOW)
        selection["results"][0]["price"] = float("nan")
        selection["results"][0]["volume_today_usd"] = float("inf")
        scanner = Mock(side_effect=lambda symbols, **kwargs: scan_symbols(
            symbols, market_builder=test_scanner.ScannerTests().market, **kwargs))
        report = run_research(screener=Mock(return_value=selection), scanner=scanner,
                              runner=Mock(side_effect=successful_runner), clock_ms=lambda: NOW + 1000)
        self.assertEqual(report["status"], "partial_failure")
        self.assertIsNone(report["screener"]["results"][0]["price"])
        self.assertTrue(math.isnan(selection["results"][0]["price"]))
        self.assertTrue(any(issue.get("path") == "screener.results[0].price" for issue in report["selection_quality"]["issues"]))
        json.dumps(report, allow_nan=False)

    def test_partial_scanner_and_model_errors_are_retained(self):
        def builder(symbol):
            if symbol == "ETH":
                raise HyperliquidAPIError("public capture failed")
            return test_scanner.ScannerTests().market(symbol)
        scanner = Mock(side_effect=lambda symbols, **kwargs: scan_symbols(symbols, market_builder=builder, **kwargs))
        selection = screen_market(client=FakeScreenerClient(), clock_ms=lambda: NOW)
        def runner(cmd, **kwargs):
            if snapshot_input(cmd, kwargs)["symbol"] == "BTC":
                raise FileNotFoundError("simulated missing Codex")
            return successful_runner(cmd, **kwargs)
        report = run_research(screener=Mock(return_value=selection), scanner=scanner, runner=runner,
                              clock_ms=lambda: NOW + 1000)
        self.assertEqual(report["status"], "partial_failure")
        self.assertEqual(report["scanner"]["status"], "partial_failure")
        self.assertEqual(report["analysis"]["status"], "partial_failure")
        errors = {row["symbol"]: row["error"] for row in report["analysis"]["results"]}
        self.assertEqual(errors["ETH"]["stage"], "capture")
        self.assertEqual(errors["BTC"]["stage"], "codex_exec")

    def test_selection_failure_keeps_stage_error_and_stops_downstream(self):
        for result in (HyperliquidAPIError("public endpoint down"), {"status": "ok"}):
            screener = Mock(side_effect=result) if isinstance(result, Exception) else Mock(return_value=result)
            pipeline, scanner = Mock(), Mock()
            report = run_research(screener=screener, pipeline=pipeline, scanner=scanner)
            self.assertEqual(report["status"], "failed")
            self.assertEqual(report["outcome"], "selection_failed")
            self.assertEqual(report["errors"][0]["stage"], "screener")
            pipeline.assert_not_called()
            scanner.assert_not_called()
            format_report(report)

    def test_unsupported_and_duplicate_symbols_preserve_ranking_and_usable_candidates(self):
        selection = screen_market(client=FakeScreenerClient(), clock_ms=lambda: NOW)
        selection["symbols"][1] = "xyz:UNSUPPORTED"
        selection["results"][1]["symbol"] = "xyz:UNSUPPORTED"
        selection["symbols"][2] = "FET"
        selection["results"][2]["symbol"] = "FET"
        before = copy.deepcopy(selection)
        runner = Mock(side_effect=successful_runner)
        scanner = Mock(side_effect=lambda symbols, **kwargs: scan_symbols(
            symbols, market_builder=test_scanner.ScannerTests().market, **kwargs))
        report = run_research(screener=Mock(return_value=selection), scanner=scanner, runner=runner,
                              clock_ms=lambda: NOW + 1000)
        self.assertEqual(report["selected_symbols"], ["FET", "xyz:UNSUPPORTED", "FET"])
        self.assertEqual(report["pipeline_symbols"], ["FET"])
        self.assertEqual(report["screener"], before)
        self.assertEqual(selection, before)
        self.assertEqual(report["status"], "partial_failure")
        self.assertEqual(runner.call_count, 1)

    def test_analysis_exception_keeps_existing_scanner_report(self):
        selection = screen_market(client=FakeScreenerClient(), clock_ms=lambda: NOW)
        scan = test_scanner.ScannerTests().scan(selection["symbols"])
        def pipeline(symbols, **kwargs):
            kwargs["scanner"](symbols)
            raise RuntimeError("simulated pipeline error")
        report = run_research(screener=Mock(return_value=selection), pipeline=pipeline,
                              scanner=Mock(return_value=scan), clock_ms=lambda: NOW + 1000)
        self.assertEqual(report["status"], "failed")
        self.assertIs(report["scanner"], scan)
        self.assertEqual(report["errors"][0]["stage"], "analysis_pipeline")

    def test_stage_timings_are_separate_and_internal_reports_are_preserved(self):
        elapsed = 0.0
        def screener(**kwargs):
            nonlocal elapsed
            result = screen_market(client=FakeScreenerClient(), **kwargs)
            elapsed += 2
            return result
        def pipeline(*args, **kwargs):
            nonlocal elapsed
            result = run_pipeline(*args, **kwargs)
            elapsed += 5
            return result
        scanner = lambda symbols, **kwargs: scan_symbols(symbols, market_builder=test_scanner.ScannerTests().market, **kwargs)
        report = run_research(screener=screener, pipeline=pipeline, scanner=scanner,
                              runner=Mock(side_effect=successful_runner), clock_ms=lambda: NOW + 1000,
                              timer=lambda: elapsed)
        self.assertEqual(report["timing"], {"screener_seconds": 2, "analysis_pipeline_seconds": 5, "total_seconds": 7})
        self.assertEqual(report["screener"]["total_seconds"], 0)
        self.assertEqual(report["analysis"]["total_seconds"], 0)

    def test_invalid_overrides_fail_before_discovery(self):
        for overrides in ({"top_n": 0}, {"limit": 0}, {"max_workers": 0}, {"sort_by": "bad"},
                          {"min_volume": float("nan")}, {"revalidation_deadline_seconds": 0}):
            screener = Mock()
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                run_research(screener=screener, **overrides)
            screener.assert_not_called()

    def test_cli_profiles_overrides_json_human_output_and_exit_codes(self):
        report = self.run_case(limit=1)
        with patch("src.research_pipeline.run_research", return_value=report) as research:
            out = io.StringIO()
            with redirect_stdout(out):
                code = main(["--json", "--profile", "experimental", "--top-n", "4", "--limit", "2",
                             "--sort-by", "volume", "--min-volume", "2000000", "--max-workers", "1"])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(out.getvalue()), report)
            self.assertEqual(research.call_args.kwargs["configuration"].profile, "experimental")
            self.assertEqual(research.call_args.kwargs["limit"], 2)
            self.assertEqual(research.call_args.kwargs["min_volume"], 2_000_000)
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(main([]), 0)
            self.assertIn("Research |", out.getvalue())
            self.assertIn("execution_authorized=false", out.getvalue())
            self.assertIsNone(research.call_args.kwargs["limit"])
            for status in ("partial_failure", "failed"):
                report["status"] = status
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(main(["--json"]), 1)

    def test_cli_invalid_config_and_overrides_do_not_access_network(self):
        for args in (["--profile", "unknown"], ["--limit", "0"], ["--min-volume", "nan"]):
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as exc:
                main(args)
            self.assertEqual(exc.exception.code, 2)
        self.network.assert_not_called()
        self.process.assert_not_called()

    def test_old_yaml_can_drive_new_cli_and_explicit_flags_override_profile(self):
        text = """default_profile: original
profiles:
  original:
    scanner:
      min_day_notional_volume_usd: 1000000
      max_spread_bps: 10
    ai:
      providers: [codex]
      consensus: false
"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "old.yaml"
            path.write_text(text)
            empty = screen_market(client=FakeScreenerClient(), min_volume=1_000_000_000)
            with patch("src.research_pipeline.screen_market", return_value=empty) as screener:
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(main(["--config", str(path), "--limit", "2", "--sort-by", "volume", "--json"]), 0)
                self.assertEqual(screener.call_args.kwargs["min_volume"], 1_000_000)
                self.assertEqual(screener.call_args.kwargs["limit"], 2)
                self.assertEqual(screener.call_args.kwargs["sort_by"], "volume")


if __name__ == "__main__":
    unittest.main()
