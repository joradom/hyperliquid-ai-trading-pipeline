import copy
import io
import json
import subprocess
import unittest
from contextlib import redirect_stdout, redirect_stderr
from types import SimpleNamespace
from threading import Barrier, Event, Lock, get_ident, local
from unittest.mock import Mock, patch

from src.analysis_pipeline import (CODEX_CWD, CODEX_TIMEOUT_SECONDS, MAX_CONCURRENT_REVIEWS, MODEL, RESPONSE_SCHEMA_PATH,
                                   codex_command, format_report, main, parse_codex_output,
                                   parse_agy_output, consolidate_responses, review_gate, run_pipeline, validate_response)
from src.scanner import scan_symbols
from src.fast_revalidation import revalidation_failure
from src.research_config import ResearchConfig
from test_market_state import NOW
import test_scanner
from test_analysis_response import directional_response


RESPONSE = {"response_schema_version": "2.0", "execution_authorized": False, "compatibility_contract": None,
            "decision": "WATCH", "confidence": "medium", "thesis": "Esperar confirmación del setup.",
            "entry_conditions": ["Confirmación del nivel"], "invalidation": ["Pérdida del soporte"],
            "limiting_data": ["Sin flujo de órdenes histórico"]}

# Preserve the pending multimodel tests as explicit consensus coverage.
CONSENSUS_CONFIG = ResearchConfig(profile="consensus", providers=("codex", "agy"), consensus=True)


