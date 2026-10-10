"""Original default contracts and explicit alternatives; all I/O is simulated."""
import copy
import io
import json
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
from threading import Barrier, local
from types import SimpleNamespace
from unittest.mock import Mock, patch

from src.analysis_pipeline import format_report, main as pipeline_main, run_pipeline
from src.decision_snapshot import build_decision_snapshot, validate_decision_snapshot
from src.integrity import attach_checksum, verify_checksum
from src.pre_analysis import POLICY, build_pre_analysis, validated_policy
from src.research_config import DEFAULT_CONFIG_PATH, ResearchConfig, load_config
from src.scanner import main as scanner_main, scan_symbols
import test_analysis_pipeline
from test_analysis_pipeline import RESPONSE, model_output
from test_fast_revalidation import CURRENT, PairClient, fast_revalidate, proposal
from test_market_state import NOW
import test_scanner


class PipelineProfileTests(unittest.TestCase):
    def market(self, volume=10_000_000, spread_bps=1):
        market = test_scanner.ScannerTests().market("HYPE")
        market["market_context"]["day_notional_volume"] = volume
        market["orderbook"]["best_bid"] = 100 - spread_bps / 200
        market["orderbook"]["best_ask"] = 100 + spread_bps / 200
        attach_checksum(market)
        return market

    def scan(self, settings=None, market=None):
        return scan_symbols(["HYPE"], market_builder=lambda symbol: market or self.market(),
                            clock_ms=lambda: NOW + 1000, configuration=settings)

    def pipeline(self, settings=None, runner=None, scan=None, timer=None):
        return run_pipeline(["HYPE"], scanner=Mock(return_value=scan or self.scan(settings)),
                            runner=runner or Mock(side_effect=lambda cmd, **kwargs: SimpleNamespace(
                                returncode=0, stdout=model_output(cmd))),
                            clock_ms=lambda: NOW + 1000, configuration=settings, timer=timer)

    def test_default_invokes_only_codex_and_preserves_response_without_consensus(self):
        runner = Mock(side_effect=lambda cmd, **kwargs: SimpleNamespace(returncode=0, stdout=model_output(cmd)))
        with patch("src.analysis_pipeline.agy_command", side_effect=AssertionError("Agy must not be prepared")), \
             patch("src.analysis_pipeline.consolidate_responses", side_effect=AssertionError("no consensus")):
            report = self.pipeline(runner=runner)
        self.assertEqual(runner.call_count, 1)
        self.assertEqual(runner.call_args.args[0][0], "codex")
        self.assertEqual(report["results"][0]["model_review"]["response"], RESPONSE)

    def test_reversed_consensus_provider_order_does_not_change_voting_roles(self):
        settings = ResearchConfig(profile="reversed", providers=("agy", "codex"), consensus=True)
        def runner(cmd, **kwargs):
            response = dict(RESPONSE, decision="NO_TRADE" if cmd[0] == "agy" else "WATCH")
            return SimpleNamespace(returncode=0, stdout=model_output(cmd, response))
        report = self.pipeline(settings, runner)
        self.assertEqual(report["results"][0]["model_review"]["response"]["decision"], "NO_TRADE")
        self.assertEqual(report["status"], "ok")

    def test_default_contract_matches_original_v12_exact_field_sets(self):
        report = self.pipeline()
        self.assertEqual(report["schema_version"], "1.2")
        self.assertEqual(set(report), {
            "kind", "schema_version", "read_only", "execution_authorized", "model", "reasoning_effort",
            "max_concurrent_reviews", "fast_revalidation_deadline_seconds", "fast_revalidations",
            "fast_revalidation_seconds_total", "status", "symbols", "counts", "codex_invocations",
            "scanner_seconds", "capture_seconds_total", "pre_analysis_seconds_total",
            "decision_snapshot_seconds_total", "codex_exec_seconds_total", "codex_review_wall_seconds",
            "total_seconds", "results"})
        row = report["results"][0]
        self.assertEqual(set(row["model_review"]), {"status", "reason", "invoked", "response",
            "expired_at_completion", "requires_revalidation", "execution_authorized", "sent_at_utc", "completed_at_utc"})
        self.assertEqual(set(row["timing"]), {"capture_seconds", "pre_analysis_seconds",
            "decision_snapshot_seconds", "total_seconds", "codex_exec_seconds", "fast_revalidation_seconds"})
        # Existing shell consumers still find classification and the model response.
        encoded = json.loads(json.dumps(report, allow_nan=False))
        self.assertEqual([r["symbol"] for r in encoded["results"] if r["classification"] == "ANALYZE"], ["HYPE"])

    def test_profiles_change_only_screening_limits_not_market_capture(self):
        original, strict = load_config(), load_config(profile="experimental")
        for volume, spread in ((2_000_000, 1), (10_000_000, 7)):
            market = self.market(volume, spread)
            before = copy.deepcopy(market)
            self.assertEqual(self.scan(original, market)["counts"]["ANALYZE"], 1)
            self.assertEqual(self.scan(strict, market)["counts"]["SKIP"], 1)
            self.assertEqual(market, before)
        for settings in (original, strict):
            row = self.scan(settings)["results"][0]
            self.assertEqual(row["classification"], "ANALYZE")
            self.assertFalse(validate_decision_snapshot(row["decision_snapshot"]))
            self.assertEqual(row["decision_snapshot"]["pre_analysis"]["policy"], row["pre_analysis"]["policy"])
            self.assertTrue(verify_checksum(row["pre_analysis"]))

    def test_concurrent_profiles_do_not_mutate_global_policy(self):
        before = copy.deepcopy(POLICY)
        market = self.market(2_000_000, 7)
        with ThreadPoolExecutor(max_workers=2) as pool:
            reports = list(pool.map(lambda settings: self.scan(settings, market),
                                    [load_config(), load_config(profile="experimental")]))
        self.assertEqual([r["results"][0]["classification"] for r in reports], ["ANALYZE", "SKIP"])
        self.assertEqual(POLICY, before)

    def test_fixed_analysis_rules_and_wrong_policy_preanalysis_are_rejected(self):
        policy = {**POLICY, **load_config(profile="experimental").scanner}
        changed = {**policy, "min_relative_volume": 0}
        with self.assertRaises(ValueError):
            validated_policy(changed)
        pre = build_pre_analysis(self.market(), as_of_ms=NOW + 1000, policy=policy)
        with self.assertRaisesRegex(ValueError, "pre_analysis does not match"):
            build_decision_snapshot(self.market(), as_of_ms=NOW + 1000, pre_analysis=pre)
        runner = Mock(side_effect=AssertionError("no dispatch on policy mismatch"))
        report = self.pipeline(runner=runner, scan=self.scan(load_config(profile="experimental")))
        runner.assert_not_called()
        self.assertEqual(report["results"][0]["model_review"]["reason"], "scanner_policy_mismatch")

    def test_experimental_consensus_and_extended_contract(self):
        settings = load_config(profile="experimental")
        runner = Mock(side_effect=lambda cmd, **kwargs: SimpleNamespace(returncode=0, stdout=model_output(cmd)))
        report = self.pipeline(settings, runner)
        self.assertEqual({call.args[0][0] for call in runner.call_args_list}, {"codex", "agy"})
        self.assertEqual(report["schema_version"], "1.3")
        self.assertEqual(report["provider_invocations"], {"codex": 1, "agy": 1})
        self.assertTrue(report["consensus"])
        self.assertEqual(report["profile"], "experimental")
        self.assertIn("[GPT-6.1-Sol]", report["results"][0]["model_review"]["response"]["thesis"])

    def test_missing_selected_provider_is_isolated_and_never_logs_credentials(self):
        for settings, missing in ((ResearchConfig(), "codex"), (load_config(profile="experimental"), "agy")):
            def runner(cmd, **kwargs):
                if cmd[0] == missing:
                    raise FileNotFoundError("private credential or process detail")
                return SimpleNamespace(returncode=0, stdout=model_output(cmd))
            with self.subTest(missing=missing):
                report = self.pipeline(settings, runner)
                self.assertEqual(report["status"], "failed")
                review = report["results"][0]["model_review"]
                self.assertEqual(review["status"], "error")
                self.assertFalse(review["execution_authorized"])
                self.assertNotIn("private", json.dumps(report))
                if settings.consensus:
                    self.assertEqual(review["response"]["decision"], "WATCH")
                    self.assertEqual(review["providers"][missing]["error"]["type"], "FileNotFoundError")
                else:
                    self.assertIsNone(review["response"])

    def test_single_agy_does_not_prepare_codex_or_consolidate(self):
        settings = ResearchConfig(profile="agy_only", providers=("agy",))
        runner = Mock(side_effect=lambda cmd, **kwargs: SimpleNamespace(returncode=0, stdout=model_output(cmd)))
        with patch("src.analysis_pipeline.codex_command", side_effect=AssertionError("no Codex")):
            report = self.pipeline(settings, runner)
        self.assertEqual(runner.call_count, 1)
        self.assertEqual(report["codex_invocations"], 0)
        self.assertEqual(report["codex_exec_seconds_total"], 0)
        self.assertEqual(report["codex_review_wall_seconds"], 0)
        self.assertEqual(report["consolidation_seconds_total"], 0)
        self.assertEqual(report["results"][0]["model_review"]["response"], RESPONSE)
        self.assertIn("codex_invoked=false", format_report(report))

    def test_provider_times_and_consolidation_are_separate_with_slow_agy(self):
        thread_clock = local()
        barrier = Barrier(2)
        def timer():
            return getattr(thread_clock, "elapsed", 0.0)
        def runner(cmd, **kwargs):
            barrier.wait(timeout=2)
            thread_clock.elapsed = 2.0 if cmd[0] == "codex" else 9.0
            return SimpleNamespace(returncode=0, stdout=model_output(cmd))
        from src.analysis_pipeline import consolidate_responses
        def consolidate(*args):
            thread_clock.elapsed = 0.25
            return consolidate_responses(*args)
        with patch("src.analysis_pipeline.consolidate_responses", side_effect=consolidate):
            report = self.pipeline(load_config(profile="experimental"), runner, timer=timer)
        timing = report["results"][0]["timing"]
        self.assertEqual(timing["codex_exec_seconds"], 2)
        self.assertEqual(timing["agy_exec_seconds"], 9)
        self.assertEqual(timing["consolidation_seconds"], 0.25)
        self.assertEqual(report["codex_exec_seconds_total"], 2)
        self.assertEqual(report["agy_exec_seconds_total"], 9)
        self.assertEqual(report["consolidation_seconds_total"], 0.25)
        self.assertEqual(report["codex_review_wall_seconds"], 2)
        self.assertGreaterEqual(report["model_review_wall_seconds"], 9)

    def test_revalidation_uses_original_selected_policy(self):
        for settings, expected in ((load_config(), "VALID"), (load_config(profile="experimental"), "INVALID")):
            row = proposal(configuration=settings)
            client = PairClient()
            client.context["dayNtlVlm"] = "2000000"
            result = fast_revalidate(row, client=client, clock_ms=lambda: CURRENT)
            self.assertEqual(result["status"], expected)

    def test_default_and_profile_cli_flags_preserve_json_and_pass_settings(self):
        report = self.scan()
        pipeline_report = self.pipeline()
        for main, target, result in ((scanner_main, "src.scanner.scan_symbols", report),
                                     (pipeline_main, "src.analysis_pipeline.run_pipeline", pipeline_report)):
            for flags, expected in (([], "default"), (["--profile", "experimental"], "experimental"),
                                    (["--config", str(DEFAULT_CONFIG_PATH), "--profile", "default"], "default")):
                with self.subTest(target=target, flags=flags), patch(target, return_value=result) as worker:
                    out = io.StringIO()
                    with redirect_stdout(out):
                        self.assertEqual(main(["hype", "--json", *flags]), 0)
                    self.assertEqual(json.loads(out.getvalue()), result)
                    self.assertEqual(worker.call_args.args[0], ["HYPE"])
                    self.assertEqual(worker.call_args.kwargs["configuration"].profile, expected)

    def test_invalid_configuration_fails_before_network_or_model_dispatch(self):
        for main, target in ((scanner_main, "src.scanner.scan_symbols"),
                             (pipeline_main, "src.analysis_pipeline.run_pipeline")):
            for flags in (["--profile", "missing"], ["--config", "/missing/research.yaml"]):
                with patch(target) as worker, redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as exc:
                    main(["HYPE", *flags])
                self.assertEqual(exc.exception.code, 2)
                worker.assert_not_called()

    def test_skip_and_watch_never_invoke_selected_providers(self):
        for settings in (load_config(), load_config(profile="experimental")):
            scan = test_analysis_pipeline.AnalysisPipelineTests().scan(("SKIP", "WATCH"))
            runner = Mock(side_effect=AssertionError("no models for SKIP/WATCH"))
            report = run_pipeline(scan["symbols"], configuration=settings, scanner=Mock(return_value=scan),
                                  runner=runner, clock_ms=lambda: NOW + 1000)
            runner.assert_not_called()
            self.assertEqual(report["codex_invocations"], 0)
            self.assertEqual(report["codex_exec_seconds_total"], 0)


if __name__ == "__main__":
    unittest.main()
