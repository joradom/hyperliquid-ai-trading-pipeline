import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import MagicMock

from src.hl_client import CapturedResponse, HyperliquidAPIError
from src.market_screener import format_table, main, screen_market

NOW = 2_000_000_000_000


class FakeScreenerClient:
    def __init__(self, assets=None, candles_by_symbol=None, error_symbol=None):
        self.assets = assets or [
            {
                "symbol": "BTC",
                "mark": "60000.0",
                "prevDayPx": "59000.0",
                "dayNtlVlm": "100000000.0",
                "openInterest": "5000.0",
                "funding": "0.00001",
            },
            {
                "symbol": "ETH",
                "mark": "3000.0",
                "prevDayPx": "3100.0",
                "dayNtlVlm": "50000000.0",
                "openInterest": "20000.0",
                "funding": "0.00002",
            },
            {
                "symbol": "FET",
                "mark": "1.5",
                "prevDayPx": "1.2",
                "dayNtlVlm": "15000000.0",
                "openInterest": "10000000.0",
                "funding": "0.00005",
            },
            {
                "symbol": "TINY",
                "mark": "0.01",
                "prevDayPx": "0.01",
                "dayNtlVlm": "5000.0",  # below 1M min_volume
                "openInterest": "1000.0",
                "funding": "0.0",
            },
        ]
        self.candles_by_symbol = candles_by_symbol or {}
        self.error_symbol = error_symbol

    def meta_and_asset_contexts(self):
        universe = [{"name": a["symbol"]} for a in self.assets]
        contexts = [
            {
                "markPx": a["mark"],
                "prevDayPx": a["prevDayPx"],
                "dayNtlVlm": a["dayNtlVlm"],
                "openInterest": a["openInterest"],
                "funding": a["funding"],
            }
            for a in self.assets
        ]
        return CapturedResponse((({"universe": universe}), contexts), NOW)

    def candles(self, symbol, interval, start_ms, end_ms):
        if symbol == self.error_symbol:
            raise HyperliquidAPIError(f"HTTP 500: candle error for {symbol}")
        if symbol in self.candles_by_symbol:
            return CapturedResponse(self.candles_by_symbol[symbol], NOW)
        # Default: 48 hourly candles where recent 24h has double volume for FET
        mult = 2.0 if symbol == "FET" else 1.0
        candles = []
        for i in range(48):
            t = NOW - (48 - i) * 3600_000
            v = 100.0 * (mult if i >= 24 else 1.0)
            candles.append({
                "t": t,
                "T": t + 3600_000 - 1,
                "o": "10",
                "h": "11",
                "l": "9",
                "c": "10",
                "v": str(v),
            })
        return CapturedResponse(candles, NOW)


