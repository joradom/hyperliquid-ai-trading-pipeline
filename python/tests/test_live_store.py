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
    def fake(self):
        client=PublicWSClient(("ETH","HYPE"),clock_ms=lambda:START+1000)
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
