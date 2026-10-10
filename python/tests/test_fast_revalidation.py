"""Offline fast-revalidation tests: no exchange, model, account or wallet I/O."""
import copy
import json
import time
import unittest
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event, Lock
from types import SimpleNamespace
from unittest.mock import Mock, patch

from src.analysis_pipeline import run_pipeline, format_report
from src.fast_revalidation import capture_pair, evaluate_capture, evaluate_predicate, fast_revalidate, _iso
from src.hl_client import CapturedResponse
from src.integrity import attach_checksum, verify_checksum
from src.scanner import scan_symbols
from test_analysis_response import base_response, directional_response, rule, unresolved
from test_market_state import NOW
import test_scanner
import test_analysis_pipeline

CURRENT = NOW + 20_000


def proposal(symbol="HYPE", decision="LONG", configuration=None):
    row = scan_symbols([symbol], market_builder=test_scanner.ScannerTests().market,
                       clock_ms=lambda: NOW + 1000, configuration=configuration)["results"][0]
    row["model_review"] = {"status":"completed", "invoked":True, "execution_authorized":False,
                           "requires_revalidation":True, "expired_at_completion":True,
                           "sent_at_utc":_iso(NOW + 1000), "completed_at_utc":_iso(CURRENT),
                           "response":directional_response(row["decision_snapshot"], decision)}
    return row


class PairClient:
    def __init__(self, symbol="HYPE", stamp=CURRENT):
        self.symbol, self.stamp = symbol, stamp
        self.book = {"coin":symbol,"time":stamp - 10,
                     "levels":[[{"px":"100","sz":"2"}],[{"px":"100.01","sz":"1"}]]}
        self.context = {"markPx":"100","midPx":"100.005","oraclePx":"100", "premium":"0",
                        "dayNtlVlm":"10000000", "funding":"0.00001", "openInterest":"1234",
                        "impactPxs":["100","100.01"]}
        self.calls = []
        self.lock = Lock()
        self.barrier = None

    def _record(self, name):
        with self.lock: self.calls.append(name)
        if self.barrier: self.barrier.wait(timeout=2)

    def l2_book(self, symbol, n_sig_figs=5):
        assert symbol == self.symbol and n_sig_figs == 5
        self._record("l2Book")
        return CapturedResponse(copy.deepcopy(self.book), self.stamp)

    def meta_and_asset_contexts(self):
        self._record("metaAndAssetCtxs")
        return CapturedResponse(({"universe":[{"name":self.symbol}]}, [copy.deepcopy(self.context)]), self.stamp)


