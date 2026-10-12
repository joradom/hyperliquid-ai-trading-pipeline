"""Standalone public READ-ONLY WS transport and side-semantics diagnostics.

No accounts, signing, REST recapture, persistence, Delta/CVD or pipeline imports.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import random
import time
from collections import Counter, OrderedDict, deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from .stream_types import (Ack, Book, Trade, Subscription, SIDE_SEMANTICS,
                           MAX_MESSAGE_BYTES, StreamValidationError, outgoing, parse_message, symbol)
from .orderbook import bbo

WS_URL = "wss://api.hyperliquid.xyz/ws"
WS_URLS = {"mainnet": WS_URL, "testnet": "wss://api.hyperliquid-testnet.xyz/ws"}


async def connect_public(url: str, **options):
    # Lazy optional dependency: offline parser/tests do not require websockets.
    from websockets.legacy.client import connect
    return await connect(url, **options)


@dataclass(frozen=True)
class ReceivedTrade:
    trade: Trade
    received_at_ms: int
    connection_epoch: int
    continuity_epoch: int
    pre_connection: bool
    out_of_order: bool


class _Stopped(Exception):
    pass


class _Reconnect(Exception):
    def __init__(self, code):
        self.code = code


class PublicWSClient:
    """One connection for all symbols; single async owner, synchronous pop/report.

    Limits bound message size, batches, trade queue, ID cache and book history.
    Overflow is explicit data loss. No transport claims complete trade history.
    """

    def __init__(self, symbols, *, connector: Callable[..., Awaitable] = connect_public,
                 clock_ms: Callable[[], int] | None = None,
                 monotonic: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], Awaitable] = asyncio.sleep,
                 jitter: Callable[[], float] = random.random,
                 heartbeat_seconds: float = 20, pong_timeout: float = 10,
                 ack_timeout: float = 10, open_timeout: float = 5, close_timeout: float = 2,
                 queue_capacity: int = 8192, queue_bytes: int = 2_097_152,
                 dedup_capacity: int = 100_000, dedup_ttl_seconds: float = 1200,
                 book_history_capacity: int = 128, late_tolerance_ms: int = 2000,
                 network: str = "mainnet"):
        if not isinstance(network,str) or network not in WS_URLS: raise ValueError("unsupported public WS network")
        self._network = network
        if isinstance(symbols, (str, bytes)):
            raise ValueError("symbols must be a sequence")
        self.symbols = tuple(dict.fromkeys(symbol(s) for s in symbols))
        if not 1 <= len(self.symbols) <= 64:
            raise ValueError("requires 1 to 64 distinct symbols")
        for value in (heartbeat_seconds, pong_timeout, ack_timeout, open_timeout, close_timeout, dedup_ttl_seconds):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError("timeouts must be positive and finite")
        for value in (queue_capacity, queue_bytes, dedup_capacity, book_history_capacity):
            if type(value) is not int or value <= 0:
                raise ValueError("capacities must be positive integers")
        if type(late_tolerance_ms) is not int or late_tolerance_ms < 0:
            raise ValueError("late tolerance must be nonnegative milliseconds")
        self.connector, self.clock_ms = connector, clock_ms or (lambda: time.time_ns() // 1_000_000)
        self.monotonic, self.sleep, self.jitter = monotonic, sleep, jitter
        self.heartbeat_seconds, self.pong_timeout, self.ack_timeout = heartbeat_seconds, pong_timeout, ack_timeout
        self.open_timeout, self.close_timeout = open_timeout, close_timeout
        self.queue_capacity, self.queue_limit_bytes = queue_capacity, queue_bytes
        self.dedup_capacity, self.dedup_ttl = dedup_capacity, dedup_ttl_seconds
        self.late_tolerance_ms = late_tolerance_ms
        self.desired = frozenset(Subscription(channel, coin, 5 if channel == "l2Book" else None)
                                 for coin in self.symbols for channel in ("trades", "l2Book"))
        self.confirmed: set[Subscription] = set()
        self.pending: dict[Subscription, float] = {}
        self.trade_queue: deque[ReceivedTrade] = deque()
        self.queue_size_bytes = 0
        self.seen: OrderedDict[tuple, tuple[Trade, float]] = OrderedDict()
        # A durable consumer checks retained identities before judging lateness.
        # Standalone transport keeps its conservative capacity/late gap policy.
        self.durable_dedup = False
        self.books: dict[str, Book] = {}
        self.book_epochs: dict[str, int] = {}
        self.book_conflicts: dict[str, int] = {}
        self.book_history = {coin: deque(maxlen=book_history_capacity) for coin in self.symbols}
        self.asset_counts = {coin: Counter() for coin in self.symbols}
        self.probe_counts = {coin: Counter() for coin in self.symbols}
        self.gap_reasons = {coin: Counter() for coin in self.symbols}
        self.continuity_epoch = {coin: 0 for coin in self.symbols}
        self.last_trade_ms: dict[str, int] = {}
        self.last_received_ms: dict[str, int] = {}
        self.last_book_received_ms: dict[str, int] = {}
        self.counts, self.errors = Counter(), Counter()
        self.state = "disconnected"
        self.connection_epoch = 0
        self.connected_at_ms = None
        self.first_connected_at_ms = None
        self.started_at_ms = None
        self.ended_at_ms = None
        self.first_connected_mono = None
        self.observation_seconds = None
        self.last_message_ms = None
        self.last_pong_ms = None
        self._awaiting_pong = None
        self._next_ping = None
        self._last_send = -math.inf
        self._stop = asyncio.Event()
        self._transport = None
        self._running = False
        self._invalid_streak = 0
        self._last_error = None

    @property
    def network(self):
        """The source network cannot change while a socket/queue is in use."""
        return self._network

    def stop(self):
        """Wake a pending recv and close; safe from the owning event loop."""
        self._stop.set()

    def _gap(self, code: str, coin: str | None = None):
        for name in (self.symbols if coin is None else (coin,)):
            self.gap_reasons[name][code] += 1
            self.continuity_epoch[name] += 1
        self.counts["continuity_interruptions"] += 1

    def _expire_ids(self, now: float):
        while self.seen and next(iter(self.seen.values()))[1] <= now - self.dedup_ttl:
            self.seen.popitem(last=False)
            self.counts["dedup_evictions"] += 1
            # Cache expiry is not missing traffic. Old replays are still checked
            # by the consumer's retained identity/retention and watermark gates.

    def _probe(self, trade: Trade, pre_connection: bool, out_of_order: bool):
        counts = self.probe_counts[trade.coin]
        if pre_connection or out_of_order:
            counts["history_or_out_of_order"] += 1
            return
        history = self.book_history[trade.coin]
        if any(book.time_ms == trade.time_ms for book in history):
            counts["same_timestamp_ambiguous"] += 1
            return
        previous = next((book for book in reversed(history) if book.time_ms < trade.time_ms), None)
        previous_bid, previous_ask = bbo(previous) if previous else (None, None)
        if previous is None:
            counts["no_preceding_book"] += 1
        elif trade.time_ms - previous.time_ms > 1500:
            counts["preceding_book_too_old"] += 1
        elif trade.price == previous_bid:
            counts[f"{trade.side}_at_bid"] += 1
        elif trade.price == previous_ask:
            counts[f"{trade.side}_at_ask"] += 1
        else:
            counts["inside_or_outside_previous_bbo"] += 1

    def _accept_trade(self, trade: Trade, received_ms: int):
        self.counts["trades_received"] += 1
        counts = self.asset_counts[trade.coin]
        counts["trades_received"] += 1
        now = self.monotonic()
        self._expire_ids(now)
        identity = (self.network, *trade.identity[1:])
        existing = self.seen.get(identity)
        if existing is not None:
            if existing[0] == trade:
                self.counts["duplicates"] += 1
                counts["duplicates"] += 1
            else:
                self.counts["conflicts"] += 1
                counts["conflicts"] += 1
                self._gap("trade_identity_conflict", trade.coin)
            return
        if len(self.seen) >= self.dedup_capacity:
            _, (evicted, _) = self.seen.popitem(last=False)
            self.counts["dedup_evictions"] += 1
            if not self.durable_dedup:
                self._gap("dedup_capacity_eviction", evicted.coin)
        self.seen[identity] = (trade, now)
        previous = self.last_trade_ms.get(trade.coin)
        out_of_order = previous is not None and trade.time_ms < previous
        pre_connection = self.connected_at_ms is not None and trade.time_ms < self.connected_at_ms
        if out_of_order:
            counts["out_of_order"] += 1
            self.counts["out_of_order"] += 1
            if previous - trade.time_ms > self.late_tolerance_ms and not self.durable_dedup:
                self._gap("late_trade_beyond_tolerance", trade.coin)
        if pre_connection:
            counts["pre_connection_trades"] += 1
        self.last_trade_ms[trade.coin] = max(trade.time_ms, previous or 0)
        self.last_received_ms[trade.coin] = received_ms
        counts["unique_trades"] += 1
        self.counts["unique_trades"] += 1
        self._probe(trade, pre_connection, out_of_order)
        if len(self.trade_queue) >= self.queue_capacity or self.queue_size_bytes + trade.buffer_bytes > self.queue_limit_bytes:
            self.counts["queue_dropped"] += 1
            counts["queue_dropped"] += 1
            self._gap("trade_queue_overflow", trade.coin)
            return
        event = ReceivedTrade(trade, received_ms, self.connection_epoch, self.continuity_epoch[trade.coin],
                              pre_connection, out_of_order)
        self.trade_queue.append(event)
        self.queue_size_bytes += trade.buffer_bytes
        self.counts["queue_peak"] = max(self.counts["queue_peak"], len(self.trade_queue))

    def pop_trade(self) -> ReceivedTrade | None:
        if not self.trade_queue:
            return None
        event = self.trade_queue.popleft()
        self.queue_size_bytes -= event.trade.buffer_bytes
        self.counts["trades_consumed"] += 1
        return event

    def handle_message(self, raw: str) -> bool:
        """Validate whole batch before changing state. No packet is logged."""
        self.counts["messages"] += 1
        now_ms = self.clock_ms()
        self.last_message_ms = now_ms
        try:
            # The official SDK recognizes this exact optional connection notice.
            if raw == "Websocket connection established.":
                self.counts["connection_notices"] += 1
                self._invalid_streak = 0
                return True
            message = parse_message(raw, frozenset(self.symbols), now_ms=now_ms)
            if isinstance(message.data, Ack):
                ack = message.data
                if (ack.method != "subscribe" or ack.subscription not in self.desired
                        or ack.subscription not in self.pending and ack.subscription not in self.confirmed):
                    raise StreamValidationError("unexpected_ack")
                if ack.subscription in self.confirmed:
                    self.counts["duplicate_acks"] += 1
                self.confirmed.add(ack.subscription)
                self.pending.pop(ack.subscription, None)
                self.counts["subscription_acks"] += 1
                if self.confirmed == self.desired:
                    self.state = "live"
            elif message.channel == "pong":
                if self._awaiting_pong is None:
                    raise StreamValidationError("unsolicited_pong")
                self._awaiting_pong = None
                self.last_pong_ms = now_ms
                self.counts["pongs"] += 1
            elif isinstance(message.data, Book):
                book = message.data
                if Subscription("l2Book", book.coin, 5) not in self.confirmed:
                    raise StreamValidationError("data_before_ack")
                previous = self.books.get(book.coin)
                if previous is not None and book.time_ms < previous.time_ms:
                    self.counts["out_of_order_books"] += 1
                    self._invalid_streak = 0
                    return True
                if previous is not None and book.time_ms == previous.time_ms:
                    if book != previous:
                        self.book_conflicts[book.coin] = book.time_ms
                        self._gap("book_timestamp_conflict", book.coin)
                        raise StreamValidationError("book_timestamp_conflict")
                    self.counts["duplicate_books"] += 1
                    self._invalid_streak = 0
                    return True
                self.books[book.coin] = book
                self.book_epochs[book.coin] = self.connection_epoch
                self.book_conflicts.pop(book.coin, None)
                self.book_history[book.coin].append(book)
                self.last_book_received_ms[book.coin] = now_ms
                self.asset_counts[book.coin]["books"] += 1
                self.counts["books"] += 1
            else:
                for trade in message.data:
                    if Subscription("trades", trade.coin) not in self.confirmed:
                        raise StreamValidationError("data_before_ack")
                self.counts["trade_batches"] += 1
                for trade in message.data:
                    self._accept_trade(trade, now_ms)
            self._invalid_streak = 0
            return True
        except StreamValidationError as exc:
            self._last_error = str(exc)
            self.errors[str(exc)] += 1
            self.counts["invalid_messages"] += 1
            self._invalid_streak += 1
            # We cannot safely identify affected symbols from an invalid packet.
            self._gap("invalid_public_message")
            return False

    async def _send(self, method, sub=None):
        # Max 25 sends/s per client, below 2000/min; one client owns its socket.
        payload = outgoing(method, sub)
        delay = self._last_send + 0.04 - self.monotonic()
        if delay > 0:
            await self.sleep(delay)
        await asyncio.wait_for(self._transport.send(payload), timeout=self.open_timeout)
        self._last_send = self.monotonic()
        self.counts["sent_messages"] += 1

    async def _receive(self, timeout):
        recv = asyncio.create_task(self._transport.recv())
        stopped = asyncio.create_task(self._stop.wait())
        try:
            done, _ = await asyncio.wait((recv, stopped), timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
            if stopped in done:
                raise _Stopped()
            if recv in done:
                return recv.result()
            raise asyncio.TimeoutError()
        finally:
            for task in (recv, stopped):
                if not task.done():
                    task.cancel()
            await asyncio.gather(recv, stopped, return_exceptions=True)

    async def _close(self):
        transport, self._transport = self._transport, None
        if transport is not None:
            try:
                await asyncio.wait_for(transport.close(), timeout=self.close_timeout)
                self.counts["clean_closes"] += 1
            except Exception:
                self.errors["close_failed"] += 1

    async def _session(self, deadline):
        self._next_ping = self.monotonic() + self.heartbeat_seconds
        self._awaiting_pong = None
        for sub in sorted(self.desired, key=lambda item: (item.coin, item.channel)):
            if self._stop.is_set() or self.monotonic() >= deadline:
                return
            await self._send("subscribe", sub)
            self.pending[sub] = self.monotonic() + self.ack_timeout
        while not self._stop.is_set():
            now = self.monotonic()
            if now >= deadline:
                return
            if self.pending and min(self.pending.values()) <= now:
                raise _Reconnect("ack_timeout")
            if self._awaiting_pong is not None and self._awaiting_pong <= now:
                raise _Reconnect("heartbeat_timeout")
            if self._awaiting_pong is None and now >= self._next_ping:
                await self._send("ping")
                self.counts["pings"] += 1
                self._awaiting_pong = self.monotonic() + self.pong_timeout
                self._next_ping = self.monotonic() + self.heartbeat_seconds
            wake = min(deadline, self._awaiting_pong or self._next_ping,
                       min(self.pending.values()) if self.pending else math.inf)
            try:
                raw = await self._receive(max(0.001, wake - self.monotonic()))
            except asyncio.TimeoutError:
                continue
            if raw is None:
                raise _Reconnect("remote_closed")
            if not self.handle_message(raw):
                if self._last_error == "server_error" or self._invalid_streak >= 3:
                    raise _Reconnect("invalid_message_burst")

    async def run(self, duration_seconds: float | None = 60):
        """Observation budget starts at the first successful connection.

        Reconnects never reset that budget. Initial connection attempts have a
        separate bounded startup budget. None runs until stop/cancellation.
        """
        if self._running:
            raise RuntimeError("client is already running")
        if duration_seconds is not None and (isinstance(duration_seconds, bool)
                or not isinstance(duration_seconds, (float, int)) or not math.isfinite(duration_seconds)
                or duration_seconds <= 0):
            raise ValueError("duration must be positive and finite")
        if self.started_at_ms is not None:
            raise RuntimeError("client instances are single-use")
        self._running = True
        self.started_at_ms = self.clock_ms()
        deadline = self.monotonic() + 2 * self.open_timeout
        retries = 0
        try:
            while not self._stop.is_set() and self.monotonic() < deadline:
                self.state = "connecting"
                try:
                    self.counts["connection_attempts"] += 1
                    self._transport = await asyncio.wait_for(self.connector(
                        WS_URLS[self.network], open_timeout=self.open_timeout, close_timeout=self.close_timeout,
                        ping_interval=None, max_size=MAX_MESSAGE_BYTES, max_queue=16, compression=None),
                        timeout=min(self.open_timeout, deadline - self.monotonic()))
                    self.counts["connections"] += 1
                    self.connection_epoch += 1
                    self.connected_at_ms = self.clock_ms()
                    if self.first_connected_at_ms is None:
                        self.first_connected_at_ms = self.connected_at_ms
                        self.first_connected_mono = self.monotonic()
                        deadline = self.monotonic() + duration_seconds if duration_seconds is not None else math.inf
                    else:
                        self.counts["reconnections"] += 1
                    self.confirmed.clear()
                    self.pending.clear()
                    self.books.clear()
                    self.book_epochs.clear()
                    self.book_conflicts.clear()
                    self.last_book_received_ms.clear()
                    for history in self.book_history.values():
                        history.clear()
                    self._invalid_streak = 0
                    self.state = "subscribing"
                    await self._session(deadline)
                    break
                except _Stopped:
                    break
                except Exception as exc:
                    code = exc.code if isinstance(exc, _Reconnect) else "transport_error"
                    self.errors[code] += 1
                    self._gap(code)
                    self.state = "disconnected"
                finally:
                    await self._close()
                if self._stop.is_set() or self.monotonic() >= deadline:
                    break
                # >=2.1 s between attempts, cap/jitter; at most ~29 new/min
                # for this client. IP usage by other processes remains external.
                backoff = min(30.0, 2.1 * 2**min(retries, 4)) * (1 + 0.2 * self.jitter())
                retries += 1
                self.counts["retry_backoffs"] += 1
                delay = min(backoff, max(0, deadline - self.monotonic()))
                await self._wait_retry(delay)
        finally:
            await self._close()
            self.ended_at_ms = self.clock_ms()
            if self.first_connected_mono is not None:
                self.observation_seconds = round(self.monotonic() - self.first_connected_mono, 3)
            self.state = "closed"
            self._running = False

    async def _wait_retry(self, delay):
        waiting = asyncio.create_task(self.sleep(delay))
        stopped = asyncio.create_task(self._stop.wait())
        try:
            await asyncio.wait((waiting, stopped), return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in (waiting, stopped):
                if not task.done():
                    task.cancel()
            await asyncio.gather(waiting, stopped, return_exceptions=True)

    def summary(self) -> dict[str, Any]:
        now = self.clock_ms()
        counters = {key: self.counts[key] for key in (
            "connection_attempts", "connections", "reconnections", "clean_closes", "messages",
            "subscription_acks", "sent_messages", "pings", "pongs", "trade_batches", "trades_received",
            "unique_trades", "duplicates", "conflicts", "out_of_order", "books", "invalid_messages",
            "queue_dropped", "dedup_evictions", "continuity_interruptions", "trades_consumed", "queue_peak")}
        counters.update(self.counts)
        def age(stamp):
            return now - stamp if stamp is not None else None
        assets = {}
        for coin in self.symbols:
            book = self.books.get(coin)
            book_age = age(book.time_ms if book else None)
            assets[coin] = {"counts": dict(self.asset_counts[coin]),
                           "confirmed_channels": sorted(sub.channel for sub in self.confirmed if sub.coin == coin),
                           "continuity_epoch": self.continuity_epoch[coin],
                           "continuity": "gap_detected" if self.gap_reasons[coin] else "no_known_gap",
                           "gap_reasons": dict(self.gap_reasons[coin]),
                           "book_levels": [len(book.bids), len(book.asks)] if book else None,
                           "book_exchange_age_ms": book_age,
                           "book_receipt_age_ms": age(self.last_book_received_ms.get(coin)),
                           "book_fresh": book_age is not None and -5000 <= book_age <= 15_000,
                           "trade_event_age_ms": age(self.last_trade_ms.get(coin)),
                           "trade_receipt_age_ms": age(self.last_received_ms.get(coin)),
                           "side_probe": dict(self.probe_counts[coin])}
        stamp = lambda value: datetime.fromtimestamp(value / 1000, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z") if value else None
        return {"kind": "public_ws_diagnostics", "schema_version": "1.0", "read_only": True,
                "execution_authorized": False, "endpoint": WS_URLS[self.network], "side_semantics": SIDE_SEMANTICS,
                "state": self.state, "connection_epoch": self.connection_epoch,
                "started_at_utc": stamp(self.started_at_ms), "first_connected_at_utc": stamp(self.first_connected_at_ms),
                "ended_at_utc": stamp(self.ended_at_ms), "observation_seconds": self.observation_seconds,
                "counts": counters, "errors": dict(self.errors),
                "last_message_age_ms": age(self.last_message_ms), "last_pong_age_ms": age(self.last_pong_ms),
                "buffer": {"queued_trades": len(self.trade_queue), "accounted_payload_bytes": self.queue_size_bytes,
                           "capacity": self.queue_capacity, "payload_bytes_limit": self.queue_limit_bytes,
                           "dedup_entries": len(self.seen), "dedup_capacity": self.dedup_capacity},
                "assets": assets}


async def _capture(args):
    client = PublicWSClient(args.symbols, network=args.network)
    async def drain():
        while True:
            while client.pop_trade() is not None:
                pass
            await asyncio.sleep(0.02)
    consumer = asyncio.create_task(drain())
    try:
        await client.run(None if args.continuous else args.seconds)
    except asyncio.CancelledError:
        client.counts["cancelled"] += 1
    finally:
        consumer.cancel()
        await asyncio.gather(consumer, return_exceptions=True)
        while client.pop_trade() is not None:
            pass
    return client.summary()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("symbols", nargs="+", help="official symbol spelling, e.g. ETH HYPE")
    parser.add_argument("--seconds", type=float, default=60, help="observation budget after first connect (default 60)")
    parser.add_argument("--continuous", action="store_true", help="run until Ctrl+C")
    parser.add_argument("--json", action="store_true", help="summary only; no raw packets or addresses")
    parser.add_argument("--network", choices=tuple(WS_URLS), default="mainnet")
    args = parser.parse_args(argv)
    try:
        report = asyncio.run(_capture(args))
    except (ValueError, StreamValidationError) as exc:
        parser.error(str(exc))
    except KeyboardInterrupt:
        return 130
    if args.json:
        print(json.dumps(report, ensure_ascii=False, sort_keys=True, allow_nan=False))
    else:
        print(f"Public WS | state={report['state']} | observed={report['observation_seconds']}s | side={SIDE_SEMANTICS}")
        print("counts=" + json.dumps(report["counts"], sort_keys=True) + " | errors=" + json.dumps(report["errors"], sort_keys=True))
        for coin, data in report["assets"].items():
            print(f"{coin} | trades={data['counts'].get('unique_trades', 0)} | book_age={data['book_exchange_age_ms']}ms"
                  f" | continuity={data['continuity']} | side_probe={json.dumps(data['side_probe'], sort_keys=True)}")
        print("READ-ONLY | execution_authorized=false")
    return 0 if report["counts"].get("connections") and all(len(a["confirmed_channels"]) == 2 for a in report["assets"].values()) and not report["errors"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