class MarketScreenerTests(unittest.TestCase):
    def test_screen_market_filters_low_volume_and_calculates_delta(self):
        client = FakeScreenerClient()
        report = screen_market(
            client=client,
            top_n=10,
            limit=5,
            sort_by="vol_delta",
            min_volume=1_000_000.0,
            clock_ms=lambda: NOW,
        )

        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["kind"], "market_screener")
        self.assertEqual(report["schema_version"], "1.0")
        self.assertEqual(report["screened_count"], 3)  # TINY filtered out
        self.assertEqual(report["returned_count"], 3)

        # FET had 2x volume in last 24h, so vol_delta_pct_24h should be +100%
        fet = next(r for r in report["results"] if r["symbol"] == "FET")
        self.assertEqual(fet["vol_delta_pct_24h"], 100.0)
        self.assertEqual(fet["price_change_pct_24h"], 25.0)
        self.assertEqual(fet["volume_prev_24h_usd"], 24000.0)
        self.assertEqual(fet["volume_today_usd"], 48000.0)

        # FET should be #1 because sort_by is vol_delta
        self.assertEqual(report["symbols"][0], "FET")

    def test_sort_by_volume(self):
        client = FakeScreenerClient()
        report = screen_market(
            client=client,
            top_n=10,
            limit=5,
            sort_by="volume",
            clock_ms=lambda: NOW,
        )
        self.assertEqual(report["symbols"], ["BTC", "ETH", "FET"])

    def test_sort_by_oi(self):
        client = FakeScreenerClient()
        report = screen_market(
            client=client,
            top_n=10,
            limit=5,
            sort_by="oi",
            clock_ms=lambda: NOW,
        )
        # BTC: 5000 * 60000 = 300,000,000
        # ETH: 20000 * 3000 = 60,000,000
        # FET: 10000000 * 1.5 = 15,000,000
        self.assertEqual(report["symbols"], ["BTC", "ETH", "FET"])

    def test_sort_by_px_change(self):
        client = FakeScreenerClient()
        report = screen_market(
            client=client,
            top_n=10,
            limit=5,
            sort_by="px_change",
            clock_ms=lambda: NOW,
        )
        # FET: 25.0%, ETH: -3.23%, BTC: 1.69%
        self.assertEqual(report["symbols"][0], "FET")

    def test_candle_error_isolation(self):
        client = FakeScreenerClient(error_symbol="ETH")
        report = screen_market(
            client=client,
            top_n=10,
            limit=5,
            clock_ms=lambda: NOW,
        )
        self.assertEqual(report["status"], "ok")
        eth = next(r for r in report["results"] if r["symbol"] == "ETH")
        self.assertIsNotNone(eth["error"])
        self.assertIn("HyperliquidAPIError", eth["error"])
        self.assertIsNone(eth["vol_delta_pct_24h"])

        fet = next(r for r in report["results"] if r["symbol"] == "FET")
        self.assertIsNone(fet["error"])
        self.assertEqual(fet["vol_delta_pct_24h"], 100.0)

    def test_invalid_parameters_raise(self):
        with self.assertRaises(ValueError):
            screen_market(top_n=0)
        with self.assertRaises(ValueError):
            screen_market(limit=0)
        with self.assertRaises(ValueError):
            screen_market(sort_by="invalid_mode")

    def test_partial_candles(self):
        # 30 candles (more than 24, less than 48)
        candles = []
        for i in range(30):
            t = NOW - (30 - i) * 3600_000
            candles.append({"t": t, "T": t + 3600_000 - 1, "c": "10", "v": "100"})
        client = FakeScreenerClient(candles_by_symbol={"FET": candles})
        report = screen_market(client=client, top_n=10, limit=5, clock_ms=lambda: NOW)
        fet = next(r for r in report["results"] if r["symbol"] == "FET")
        self.assertIsNone(fet["volume_prev_24h_usd"])
        self.assertIsNone(fet["vol_delta_pct_24h"])

    def test_format_table(self):
        client = FakeScreenerClient()
        report = screen_market(client=client, top_n=10, limit=5, clock_ms=lambda: NOW)
        table = format_table(report)
        self.assertIn("SYMBOL", table)
        self.assertIn("VOL DELTA (%)", table)
        self.assertIn("FET", table)

    def test_main_cli_symbols_only(self):
        with unittest.mock.patch("src.market_screener.screen_market") as mock_screen:
            mock_screen.return_value = {
                "symbols": ["FET", "ADA", "NIL"],
                "results": [],
            }
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                code = main(["--symbols-only"])
            self.assertEqual(code, 0)
            self.assertEqual(stdout.getvalue().strip(), "FET ADA NIL")

    def test_main_cli_json(self):
        with unittest.mock.patch("src.market_screener.screen_market") as mock_screen:
            mock_screen.return_value = {
                "kind": "market_screener",
                "symbols": ["FET"],
                "results": [],
            }
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                code = main(["--json"])
            self.assertEqual(code, 0)
            parsed = json.loads(stdout.getvalue().strip())
            self.assertEqual(parsed["kind"], "market_screener")
            self.assertEqual(parsed["symbols"], ["FET"])


if __name__ == "__main__":
    unittest.main()
