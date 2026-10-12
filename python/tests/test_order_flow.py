"""Known synthetic aggressor semantics; deterministic event-time vectors."""
import copy
import unittest
from decimal import Decimal

from src.order_flow import Operation, OrderFlow, CALCULATION_VERSION
from src.stream_types import Trade, SIDE_SEMANTICS, SIDE_SEMANTICS_VERSION

START=2_000_000_000_000


def event(tid,seconds,side="B",size="1",price="10",coin="ETH",network="synthetic",**kwargs):
    trade=Trade(coin,side,Decimal(price),Decimal(size),START+int(seconds*1000),tid)
    return Operation("trade",network,coin,START+int(seconds*1000)+2000,trade,**kwargs)


def connected(**config):
    flow=OrderFlow(**config)
    flow.apply(Operation("connect","synthetic","ETH",START))
    return flow


def advance(flow,seconds,coin="ETH",network="synthetic",contact=True):
    stamp=START+int(seconds*1000)
    flow.apply(Operation("advance",network,coin,stamp,contact_ms=stamp if contact else None))
    return flow.snapshot(network,coin)


def legacy_record(network="mainnet", *, connected=True, **config):
    """Synthetic v1 checkpoint shape, never derived from a real capture."""
    record=OrderFlow(**config).export_asset(network,"ETH")
    record["calculation_version"]="1.0"
    record.pop("side_semantics");record.pop("side_semantics_version")
    record["signed"]=network=="synthetic"
    if not record["signed"]:
        for field in ("buy_base","sell_base","buy_quote","sell_quote"):
            record["closed"][field]=None
    if connected:
        record.update(connected=True,segment_id=1,segment_start_ms=START,
                      evaluated_at_ms=START,coverage_through_ms=START,
                      watermark_ms=START-2000,contact_ms=START)
    return record


