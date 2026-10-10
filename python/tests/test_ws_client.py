"""Offline WS state machine tests with an injected async transport."""
import asyncio
import json
import unittest
from unittest.mock import patch

from src.ws_client import PublicWSClient, WS_URL
from src.stream_types import Subscription
from test_stream_types import NOW, ack, book, packet, trade


def ready(**options):
    clock = options.pop("clock_ms", lambda: NOW)
    client = PublicWSClient(("ETH", "HYPE"), clock_ms=clock, **options)
    client.connection_epoch = 1
    client.connected_at_ms = NOW - 1000
    client.pending = {sub: 100 for sub in client.desired}
    for sub in client.desired: assert client.handle_message(ack(sub))
    return client


class WSStateTests(unittest.TestCase):
    def test_only_accepted_l2_snapshot_updates_book_connection_epoch(self):
        client=ready()
        original=book(time_ms=NOW-100)
        client.handle_message(packet("l2Book",original))
        self.assertEqual(client.book_epochs["ETH"],1)
        client.connection_epoch=2
        for value in (original,book(time_ms=NOW-200),book(time_ms=NOW-100,bid="99")):
            client.handle_message(packet("l2Book",value))
        self.assertEqual(client.book_epochs["ETH"],1)
        client.handle_message(packet("l2Book",book(time_ms=NOW)))
        self.assertEqual(client.book_epochs["ETH"],2)

    def test_all_symbols_use_independent_state_and_confirmations(self):
        client = ready()
        self.assertEqual(client.state, "live")
        self.assertEqual(len(client.confirmed), 4)
        for coin in ("ETH", "HYPE"):
            self.assertTrue(client.handle_message(packet("l2Book", book(coin))))
            self.assertTrue(client.handle_message(packet("trades", [trade(coin)])))
        events = [client.pop_trade(), client.pop_trade()]
        self.assertEqual([event.trade.coin for event in events], ["ETH", "HYPE"])
        self.assertIsNone(client.pop_trade())
        self.assertEqual(client.queue_size_bytes, 0)
        self.assertEqual(client.summary()["assets"]["ETH"]["side_probe"]["B_at_ask"], 1)

    def test_duplicates_conflicts_and_normalized_decimals(self):
        client = ready()
        for px in ("101", "101.0", "102"):
            client.handle_message(packet("trades", [trade(px=px)]))
        self.assertEqual(len(client.trade_queue), 1)
        self.assertEqual(client.counts["duplicates"], 1)
        self.assertEqual(client.counts["conflicts"], 1)
        self.assertEqual(client.summary()["assets"]["HYPE"]["continuity"], "no_known_gap")
        self.assertEqual(client.summary()["assets"]["ETH"]["continuity"], "gap_detected")

    def test_malformed_batch_is_not_partially_accepted_or_logged(self):
        client = ready()
        row = trade(tid=2); row["sz"] = "private"
        self.assertFalse(client.handle_message(packet("trades", [trade(), row])))
        self.assertEqual(len(client.trade_queue), 0)
        self.assertNotIn("private", json.dumps(client.summary()))
        self.assertNotIn("not-retained", json.dumps(client.summary()))

    def test_out_of_order_and_late_events_have_explicit_metadata(self):
        client = ready()
        for tid, stamp in ((1,NOW), (2,NOW-10), (3,NOW-2001)):
            client.handle_message(packet("trades", [trade(tid=tid, time_ms=stamp)]))
        events = list(client.trade_queue)
        self.assertFalse(events[0].out_of_order)
        self.assertTrue(events[1].out_of_order)
        self.assertTrue(events[2].pre_connection)
        self.assertEqual(client.counts["out_of_order"], 2)
        self.assertEqual(client.last_trade_ms["ETH"], NOW)
        self.assertEqual(client.gap_reasons["ETH"]["late_trade_beyond_tolerance"], 1)

    def test_queue_count_and_byte_saturation_are_bounded_and_signal_loss(self):
        for options in ({"queue_capacity":1}, {"queue_bytes":1}):
            client = ready(**options)
            for tid in range(5): client.handle_message(packet("trades", [trade(tid=tid)]))
            self.assertLessEqual(len(client.trade_queue), 1)
            self.assertLessEqual(client.queue_size_bytes, client.queue_limit_bytes)
            self.assertGreater(client.counts["queue_dropped"], 0)
            self.assertIn("trade_queue_overflow", client.gap_reasons["ETH"])

    def test_dedup_capacity_and_ttl_are_explicit_uncertainty(self):
        now = 10.0
        client = ready(dedup_capacity=1, monotonic=lambda: now, dedup_ttl_seconds=1)
        client.handle_message(packet("trades", [trade()]))
        client.handle_message(packet("trades", [trade(tid=2)]))
        self.assertEqual(len(client.seen), 1)
        self.assertIn("dedup_capacity_eviction", client.gap_reasons["ETH"])
        now += 2
        client.handle_message(packet("trades", [trade(tid=3)]))
        self.assertIn("dedup_horizon_expired", client.gap_reasons["ETH"])

    def test_book_history_is_bounded_old_books_never_replace_new(self):
        client = ready(book_history_capacity=2)
        for offset in (300,200,100,250): client.handle_message(packet("l2Book", book(time_ms=NOW-offset)))
        self.assertEqual(len(client.book_history["ETH"]), 2)
        self.assertEqual(client.books["ETH"].time_ms, NOW-100)
        self.assertEqual(client.counts["out_of_order_books"], 1)
        self.assertEqual(client.summary()["assets"]["ETH"]["book_levels"], [1,1])
        client.handle_message(packet("l2Book", book(time_ms=NOW-100, bid="99")))
        self.assertIn("book_timestamp_conflict", client.gap_reasons["ETH"])

    def test_side_probe_counts_both_hypotheses_and_ambiguities_without_signs(self):
        client = ready()
        client.handle_message(packet("l2Book", book()))
        for tid, (side, px) in enumerate((("B","101"),("A","100"),("B","100"),("A","101"),("B","100.5"))):
            client.handle_message(packet("trades", [trade(tid=tid, side=side, px=px)]))
        client.handle_message(packet("trades", [trade(tid=10, time_ms=NOW-100)]))
        probe = client.summary()["assets"]["ETH"]["side_probe"]
        for key in ("B_at_ask", "A_at_bid", "B_at_bid", "A_at_ask", "inside_or_outside_previous_bbo"):
            self.assertEqual(probe[key], 1)
        self.assertEqual(client.summary()["side_semantics"], "unverified")
        self.assertNotIn("delta", json.dumps(client.summary()).lower())
        self.assertNotIn("cvd", json.dumps(client.summary()).lower())

    def test_no_book_pong_or_stale_book_is_not_new_market_evidence(self):
        now = NOW
        client = ready(clock_ms=lambda:now)
        self.assertFalse(client.summary()["assets"]["ETH"]["book_fresh"])
        client.handle_message(packet("l2Book", book()))
        now += 20_000
        client._awaiting_pong = 1
        client.handle_message('{"channel":"pong"}')
        self.assertFalse(client.summary()["assets"]["ETH"]["book_fresh"])
        self.assertEqual(client.summary()["assets"]["ETH"]["book_exchange_age_ms"], 20100)

    def test_ack_and_data_gates_and_config_fail_before_network(self):
        client = PublicWSClient(("ETH",), clock_ms=lambda:NOW)
        self.assertFalse(client.handle_message(packet("trades", [trade()])))
        self.assertFalse(client.handle_message(ack(Subscription("trades", "ETH"))))
        for options in ({"queue_capacity":0}, {"queue_bytes":True}, {"heartbeat_seconds":float("nan")},
                        {"dedup_capacity":0}, {"late_tolerance_ms":-1}):
            with self.assertRaises(ValueError): PublicWSClient(("ETH",), **options)
        with self.assertRaises(ValueError): PublicWSClient("ETH")
        with self.assertRaises(ValueError): PublicWSClient(())


