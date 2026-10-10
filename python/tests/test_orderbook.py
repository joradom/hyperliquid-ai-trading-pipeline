"""Offline L2 maths, coverage and pre-extraction compatibility fixtures."""
import copy
import json
import unittest
from decimal import Decimal, localcontext, ROUND_DOWN
from pathlib import Path
from unittest.mock import patch

from src import market_state, fast_revalidation
from src.orderbook import (BANDS, MATH, bbo, calculate_book, decimal_text, l2_view,
                           normalize_ws_book, midpoint, spread_absolute, spread_pct, spread_bps, obi)
from src.stream_types import Book, Level, StreamValidationError, _book
from src.integrity import canonical_checksum_payload
from test_stream_types import NOW, book

FIXTURE = Path(__file__).parent / "fixtures/orderbook_legacy.json"


def normalized(raw=None):
    return normalize_ws_book(raw or book(time_ms=NOW), NOW, frozenset(("ETH",)))


def deep_book():
    return Book("ETH", NOW,
                tuple(Level(Decimal(p), Decimal(s), 1) for p, s in
                      [("99.99", "2"), ("99.9", "5"), ("99.75", "7"), ("99.5", "11"), ("99", "13")]),
                tuple(Level(Decimal(p), Decimal(s), 1) for p, s in
                      [("100.01", "3"), ("100.1", "17"), ("100.25", "19"), ("100.5", "23"), ("101", "29")]))


def view(value=None, **changes):
    options = {"as_of_ms": NOW, "received_at_ms": NOW, "network": "mainnet", "symbol": "ETH",
               "book_epoch": 1, "current_epoch": 1}
    options.update(changes)
    return l2_view(value or deep_book(), **options)


def outcome(call):
    try:
        return {"value": call()}
    except Exception as exc:
        return {"error_type": type(exc).__name__, "error": str(exc)}


class OrderBookCompatibilityTests(unittest.TestCase):
    def test_all_three_adapters_equal_pre_extraction_outputs_errors_and_types(self):
        fixtures = json.loads(FIXTURE.read_text())
        self.assertTrue(fixtures["captured_before_phase3"])
        for case in fixtures["cases"]:
            with self.subTest(name=case["name"]):
                raw = copy.deepcopy(case["raw"])
                before = copy.deepcopy(raw)
                issues = []
                actual = outcome(lambda: market_state._book(raw, issues))
                actual["issues"] = issues
                # Byte equality detects int/float changes masked by Python ==.
                self.assertEqual(canonical_checksum_payload(actual), canonical_checksum_payload(case["market"]))
                actual = outcome(lambda: fast_revalidation._normalize_book(raw, "ETH"))
                self.assertEqual(canonical_checksum_payload(actual), canonical_checksum_payload(case["revalidation"]))
                actual = outcome(lambda: _book(raw, NOW, frozenset(("ETH",))))
                if "value" in actual:
                    value = actual["value"]
                    self.assertIsInstance(value, Book)
                    actual["value"] = {"coin": value.coin, "time_ms": value.time_ms,
                                       "bids": [[str(l.price), str(l.size), l.order_count] for l in value.bids],
                                       "asks": [[str(l.price), str(l.size), l.order_count] for l in value.asks]}
                self.assertEqual(actual, case["ws"])
                self.assertEqual(raw, before)

    def test_adapters_keep_locked_repeated_and_zero_level_size_differences(self):
        raw = book(bid="101", ask="101")
        raw["levels"][0] = [{"px":"101", "sz":"0", "n":1}, {"px":"101", "sz":"2", "n":1}]
        self.assertEqual(fast_revalidation._normalize_book(raw, "ETH")["spread_bps"], 0)
        self.assertEqual(market_state._book(raw, [])["depth"]["bid_total_size"], 2)
        with self.assertRaisesRegex(StreamValidationError, "invalid_decimal"):
            normalized(raw)