class FastRevalidationTests(unittest.TestCase):
    def test_complete_documents_equal_pre_phase3_checksums_gates_and_errors(self):
        from src.research_config import load_config
        # Keep the pending L2 fixtures unchanged: their captured policy was strict.
        self.row = proposal(configuration=load_config(profile="experimental"))
        fixture=json.loads((Path(__file__).parent/"fixtures/orderbook_legacy.json").read_text())
        for case in fixture["revalidation_checksums"]:
            with self.subTest(name=case["name"]):
                captured={"symbol":"HYPE","started_at_ms":CURRENT,"completed_at_ms":CURRENT,
                    "total_seconds":0.25,"deadline_seconds":3.0,"deadline_exceeded":case["deadline_exceeded"],
                    "sources":{
                        "l2Book":{"response":CapturedResponse(copy.deepcopy(case["book"]),CURRENT),
                                  "request_started_at_ms":CURRENT,"duration_seconds":0.1,"error":None},
                        "metaAndAssetCtxs":{"response":CapturedResponse(({"universe":[{"name":"HYPE"}]},
                                                [copy.deepcopy(case["context"])]),CURRENT),
                                             "request_started_at_ms":CURRENT,"duration_seconds":0.2,"error":None}}}
                doc=evaluate_capture(self.row,captured,as_of_ms=CURRENT)
                self.assertEqual(doc["status"],case["status"])
                self.assertEqual(doc["reasons"],case["reasons"])
                self.assertEqual(doc["integrity"]["checksum"],case["checksum"])
                self.assertTrue(verify_checksum(doc))

    def setUp(self):
        self.row = proposal()
        self.client = PairClient()

    def run_fast(self, row=None, client=None, current=CURRENT, **kwargs):
        with patch("urllib.request.urlopen", side_effect=AssertionError("real network forbidden")), \
             patch("subprocess.run", side_effect=AssertionError("mm/model forbidden")):
            return fast_revalidate(row or self.row, client=client or self.client, clock_ms=lambda:current, **kwargs)

    def assert_result(self, doc, status, reason=None):
        self.assertEqual(doc["status"], status, doc["reasons"])
        self.assertFalse(doc["execution_authorized"])
        self.assertTrue(doc["read_only"])
        self.assertTrue(verify_checksum(doc))
        json.dumps(doc, allow_nan=False)
        if reason:
            self.assertIn(reason, [item["code"] for item in doc["reasons"]])
        self.assertEqual(doc["valid_until_utc"] is not None, status == "VALID")

    def test_complete_long_and_short_valid_and_only_two_concurrent_calls(self):
        before = copy.deepcopy(self.row)
        self.client.barrier = Barrier(2)
        doc = self.run_fast()
        self.assert_result(doc, "VALID")
        self.assertCountEqual(self.client.calls, ["l2Book", "metaAndAssetCtxs"])
        self.assertEqual(self.row, before)
        self.assertEqual(doc["source"]["decision_snapshot_checksum"], self.row["decision_snapshot"]["integrity"]["checksum"])
        self.assertEqual(doc["observations"]["depth"]["levels_per_side"], [1,1])
        self.assertEqual(doc["valid_until_utc"], _iso(CURRENT - 10 + 15_000))
        self.assert_result(self.run_fast(row=proposal(decision="SHORT")), "VALID")

    def test_required_entry_failure_and_invalidation_trigger(self):
        extra = rule(rule_id="entry", predicate_type="price_above", field="mark", threshold=103,
                     reference_source={"primary":"timeframes.15m.indicators.ema.21","secondary":None}, purpose="entry")
        self.row["model_review"]["response"]["compatibility_contract"]["rules"].append(extra)
        self.assert_result(self.run_fast(), "INVALID", "REQUIRED_CONDITION_FAILED")
        extra["required_for_valid"] = False
        self.assert_result(self.run_fast(), "VALID")
        self.client.book["levels"][0][0]["px"] = "98"
        self.client.book["levels"][1][0]["px"] = "98.01"
        self.assert_result(self.run_fast(), "INVALID", "INVALIDATION_TRIGGERED")

    def test_incomplete_and_unresolved_never_valid(self):
        contract = self.row["model_review"]["response"]["compatibility_contract"]
        contract.update(complete=False, unresolved_conditions=[unresolved()])
        self.assert_result(self.run_fast(), "INDETERMINATE", "CONTRACT_INCOMPLETE")
        self.assert_result(self.run_fast(), "INDETERMINATE", "UNRESOLVED_CONDITIONS")
        contract["complete"] = True
        self.assert_result(self.run_fast(), "INDETERMINATE", "ORIGINAL_PROVENANCE_OR_CONTRACT_INVALID")

    def test_stale_l2_and_context_and_future_timestamps(self):
        self.client.book["time"] = CURRENT - 15_001
        self.assert_result(self.run_fast(), "INDETERMINATE", "L2_STALE")
        self.client.book["time"] = CURRENT
        self.client.stamp = CURRENT - 30_001
        self.assert_result(self.run_fast(), "INDETERMINATE", "DEPENDENCY_UNUSABLE")
        self.client.stamp = CURRENT
        self.client.book["time"] = CURRENT + 5001
        self.assert_result(self.run_fast(), "INDETERMINATE", "L2_STALE")

    def test_freshness_inclusive_boundaries(self):
        self.client.book["time"] = CURRENT - 15_000
        self.client.stamp = CURRENT - 30_000
        self.assert_result(self.run_fast(), "VALID")

    def test_spread_and_liquidity_use_original_limits(self):
        self.client.book["levels"] = [[{"px":"99.9","sz":"2"}],[{"px":"100.1","sz":"1"}]]
        self.assert_result(self.run_fast(), "INVALID", "SPREAD_LIMIT_EXCEEDED")
        self.client.book["levels"][0][0]["px"] = "100"
        self.client.book["levels"][1][0]["px"] = "100.01"
        self.client.context["dayNtlVlm"] = "999999"
        self.assert_result(self.run_fast(), "INVALID", "DAILY_LIQUIDITY_BELOW_MINIMUM")
        self.client.context["dayNtlVlm"] = str(self.row["decision_snapshot"]["pre_analysis"]["policy"]["min_day_notional_volume_usd"])
        self.assert_result(self.run_fast(), "VALID")

    def test_exact_spread_boundary_and_one_price_increment_outside(self):
        self.client.book["levels"] = [[{"px":"1999","sz":"2"}],[{"px":"2001","sz":"1"}]]
        self.assert_result(self.run_fast(), "VALID")
        self.client.book["levels"][1][0]["px"] = "2001.001"
        self.assert_result(self.run_fast(), "INVALID", "SPREAD_LIMIT_EXCEEDED")

    def test_crossed_malformed_nonfinite_unsorted_and_empty_books(self):
        modes = [[], [[{"px":"100","sz":"2"}]],
                 [[{"px":"101","sz":"2"}],[{"px":"100","sz":"1"}]],
                 [[{"px":"NaN","sz":"2"}],[{"px":"100","sz":"1"}]],
                 [[{"px":"100","sz":"-1"}],[{"px":"101","sz":"1"}]],
                 [[{"px":"100","sz":"0"}],[{"px":"101","sz":"1"}]],
                 [[{"px":"100","sz":"1"},{"px":"101","sz":"1"}],[{"px":"102","sz":"1"}]]]
        for levels in modes:
            self.client.book["levels"] = levels
            self.assert_result(self.run_fast(), "INDETERMINATE", "MALFORMED_L2")
        self.client.book["time"] = True
        self.assert_result(self.run_fast(), "INDETERMINATE", "MALFORMED_L2")

    def test_missing_required_prices_or_volume_and_wrong_symbol(self):
        for key in ("markPx", "midPx", "oraclePx", "dayNtlVlm"):
            client = PairClient(); client.context[key] = None
            self.assert_result(self.run_fast(client=client), "INDETERMINATE")
        self.client.book["coin"] = "ETH"
        self.assert_result(self.run_fast(), "INDETERMINATE", "MALFORMED_L2")

    def test_new_relevant_close_is_indeterminate_without_candle_fetch(self):
        next_close = NOW - 1 + 300_000
        client = PairClient(stamp=next_close)
        doc = self.run_fast(client=client, current=next_close)
        self.assert_result(doc, "INDETERMINATE", "NEW_CLOSED_CANDLE_UNVERIFIED")
        self.assertCountEqual(client.calls, ["l2Book","metaAndAssetCtxs"])
        client.stamp -= 1; client.book["time"] -= 1
        self.assert_result(self.run_fast(client=client, current=next_close-1), "VALID")

    def test_original_checksum_and_links_gate_before_network(self):
        modes = ["checksum", "pre_checksum", "link", "symbol", "classification", "benchmark", "dispatch", "contract"]
        for mode in modes:
            row = copy.deepcopy(self.row)
            if mode == "checksum": row["decision_snapshot"]["price"]["mark"] += 1
            if mode == "pre_checksum": row["pre_analysis"]["features"]["mark"] += 1
            if mode == "link": row["decision_snapshot"]["source_market_checksum"] = "0"*64; attach_checksum(row["decision_snapshot"])
            if mode == "symbol": row["symbol"] = "ETH"
            if mode == "classification": row["classification"] = "WATCH"
            if mode == "benchmark": row["decision_snapshot"]["benchmark_override"]["applied"] = True; attach_checksum(row["decision_snapshot"])
            if mode == "dispatch": row["model_review"]["sent_at_utc"] = _iso(CURRENT)
            if mode == "contract": row["model_review"]["response"]["compatibility_contract"]["source_snapshot_checksum"] = "0"*64
            client = PairClient()
            self.assert_result(self.run_fast(row=row, client=client), "INDETERMINATE", "ORIGINAL_PROVENANCE_OR_CONTRACT_INVALID")
            self.assertEqual(client.calls, [])

    def test_no_implicit_funding_oi_or_imbalance_gates(self):
        self.client.context.update(funding="999", openInterest="1")
        self.client.book["levels"][0][0]["sz"] = "0.001"
        self.client.book["levels"][1][0]["sz"] = "999"
        self.assert_result(self.run_fast(), "VALID")
        self.client.context.update(funding=None, openInterest=None, premium=None)
        self.assert_result(self.run_fast(), "VALID")

    def test_explicit_funding_and_imbalance_rules_and_missing_values(self):
        contract = self.row["model_review"]["response"]["compatibility_contract"]
        extra = rule(rule_id="funding", field="funding_current", predicate_type="funding_lte", purpose="quality",
                     threshold=0.00001, reference_source={"primary":"derivatives.funding.current_rate","secondary":None})
        contract["rules"].append(extra)
        self.assert_result(self.run_fast(), "VALID")
        self.client.context["funding"] = "0.00002"
        self.assert_result(self.run_fast(), "INVALID", "REQUIRED_CONDITION_FAILED")
        self.client.context["funding"] = None
        self.assert_result(self.run_fast(), "INDETERMINATE", "REQUIRED_RULE_UNEVALUABLE")
        contract["rules"].pop()
        imbalance = self.row["decision_snapshot"]["orderbook"]["imbalance"]["value"]
        contract["rules"].append(rule(rule_id="imbalance", field="orderbook_imbalance", predicate_type="imbalance_gte",
            purpose="quality", threshold=imbalance, reference_source={"primary":"orderbook.imbalance.value","secondary":None}))
        self.client.book["levels"][0][0]["sz"] = "1"
        self.client.book["levels"][1][0]["sz"] = "100"
        self.assert_result(self.run_fast(), "INVALID", "REQUIRED_CONDITION_FAILED")

    def test_all_eleven_predicates_and_oi_fractional_change(self):
        snapshot = copy.deepcopy(self.row["decision_snapshot"])
        snapshot["derivatives"]["open_interest"] = 100
        snapshot["price"]["mark"] = 100
        snapshot["timeframes"]["15m"]["indicators"]["atr_14"] = 2
        cases = [("price_above",101,100),("price_below",99,100),("price_in_range",100,None),
                 ("spread_lte",10,10),("funding_lte",0.001,0.001),("funding_gte",0.001,0.001),
                 ("oi_change_gte",110,0.1),("oi_change_lte",110,0.1),
                 ("imbalance_gte",0.2,0.2),("imbalance_lte",0.2,0.2),("max_price_displacement_atr",101,0.5)]
        for predicate, observed, threshold in cases:
            r = rule(purpose="entry", predicate_type=predicate, threshold=threshold,
                     range={"lower":99,"upper":101} if predicate == "price_in_range" else None,
                     baseline_field="mark", atr_field="original_atr_15m")
            result = evaluate_predicate(r, observed, snapshot)
            self.assertTrue(result["passed"], (predicate,result))
            r["purpose"] = "invalidation"
            self.assertFalse(evaluate_predicate(r, observed, snapshot)["passed"])
        snapshot["derivatives"]["open_interest"] = 0
        self.assertIsNone(evaluate_predicate(rule(predicate_type="oi_change_gte",threshold=0),100,snapshot)["passed"])

    def test_unapproved_oi_contract_still_cannot_bypass_v2_provenance(self):
        self.row["model_review"]["response"]["compatibility_contract"]["rules"].append(rule(
            rule_id="oi",field="open_interest",predicate_type="oi_change_gte",purpose="quality",threshold=0,
            reference_source={"primary":"derivatives.open_interest","secondary":None}))
        self.assert_result(self.run_fast(), "INDETERMINATE", "ORIGINAL_PROVENANCE_OR_CONTRACT_INVALID")
        self.assertEqual(self.client.calls, [])

    def test_atr_displacement_is_only_explicit_and_snapshot_derived(self):
        snapshot = self.row["decision_snapshot"]
        threshold = abs(snapshot["price"]["mark"]-99)/snapshot["timeframes"]["15m"]["indicators"]["atr_14"]
        extra = rule(rule_id="drift",field="mark",predicate_type="max_price_displacement_atr",purpose="quality",
            threshold=threshold,derivation="snapshot_distance_atr",threshold_origin="model_derived_from_snapshot",
            baseline_field="mark",atr_field="original_atr_15m",
            reference_source={"primary":"price.mark","secondary":"timeframes.15m.latest_candle.low"})
        self.client.context["markPx"] = "110"
        self.assert_result(self.run_fast(), "VALID")
        self.row["model_review"]["response"]["compatibility_contract"]["rules"].append(extra)
        self.assert_result(self.run_fast(), "INVALID", "REQUIRED_CONDITION_FAILED")

    def test_timeout_returns_without_joining_blocked_worker_and_late_result_is_isolated(self):
        entered, release, done = Event(), Event(), Event()
        normal = self.client.l2_book
        def blocked(*args, **kwargs):
            entered.set(); release.wait(timeout=2)
            result = normal(*args, **kwargs)
            done.set()
            return result
        self.client.l2_book = blocked
        start = time.monotonic()
        try:
            doc = self.run_fast(deadline_seconds=0.05)
            elapsed = time.monotonic()-start
            self.assertTrue(entered.is_set())
            self.assertLess(elapsed,0.5)
            self.assert_result(doc, "INDETERMINATE", "DEADLINE_EXCEEDED")
            before = copy.deepcopy(doc)
        finally:
            release.set()
        self.assertTrue(done.wait(timeout=1))
        # Slot/resources are released by the worker, never by the caller twice.
        self.assertEqual(doc, before)

    def test_reused_funding_is_only_a_dependency_for_required_history_rules(self):
        market=test_scanner.ScannerTests().market("HYPE")
        market["capture"]["sources"]["funding"]["source_timestamp_utc"]=_iso(NOW-5_400_000+10_000)
        attach_checksum(market)
        row=scan_symbols(["HYPE"],market_builder=lambda _:market,clock_ms=lambda:NOW+1000)["results"][0]
        row["model_review"]=copy.deepcopy(self.row["model_review"])
        row["model_review"]["response"]=directional_response(row["decision_snapshot"])
        self.assert_result(self.run_fast(row=row),"VALID")
        historical=rule(rule_id="historical_funding",field="funding_current",predicate_type="funding_lte",purpose="quality",
                        threshold=0.1,reference_source={"primary":"derivatives.funding.history_72h_mean","secondary":None})
        row["model_review"]["response"]["compatibility_contract"]["rules"].append(historical)
        doc=self.run_fast(row=row)
        self.assert_result(doc,"INDETERMINATE","DEPENDENCY_UNUSABLE")
        self.assertTrue(doc["dependencies"]["original_funding_history"]["required_for_valid"])
        historical["required_for_valid"]=False
        doc=self.run_fast(row=row)
        self.assert_result(doc,"VALID")
        self.assertFalse(doc["dependencies"]["original_funding_history"]["required_for_valid"])

    def test_pure_capture_evaluator_cannot_bypass_original_integrity(self):
        captured=capture_pair("HYPE",client=self.client,clock_ms=lambda:CURRENT)
        self.row["decision_snapshot"]["price"]["mark"]+=1
        doc=evaluate_capture(self.row,captured,as_of_ms=CURRENT)
        self.assert_result(doc,"INDETERMINATE","ORIGINAL_PROVENANCE_OR_CONTRACT_INVALID")

    def test_total_deadline_includes_original_gate_not_only_network(self):
        with patch("src.fast_revalidation._original_gate",side_effect=lambda _:time.sleep(0.01)):
            doc=self.run_fast(deadline_seconds=0.005)
        self.assert_result(doc,"INDETERMINATE","DEADLINE_EXCEEDED")
        self.assertEqual(self.client.calls,[])

    def test_error_is_indeterminate_without_retry(self):
        self.client.l2_book = Mock(side_effect=TimeoutError("timeout"))
        self.assert_result(self.run_fast(), "INDETERMINATE", "API_TIMEOUT")
        self.client.l2_book.assert_called_once()

    def test_capture_capacity_and_invalid_deadline_are_bounded(self):
        with patch("src.fast_revalidation._CAPTURE_SLOTS") as slots:
            slots.acquire.return_value = False
            doc = self.run_fast()
        self.assert_result(doc,"INDETERMINATE","CAPTURE_CAPACITY_UNAVAILABLE")
        self.assertEqual(self.client.calls,[])
        for deadline in (0,-1,float("nan"),float("inf"),True):
            with self.assertRaises(ValueError): capture_pair("HYPE",client=self.client,deadline_seconds=deadline)

    def test_symbol_isolation_and_concurrent_revalidations(self):
        rows = [proposal("HYPE"),proposal("ETH","SHORT")]
        clients = [PairClient("HYPE"),PairClient("ETH")]
        barrier = Barrier(4)
        for client in clients: client.barrier = barrier
        clients[1].context["dayNtlVlm"] = "999"
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(self.run_fast,row=row,client=client) for row,client in zip(rows,clients)]
            docs = [future.result(timeout=2) for future in futures]
        self.assert_result(docs[0],"VALID")
        self.assert_result(docs[1],"INVALID")
        self.assertEqual([doc["symbol"] for doc in docs],["HYPE","ETH"])


