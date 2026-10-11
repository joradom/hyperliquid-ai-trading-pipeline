import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import MagicMock, patch

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
        self.assertEqual(report["status"], "partial_failure")
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

    def test_context_rankings_need_only_one_meta_request(self):
        expected = {"volume": ["BTC", "ETH", "FET"], "oi": ["BTC", "ETH", "FET"],
                    "px_change": ["FET", "ETH", "BTC"], "momentum": ["FET", "ETH", "BTC"]}
        for mode, symbols in expected.items():
            with self.subTest(mode=mode):
                client = FakeScreenerClient()
                client.candles = MagicMock(side_effect=AssertionError("history unnecessary"))
                client.meta_and_asset_contexts = MagicMock(wraps=client.meta_and_asset_contexts)
                report = screen_market(client=client, sort_by=mode, clock_ms=lambda: NOW)
                client.candles.assert_not_called()
                client.meta_and_asset_contexts.assert_called_once()
                self.assertEqual(report["symbols"], symbols)
                self.assertEqual(report["status"], "ok")
                for row in report["results"]:
                    self.assertEqual(row["history_status"], "not_requested")
                    self.assertEqual(row["volume_today_usd"], row["volume_24h_usd"])
                    self.assertIsNone(row["volume_prev_24h_usd"])
                    self.assertIsNone(row["vol_delta_pct_24h"])
                    self.assertIsNone(row["error"])

    def test_delta_history_queries_pool_before_limit(self):
        client = FakeScreenerClient()
        client.candles = MagicMock(wraps=client.candles)
        report = screen_market(client=client, top_n=3, limit=1, clock_ms=lambda: NOW)
        self.assertEqual(client.candles.call_count, 3)
        for call in client.candles.call_args_list:
            self.assertEqual(call.args[1:], ("1h", NOW - 49 * 3600_000, NOW))
        self.assertEqual(report["symbols"], ["FET"])
        self.assertEqual(report["results"][0]["history_status"], "available")

    def test_errors_survive_limit(self):
        report = screen_market(client=FakeScreenerClient(error_symbol="ETH"), limit=1, clock_ms=lambda: NOW)
        self.assertEqual(report["status"], "partial_failure")
        self.assertEqual(report["symbols"], ["FET"])
        self.assertIsNone(report["results"][0]["error"])
        self.assertEqual([e["symbol"] for e in report["errors"]], ["ETH"])

    def test_all_history_failures_keep_context_results(self):
        client = FakeScreenerClient()
        client.candles = MagicMock(side_effect=HyperliquidAPIError("HTTP 429: synthetic"))
        report = screen_market(client=client, clock_ms=lambda: NOW)
        self.assertEqual(report["status"], "partial_failure")
        self.assertEqual(len(report["errors"]), 3)
        self.assertEqual(report["symbols"], ["BTC", "ETH", "FET"])
        for row in report["results"]:
            self.assertEqual(row["history_status"], "error")
            self.assertGreater(row["volume_24h_usd"], 0)
            self.assertIsNone(row["vol_delta_pct_24h"])

    def test_absent_history_differs_from_malformed_response(self):
        report = screen_market(client=FakeScreenerClient(candles_by_symbol={"FET": []}), clock_ms=lambda: NOW)
        row = next(r for r in report["results"] if r["symbol"] == "FET")
        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["errors"], [])
        self.assertEqual(row["history_status"], "insufficient")
        self.assertIsNone(row["error"])
        client = FakeScreenerClient()
        client.candles = MagicMock(return_value=CapturedResponse({}, NOW))
        report = screen_market(client=client, clock_ms=lambda: NOW)
        self.assertEqual(report["status"], "partial_failure")
        self.assertEqual(len(report["errors"]), 3)

    def test_worker_validation_precedes_network(self):
        for workers in (0, -1, True, False, 1.5, "2", None):
            with self.subTest(workers=workers):
                client = MagicMock()
                with self.assertRaisesRegex(ValueError, "max_workers"):
                    screen_market(client=client, max_workers=workers, sort_by="volume")
                client.meta_and_asset_contexts.assert_not_called()
        report = screen_market(client=FakeScreenerClient(), max_workers=1, clock_ms=lambda: NOW)
        self.assertEqual(report["parameters"]["max_workers"], 1)

    def test_partial_cli_preserves_stdout_and_warns_on_stderr(self):
        report = screen_market(client=FakeScreenerClient(error_symbol="ETH"), limit=1, clock_ms=lambda: NOW)
        for flags in (["--json"], ["--symbols-only"], []):
            with self.subTest(flags=flags), patch("src.market_screener.screen_market", return_value=report) as screen:
                stdout, stderr = io.StringIO(), io.StringIO()
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    code = main([*flags, "--max-workers", "2"])
                self.assertEqual(code, 1)
                self.assertIn("ETH", stderr.getvalue())
                self.assertIn("partial", stderr.getvalue())
                self.assertEqual(screen.call_args.kwargs["max_workers"], 2)
                if flags == ["--json"]:
                    self.assertEqual(json.loads(stdout.getvalue()), report)
                elif flags == ["--symbols-only"]:
                    self.assertEqual(stdout.getvalue(), "FET\n")
                else:
                    self.assertIn("SYMBOL", stdout.getvalue())

    def test_cli_invalid_workers_never_constructs_client(self):
        with patch("src.market_screener.HyperliquidInfoClient") as client, redirect_stderr(io.StringIO()):
            self.assertEqual(main(["--max-workers", "0"]), 1)
        client.assert_not_called()

    def test_screener_waits_for_recoverable_budget_before_http_deadline(self):
        from src.hl_client import HyperliquidInfoClient
        from src.rest_policy import RESTConfig, RESTCoordinator
        from test_rest_policy import Clock
        from test_hl_client import Response
        clock=Clock(); calls=[]; fake=FakeScreenerClient()
        coordinator=RESTCoordinator(RESTConfig(budget=80),timer=clock,wait=clock.wait)
        def transport(request,**kwargs):
            payload=json.loads(request.data); calls.append((payload['type'],kwargs['timeout']))
            value=fake.meta_and_asset_contexts().value if payload['type']=='metaAndAssetCtxs' else fake.candles(
                payload['req']['coin'],'1h',0,NOW).value
            return Response(json.dumps(value))
        client=HyperliquidInfoClient(coordinator=coordinator,transport=transport,clock_ms=lambda:NOW+int(clock.now*1000))
        report=screen_market(client=client,max_workers=1,clock_ms=lambda:NOW+int(clock.now*1000))
        self.assertEqual(clock.now,60)
        self.assertEqual(report['status'],'ok')
        self.assertEqual(report['errors'],[])
        self.assertTrue(all(row['vol_delta_pct_24h'] is not None for row in report['results']))
        self.assertEqual(len(calls),4)
        self.assertEqual(calls[-1][1],20)

    def test_screener_bound_reports_unsent_metrics_even_after_limit(self):
        from src.hl_client import HyperliquidInfoClient
        from src.rest_policy import RESTConfig, RESTCoordinator
        from test_rest_policy import Clock
        from test_hl_client import Response
        clock=Clock(); fake=FakeScreenerClient(); calls=[]
        coordinator=RESTCoordinator(RESTConfig(budget=80,admission_wait_seconds=20),timer=clock,wait=clock.wait)
        def transport(request,**kwargs):
            payload=json.loads(request.data); calls.append(payload['type'])
            value=fake.meta_and_asset_contexts().value if payload['type']=='metaAndAssetCtxs' else fake.candles(payload['req']['coin'],'1h',0,NOW).value
            return Response(json.dumps(value))
        client=HyperliquidInfoClient(coordinator=coordinator,transport=transport,clock_ms=lambda:NOW)
        report=screen_market(client=client,max_workers=1,limit=1,clock_ms=lambda:NOW)
        self.assertEqual(report['status'],'partial_failure')
        self.assertEqual(report['screened_count'],3)
        self.assertEqual(report['returned_count'],1)
        self.assertEqual(report['errors'][0]['symbol'],'FET')
        self.assertIn('admission_wait_exceeded',report['errors'][0]['message'])
        self.assertEqual(len(calls),3)
        self.assertLessEqual(clock.now,20)

    def test_initial_context_waits_before_request_when_previous_window_is_full(self):
        from src.hl_client import HyperliquidInfoClient
        from src.rest_policy import RESTConfig, RESTCoordinator
        from test_rest_policy import Clock
        from test_hl_client import Response
        clock=Clock(); fake=FakeScreenerClient(); calls=[]
        coordinator=RESTCoordinator(RESTConfig(budget=80),timer=clock,wait=clock.wait)
        ticket=coordinator.acquire(80,10); coordinator.release(ticket)
        def transport(request,**kwargs):
            payload=json.loads(request.data); calls.append((payload['type'],clock.now,kwargs['timeout']))
            value=fake.meta_and_asset_contexts().value if payload['type']=='metaAndAssetCtxs' else fake.candles(payload['req']['coin'],'1h',0,NOW).value
            return Response(json.dumps(value))
        client=HyperliquidInfoClient(coordinator=coordinator,transport=transport,clock_ms=lambda:NOW+int(clock.now*1000))
        report=screen_market(client=client,max_workers=1,clock_ms=lambda:NOW+int(clock.now*1000))
        self.assertEqual(report['status'],'ok')
        self.assertEqual(report['errors'],[])
        self.assertEqual(calls[0],('metaAndAssetCtxs',60,20))
        self.assertEqual(clock.now,120)
        self.assertEqual(len(calls),4)

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