class FakeSocket:
    def __init__(self, *, auto_ack=True, auto_pong=True, rows=(), disconnect=False):
        self.inbox = asyncio.Queue()
        self.sent, self.closed = [], False
        self.auto_ack, self.auto_pong = auto_ack, auto_pong
        self.rows, self.disconnect = rows, disconnect
        self.released = False

    async def send(self, value):
        row = json.loads(value)
        self.sent.append(row)
        if row["method"] == "subscribe" and self.auto_ack:
            response = json.loads(value)
            if response["subscription"]["type"] == "l2Book":
                response["subscription"].update(mantissa=None, fast=False)
            await self.inbox.put(packet("subscriptionResponse", response))
        if row["method"] == "ping" and self.auto_pong:
            await self.inbox.put('{"channel":"pong"}')
        if len(self.sent) == 4 and not self.released:
            self.released = True
            for item in self.rows: await self.inbox.put(item)
            if self.disconnect: await self.inbox.put(None)

    async def recv(self):
        return await self.inbox.get()

    async def close(self):
        self.closed = True


async def instant_sleep(_):
    await asyncio.sleep(0)


class WSAsyncTests(unittest.IsolatedAsyncioTestCase):
    def client(self, connector, **options):
        return PublicWSClient(("ETH","HYPE"), connector=connector, clock_ms=lambda:NOW,
                              sleep=instant_sleep, jitter=lambda:0, **options)

    async def run_until(self, client, predicate):
        # Observe a state transition, not a fragile millisecond wall-time budget.
        task = asyncio.create_task(client.run(2))
        async def wait_condition():
            while not predicate():
                await asyncio.sleep(0.001)
        try:
            await asyncio.wait_for(wait_condition(), 1)
        finally:
            client.stop()
            await task

    async def test_single_realistic_connection_four_subscriptions_heartbeat_and_close(self):
        socket = FakeSocket(rows=(packet("l2Book",book()),packet("trades",[trade()])))
        calls = []
        async def connector(url, **options): calls.append((url,options)); return socket
        client = self.client(connector, heartbeat_seconds=0.02, pong_timeout=0.02)
        with patch("subprocess.run",side_effect=AssertionError("financial operation forbidden")), \
             patch("urllib.request.urlopen",side_effect=AssertionError("REST forbidden")):
            await self.run_until(client, lambda: client.counts["pongs"] >= 2)
        self.assertEqual(len(calls),1)
        self.assertEqual(calls[0][0],WS_URL)
        self.assertIsNone(calls[0][1]["ping_interval"])
        self.assertTrue(socket.closed)
        self.assertEqual(len(client.confirmed),4)
        self.assertGreaterEqual(client.counts["pongs"],2)
        self.assertEqual(client.state,"closed")
        self.assertFalse(client.summary()["execution_authorized"])
        self.assertTrue(all(row["method"] in ("subscribe","ping") for row in socket.sent))
        self.assertTrue(all("user" not in row.get("subscription",{}) for row in socket.sent))

    async def test_disconnect_reconnect_retains_dedup_and_marks_epochs(self):
        row = packet("trades",[trade()])
        sockets = [FakeSocket(rows=(row,),disconnect=True),FakeSocket(rows=(row,packet("trades",[trade(tid=2)])))]
        calls = []
        async def connector(url, **options): calls.append(url); return sockets[len(calls)-1]
        client = self.client(connector)
        await self.run_until(client, lambda: client.counts["unique_trades"] == 2 and client.connection_epoch == 2)
        self.assertEqual(len(calls),2)
        self.assertEqual(client.counts["reconnections"],1)
        self.assertEqual(client.counts["duplicates"],1)
        self.assertEqual([e.connection_epoch for e in client.trade_queue],[1,2])
        self.assertIn("remote_closed",client.gap_reasons["ETH"])
        self.assertTrue(all(socket.closed for socket in sockets))

    async def test_real_reconnect_path_clears_all_l2_provenance_until_new_snapshot(self):
        sockets=[FakeSocket(rows=(packet("l2Book",book(time_ms=NOW-50)),
                                  packet("l2Book",book(time_ms=NOW-50,bid="99"))),disconnect=True),
                 FakeSocket()]
        calls=[]
        async def connector(url,**options):
            calls.append(url)
            return sockets[len(calls)-1]
        client=self.client(connector)
        await self.run_until(client,lambda:client.connection_epoch==2 and client.confirmed==client.desired)
        self.assertEqual(client.books,{})
        self.assertEqual(client.book_epochs,{})
        self.assertEqual(client.book_conflicts,{})
        self.assertEqual(client.last_book_received_ms,{})
        self.assertIn("book_timestamp_conflict",client.gap_reasons["ETH"])
        self.assertTrue(client.handle_message(packet("l2Book",book(time_ms=NOW-100))))
        self.assertEqual(client.book_epochs["ETH"],2)
        self.assertEqual(client.books["ETH"].time_ms,NOW-100)

    async def test_heartbeat_timeout_even_when_other_data_arrives(self):
        sockets = [FakeSocket(auto_pong=False), FakeSocket()]
        count = 0
        async def connector(url, **options):
            nonlocal count
            socket = sockets[min(count,1)]; count += 1; return socket
        client = self.client(connector, heartbeat_seconds=0.01,pong_timeout=0.01)
        await self.run_until(client, lambda: client.counts["reconnections"] >= 1 and client.confirmed == client.desired)
        self.assertGreaterEqual(client.errors["heartbeat_timeout"],1)
        self.assertGreaterEqual(client.counts["reconnections"],1)

    async def test_ack_timeout_reconnects_and_unknown_ack_does_not_confirm(self):
        count = 0
        async def connector(url, **options):
            nonlocal count
            count += 1
            return FakeSocket(auto_ack=count != 1)
        client = self.client(connector,ack_timeout=0.05)
        await self.run_until(client, lambda: client.counts["reconnections"] == 1 and client.confirmed == client.desired)
        self.assertEqual(client.errors["ack_timeout"],1)
        self.assertEqual(len(client.confirmed),4)

    async def test_invalid_burst_reconnects_with_fixed_error_codes(self):
        sockets = [FakeSocket(rows=("private","private","private")),FakeSocket()]
        count = 0
        async def connector(url, **options):
            nonlocal count
            socket=sockets[min(count,1)]; count+=1; return socket
        client=self.client(connector)
        await self.run_until(client, lambda: client.counts["reconnections"] == 1 and client.confirmed == client.desired)
        self.assertEqual(client.counts["invalid_messages"],3)
        self.assertEqual(client.counts["reconnections"],1)
        self.assertNotIn("private",json.dumps(client.summary()))

    async def test_stop_wakes_recv_and_backoff_without_waiting_for_heartbeat(self):
        socket = FakeSocket()
        async def connector(url, **options): return socket
        client=self.client(connector)
        task=asyncio.create_task(client.run(None))
        while client.state != "live": await asyncio.sleep(0)
        client.stop()
        await asyncio.wait_for(task,0.2)
        self.assertTrue(socket.closed)
        with self.assertRaises(RuntimeError): await client.run(1)
        async def unavailable(url, **options): raise OSError("private connection detail")
        client=PublicWSClient(("ETH",),connector=unavailable)
        task=asyncio.create_task(client.run(1))
        while not client.counts["retry_backoffs"]: await asyncio.sleep(0)
        client.stop()
        await asyncio.wait_for(task,0.2)
        self.assertNotIn("private",json.dumps(client.summary()))

    async def test_cancel_closes_transport_and_all_receive_tasks(self):
        socket=FakeSocket()
        async def connector(url, **options): return socket
        client=self.client(connector)
        task=asyncio.create_task(client.run(None))
        while client.state != "live": await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError): await task
        self.assertTrue(socket.closed)
        self.assertFalse(client._running)
        self.assertEqual(client.state,"closed")

    async def test_backoff_increases_and_startup_budget_is_bounded(self):
        now=0.0
        delays=[]
        async def sleep(seconds):
            nonlocal now
            delays.append(seconds); now+=seconds
        async def connector(url, **options): raise OSError("private")
        client=PublicWSClient(("ETH",),connector=connector,monotonic=lambda:now,sleep=sleep,jitter=lambda:0)
        await client.run(1)
        self.assertEqual(delays[:2],[2.1,4.2])
        self.assertLessEqual(now,10)
        self.assertEqual(client.counts["connections"],0)
        self.assertEqual(client.state,"closed")

    async def test_duplicate_run_and_invalid_duration_never_open_extra_socket(self):
        socket=FakeSocket()
        async def connector(url, **options): return socket
        client=self.client(connector)
        for duration in (0,-1,True,float("inf")):
            with self.assertRaises(ValueError): await client.run(duration)
        task=asyncio.create_task(client.run(None))
        while client.state != "live": await asyncio.sleep(0)
        with self.assertRaises(RuntimeError): await client.run(1)
        client.stop(); await task
