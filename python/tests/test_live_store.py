"""Immutable flow/legacy book views and additional public L2 analytics, offline."""
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from src.flow_store import FlowStore
from src.live_store import LiveStore, recovery_check, replay
from src.order_flow import OrderFlow, Operation
from src.stream_types import Subscription
from src.ws_client import PublicWSClient
from test_order_flow import START, event
from test_stream_types import ack, book, packet, trade


class LiveStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.journal=FlowStore(Path(self.temp.name)/"flow.sqlite3");self.addCleanup(self.journal.close)
    def fake(self,network="mainnet"):
        client=PublicWSClient(("ETH","HYPE"),clock_ms=lambda:START+1000,network=network)
        client.connection_epoch=1;client.connected_at_ms=START
        client.pending={sub:100 for sub in client.desired}
        for sub in client.desired:client.handle_message(ack(sub))
        return client

    def l2_live(self):
        client=self.fake();live=LiveStore(self.journal)
        client.handle_message(packet("l2Book",book("ETH",time_ms=START+900,bid="99.99",ask="100.01")))
        live.sync_ws(client,now_ms=START+1000)
        return client,live

    def test_additional_l2_view_keeps_read_contract_and_is_deeply_immutable(self):
        client,live=self.l2_live()
        legacy=live.read("mainnet","ETH").to_dict()
        self.assertEqual(legacy["l2_book"],{"time_ms":START+900,
            "bids":[{"price":"99.99","size":"2","n":1}],
            "asks":[{"price":"100.01","size":"3","n":2}]})
        metrics=live.read_l2("mainnet","ETH",as_of_ms=START+1000)
        self.assertEqual(live.read("mainnet","ETH").to_dict(),legacy)
        self.assertNotIn("metrics",legacy)
        self.assertTrue(metrics["quality"]["usable"])
        self.assertEqual(metrics["source"]["received_at_ms"],START+1000)
        self.assertEqual(metrics["freshness"]["exchange"]["age_ms"],100)
        self.assertEqual(metrics["freshness"]["receipt"]["age_ms"],0)
        with self.assertRaises(TypeError):metrics["metrics"]["bbo"]["midpoint"]="999"
        with self.assertRaises(TypeError):metrics["metrics"]["depth"]["bids"]["levels"][0]["size_base"]="999"
        detached=metrics.to_dict();detached["metrics"]["bbo"]["midpoint"]="999"
        self.assertEqual(metrics["metrics"]["bbo"]["midpoint"],"1e2")
        client.clock_ms=lambda:START+1100
        client.handle_message(packet("l2Book",book("ETH",time_ms=START+1100,bid="100",ask="100.1")))
        live.sync_ws(client,now_ms=START+1100)
        self.assertEqual(metrics["source"]["book_time_ms"],START+900)
        self.assertEqual(live.read_l2("mainnet","ETH",as_of_ms=START+1100)["source"]["book_time_ms"],START+1100)

    def test_recent_pong_and_trade_do_not_refresh_stale_l2(self):
        client,live=self.l2_live()
        client.clock_ms=lambda:START+17000
        client._awaiting_pong=client.monotonic()+10
        client.handle_message('{"channel":"pong"}')
        client.handle_message(packet("trades",[trade("ETH",time_ms=START+17000)]))
        live.sync_ws(client,now_ms=START+17000)
        self.assertEqual(client.state,"live")
        metrics=live.read_l2("mainnet","ETH",as_of_ms=START+17000)
        self.assertIn("book_exchange_stale",metrics["quality"]["reasons"])
        self.assertEqual(metrics["source"]["received_at_ms"],START+1000)
        self.assertIsNotNone(metrics["metrics"]["obi_legacy"]["observed"])
        self.assertIsNone(metrics["metrics"]["obi_legacy"]["valid"])

    def test_old_duplicate_and_conflicting_books_do_not_renew_receipt(self):
        client,live=self.l2_live()
        client.clock_ms=lambda:START+1200
        for value in (book("ETH",time_ms=START+800),
                      book("ETH",time_ms=START+900,bid="99.99",ask="100.01"),
                      book("ETH",time_ms=START+900,bid="99.98",ask="100.01")):
            client.handle_message(packet("l2Book",value))
        live.sync_ws(client,now_ms=START+1200)
        metrics=live.read_l2("mainnet","ETH",as_of_ms=START+1200)
        self.assertEqual(metrics["source"]["received_at_ms"],START+1000)
        self.assertEqual(metrics["source"]["book_time_ms"],START+900)
        self.assertEqual(metrics["source"]["connection_epoch"],1)
        self.assertEqual(metrics["metrics"]["bbo"]["best_bid"],"99.99")
        self.assertIn("book_timestamp_conflict",metrics["quality"]["reasons"])
        self.assertIsNone(metrics["metrics"]["obi_legacy"]["valid"])
        client.clock_ms=lambda:START+1300
        client.handle_message(packet("l2Book",book("ETH",time_ms=START+1300,bid="99.99",ask="100.01")))
        live.sync_ws(client,now_ms=START+1300)
        self.assertTrue(live.read_l2("mainnet","ETH",as_of_ms=START+1300)["quality"]["usable"])
        self.assertIn("book_timestamp_conflict",client.gap_reasons["ETH"])

    def test_reconnection_requires_book_from_new_epoch_but_not_tape_continuity(self):
        client,live=self.l2_live()
        client.connection_epoch=2
        live.sync_ws(client,now_ms=START+1100)
        self.assertIn("book_epoch_mismatch",live.read_l2("mainnet","ETH",as_of_ms=START+1100)["quality"]["reasons"])
        # The real transport clears these maps on establishing a new socket.
        client.books.clear();client.last_book_received_ms.clear();client.book_epochs.clear();client.book_conflicts.clear()
        live.sync_ws(client,now_ms=START+1200)
        self.assertIn("missing_book",live.read_l2("mainnet","ETH",as_of_ms=START+1200)["quality"]["reasons"])
        client.clock_ms=lambda:START+1300
        client.handle_message(packet("l2Book",book("ETH",time_ms=START+1300,bid="99.99",ask="100.01")))
        live.sync_ws(client,now_ms=START+1300)
        metrics=live.read_l2("mainnet","ETH",as_of_ms=START+1300)
        self.assertTrue(metrics["quality"]["usable"])
        self.assertEqual(metrics["source"]["connection_epoch"],2)
        self.assertIsNone(live.read("mainnet","ETH")["cvd"]["base"])
        self.assertFalse(live.read("mainnet","ETH")["windows"]["1m"]["ready"])
        recovered=LiveStore(self.journal)
        recovered_metrics=recovered.read_l2("mainnet","ETH",as_of_ms=START+1300)
        self.assertIn("missing_book",recovered_metrics["quality"]["reasons"])

    def test_l2_default_clock_is_independent_of_flow_and_missing_asset_is_explicit(self):
        _,live=self.l2_live()
        with patch("src.live_store.time.time_ns",return_value=(START+17000)*1_000_000):
            metrics=live.read_l2("mainnet","ETH")
        self.assertEqual(metrics["as_of_ms"],START+17000)
        self.assertFalse(metrics["quality"]["usable"])
        self.assertEqual(live.read("mainnet","ETH")["as_of_ms"],START+1000)
        with self.assertRaises(KeyError):live.read_l2("mainnet","BTC",as_of_ms=START+1000)

    def test_failed_journal_write_does_not_publish_new_book_metrics(self):
        client,live=self.l2_live()
        previous=live.read_l2("mainnet","ETH",as_of_ms=START+1100).to_dict()
        client.clock_ms=lambda:START+2000
        client.handle_message(packet("l2Book",book("ETH",time_ms=START+2000,bid="100",ask="100.1")))
        with patch.object(self.journal,"write",side_effect=RuntimeError("failure")),self.assertRaises(RuntimeError):
            live.sync_ws(client,now_ms=START+2000)
        self.assertEqual(live.read_l2("mainnet","ETH",as_of_ms=START+1100).to_dict(),previous)


    def test_deep_immutability_and_snapshot_does_not_track_future_changes(self):
        flow=OrderFlow();live=LiveStore(self.journal,flow)
        live.write([Operation("connect","synthetic","ETH",START),event(1,1)])
        before=live.read("synthetic","ETH")
        with self.assertRaises(TypeError):before["observed_total"]["volume_base"]="999"
        with self.assertRaises(TypeError):before["windows"]["1m"]["quality_reasons"][0]="999"
        detached=before.to_dict();detached["observed_total"]["volume_base"]="888"
        live.write([event(2,2)])
        self.assertEqual(before["observed_total"]["volume_base"],"1")
        self.assertEqual(live.read("synthetic","ETH")["observed_total"]["volume_base"],"2")

    def test_adapter_reuses_trade_queue_and_latest_book_without_calculations(self):
        client=self.fake();live=LiveStore(self.journal)
        live.sync_ws(client,now_ms=START+1000)
        client.handle_message(packet("l2Book",book("ETH",time_ms=START+900)))
        client.handle_message(packet("trades",[trade("ETH",time_ms=START+1000)]))
        live.sync_ws(client,now_ms=START+2000)
        view=live.read("mainnet","ETH")
        self.assertEqual(view["observed_total"]["unique_trades"],1)
        self.assertEqual(view["l2_book"]["bids"][0]["size"],"2")
        self.assertNotIn("imbalance",view["l2_book"])
        self.assertIsNone(view["cvd"]["base"])
        self.assertEqual(len(client.trade_queue),0)
        self.assertTrue(recovery_check(self.journal,live.engine)["state_matches"])

    def test_transport_epoch_change_disconnect_and_restart_open_new_segments(self):
        client=self.fake();live=LiveStore(self.journal)
        live.sync_ws(client,now_ms=START+1000)
        first=live.read("mainnet","ETH")["cvd"]["segment_id"]
        client.continuity_epoch["ETH"]+=1
        live.sync_ws(client,now_ms=START+1100)
        self.assertEqual(live.read("mainnet","ETH")["cvd"]["segment_id"],first+1)
        recovered=LiveStore(self.journal)
        self.assertEqual(recovered.transport_state,"recovered_not_live")
        view=recovered.read("mainnet","ETH")
        self.assertFalse(view["connection"]["connected"])
        self.assertFalse(view["windows"]["1m"]["ready"])
        self.assertIsNone(view["l2_book"])
        recovered.sync_ws(client,now_ms=START+2000)
        self.assertIn("restart",recovered.read("mainnet","ETH")["gap_reasons"])

    def test_accelerated_replay_exact_counts_recovery_and_memory_limits(self):
        result=replay(1024,Path(self.temp.name)/"replay.sqlite3")
        self.assertEqual(result["input_trades"],1024)
        self.assertTrue(result["recovery"]["state_matches"])
        self.assertEqual(sum(view["observed_total"]["unique_trades"] for view in result["assets"].values()),1024)
        for view in result["assets"].values():
            self.assertLessEqual(view["memory"]["seen"],512)
            self.assertFalse(view["execution_authorized"])

    def test_consumer_failure_restores_ws_queue_and_does_not_publish_progress(self):
        client=self.fake();live=LiveStore(self.journal)
        live.sync_ws(client,now_ms=START+1000)
        client.handle_message(packet("trades",[trade("ETH",time_ms=START+1000)]))
        before=len(client.trade_queue);bytes_before=client.queue_size_bytes
        with patch.object(self.journal,"write",side_effect=RuntimeError("failure")),self.assertRaises(RuntimeError):
            live.sync_ws(client,now_ms=START+2000)
        self.assertEqual(len(client.trade_queue),before)
        self.assertEqual(client.queue_size_bytes,bytes_before)
        self.assertEqual(live.read("mainnet","ETH")["observed_total"]["unique_trades"],0)
        live.sync_ws(client,now_ms=START+2000)
        self.assertEqual(live.read("mainnet","ETH")["observed_total"]["unique_trades"],1)

    def timed_live(self, *, cache_capacity=100_000):
        clock = [1]
        client = self.fake()
        client.clock_ms = lambda: START+clock[0]*1000
        client.monotonic = lambda: float(clock[0])
        client.dedup_capacity = cache_capacity
        live = LiveStore(self.journal, OrderFlow(max_seen=2))
        live.sync_ws(client, now_ms=client.clock_ms())
        self.assertTrue(client.durable_dedup)
        return client, live, clock

    def send_at(self, client, live, clock, seconds, rows):
        clock[0] = seconds
        self.assertTrue(client.handle_message(packet("trades", rows)))
        live.sync_ws(client, now_ms=client.clock_ms())

    def test_thirty_minute_continuity_survives_default_ttl_and_recovery(self):
        client, live, clock = self.timed_live()
        first = live.read("mainnet", "ETH").to_dict()
        # Every 10s supplies stream contact; no simulated disconnect/stale gap.
        for tid, seconds in enumerate(range(1, 1802, 10), 1):
            self.send_at(client, live, clock, seconds,
                         [trade(coin, tid=tid, time_ms=START+seconds*1000)
                          for coin in client.symbols])
        clock[0] = 1804
        self.assertTrue(client.handle_message(packet("l2Book", book(time_ms=client.clock_ms()))))
        live.sync_ws(client, now_ms=client.clock_ms())
        self.assertGreater(client.counts["dedup_evictions"], 0)
        self.assertEqual(client.continuity_epoch, {"ETH": 0, "HYPE": 0})
        for coin in client.symbols:
            view = live.read("mainnet", coin).to_dict()
            self.assertEqual(view["cvd"]["segment_id"], 1)
            self.assertEqual(view["observed_total"]["unique_trades"], 181)
            self.assertEqual(view["counts"].get("pending_abandoned", 0), 0)
            self.assertEqual(view["counts"]["closed_trades"], 181)
            self.assertEqual(view["gap_reasons"], {})
            self.assertEqual(view["memory"]["pending"], 0)
            self.assertEqual(view["side_semantics"], "documented_hyperliquid_aggressor")
            self.assertEqual(view["cvd"]["base"], "181")
            for window in view["windows"].values():
                self.assertTrue(window["ready"])
                self.assertTrue(window["signed_available"])
                self.assertEqual(window["delta_base"], window["buy_volume_base"])
            self.assertEqual(set(first), set(view))
        self.assertTrue(recovery_check(self.journal, live.engine)["state_matches"])
        state = {coin: live.engine.export_asset("mainnet", coin, include_seen=False)
                 for coin in client.symbols}
        self.journal.close()
        with FlowStore(Path(self.temp.name)/"flow.sqlite3") as reopened:
            recovered = reopened.recover()
            for coin in client.symbols:
                self.assertEqual(recovered.export_asset("mainnet", coin, include_seen=False), state[coin])

    def test_sqlite_deduplicates_old_replay_after_ttl_without_segment_reset(self):
        client, live, clock = self.timed_live()
        original = trade(tid=1, time_ms=START+1000)
        for seconds, row in ((1, original), (11, trade(tid=2, time_ms=START+11000)),
                             (1202, trade(tid=3, time_ms=START+1202000))):
            self.send_at(client, live, clock, seconds, [row])
        self.assertNotIn(("mainnet", START+1000, "ETH", 1), client.seen)
        self.assertNotIn((START+1000, 1), live.engine.asset("mainnet", "ETH").seen)
        before = live.read("mainnet", "ETH").to_dict()
        self.send_at(client, live, clock, 1203, [original])
        after = live.read("mainnet", "ETH").to_dict()
        self.assertEqual(after["counts"]["duplicates"], 1)
        self.assertEqual(after["observed_total"], before["observed_total"])
        self.assertEqual(after["cvd"]["segment_id"], before["cvd"]["segment_id"])
        self.assertEqual(after["gap_reasons"], {})
        self.assertEqual(client.continuity_epoch["ETH"], 0)
        self.assertEqual(self.journal.db.execute("SELECT count(*) FROM journal WHERE kind='trade'").fetchone()[0], 3)
        self.assertTrue(recovery_check(self.journal, live.engine)["state_matches"])

    def test_recovered_sqlite_handles_evicted_duplicates_and_side_conflicts(self):
        client, live, clock = self.timed_live(cache_capacity=1)
        original = trade(tid=1, time_ms=START+1000)
        for seconds, row in ((1, original), (11, trade(tid=2, time_ms=START+11000)),
                             (1202, trade(tid=3, time_ms=START+1202000))):
            self.send_at(client, live, clock, seconds, [row])
        self.assertEqual(client.continuity_epoch["ETH"], 0)
        self.assertEqual(live.read("mainnet", "ETH")["cvd"]["segment_id"], 1)
        self.journal.close()
        with FlowStore(Path(self.temp.name)/"flow.sqlite3") as reopened:
            recovered = LiveStore(reopened)
            client.seen.clear()  # A restored WS has no RAM identity cache.
            self.send_at(client, recovered, clock, 1203, [original])
            view = recovered.read("mainnet", "ETH")
            self.assertEqual(view["observed_total"]["unique_trades"], 3)
            self.assertEqual(view["counts"]["duplicates"], 1)
            self.assertEqual(view["gap_reasons"], {"restart": 1})
            segment = view["cvd"]["segment_id"]
            # Expire the replay's RAM ID before a conflicting side arrives.
            self.send_at(client, recovered, clock, 2404,
                         [trade(tid=1, time_ms=START+1000, side="A")])
            view = recovered.read("mainnet", "ETH")
            self.assertEqual(view["counts"]["conflicts"], 1)
            self.assertEqual(view["cvd"]["segment_id"], segment+1)
            self.assertEqual(view["gap_reasons"], {"restart": 1, "identity_conflict": 1})
            self.assertEqual(view["observed_total"]["unique_trades"], 3)
            self.assertEqual(reopened.verify()["integrity"], "verified")

    def test_durable_consumer_keeps_real_late_and_retention_gaps(self):
        client, live, clock = self.timed_live(cache_capacity=1)
        self.send_at(client, live, clock, 1, [trade(tid=1, time_ms=START+1000)])
        self.send_at(client, live, clock, 11, [trade(tid=2, time_ms=START+11000)])
        self.send_at(client, live, clock, 21, [trade(tid=3, time_ms=START+21000)])
        self.send_at(client, live, clock, 22, [trade(tid=4, time_ms=START+5000)])
        view = live.read("mainnet", "ETH")
        self.assertEqual(view["counts"]["late_trades"], 1)
        self.assertEqual(view["gap_reasons"], {"late_trade": 1})
        self.assertEqual(view["cvd"]["segment_id"], 2)
        # Prune identities inside the current segment, then resend an old ID.
        self.journal.max_rows = 6
        for seconds in (31, 41, 51, 61):
            self.send_at(client, live, clock, seconds,
                         [trade(tid=seconds, time_ms=START+seconds*1000)])
        floor = live.engine.asset("mainnet", "ETH").retention_floor_ms
        self.assertGreaterEqual(floor, START+31000)
        before = live.read("mainnet", "ETH").to_dict()["observed_total"]
        self.send_at(client, live, clock, 62, [trade(tid=31, time_ms=START+31000)])
        view = live.read("mainnet", "ETH")
        self.assertEqual(view["counts"]["outside_retention"], 1)
        self.assertEqual(view["observed_total"], before)
        self.assertEqual(view["gap_reasons"]["late_trade"], 2)
        self.assertTrue(recovery_check(self.journal, live.engine)["state_matches"])

    def test_live_signed_mainnet_and_testnet_reuse_engine_without_l2_side_inference(self):
        live=LiveStore(self.journal)
        for network in ("mainnet","testnet"):
            client=self.fake(network);live.sync_ws(client,now_ms=START+1000)
            client.handle_message(packet("trades",[
                trade(tid=1,time_ms=START+1000,side="B",px="10",sz="3"),
                trade(tid=2,time_ms=START+2000,side="A",px="20",sz="1")]))
            client.clock_ms=lambda:START+63000
            client.handle_message(packet("l2Book",book(time_ms=START+63000,bid="90",ask="91")))
            live.sync_ws(client,now_ms=START+63000)
            view=live.read(network,"ETH")
            self.assertEqual(view["network"],network)
            self.assertEqual(view["side_semantics"],"documented_hyperliquid_aggressor")
            self.assertEqual(view["side_semantics_version"],"1.0")
            self.assertEqual(view["calculation_version"],"2.0")
            self.assertEqual(view["windows"]["1m"]["delta_base"],"2")
            self.assertEqual(view["windows"]["1m"]["delta_quote"],"1e1")
            self.assertEqual(view["cvd"]["base"],"2")
            self.assertFalse(view["windows"]["5m"]["signed_available"])
            self.assertIsNone(view["windows"]["5m"]["delta_base"])
            self.assertFalse(view["execution_authorized"])
            with self.assertRaises(TypeError):view["windows"]["1m"]["delta_base"]="999"
            client.clock_ms=lambda:START+64000
            client.handle_message(packet("trades",[trade(tid=1,time_ms=START+1000,side="B",px="10",sz="3")]))
            live.sync_ws(client,now_ms=START+64000)
            after=live.read(network,"ETH")
            self.assertEqual(after["cvd"]["base"],"2")
            self.assertEqual(after["observed_total"]["unique_trades"],2)
            self.assertTrue(recovery_check(self.journal,live.engine)["state_matches"])

    def test_recovered_assets_of_other_network_never_become_live_implicitly(self):
        client,live,clock=self.timed_live()
        self.send_at(client,live,clock,1,[trade(tid=1,time_ms=START+1000)])
        recovered=LiveStore(self.journal)
        other=self.fake("testnet")
        recovered.sync_ws(other,now_ms=START+1000)
        old=recovered.read("mainnet","ETH")
        self.assertTrue(old["recovered_not_live"])
        self.assertFalse(old["connection"]["fresh"])
        self.assertFalse(old["cvd"]["available"])
        self.assertIsNone(old["cvd"]["base"])
        self.assertFalse(recovered.read("testnet","ETH")["recovered_not_live"])

    def test_legacy_live_restart_starts_new_calculation_without_converting_history(self):
        from test_order_flow import legacy_record
        flow=OrderFlow();flow.import_asset(legacy_record())
        self.journal.write(flow,[event(1,1,network="mainnet",size="3"),
            Operation("advance","mainnet","ETH",START+63000,contact_ms=START+63000)])
        before=self.journal.db.execute("SELECT payload FROM checkpoints").fetchone()[0]
        live=LiveStore(self.journal)
        self.assertEqual(self.journal.db.execute("SELECT payload FROM checkpoints").fetchone()[0],before)
        old=live.read("mainnet","ETH")
        self.assertEqual(old["calculation_version"],"1.0")
        self.assertTrue(old["recovered_not_live"])
        self.assertIsNone(old["cvd"]["base"])
        client=self.fake();client.clock_ms=lambda:START+65000
        client.last_message_ms=START+65000
        live.sync_ws(client,now_ms=START+65000)
        new=live.read("mainnet","ETH")
        self.assertEqual(new["calculation_version"],"2.0")
        self.assertEqual(new["cvd"]["segment_id"],2)
        self.assertFalse(new["windows"]["1m"]["signed_available"])
        client.clock_ms=lambda:START+66000
        client.handle_message(packet("trades",[trade(tid=2,time_ms=START+66000,side="A",sz="2")]))
        live.sync_ws(client,now_ms=START+66000)
        client.clock_ms=lambda:START+69000
        client.handle_message(packet("l2Book",book(time_ms=START+69000)))
        live.sync_ws(client,now_ms=START+69000)
        new=live.read("mainnet","ETH")
        self.assertEqual(new["cvd"]["base"],"-2")
        self.assertEqual(new["observed_total"]["volume_base"],"5")
        self.assertEqual(new["gap_reasons"],{"restart":1})
        self.assertTrue(recovery_check(self.journal,live.engine)["state_matches"])