def output(response=None):
    events = [{"type": "thread.started", "thread_id": "mock"}, {"type": "turn.started"},
              {"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(response or RESPONSE)}},
              {"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 10}}]
    return "\n".join(json.dumps(event) for event in events)


def model_output(command, response=None):
    response = response if response is not None else RESPONSE
    return output(response) if command[0] == "codex" else json.dumps({"response": json.dumps(response)})


def snapshot_input(command, kwargs):
    return json.loads(kwargs["input"] if command[0] == "codex" else command[2].split("\n\nJSON Data:\n", 1)[1])


def successful_runner(command, **kwargs):
    return SimpleNamespace(returncode=0, stdout=model_output(command), stderr="")


def expected_consensus(response=None):
    response = response if response is not None else RESPONSE
    return consolidate_responses(response, response)


class AnalysisPipelineTests(unittest.TestCase):
    def market(self, symbol, label="ANALYZE"):
        return test_scanner.ScannerTests().market(symbol, label)

    def scan(self, labels=("ANALYZE",), symbols=None):
        symbols = symbols or ["HYPE", "ETH", "SOL"][:len(labels)]
        markets = {s: self.market(s, label) for s, label in zip(symbols, labels)}
        return scan_symbols(symbols, market_builder=markets.__getitem__, clock_ms=lambda: NOW + 1000)

    def run_report(self, scan, runner=None, clock=None):
        runner = runner or Mock(side_effect=successful_runner)
        report = run_pipeline(scan["symbols"], configuration=CONSENSUS_CONFIG, scanner=Mock(return_value=scan), runner=runner,
                              clock_ms=clock or (lambda: NOW + 1000),
                              revalidator=lambda row, **kwargs: revalidation_failure(row, "TEST_NO_CAPTURE", now_ms=NOW+120_000))
        return report, runner

    def test_skip_watch_never_invoke_even_with_forged_operational_snapshot(self):
        scan = self.scan(("SKIP", "WATCH"))
        operational = self.scan()["results"][0]["decision_snapshot"]
        for row in scan["results"]:
            row["decision_snapshot"] = operational
        with patch("src.analysis_pipeline.codex_command") as command:
            with patch("src.analysis_pipeline.ThreadPoolExecutor") as pool:
                report, runner = self.run_report(scan)
        pool.assert_not_called()
        command.assert_not_called()
        runner.assert_not_called()
        self.assertEqual(report["codex_invocations"], 0)
        self.assertEqual(report["codex_review_wall_seconds"], 0)
        self.assertEqual([r["classification"] for r in report["results"]], ["SKIP", "WATCH"])
        self.assertTrue(all(r["model_review"]["response"] is None for r in report["results"]))

    def test_real_scanner_in_memory_used_once_without_mutation(self):
        scan = self.scan(("WATCH", "ANALYZE", "SKIP"))
        before = copy.deepcopy(scan)
        scanner = Mock(return_value=scan)
        runner = Mock(side_effect=successful_runner)
        with patch("src.analysis_pipeline.serialize_decision_snapshot", wraps=__import__(
                "src.decision_snapshot", fromlist=["serialize_decision_snapshot"]).serialize_decision_snapshot) as serialize:
            report = run_pipeline(["hype", "ETH", "SOL"], configuration=CONSENSUS_CONFIG, scanner=scanner, runner=runner, clock_ms=lambda: NOW + 1000)
        scanner.assert_called_once()
        self.assertEqual(scanner.call_args.args[0], ["HYPE", "ETH", "SOL"])
        self.assertIs(serialize.call_args.args[0], scan["results"][1]["decision_snapshot"])
        self.assertEqual(scan, before)
        self.assertEqual(runner.call_count, 2)
        codex_call = next(call for call in runner.call_args_list if call.args[0][0] == "codex")
        self.assertEqual(snapshot_input(codex_call.args[0], codex_call.kwargs), scan["results"][1]["decision_snapshot"])
        self.assertEqual(report["codex_invocations"], 1)
        self.assertEqual([r["symbol"] for r in report["results"]], ["HYPE", "ETH", "SOL"])
        self.assertEqual(report["results"][1]["model_review"]["response"], expected_consensus())
        self.assertFalse(report["results"][1]["model_review"]["requires_revalidation"])
        self.assertFalse(report["results"][1]["model_review"]["execution_authorized"])

    def test_degraded_warning_does_not_block_usable_snapshot(self):
        scan = self.scan()
        snapshot = scan["results"][0]["decision_snapshot"]
        self.assertEqual(snapshot["quality"]["market_status"], "degraded")
        self.assertTrue(snapshot["quality"]["warnings"])
        report, runner = self.run_report(scan)
        self.assertEqual(runner.call_count, 2)
        self.assertEqual(report["status"], "ok")

    def test_all_ineligible_conditions_do_not_spawn(self):
        mutations = {
            "missing": lambda row: row.update(decision_snapshot=None),
            "eligibility": lambda row: row["decision_snapshot"]["decision_contract"].update(eligible_for_model_review=False),
            "blocker": lambda row: row["decision_snapshot"]["quality"]["blockers"].append(
                {"code": "blocked", "scope": "test", "detail": "fixture"}),
            "checksum": lambda row: row["decision_snapshot"]["price"].update(mark=999),
            "wrong_symbol": lambda row: row.update(symbol="ETH"),
            "pre_checksum": lambda row: row["pre_analysis"].update(symbol="ETH"),
            "scanner_error": lambda row: row.update(error={"stage": "decision_snapshot", "message": "failed"}, status="error"),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                scan = self.scan()
                mutate(scan["results"][0])
                report, runner = self.run_report(scan)
                runner.assert_not_called()
                self.assertEqual(report["codex_invocations"], 0)

    def test_expired_snapshot_never_spawns(self):
        scan = self.scan()
        report, runner = self.run_report(scan, clock=lambda: NOW + 120_000)
        runner.assert_not_called()
        self.assertEqual(report["results"][0]["model_review"]["reason"], "snapshot_expired")

    def test_expiry_between_initial_check_and_spawn(self):
        scan = self.scan()
        clock = Mock(side_effect=[NOW + 1000, NOW + 120_000])
        report, runner = self.run_report(scan, clock=clock)
        runner.assert_not_called()
        self.assertEqual(report["results"][0]["model_review"]["reason"], "snapshot_expired")

    def test_exact_expiry_boundary_uses_existing_inclusive_validity(self):
        scan = self.scan()
        row = scan["results"][0]
        from src.analysis_pipeline import _milliseconds
        expiry = _milliseconds(row["decision_snapshot"]["valid_until_utc"])
        self.assertIsNone(review_gate(row, expiry))
        self.assertEqual(review_gate(row, expiry + 1), "snapshot_expired")

    def test_two_analyze_start_concurrently_and_expired_responses_are_retained(self):
        scan = self.scan(("ANALYZE", "ANALYZE"))
        now = NOW + 1000
        started = Barrier(4)
        def runner(*args, **kwargs):
            nonlocal now
            # Both processes must have passed the dispatch gate before expiry.
            started.wait(timeout=5)
            now = NOW + 120_000
            return SimpleNamespace(returncode=0, stdout=model_output(args[0]), stderr="")
        mocked = Mock(side_effect=runner)
        report, _ = self.run_report(scan, runner=mocked, clock=lambda: now)
        self.assertEqual(mocked.call_count, 4)
        self.assertEqual(report["codex_invocations"], 2)
        self.assertEqual(report["status"], "ok")
        for row in report["results"]:
            self.assertEqual(row["model_review"]["response"], expected_consensus())
            self.assertEqual(row["model_review"]["status"], "completed")
            self.assertTrue(row["model_review"]["expired_at_completion"])
            self.assertTrue(row["model_review"]["requires_revalidation"])
            self.assertFalse(row["model_review"]["execution_authorized"])
        self.assertIn("requires_revalidation=true", format_report(report))
        self.assertIn("historical/as-of", format_report(report))

    def test_maximum_three_simultaneous_reviews(self):
        self.assertEqual(MAX_CONCURRENT_REVIEWS, 3)
        symbols = ["HYPE", "ETH", "SOL", "BTC", "DOGE", "AVAX"]
        scan = self.scan(("ANALYZE",) * 6, symbols)
        started = Barrier(6)
        lock = Lock()
        active = peak = 0
        def runner(*args, **kwargs):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            try:
                started.wait(timeout=5)
                return SimpleNamespace(returncode=0, stdout=model_output(args[0]), stderr="")
            finally:
                with lock: active -= 1
        report, _ = self.run_report(scan, runner=Mock(side_effect=runner))
        self.assertEqual(peak, 6)
        self.assertEqual(active, 0)
        self.assertEqual(report["codex_invocations"], 6)
        self.assertEqual(report["status"], "ok")
        self.assertEqual([row["symbol"] for row in report["results"]], symbols)

    def test_queued_candidate_expired_before_worker_start_never_invokes(self):
        scan = self.scan(("ANALYZE",) * 4, ["HYPE", "ETH", "SOL", "BTC"])
        now = NOW + 1000
        started = Barrier(6)
        def runner(*args, **kwargs):
            nonlocal now
            started.wait(timeout=5)
            now = NOW + 120_000
            return SimpleNamespace(returncode=0, stdout=model_output(args[0]), stderr="")
        report, mocked = self.run_report(scan, runner=Mock(side_effect=runner), clock=lambda: now)
        self.assertEqual(mocked.call_count, 6)
        self.assertEqual(report["codex_invocations"], 3)
        self.assertEqual(report["results"][3]["model_review"]["reason"], "snapshot_expired")
        self.assertFalse(report["results"][3]["model_review"]["invoked"])

    def test_completion_order_and_separate_payloads_do_not_mix_assets(self):
        scan = self.scan(("ANALYZE", "ANALYZE"))
        started = Barrier(4)
        eth_done = Event()
        lock = Lock()
        completions, inputs, commands = [], {}, []
        def runner(command, **kwargs):
            snapshot = snapshot_input(command, kwargs)
            symbol = snapshot["symbol"]
            with lock:
                inputs[symbol] = snapshot
                commands.append(command)
            started.wait(timeout=5)
            if symbol == "HYPE": self.assertTrue(eth_done.wait(timeout=5))
            with lock: completions.append(symbol)
            if symbol == "ETH": eth_done.set()
            response = directional_response(snapshot, "LONG" if symbol == "HYPE" else "SHORT")
            response["thesis"] = "Tesis " + symbol
            return SimpleNamespace(returncode=0, stdout=model_output(command, response), stderr="")
        report, _ = self.run_report(scan, runner=Mock(side_effect=runner))
        self.assertEqual(completions[0], "ETH")
        self.assertCountEqual(completions, ["ETH", "ETH", "HYPE", "HYPE"])
        self.assertIsNot(commands[0], commands[1])
        self.assertEqual(report["codex_invocations"], 2)
        for original, reviewed in zip(scan["results"], report["results"]):
            symbol = original["symbol"]
            self.assertEqual(inputs[symbol], original["decision_snapshot"])
            self.assertEqual(reviewed["symbol"], symbol)
            self.assertEqual(reviewed["model_review"]["response"]["thesis"], expected_consensus(directional_response(inputs[symbol], "LONG" if symbol == "HYPE" else "SHORT") | {"thesis": "Tesis " + symbol})["thesis"])

    def test_simulated_concurrent_wall_time_is_maximum_not_sum(self):
        scan = self.scan(("ANALYZE", "ANALYZE"))
        parent = get_ident()
        thread_clock = local()
        lock = Lock()
        started = Barrier(4)
        wall = 0.0
        # Independent worker timelines start together. The parent's virtual wall
        # advances to the latest completion, not the sum of process durations.
        def timer():
            if get_ident() == parent:
                with lock: return wall
            return getattr(thread_clock, "elapsed", 0.0)
        def runner(*args, **kwargs):
            nonlocal wall
            symbol = snapshot_input(args[0], kwargs)["symbol"]
            started.wait(timeout=5)
            duration = 2.0 if symbol == "HYPE" else 3.0
            thread_clock.elapsed = duration
            with lock: wall = max(wall, duration)
            return SimpleNamespace(returncode=0, stdout=model_output(args[0]), stderr="")
        report = run_pipeline(scan["symbols"], configuration=CONSENSUS_CONFIG, scanner=Mock(return_value=scan), runner=runner,
                              timer=timer, clock_ms=lambda: NOW + 1000)
        durations = [row["timing"]["codex_exec_seconds"] for row in report["results"]]
        provider_durations = [row["model_review"]["providers"]["codex"]["seconds"] for row in report["results"]]
        self.assertEqual(provider_durations, [2.0, 3.0])
        self.assertEqual(report["codex_review_wall_seconds"], max(provider_durations))
        self.assertEqual(report["codex_exec_seconds_total"], sum(durations))
        self.assertLess(report["codex_review_wall_seconds"], sum(provider_durations))
        self.assertEqual(report["total_seconds"], 3.0)

    def test_timeout_nonzero_and_malformed_output_isolated_per_asset(self):
        failures = [subprocess.TimeoutExpired("codex", 180), FileNotFoundError("codex not installed"),
                    SimpleNamespace(returncode=1, stdout="", stderr="sensitive authentication detail"),
                    SimpleNamespace(returncode=0, stdout="not json", stderr="")]
        for failure in failures:
            with self.subTest(failure=type(failure).__name__):
                started = Barrier(4)
                def invoke(*args, **kwargs):
                    started.wait(timeout=5)
                    if snapshot_input(args[0], kwargs)["symbol"] == "HYPE":
                        if isinstance(failure, BaseException): raise failure
                        return failure
                    return SimpleNamespace(returncode=0, stdout=model_output(args[0]), stderr="")
                runner = Mock(side_effect=invoke)
                report, _ = self.run_report(self.scan(("ANALYZE", "ANALYZE")), runner=runner)
                self.assertEqual(runner.call_count, 4)
                self.assertEqual(report["status"], "partial_failure")
                self.assertEqual(report["results"][0]["model_review"]["status"], "error")
                self.assertEqual(report["results"][1]["model_review"]["status"], "completed")
                self.assertNotIn("sensitive authentication detail", json.dumps(report))

    def test_invalid_v2_is_isolated_and_directional_summary_is_short(self):
        scan = self.scan(("ANALYZE", "ANALYZE"))
        started = Barrier(4)
        def runner(*args, **kwargs):
            snapshot = snapshot_input(args[0], kwargs)
            response = directional_response(snapshot)
            if snapshot["symbol"] == "HYPE":
                response["compatibility_contract"]["rules"][0]["threshold"] += 1
            started.wait(timeout=5)
            return SimpleNamespace(returncode=0, stdout=model_output(args[0], response), stderr="")
        report, mocked = self.run_report(scan, runner=Mock(side_effect=runner), clock=lambda: NOW + 1000)
        self.assertEqual(mocked.call_count, 4)
        self.assertEqual(report["status"], "partial_failure")
        self.assertEqual(report["results"][0]["model_review"]["status"], "error")
        self.assertEqual(report["results"][0]["model_review"]["response"]["decision"], "WATCH")
        self.assertEqual(report["results"][1]["model_review"]["status"], "completed")
        human = format_report(report)
        self.assertIn("compatibility: complete=yes | rules=1 | unresolved=0", human)
        self.assertNotIn('"reference_source"', human)

    def test_directional_expired_response_retains_incomplete_contract(self):
        from test_analysis_response import unresolved
        scan = self.scan()
        now = NOW + 1000
        def runner(*args, **kwargs):
            nonlocal now
            response = directional_response(snapshot_input(args[0], kwargs), "SHORT")
            response["compatibility_contract"].update(complete=False, unresolved_conditions=[unresolved()])
            now = NOW + 120_000
            return SimpleNamespace(returncode=0, stdout=model_output(args[0], response), stderr="")
        report, _ = self.run_report(scan, runner=runner, clock=lambda: now)
        review = report["results"][0]["model_review"]
        self.assertEqual(review["status"], "completed")
        self.assertTrue(review["requires_revalidation"])
        self.assertFalse(review["execution_authorized"])
        self.assertIn("compatibility: complete=no | rules=1 | unresolved=1", format_report(report))

    def test_subprocess_flags_stdin_no_shell_no_snapshot_file(self):
        report, runner = self.run_report(self.scan())
        codex_call = next(call for call in runner.call_args_list if call.args[0][0] == "codex")
        command = codex_call.args[0]
        self.assertEqual(command[:2], ["codex", "exec"])
        self.assertEqual(command[command.index("--model") + 1], "gpt-6.1-sol")
        self.assertEqual(command[command.index("--sandbox") + 1], "read-only")
        for flag in ("--ephemeral", "--skip-git-repo-check", "--ignore-user-config", "--ignore-rules", "--strict-config", "--json"):
            self.assertIn(flag, command)
        for config in ('model_reasoning_effort="low"', 'approval_policy="never"', 'web_search="disabled"',
                       "features.shell_tool=false", "features.unified_exec=false", "features.code_mode=false",
                       "features.plugins=false", "project_doc_max_bytes=0", "mcp_servers={}"):
            self.assertIn(config, command)
        self.assertNotIn("--output-last-message", command)
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", command)
        kwargs = codex_call.kwargs
        self.assertFalse(kwargs["shell"])
        self.assertEqual(kwargs["cwd"], CODEX_CWD)
        self.assertEqual(kwargs["timeout"], CODEX_TIMEOUT_SECONDS)
        self.assertEqual(json.loads(kwargs["input"])["kind"], "decision_snapshot")
        self.assertEqual(report["model"], MODEL)
        self.assertEqual(report["reasoning_effort"], "low")
        self.assertEqual(report["schema_version"], "1.3")
        agy_call = next(call for call in runner.call_args_list if call.args[0][0] == "agy")
        self.assertEqual(snapshot_input(agy_call.args[0], agy_call.kwargs), json.loads(kwargs["input"]))
        self.assertIn("--sandbox", agy_call.args[0])
        self.assertIn("--disable-slash-commands", agy_call.args[0])
        self.assertNotIn("--dangerously-skip-permissions", agy_call.args[0])
        self.assertFalse(agy_call.kwargs["shell"])
        self.assertEqual(agy_call.kwargs["cwd"], CODEX_CWD)
        self.assertEqual(report["models"]["agy"], "gemini-3.8-flash-low")

    def test_all_consensus_pairs_preserve_existing_priority_and_codex_contract(self):
        snapshot = self.scan()["results"][0]["decision_snapshot"]
        decisions = ("NO_TRADE", "WATCH", "LONG", "SHORT")
        matrix = (("NO_TRADE", "NO_TRADE", "NO_TRADE", "NO_TRADE"),
                  ("NO_TRADE", "WATCH", "WATCH", "WATCH"),
                  ("NO_TRADE", "WATCH", "LONG", "WATCH"),
                  ("NO_TRADE", "WATCH", "WATCH", "SHORT"))
        for codex in ("NO_TRADE", "WATCH", "LONG", "SHORT"):
            for agy in ("NO_TRADE", "WATCH", "LONG", "SHORT"):
                expected = matrix[decisions.index(codex)][decisions.index(agy)]
                responses = {name: directional_response(snapshot, decision) if decision in ("LONG", "SHORT")
                             else dict(RESPONSE, decision=decision)
                             for name, decision in (("codex", codex), ("agy", agy))}
                def runner(command, **kwargs):
                    return SimpleNamespace(returncode=0, stdout=model_output(command, responses[command[0]]))
                with self.subTest(codex=codex, agy=agy):
                    report, _ = self.run_report(self.scan(), runner=runner)
                    review = report["results"][0]["model_review"]
                    self.assertEqual(review["status"], "completed")
                    self.assertEqual(review["response"]["decision"], expected)
                    self.assertEqual(review["response"]["confidence"], "high" if codex == agy else "low")
                    self.assertEqual(review["response"]["compatibility_contract"],
                                     responses["codex"]["compatibility_contract"] if expected in ("LONG", "SHORT") else None)
                    self.assertEqual(set(review["providers"]), {"codex", "agy"})
                    self.assertFalse(review["response"]["execution_authorized"])

    def test_each_provider_validates_snapshot_provenance_even_when_other_vetoes(self):
        scan = self.scan()
        snapshot = scan["results"][0]["decision_snapshot"]
        for failed in ("codex", "agy"):
            for defect in ("checksum", "threshold", "execution", "enum", "extra"):
                bad = directional_response(snapshot)
                if defect == "checksum": bad["compatibility_contract"]["source_snapshot_checksum"] = "0" * 64
                elif defect == "threshold": bad["compatibility_contract"]["rules"][0]["threshold"] += 1
                elif defect == "execution": bad["execution_authorized"] = True
                elif defect == "enum": bad["decision"] = "BUY"
                else: bad["extra"] = True
                def runner(command, **kwargs):
                    response = bad if command[0] == failed else dict(RESPONSE, decision="NO_TRADE")
                    return SimpleNamespace(returncode=0, stdout=model_output(command, response))
                with self.subTest(provider=failed, defect=defect):
                    report, _ = self.run_report(scan, runner=runner)
                    review = report["results"][0]["model_review"]
                    self.assertEqual(report["status"], "failed")
                    self.assertEqual(review["providers"][failed]["status"], "error")
                    self.assertIsNone(review["providers"][failed]["response"])
                    self.assertEqual(review["response"]["decision"], "NO_TRADE")
                    self.assertFalse(review["execution_authorized"])

    def test_single_provider_failure_is_safe_watch_and_does_not_revalidate(self):
        for failed in ("codex", "agy"):
            for failure in (subprocess.TimeoutExpired("private command payload", 180),
                            RuntimeError("private exception payload")):
                scan = self.scan()
                snapshot = scan["results"][0]["decision_snapshot"]
                def runner(command, **kwargs):
                    if command[0] == failed: raise failure
                    return SimpleNamespace(returncode=0, stdout=model_output(command, directional_response(snapshot)))
                with self.subTest(provider=failed, failure=type(failure).__name__):
                    report, _ = self.run_report(scan, runner=runner)
                    row = report["results"][0]
                    self.assertEqual(row["model_review"]["response"]["decision"], "WATCH")
                    self.assertIsNone(row["fast_revalidation"])
                    self.assertEqual(row["model_review"]["providers"][failed]["status"], "error")
                    self.assertNotIn("private", json.dumps(report))

    def test_agy_envelopes_are_strict_and_support_pretty_json_and_jsonl(self):
        for value in (json.dumps({"response": json.dumps(RESPONSE)}, indent=2),
                      json.dumps({"response": RESPONSE}),
                      json.dumps({"type": "metadata"}) + "\n" + model_output(["agy"])):
            self.assertEqual(parse_agy_output(value), RESPONSE)
        for value in ("[]", "{}", '{"response":"{}","response":"{}"}',
                      json.dumps({"response": "```json\n" + json.dumps(RESPONSE) + "\n```"}),
                      json.dumps({"response": json.dumps(RESPONSE) + " trailing"}),
                      json.dumps({"response": RESPONSE, "is_error": True}),
                      model_output(["agy"]) + "\n" + model_output(["agy"]),
                      '{"response":NaN}', json.dumps({"response": dict(RESPONSE, confidence=0.5)})):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_agy_output(value)

    def test_response_schema_and_runtime_validator_agree(self):
        schema = json.loads(RESPONSE_SCHEMA_PATH.read_text())
        self.assertEqual(set(schema["required"]), set(RESPONSE))
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(validate_response(RESPONSE), RESPONSE)
        for field, value in (("decision", "BUY"), ("confidence", 0.8), ("thesis", ""),
                             ("entry_conditions", "now"), ("invalidation", [1]), ("limiting_data", [""])):
            bad = dict(RESPONSE, **{field: value})
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate_response(bad)
        with self.assertRaises(ValueError): validate_response(dict(RESPONSE, extra=True))
        with self.assertRaises(ValueError): validate_response({})

    def test_reject_tool_events_incomplete_turn_and_invalid_contract(self):
        for tool in ("command_execution", "mcp_tool_call", "web_search", "file_change", "collab_tool_call"):
            log = json.dumps({"type": "item.started", "item": {"type": tool}}) + "\n" + output()
            with self.subTest(tool=tool), self.assertRaises(ValueError): parse_codex_output(log)
        for log in (output().rsplit("\n", 1)[0], '{"type":"turn.failed"}', '{"type":"error"}',
                    output(dict(RESPONSE, confidence=80)), '[]', '{"type":"turn.completed","type":"turn.started"}',
                    '{"type":"turn.completed","usage":NaN}'):
            with self.assertRaises(ValueError): parse_codex_output(log)

    def test_pipeline_does_not_open_or_write_files(self):
        scan = self.scan()
        with patch("builtins.open", side_effect=AssertionError("unexpected file IO")), patch(
                "pathlib.Path.open", side_effect=AssertionError("unexpected file IO")):
            report, runner = self.run_report(scan)
        self.assertEqual(runner.call_count, 2)
        self.assertEqual(report["status"], "ok")

    def test_unknown_response_contract_rejected_before_codex(self):
        from src.integrity import attach_checksum
        scan = self.scan()
        snapshot = scan["results"][0]["decision_snapshot"]
        snapshot["decision_contract"]["required_response_fields"].append("unknown_field")
        attach_checksum(snapshot)
        report, runner = self.run_report(scan)
        runner.assert_not_called()
        self.assertEqual(report["results"][0]["model_review"]["reason"], "unsupported_response_contract")

    def test_capture_error_does_not_invoke_but_other_asset_runs(self):
        scan = self.scan(("SKIP", "ANALYZE"))
        scan["results"][0].update(status="error", error={"stage": "capture", "message": "fixture failed"})
        report, runner = self.run_report(scan)
        self.assertEqual(runner.call_count, 2)
        self.assertEqual(report["status"], "partial_failure")

    def test_separate_timings_and_human_output(self):
        scan = self.scan(("WATCH", "ANALYZE"))
        report, _ = self.run_report(scan)
        for field in ("capture_seconds_total", "pre_analysis_seconds_total", "decision_snapshot_seconds_total"):
            self.assertEqual(report[field], scan[field])
        self.assertEqual(report["scanner_seconds"], scan["total_seconds"])
        self.assertGreaterEqual(report["codex_exec_seconds_total"], 0)
        self.assertGreaterEqual(report["codex_review_wall_seconds"], 0)
        self.assertGreaterEqual(report["total_seconds"], report["codex_exec_seconds_total"])
        text = format_report(report)
        self.assertIn("HYPE WATCH", text)
        self.assertIn("ETH ANALYZE", text)
        self.assertEqual(text.count("confidence="), 1)
        self.assertIn(RESPONSE["thesis"], text)
        self.assertIn("codex_invoked=false", text)
        self.assertIn("codex_invoked=true", text)
        self.assertIn("requires_revalidation=false", text)

    def test_cli_json_human_exit_and_invalid_symbols(self):
        report, _ = self.run_report(self.scan(("WATCH",)))
        with patch("src.analysis_pipeline.run_pipeline", return_value=report) as pipeline:
            buf = io.StringIO()
            with redirect_stdout(buf): self.assertEqual(main(["hype", "--json"]), 0)
            self.assertEqual(json.loads(buf.getvalue()), report)
            self.assertEqual(pipeline.call_args.args[0], ["HYPE"])
            buf = io.StringIO()
            with redirect_stdout(buf): main(["HYPE"])
            self.assertIn("HYPE WATCH", buf.getvalue())
            self.assertNotIn("confidence=", buf.getvalue())
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as exc:
                main(["HYPE;bad"])
            self.assertEqual(exc.exception.code, 2)
        report["status"] = "failed"
        with patch("src.analysis_pipeline.run_pipeline", return_value=report), redirect_stdout(io.StringIO()):
            self.assertEqual(main(["HYPE"]), 1)

    def test_production_subprocess_is_mocked_in_normal_tests(self):
        scan = self.scan()
        with patch("src.analysis_pipeline.scan_symbols", return_value=scan), patch(
                "src.analysis_pipeline.subprocess.run", side_effect=successful_runner) as runner:
            report = run_pipeline(["HYPE"], configuration=CONSENSUS_CONFIG, clock_ms=lambda: NOW + 1000)
        self.assertEqual(runner.call_count, 2)
        self.assertEqual(report["codex_invocations"], 1)


if __name__ == "__main__":
    unittest.main()
