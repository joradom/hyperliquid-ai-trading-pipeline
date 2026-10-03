import copy
import unittest

from src.hl_client import CapturedResponse, HyperliquidAPIError
from src.market_state import INTERVAL_MS, attach_checksum, build_market_state, verify_checksum

NOW = 2_000_000_000_000


class FakeClient:
    def __init__(self, mode="normal"):
        self.mode = mode

    def market_context(self, symbol):
        context = {"markPx": "100.12345678901234", "oraclePx": "100", "midPx": "100.1", "prevDayPx": "99",
                   "funding": "0.00001", "openInterest": "1234", "dayNtlVlm": "99", "premium": "0", "impactPxs": ["100", "101"]}
        if self.mode == "missing": context.pop("midPx")
        if self.mode == "null": context["oraclePx"] = None
        if self.mode == "funding_missing": context.pop("funding")
        return CapturedResponse(({"name": symbol, "maxLeverage": 3, "szDecimals": 2}, context), NOW + 10)

    def candles(self, symbol, interval, start_ms, end_ms):
        duration = INTERVAL_MS[interval]; count = 119 if self.mode == "insufficient" else 120
        first = NOW - count * duration
        candles = [{"t": first+i*duration, "T": first+(i+1)*duration-1, "o":"100", "h":"102", "l":"99", "c":str(100+i/100), "v":"10", "n":2} for i in range(count)]
        if self.mode == "gap": candles.pop(10)
        if self.mode == "open": candles.append({"t": NOW, "T": NOW+duration-1, "o":"1", "h":"1", "l":"1", "c":"1", "v":"1", "n":1})
        if self.mode == "old":
            for candle in candles: candle["t"] -= 20*duration; candle["T"] -= 20*duration
        return CapturedResponse(candles, NOW + 20)

    def l2_book(self, symbol):
        levels = [[{"px":"100.12345678901234", "sz":"2", "n":1}], [{"px":"100.22345678901234", "sz":"1", "n":1}]]
        if self.mode == "empty_book": levels = []
        if self.mode == "one_side": levels = levels[:1]
        return CapturedResponse({"time": NOW-1000, "levels": levels}, NOW+30)

    def funding_history(self, symbol, start_ms, end_ms):
        return CapturedResponse([{"time": NOW-3_600_000, "fundingRate":"0.1"}], NOW+40)


class MarketStateTests(unittest.TestCase):
    def build(self, mode="normal"):
        return build_market_state("HYPE", FakeClient(mode), candle_count=120, clock_ms=lambda: NOW)

    def codes(self, snapshot):
        return [issue["code"] for issue in snapshot["data_quality"]["issues"]]

    def test_normal_and_checksum(self):
        snapshot = self.build()
        self.assertEqual(snapshot["schema_version"], "1.1")
        self.assertTrue(verify_checksum(snapshot))
        self.assertEqual(snapshot["orderbook"]["spread_absolute"], 0.1)
        tampered = copy.deepcopy(snapshot); tampered["price"]["mark"] = 1
        self.assertFalse(verify_checksum(tampered))

    def test_missing_and_null_fields(self):
        self.assertIn("missing_field", self.codes(self.build("missing")))
        self.assertIn("missing_field", self.codes(self.build("null")))
        self.assertIn("missing_field", self.codes(self.build("funding_missing")))

    def test_insufficient_gap_open_and_old_candles(self):
        self.assertIn("insufficient_candles", self.codes(self.build("insufficient")))
        self.assertIn("timestamp_gap", self.codes(self.build("gap")))
        opened = self.build("open")
        self.assertEqual(opened["timeframes"]["5m"]["candle_count"], 120)
        self.assertLess(opened["timeframes"]["5m"]["latest_candle"]["close_time_ms"], NOW)
        old = self.build("old")
        self.assertEqual(old["capture"]["sources"]["ohlcv"]["5m"]["freshness"]["status"], "stale")
        self.assertIn("stale_data", self.codes(old))

    def test_empty_and_one_sided_books(self):
        for mode in ("empty_book", "one_side"):
            snapshot = self.build(mode)
            self.assertIn("missing_orderbook_side", self.codes(snapshot))
            self.assertIsNone(snapshot["orderbook"]["spread_absolute"])

    def test_unknown_when_hyperliquid_has_no_timestamp(self):
        snapshot = self.build()
        self.assertEqual(snapshot["capture"]["sources"]["open_interest"]["freshness"]["status"], "unknown")
        self.assertIn("unknown_freshness", self.codes(snapshot))

    def test_endpoint_error_is_recorded(self):
        client = FakeClient()
        client.candles = lambda *args: (_ for _ in ()).throw(HyperliquidAPIError("boom"))
        snapshot = build_market_state("HYPE", client, candle_count=120, clock_ms=lambda: NOW)
        self.assertIn("endpoint_unavailable", self.codes(snapshot))


if __name__ == "__main__": unittest.main()