class OrderBookMetricsTests(unittest.TestCase):
    def test_bbo_spread_units_and_legacy_obi(self):
        metrics = calculate_book(deep_book())
        self.assertEqual(bbo(deep_book()), (Decimal("99.99"), Decimal("100.01")))
        self.assertEqual(metrics["bbo"], {"best_bid":"99.99", "best_ask":"100.01", "midpoint":"1e2",
                                          "spread_absolute":"0.02", "spread_pct":"0.02", "spread_bps":"2"})
        self.assertEqual(metrics["depth"]["bids"]["total_base"], "38")
        self.assertEqual(metrics["depth"]["asks"]["total_base"], "91")
        with localcontext(MATH):
            self.assertEqual(metrics["obi_legacy"]["observed"], decimal_text(Decimal(-53) / 129))
        self.assertEqual(metrics["obi_legacy"]["valid"], metrics["obi_legacy"]["observed"])
        self.assertEqual(metrics["obi_legacy"]["entire_book_coverage"], "unknown")

    def test_shared_scalar_functions_preserve_float_operation_order(self):
        self.assertEqual(midpoint(1e308, 1e308, overflow_safe=True), 1e308)
        self.assertEqual(midpoint(100.0, 101.0), 100.5)
        self.assertEqual(spread_absolute(100, 101), 1)
        self.assertEqual(spread_pct(1, 100), 1)
        self.assertEqual(spread_bps(1, 100), 100)
        self.assertIsNone(obi(0, 0))

    def test_accumulated_depth_base_and_quote_are_separate(self):
        depth = calculate_book(deep_book())["depth"]
        self.assertEqual(depth["bids"]["levels"][0]["notional_quote"], "199.98")
        self.assertEqual(depth["bids"]["levels"][1]["cumulative_base"], "7")
        self.assertEqual(depth["bids"]["levels"][1]["cumulative_quote"], "699.48")
        self.assertEqual(depth["asks"]["levels"][1]["cumulative_quote"], "2001.73")
        self.assertEqual(depth["bids"]["total_quote"], "3779.23")
        self.assertEqual(depth["asks"]["total_quote"], "9146.98")

    def test_all_four_band_boundaries_inclusive_and_quote_depth(self):
        bands = calculate_book(deep_book())["bands"]
        expected = [("0.001", "99.9", "100.1", "7", "20", "complete"),
                    ("0.0025", "99.75", "100.25", "14", "39", "complete"),
                    ("0.005", "99.5", "100.5", "25", "62", "complete"),
                    ("0.01", "99", "101", "38", "91", "complete")]
        for key, lower, upper, bid_base, ask_base, coverage in expected:
            with self.subTest(band=key):
                band = bands[key]
                self.assertEqual((band["lower_bound"], band["upper_bound"]), (lower, upper))
                self.assertEqual((Decimal(band["bids"]["observed_base"]), Decimal(band["asks"]["observed_base"])),
                                 (Decimal(bid_base), Decimal(ask_base)))
                self.assertEqual(band["coverage"], coverage)
                self.assertEqual(band["obi_valid"], band["obi_observed"])
                with localcontext(MATH):
                    expected_obi = (Decimal(bid_base) - Decimal(ask_base)) / (Decimal(bid_base) + Decimal(ask_base))
                self.assertEqual(band["obi_valid"], decimal_text(expected_obi))
        self.assertEqual(bands["0.001"]["bids"]["observed_quote"], "699.48")
        self.assertEqual(bands["0.001"]["asks"]["observed_quote"], "2001.73")

    def test_levels_just_outside_each_band_are_excluded(self):
        for fraction in BANDS:
            with self.subTest(fraction=fraction), localcontext(MATH):
                lower, upper = 100 * (1-fraction), 100 * (1+fraction)
                value = Book("ETH", NOW,
                    (Level(Decimal("99.99"), Decimal(2), 1), Level(lower, Decimal(5), 1),
                     Level(lower-Decimal("0.000000000001"), Decimal(100), 1)),
                    (Level(Decimal("100.01"), Decimal(3), 1), Level(upper, Decimal(7), 1),
                     Level(upper+Decimal("0.000000000001"), Decimal(100), 1)))
                band = calculate_book(value)["bands"][decimal_text(fraction)]
                self.assertEqual(band["bids"]["observed_base"], "7")
                self.assertEqual(Decimal(band["asks"]["observed_base"]), Decimal(10))
                self.assertEqual(band["coverage"], "complete")

    def test_partial_obi_is_observed_but_never_valid(self):
        metrics = calculate_book(normalized(book(bid="99.99", ask="100.01", time_ms=NOW)))
        for band in metrics["bands"].values():
            self.assertEqual(band["coverage"], "partial")
            self.assertEqual(band["obi_observed"], "-0.2")
            self.assertIsNone(band["obi_valid"])
            self.assertIn("band_coverage_partial", band["reasons"])

    def test_twenty_levels_do_not_prove_band_coverage(self):
        value = Book("ETH", NOW,
                     tuple(Level(Decimal("99.99")-Decimal(i)/10000, Decimal(1), 1) for i in range(20)),
                     tuple(Level(Decimal("100.01")+Decimal(i)/10000, Decimal(1), 1) for i in range(20)))
        for band in calculate_book(value)["bands"].values():
            self.assertEqual(band["coverage"], "partial")
            self.assertIsNone(band["obi_valid"])

    def test_one_side_boundary_evidence_is_insufficient(self):
        value = deep_book()
        value = Book(value.coin, value.time_ms, value.bids, value.asks[:1])
        band = calculate_book(value)["bands"]["0.01"]
        self.assertEqual(band["bids"]["coverage"], "complete")
        self.assertEqual(band["asks"]["coverage"], "partial")
        self.assertIsNone(band["obi_valid"])

    def test_zero_in_band_denominator_has_explicit_reason(self):
        band = calculate_book(normalized(book(bid="99", ask="101")))["bands"]["0.001"]
        self.assertEqual(band["coverage"], "complete")
        self.assertIsNone(band["obi_observed"])
        self.assertIsNone(band["obi_valid"])
        self.assertIn("no_visible_liquidity_in_band", band["reasons"])

    def test_empty_missing_and_crossed_books_have_unknown_coverage(self):
        for value in (None, Book("ETH", NOW, (), ()), normalized(book(bid="100", ask="101"))):
            if value is not None and value.bids:
                value = Book("ETH", NOW, value.asks, value.bids)
            with self.subTest(value=value):
                metrics = calculate_book(value)
                self.assertFalse(metrics["structure"]["valid"])
                self.assertIsNone(metrics["bbo"])
                self.assertIsNone(metrics["obi_legacy"]["observed"])
                for band in metrics["bands"].values():
                    self.assertEqual(band["coverage"], "unknown")
                    self.assertIsNone(band["obi_valid"])
                    self.assertTrue(band["reasons"])

    def test_invalid_normalized_levels_never_produce_valid_metrics(self):
        original = normalized()
        for level in (Level(Decimal(100), Decimal(0), 1), Level(Decimal(100), Decimal(-1), 1),
                      Level(Decimal("NaN"), Decimal(1), 1), Level(Decimal(100), Decimal(1), True)):
            with self.subTest(level=level):
                value = Book("ETH", NOW, (level,), original.asks)
                self.assertFalse(calculate_book(value)["structure"]["valid"])
        value = Book("ETH", NOW, original.bids*2, original.asks)
        self.assertEqual(calculate_book(value)["structure"]["reasons"], ["unsorted_or_repeated_levels"])

    def test_decimal_metrics_are_independent_of_ambient_context(self):
        expected = calculate_book(deep_book())
        with localcontext() as ambient:
            ambient.prec = 6
            ambient.rounding = ROUND_DOWN
            self.assertEqual(calculate_book(deep_book()), expected)
            self.assertEqual(ambient.prec, 6)
            self.assertEqual(ambient.rounding, ROUND_DOWN)

    def test_input_precision_is_preserved_and_output_detached(self):
        raw = book(bid="100.12345678901234567890123456789", ask="100.12345678901234567890123456790")
        original = copy.deepcopy(raw)
        value = normalized(raw)
        metrics = calculate_book(value)
        self.assertEqual(metrics["bbo"]["spread_absolute"], "1e-29")
        metrics["depth"]["bids"]["levels"][0]["size_base"] = "999"
        self.assertEqual(calculate_book(value)["depth"]["bids"]["levels"][0]["size_base"], "2")
        self.assertEqual(raw, original)

    def test_pure_calculations_never_call_network_or_subprocess(self):
        with patch("urllib.request.urlopen", side_effect=AssertionError("network forbidden")), \
             patch("subprocess.run", side_effect=AssertionError("execution forbidden")):
            self.assertTrue(view()["quality"]["usable"])
            json.dumps(view(), allow_nan=False)


