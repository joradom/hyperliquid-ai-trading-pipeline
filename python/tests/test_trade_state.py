import unittest

from src.hl_client import CapturedResponse
from src.integrity import verify_checksum
from src.trade_state import build_trade_state

NOW = 2_000_000_000_000
USER = "0x" + "1" * 40


def position(symbol="HYPE", size="1", liquidation="50"):
    return {"position": {"coin":symbol, "szi":size, "entryPx":"100", "positionValue":"105", "unrealizedPnl":"5",
        "returnOnEquity":"0.5", "liquidationPx":liquidation, "marginUsed":"10", "leverage":{"type":"cross","value":3}}}


def order(oid, side, kind, size="1", symbol="HYPE"):
    return {"coin":symbol, "oid":oid, "side":side, "sz":size, "origSz":size, "limitPx":"0", "triggerPx":"90" if "Stop" in kind else "120",
            "orderType":kind, "triggerCondition":"below" if "Stop" in kind else "above", "isTrigger":True,
            "isPositionTpsl":True, "reduceOnly":True, "timestamp":NOW-1000}


class FakeClient:
    def __init__(self, positions=None, orders=None, fills=None, funding=None, malformed=False):
        self.positions=positions or []; self.orders=orders or []; self.fills=fills or []; self.funding=funding or []; self.malformed=malformed
    def clearinghouse_state(self,user):
        state={"marginSummary":{"accountValue":"10","totalNtlPos":"0","totalRawUsd":"10","totalMarginUsed":"0"},
               "crossMaintenanceMarginUsed":"0","withdrawable":"10","assetPositions":self.positions,"time":NOW}
        if self.malformed: state["marginSummary"]=[]; state["assetPositions"]=[None]
        return CapturedResponse(state,NOW)
    def frontend_open_orders(self,user): return CapturedResponse(self.orders,NOW)
    def user_fills(self,user,aggregate_by_time=False): return CapturedResponse(self.fills,NOW)
    def user_funding(self,user,start_ms,end_ms): return CapturedResponse(self.funding,NOW)
    def meta_and_asset_contexts(self): return CapturedResponse(({"universe":[{"name":"HYPE"},{"name":"ETH"}]},[{"markPx":"105"},{"markPx":"200"}]),NOW)


class TradeStateTests(unittest.TestCase):
    def build(self,client,symbol=None): return build_trade_state(USER,symbol,client,lambda:NOW)
    def codes(self,s): return [x["code"] for x in s["data_quality"]["issues"]]

    def test_account_without_positions(self):
        s=self.build(FakeClient()); self.assertEqual(s["positions"],[]); self.assertEqual(s["risk"]["total_exposure_notional"],0); self.assertTrue(verify_checksum(s))

    def test_long_and_short_positions(self):
        s=self.build(FakeClient([position(size="1"),position("ETH","-2",None)]))
        self.assertEqual([p["side"] for p in s["positions"]],["long","short"]); self.assertIsNone(s["positions"][1]["liquidation_price"])

    def test_full_stop_and_take_profit(self):
        s=self.build(FakeClient([position()], [order(1,"A","Stop Market"),order(2,"A","Take Profit Market")]))
        p=s["positions"][0]; self.assertTrue(p["protection"]["protection_complete"]); self.assertEqual(p["protection"]["stop_coverage_pct"],100); self.assertIsNotNone(p["risk"]["reward_risk"])

    def test_missing_and_partial_protection(self):
        missing=self.build(FakeClient([position()])); self.assertIn("missing_stop_loss",self.codes(missing)); self.assertFalse(missing["positions"][0]["protection"]["protection_complete"])
        partial=self.build(FakeClient([position()], [order(1,"A","Stop Market","0.5"),order(2,"A","Take Profit Market","0.5")]))
        self.assertEqual(partial["positions"][0]["protection"]["stop_coverage_pct"],50); self.assertIsNone(partial["positions"][0]["risk"]["reward_risk"])

    def test_unrelated_and_unclassifiable_orders(self):
        unrelated=order(3,"A","Stop Market",symbol="ETH")
        limit={**order(4,"A","Mystery Trigger"),"reduceOnly":True}
        s=self.build(FakeClient([position()],[unrelated,limit])); self.assertEqual(s["positions"][0]["protection"]["stop_losses"],[])

    def test_recent_closed_trade_includes_fees_and_funding(self):
        fills=[{"coin":"HYPE","px":"100","sz":"1","side":"A","time":NOW-10000,"startPosition":"0","dir":"Open Short","closedPnl":"0","fee":"0.01","feeToken":"USDC","oid":1,"tid":1},
               {"coin":"HYPE","px":"101","sz":"1","side":"B","time":NOW-1000,"startPosition":"-1","dir":"Close Short","closedPnl":"-1","fee":"0.02","feeToken":"USDC","oid":2,"tid":2}]
        funding=[{"time":NOW-5000,"delta":{"coin":"HYPE","usdc":"-0.001","fundingRate":"0.1"}}]
        trade=self.build(FakeClient(fills=fills,funding=funding))["recent_history"]["closed_trades"][0]
        self.assertEqual(trade["side"],"short"); self.assertAlmostEqual(trade["net_pnl_usdc"],-1.031)

    def test_symbol_filter_and_malformed_response(self):
        s=self.build(FakeClient([position(),position("ETH","1")]),"HYPE"); self.assertEqual([p["symbol"] for p in s["positions"]],["HYPE"])
        malformed=self.build(FakeClient(malformed=True)); self.assertIn("malformed_response",self.codes(malformed)); self.assertIn("malformed_position",self.codes(malformed))


if __name__ == "__main__": unittest.main()