class PipelineRevalidationTests(unittest.TestCase):
    def scan(self, symbols=("HYPE",)):
        return scan_symbols(symbols,market_builder=test_scanner.ScannerTests().market,clock_ms=lambda:NOW+1000)

    def test_watch_no_trade_and_unexpired_long_short_do_not_revalidate(self):
        for decision in ("WATCH","NO_TRADE","LONG","SHORT"):
            scan = self.scan()
            snapshot = scan["results"][0]["decision_snapshot"]
            response = directional_response(snapshot,decision) if decision in {"LONG","SHORT"} else base_response(decision)
            now = NOW+1000
            def runner(*args, **kwargs):
                nonlocal now
                if decision in {"WATCH","NO_TRADE"}: now=CURRENT
                return SimpleNamespace(returncode=0,stdout=test_analysis_pipeline.model_output(args[0], response))
            revalidator = Mock(side_effect=AssertionError("unexpected recapture"))
            report = run_pipeline(scan["symbols"],scanner=Mock(return_value=scan),runner=runner,
                                  revalidator=revalidator,clock_ms=lambda:now)
            revalidator.assert_not_called()
            row=report["results"][0]
            self.assertIsNone(row["fast_revalidation"])
            self.assertFalse(row["model_review"]["execution_authorized"])
            self.assertEqual(report["fast_revalidations"],0)

    def test_revalidation_begins_in_fast_worker_before_other_codex_finishes(self):
        scan = self.scan(("HYPE","ETH"))
        started, revalidation_began = Barrier(4), Event()
        now = NOW+1000
        def runner(*args, **kwargs):
            nonlocal now
            snapshot = test_analysis_pipeline.snapshot_input(args[0], kwargs)
            started.wait(timeout=2)
            now=CURRENT
            if snapshot["symbol"] == "ETH": self.assertTrue(revalidation_began.wait(timeout=2))
            return SimpleNamespace(returncode=0,stdout=test_analysis_pipeline.model_output(args[0], directional_response(snapshot)))
        def revalidator(row, **kwargs):
            if row["symbol"] == "HYPE": revalidation_began.set()
            return fast_revalidate(row,client=PairClient(row["symbol"]),**kwargs)
        report = run_pipeline(scan["symbols"],scanner=Mock(return_value=scan),runner=runner,
                              revalidator=revalidator,clock_ms=lambda:now,
                              configuration=test_analysis_pipeline.CONSENSUS_CONFIG)
        self.assertEqual(report["fast_revalidations"],2)
        self.assertTrue(all(row["fast_revalidation"]["status"] == "VALID" for row in report["results"]))
        self.assertIn("revalidation=VALID",format_report(report))
        self.assertTrue(all(row["model_review"]["requires_revalidation"] for row in report["results"]))

    def test_revalidator_error_preserves_model_review_and_other_symbols(self):
        scan = self.scan(("HYPE","ETH"))
        now=NOW+1000
        def runner(*args, **kwargs):
            nonlocal now
            now=CURRENT
            snapshot = test_analysis_pipeline.snapshot_input(args[0], kwargs)
            return SimpleNamespace(returncode=0,stdout=test_analysis_pipeline.model_output(args[0], directional_response(snapshot)))
        # This test exercises one review; avoid shared dispatch clock changes.
        scan=self.scan()
        report=run_pipeline(scan["symbols"],scanner=Mock(return_value=scan),runner=runner,clock_ms=lambda:now,
                            revalidator=Mock(side_effect=RuntimeError("private error payload")))
        row=report["results"][0]
        self.assertEqual(row["model_review"]["status"],"completed")
        self.assertEqual(row["fast_revalidation"]["status"],"INDETERMINATE")
        self.assertNotIn("private error payload",json.dumps(report))


if __name__ == "__main__":
    unittest.main()