class OrderBookQualityTests(unittest.TestCase):
    def test_conflicting_timestamp_and_unknown_precision_have_no_valid_obi(self):
        result=view(conflicting_timestamp_ms=NOW)
        self.assertIn("book_timestamp_conflict",result["quality"]["reasons"])
        self.assertIsNone(result["metrics"]["obi_legacy"]["valid"])
        self.assertIsNotNone(result["metrics"]["obi_legacy"]["observed"])
        self.assertTrue(view(conflicting_timestamp_ms=NOW-1)["quality"]["usable"])
        self.assertIn("book_precision_unknown",view(n_sig_figs=None)["quality"]["reasons"])
        self.assertIn("book_epoch_unknown",view(book_epoch=True)["quality"]["reasons"])

    def test_freshness_exact_inclusive_boundaries(self):
        self.assertTrue(view(as_of_ms=NOW+15000)["quality"]["usable"])
        self.assertTrue(view(as_of_ms=NOW-5000)["quality"]["usable"])
        self.assertIn("book_exchange_stale", view(as_of_ms=NOW+15001)["quality"]["reasons"])
        self.assertIn("book_exchange_future", view(as_of_ms=NOW-5001)["quality"]["reasons"])

    def test_stale_book_retains_observed_obi_and_spatial_coverage(self):
        result = view(as_of_ms=NOW+15001)
        self.assertFalse(result["quality"]["usable"])
        self.assertIsNotNone(result["metrics"]["obi_legacy"]["observed"])
        self.assertIsNone(result["metrics"]["obi_legacy"]["valid"])
        for band in result["metrics"]["bands"].values():
            self.assertEqual(band["coverage"], "complete")
            self.assertIsNotNone(band["obi_observed"])
            self.assertIsNone(band["obi_valid"])
            self.assertIn("book_exchange_stale", band["reasons"])

    def test_receipt_age_is_independent_and_required(self):
        result = view(received_at_ms=NOW-15001)
        self.assertEqual(result["freshness"]["exchange"]["status"], "fresh")
        self.assertEqual(result["freshness"]["receipt"]["status"], "stale")
        self.assertIsNone(result["metrics"]["obi_legacy"]["valid"])
        self.assertIn("book_receipt_time_unknown", view(received_at_ms=None)["quality"]["reasons"])
        self.assertIn("book_receipt_future", view(received_at_ms=NOW+5001)["quality"]["reasons"])

    def test_prior_epoch_does_not_become_current_by_reading(self):
        result = view(current_epoch=2)
        self.assertIn("book_epoch_mismatch", result["quality"]["reasons"])
        self.assertIsNone(result["metrics"]["bands"]["0.01"]["obi_valid"])
        self.assertTrue(view(book_epoch=2, current_epoch=2)["quality"]["usable"])
        self.assertIn("book_epoch_unknown", view(book_epoch=None)["quality"]["reasons"])

    def test_provenance_precision_units_and_symbol_check(self):
        result = view()
        self.assertEqual(result["source"]["book_time_ms"], NOW)
        self.assertEqual(result["source"]["received_at_ms"], NOW)
        self.assertEqual(result["precision"]["n_sig_figs"], 5)
        self.assertEqual(result["precision"]["decimal_precision"], 192)
        self.assertEqual(result["precision"]["coverage_basis"], "returned_aggregated_price_range")
        self.assertEqual(result["units"]["notional"], "quote")
        self.assertFalse(result["execution_authorized"])
        self.assertIn("book_symbol_mismatch", view(symbol="HYPE")["quality"]["reasons"])

    def test_invalid_evaluation_parameters_rejected(self):
        for options in ({"as_of_ms":True}, {"freshness_ms":-1}, {"max_future_skew_ms":0.5}, {"n_sig_figs":True}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                view(**options)


if __name__ == "__main__":
    unittest.main()
