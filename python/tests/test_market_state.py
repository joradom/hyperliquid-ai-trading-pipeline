import copy
import json
import unittest
from pathlib import Path
from unittest.mock import Mock
from test_rest_policy import Clock

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
    def test_full_snapshots_keep_pre_phase3_checksums(self):
        fixture = json.loads((Path(__file__).parent / "fixtures/orderbook_legacy.json").read_text())
        for mode, checksum in fixture["market_state_checksums"].items():
            with self.subTest(mode=mode):
                snapshot = self.build(mode)
                self.assertTrue(verify_checksum(snapshot))
                self.assertEqual(snapshot["integrity"]["checksum"], checksum)

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

    def test_capture_deadline_stops_later_transport_and_preserves_partial_data(self):
        clock = Clock(); client = FakeClient()
        original = client.candles
        def delayed(*args):
            clock.now += 26
            return original(*args)
        client.candles = Mock(side_effect=delayed)
        client.l2_book = Mock(wraps=client.l2_book)
        client.funding_history = Mock(wraps=client.funding_history)
        snapshot = build_market_state("HYPE", client, candle_count=120,
                                      clock_ms=lambda: NOW, timer=clock)
        self.assertEqual(client.candles.call_count, 1)
        client.l2_book.assert_not_called()
        client.funding_history.assert_not_called()
        self.assertTrue(verify_checksum(snapshot))
        self.assertIn("endpoint_unavailable", self.codes(snapshot))
        self.assertEqual(snapshot["timeframes"]["5m"]["candle_count"], 0)
        self.assertIsNotNone(snapshot["price"]["mark"])

    def test_invalid_capture_deadline(self):
        for deadline in (0, -1, True, float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                build_market_state("HYPE", FakeClient(), deadline_seconds=deadline)

    def test_shared_context_preserves_timestamp_and_does_not_fetch_per_asset(self):
        from src.market_context import MarketContextProvider
        clock=Clock(); client=FakeClient()
        asset,context=client.market_context("HYPE").value
        client.meta_and_asset_contexts=Mock(return_value=CapturedResponse(({'universe':[asset]},[context]),NOW))
        client.market_context=Mock(side_effect=AssertionError("per-asset context forbidden"))
        provider=MarketContextProvider(client, timer=clock,wait=clock.wait,clock_ms=lambda:NOW+int(clock.now*1000))
        provider.get(); clock.now=5
        snapshot=build_market_state("HYPE",client,candle_count=120,context_provider=provider,
                                    clock_ms=lambda:NOW+5000,timer=clock)
        client.meta_and_asset_contexts.assert_called_once()
        client.market_context.assert_not_called()
        self.assertEqual(snapshot["capture"]["sources"]["market_context_and_prices"]["captured_at_utc"],
                         "2033-05-18T03:33:20.000Z")
        self.assertTrue(verify_checksum(snapshot))

    def test_insufficient_capture_budget_rejects_before_any_transport(self):
        from src.hl_client import HyperliquidInfoClient
        from src.rest_policy import RESTConfig, RESTCoordinator
        from test_hl_client import Response
        clock=Clock(); calls=[]
        asset,context=FakeClient().market_context("HYPE").value
        def transport(request,**kwargs):
            calls.append(json.loads(request.data)["type"])
            return Response(json.dumps([{'universe':[asset]},[context]]))
        coordinator=RESTCoordinator(RESTConfig(budget=20),timer=clock,wait=clock.wait)
        client=HyperliquidInfoClient(coordinator=coordinator,transport=transport,clock_ms=lambda:NOW)
        with self.assertRaises(HyperliquidAPIError) as caught:
            build_market_state("HYPE",client,candle_count=120,clock_ms=lambda:NOW,timer=clock)
        self.assertEqual(caught.exception.code,"request_weight_exceeds_budget")
        self.assertEqual(calls,[])

    def test_endpoint_error_is_recorded(self):
        client = FakeClient()
        client.candles = lambda *args: (_ for _ in ()).throw(HyperliquidAPIError("boom"))
        snapshot = build_market_state("HYPE", client, candle_count=120, clock_ms=lambda: NOW)
        self.assertIn("endpoint_unavailable", self.codes(snapshot))


if __name__ == "__main__": unittest.main()