class OrderFlowTests(unittest.TestCase):
    def test_positive_negative_neutral_delta_and_base_quote_are_distinct(self):
        for buy,sell,delta in ((3,1,"2"),(1,3,"-2"),(2,2,"0")):
            flow=connected()
            flow.apply(event(1,1,size=str(buy)))
            flow.apply(event(2,2,side="A",size=str(sell)))
            view=advance(flow,63)["windows"]["1m"]
            self.assertEqual(view["delta_base"],delta)
            self.assertEqual(Decimal(view["delta_quote"]),Decimal(delta)*10)
            self.assertEqual(view["cvd_window_base"],view["delta_base"])
        flow=connected();flow.apply(event(1,1,size="2",price="10"));flow.apply(event(2,2,side="A",price="20"))
        view=advance(flow,63)["windows"]["1m"]
        self.assertEqual(view["delta_base"],"1");self.assertEqual(view["delta_quote"],"0")
        self.assertEqual(view["buy_sell_ratio_base"],"2")
        self.assertEqual(view["trades"],2)

    def test_event_window_boundary_and_watermark_are_half_open(self):
        flow=connected()
        for tid,second in enumerate((0,60,61)):flow.apply(event(tid,second))
        view=advance(flow,63)
        self.assertEqual(view["cvd"]["base"],"2") # time=61 is at watermark, still pending
        self.assertEqual(view["windows"]["1m"]["from_ms"],START+1000)
        self.assertEqual(view["windows"]["1m"]["delta_base"],"1")
        self.assertEqual(view["cvd"]["pending_trades"],1)

    def test_cvd_window_and_segment_values_and_slope_known_vector(self):
        flow=connected()
        for second in range(960):
            flow.apply(event(second,second))
            advance(flow,second+2)
        view=advance(flow,962)
        self.assertEqual(Decimal(view["cvd"]["base"]),960)
        for label,count in (("1m",60),("5m",300),("15m",900)):
            window=view["windows"][label]
            self.assertEqual(Decimal(window["delta_base"]),count)
            self.assertEqual(Decimal(window["delta_quote"]),count*10)
            self.assertEqual(window["cvd_slope_base_per_second"],"1")
            self.assertEqual(Decimal(window["cvd_slope_quote_per_second"]),10)
            self.assertEqual(window["trades_per_second"],"1")
            self.assertIsNone(window["buy_sell_ratio_base"])
            self.assertIn("zero_sell_denominator",window["quality_reasons"])

    def test_assets_networks_and_documented_aggressor_semantics_are_separate(self):
        flow=connected()
        for network,coin,size in (("mainnet","ETH",2),("testnet","ETH",3),("synthetic","HYPE",4)):
            flow.apply(Operation("connect",network,coin,START))
            flow.apply(event(1,1,coin=coin,network=network,size=str(size)))
            view=advance(flow,63,coin,network)
            self.assertEqual(view["observed_total"]["volume_base"],str(size))
            self.assertEqual(view["side_semantics"],SIDE_SEMANTICS if network!="synthetic" else "verified_synthetic_aggressor")
            self.assertEqual(view["calculation_version"],CALCULATION_VERSION)
            self.assertEqual(view["side_semantics_version"],SIDE_SEMANTICS_VERSION)
            self.assertEqual(view["cvd"]["base"],str(size))
            window=view["windows"]["1m"]
            self.assertTrue(window["signed_available"])
            self.assertEqual(window["buy_volume_base"],str(size))
            self.assertEqual(window["sell_volume_base"],"0")
            self.assertEqual(window["delta_base"],str(size))

    def test_real_ab_volumes_delta_cvd_and_all_windows_reuse_decimal_engine(self):
        for network in ("mainnet","testnet"):
            with self.subTest(network=network):
                flow=OrderFlow()
                flow.apply(Operation("connect",network,"ETH",START))
                for second in range(960):
                    flow.apply(event(2*second,second,network=network,size="0.3",price="10"))
                    flow.apply(event(2*second+1,second,network=network,side="A",size="0.1",price="20"))
                    advance(flow,second+2,network=network)
                view=advance(flow,962,network=network)
                self.assertEqual(Decimal(view["cvd"]["base"]),Decimal("192"))
                self.assertEqual(Decimal(view["cvd"]["quote"]),Decimal("960"))
                for name,count in (("1m",60),("5m",300),("15m",900)):
                    window=view["windows"][name]
                    self.assertTrue(window["ready"])
                    self.assertTrue(window["signed_available"])
                    self.assertEqual(Decimal(window["buy_volume_base"]),Decimal("0.3")*count)
                    self.assertEqual(Decimal(window["sell_volume_base"]),Decimal("0.1")*count)
                    self.assertEqual(Decimal(window["delta_base"]),Decimal("0.2")*count)
                    self.assertEqual(Decimal(window["delta_quote"]),Decimal(count))
                    self.assertEqual(window["cvd_window_base"],window["delta_base"])
                    self.assertEqual(window["cvd_slope_base_per_second"],"0.2")
                    self.assertEqual(window["cvd_slope_quote_per_second"],"1")
                self.assertFalse(view["execution_authorized"])

    def test_real_signed_values_require_coverage_and_real_gaps_reset_cvd(self):
        for network in ("mainnet","testnet"):
            flow=OrderFlow();flow.apply(Operation("connect",network,"ETH",START))
            flow.apply(event(1,1,network=network,size="3"))
            early=advance(flow,30,network=network)
            self.assertFalse(early["windows"]["1m"]["signed_available"])
            self.assertIsNone(early["windows"]["1m"]["delta_base"])
            self.assertEqual(early["cvd"]["base"],"3")
            flow.apply(Operation("gap",network,"ETH",START+31000,reason="ws_continuity_change"))
            flow.apply(event(2,32,network=network,side="A",size="2"))
            view=advance(flow,35,network=network)
            self.assertEqual(view["cvd"]["base"],"-2")
            self.assertEqual(view["cvd"]["segment_id"],2)
            self.assertFalse(view["windows"]["1m"]["ready"])
            self.assertEqual(view["observed_total"]["volume_base"],"5")
            stale=advance(flow,66,network=network,contact=False)
            self.assertFalse(stale["cvd"]["available"])
            self.assertIsNone(stale["cvd"]["base"])

    def test_checkpoint_semantics_and_signed_aggregates_cannot_be_forged(self):
        flow=OrderFlow();flow.apply(Operation("connect","mainnet","ETH",START))
        record=flow.export_asset("mainnet","ETH")
        for mutate in (lambda r:r.pop("side_semantics_version"),
                       lambda r:r.update(side_semantics_version="unknown"),
                       lambda r:r.update(signed=False),
                       lambda r:r["closed"].update(buy_base=None),
                       lambda r:r["closed"].update(buy_base="1"),
                       lambda r:r["closed"].update(base="NaN")):
            bad=copy.deepcopy(record);mutate(bad)
            with self.assertRaises((ValueError,ArithmeticError)):
                OrderFlow().import_asset(bad)

    def test_legacy_unsigned_checkpoint_stays_unsigned_until_new_segment(self):
        flow=OrderFlow();record=legacy_record();flow.import_asset(record)
        self.assertEqual(flow.export_asset("mainnet","ETH"),record)
        flow.apply(event(1,1,network="mainnet",size="3"))
        old=advance(flow,63,network="mainnet")
        self.assertEqual(old["calculation_version"],"1.0")
        self.assertEqual(old["side_semantics"],"unverified")
        self.assertIsNone(old["cvd"]["base"])
        self.assertIsNone(old["windows"]["1m"]["delta_base"])
        flow.apply(Operation("disconnect","mainnet","ETH",START+64000,reason="restart"))
        flow.apply(Operation("connect","mainnet","ETH",START+65000))
        flow.apply(event(2,66,network="mainnet",side="A",size="2"))
        new=advance(flow,128,network="mainnet")
        self.assertEqual(new["calculation_version"],"2.0")
        self.assertEqual(new["cvd"]["base"],"-2")
        self.assertEqual(new["observed_total"]["volume_base"],"5")
        archive=flow.export_asset("mainnet","ETH")["closed_segments"][0]
        self.assertEqual(archive["calculation_version"],"1.0")
        self.assertIsNone(archive["observed"]["buy_base"])
        self.assertEqual(archive["observed"]["base"],"3")

    def test_legacy_synthetic_checkpoint_remains_signed_and_round_trips(self):
        record=legacy_record("synthetic");flow=OrderFlow();flow.import_asset(record)
        self.assertEqual(flow.export_asset("synthetic","ETH"),record)
        flow.apply(event(1,1,size="2"))
        view=advance(flow,63)
        self.assertEqual(view["calculation_version"],"1.0")
        self.assertEqual(view["side_semantics"],"verified_synthetic_aggressor")
        self.assertEqual(view["cvd"]["base"],"2")
        bad=legacy_record();bad["signed"]=True
        with self.assertRaises(ValueError):OrderFlow().import_asset(bad)

    def test_legacy_testnet_can_open_new_signed_segment_without_mixing_history(self):
        flow=OrderFlow();flow.import_asset(legacy_record("testnet"))
        flow.apply(event(1,1,network="testnet",side="A",size="4"))
        advance(flow,63,network="testnet")
        state=flow.export_asset("testnet","ETH")
        self.assertFalse(state["signed"])
        self.assertIsNone(state["closed"]["sell_base"])
        flow.apply(Operation("disconnect","testnet","ETH",START+64000,reason="restart"))
        flow.apply(Operation("connect","testnet","ETH",START+65000))
        flow.apply(event(2,66,network="testnet",size="2"))
        view=advance(flow,69,network="testnet")
        self.assertEqual(view["cvd"]["base"],"2")
        self.assertEqual(view["observed_total"]["volume_base"],"6")
        archived=flow.export_asset("testnet","ETH")["closed_segments"][0]
        self.assertIsNone(archived["observed"]["sell_base"])
        bad=flow.export_asset("testnet","ETH")
        bad["closed_segments"][0]["side_semantics_version"]="1.0"
        with self.assertRaises(ValueError):OrderFlow().import_asset(bad)

    def test_restored_pending_trades_cannot_cross_networks(self):
        flow=OrderFlow();flow.apply(Operation("connect","mainnet","ETH",START))
        flow.apply(event(1,1,network="mainnet"))
        state=flow.export_asset("mainnet","ETH")
        state["pending"][0]["network"]="testnet"
        with self.assertRaises(ValueError):OrderFlow().import_asset(state)

    def test_duplicates_conflicts_no_double_count_and_new_segment(self):
        flow=connected();op=event(1,1)
        flow.apply(op);self.assertEqual(flow.apply(op),"duplicate")
        self.assertEqual(flow.apply(event(1,1,size="2")),"conflict")
        view=flow.snapshot("synthetic","ETH")
        self.assertEqual(view["observed_total"]["unique_trades"],1)
        self.assertEqual(view["counts"]["duplicates"],1)
        self.assertEqual(view["counts"]["conflicts"],1)
        self.assertEqual(view["cvd"]["segment_id"],2)
        self.assertIn("identity_conflict",view["gap_reasons"])

    def test_out_of_order_before_watermark_same_result_late_after_close_is_gap(self):
        first,second=connected(),connected()
        rows=[event(1,1,side="B",size="3"),event(2,2,side="A",size="1")]
        for op in rows:first.apply(op)
        for op in reversed(rows):second.apply(op)
        a,b=advance(first,63),advance(second,63)
        self.assertEqual(a["windows"],b["windows"])
        self.assertEqual(b["counts"]["out_of_order"],1)
        self.assertEqual(second.apply(event(3,10)),"late")
        self.assertEqual(second.asset("synthetic","ETH").segment_id,2)

    def test_warmup_missing_windows_and_quiet_covered_periods(self):
        flow=connected();flow.apply(event(1,1))
        early=advance(flow,40)["windows"]["1m"]
        self.assertFalse(early["ready"]);self.assertIsNone(early["delta_base"])
        self.assertEqual(early["observed_partial"]["volume_base"],"1")
        view=advance(flow,63)
        self.assertTrue(view["windows"]["1m"]["ready"])
        self.assertFalse(view["windows"]["5m"]["ready"])
        empty=advance(flow,130)["windows"]["1m"]
        self.assertEqual(empty["volume_base"],"0")
        self.assertEqual(empty["trades"],0)
        self.assertEqual(empty["exchange_completeness"],"unknown")

    def test_disconnect_reconnect_does_not_join_cvd(self):
        flow=connected();flow.apply(event(1,1,size="5"));advance(flow,10)
        flow.apply(Operation("disconnect","synthetic","ETH",START+11_000))
        self.assertIsNone(flow.snapshot("synthetic","ETH")["cvd"]["base"])
        flow.apply(Operation("connect","synthetic","ETH",START+20_000,source_epoch=2))
        flow.apply(event(2,21,side="A",size="2",source_epoch=2))
        view=advance(flow,30)
        self.assertEqual(view["cvd"]["base"],"-2")
        self.assertEqual(view["cvd"]["segment_id"],2)
        self.assertFalse(view["windows"]["1m"]["ready"])
        self.assertEqual(view["observed_total"]["volume_base"],"7")

    def test_stale_stream_bootstrap_and_old_epoch_are_not_valid_cvd(self):
        flow=connected();flow.apply(event(1,-1,pre_connection=True))
        flow.apply(event(2,1,source_epoch=9))
        view=advance(flow,31,contact=False)
        self.assertFalse(view["connection"]["connected"])
        self.assertIn("stream_stale",view["gap_reasons"])
        self.assertIsNone(view["cvd"]["base"])
        self.assertEqual(view["counts"]["bootstrap_trades"],1)
        self.assertEqual(view["counts"]["outside_active_epoch"],1)

    def test_pending_seen_buckets_and_segments_are_bounded(self):
        flow=connected(max_pending=2,max_seen=4,max_buckets=5)
        for index in range(30):
            flow.apply(event(index,index))
            if index%3==0:advance(flow,index+2)
        view=advance(flow,40)
        self.assertLessEqual(view["memory"]["pending"],2)
        self.assertLessEqual(view["memory"]["seen"],4)
        self.assertLessEqual(view["memory"]["buckets"],5)
        self.assertLessEqual(len(flow.asset("synthetic","ETH").closed_segments),8)
        self.assertIn("history_retention",view["windows"]["1m"]["quality_reasons"])
        self.assertGreater(view["counts"].get("gaps",0),0)

    def test_export_import_including_pending_is_deterministic_and_detached(self):
        flow=connected();flow.apply(event(1,1));state=flow.export_asset("synthetic","ETH")
        restored=OrderFlow();restored.import_asset(state)
        self.assertEqual(flow.export_asset("synthetic","ETH"),restored.export_asset("synthetic","ETH"))
        self.assertEqual(advance(flow,63),advance(restored,63))
        state["counts"]["unique_trades"]=999
        self.assertNotEqual(flow.asset("synthetic","ETH").counts["unique_trades"],999)
        invalid=copy.deepcopy(state);invalid["network"]="mainnet"
        with self.assertRaises(ValueError):restored.import_asset(invalid)

    def test_invalid_operations_and_financial_actions_rejected(self):
        for kind in ("open","close_order","sign","exchange","wallet"):
            with self.assertRaises(ValueError):Operation(kind,"synthetic","ETH",START)
        with self.assertRaises(ValueError):Operation("gap","mainnet","ETH",START,reason="personal-address")
        with self.assertRaises(ValueError):OrderFlow(max_pending=0)
        flow=connected();advance(flow,10)
        with self.assertRaises(ValueError):advance(flow,9)

    def test_view_timestamp_includes_received_events_and_close_boundary(self):
        flow=connected();flow.apply(event(1,1))
        self.assertGreaterEqual(flow.snapshot("synthetic","ETH")["freshness"]["trade_receipt_age_ms"],0)
        with self.assertRaises(ValueError):flow.snapshot("synthetic","ETH",as_of_ms=START+2000)
        flow.apply(Operation("disconnect","synthetic","ETH",START+5000,reason="capture_end"))
        self.assertEqual(flow.snapshot("synthetic","ETH")["as_of_ms"],START+5000)

    def test_decimal_extremes_preserve_small_delta_between_large_flows(self):
        tiny="0."+"1234567890"*6+"12"
        flow=connected()
        flow.apply(event(1,1,size="1e32"))
        flow.apply(event(2,2,size=tiny))
        flow.apply(event(3,3,side="A",size="1e32"))
        view=advance(flow,63)["windows"]["1m"]
        self.assertEqual(Decimal(view["delta_base"]),Decimal(tiny))
