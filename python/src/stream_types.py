"""Strict public Hyperliquid WS messages; normalized values contain no users/hash.

This module performs no I/O; A/B is preserved using documented aggressor semantics.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from .orderbook import normalize_ws_book

SIDE_SEMANTICS = "documented_hyperliquid_aggressor"
SIDE_SEMANTICS_VERSION = "1.0"  # B buy aggressor, A sell aggressor; never inferred from L2.
SYNTHETIC_SIDE_SEMANTICS = "verified_synthetic_aggressor"
CHANNELS = frozenset(("trades", "l2Book"))
MAX_MESSAGE_BYTES = 1_048_576
MAX_BATCH_TRADES = 4096


class StreamValidationError(ValueError):
    """A fixed error code, never a raw packet or arbitrary field value."""


def symbol(value: Any) -> str:
    # Preserve official spelling, including case and optional perp DEX prefix.
    if not isinstance(value, str) or len(value) > 64 or not re.fullmatch(r"[A-Za-z0-9]+(?::[A-Za-z0-9]+)?", value):
        raise StreamValidationError("invalid_symbol")
    return value


def _keys(value: Any, required: set[str], optional: set[str] = frozenset()) -> dict:
    if not isinstance(value, dict) or not required <= value.keys() or value.keys() - required - optional:
        raise StreamValidationError("invalid_fields")
    return value


def _integer(value: Any, *, minimum: int = 0, maximum: int = 2**63 - 1) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise StreamValidationError("invalid_integer")
    return value


def _positive_decimal(value: Any, *, allow_zero: bool = False) -> Decimal:
    if not isinstance(value, str) or len(value) > 64 or not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?", value):
        raise StreamValidationError("invalid_decimal")
    try:
        result = Decimal(value)
    except InvalidOperation:
        raise StreamValidationError("invalid_decimal") from None
    if (not result.is_finite() or result < 0 or (result == 0 and not allow_zero)
            or not -32 <= result.adjusted() <= 32):
        raise StreamValidationError("invalid_decimal")
    return result


def _timestamp(value: Any, now_ms: int) -> int:
    value = _integer(value, minimum=1)
    if value > now_ms + 5000:
        raise StreamValidationError("future_timestamp")
    return value


@dataclass(frozen=True)
class Subscription:
    channel: str
    coin: str
    n_sig_figs: int | None = None

    def __post_init__(self):
        symbol(self.coin)
        if not isinstance(self.channel, str) or self.channel not in CHANNELS:
            raise StreamValidationError("nonpublic_subscription")
        if self.channel == "trades" and self.n_sig_figs is not None:
            raise StreamValidationError("invalid_precision")
        if self.n_sig_figs is not None and (type(self.n_sig_figs) is not int or self.n_sig_figs not in (2, 3, 4, 5)):
            raise StreamValidationError("invalid_precision")

    def wire(self) -> dict:
        result = {"type": self.channel, "coin": self.coin}
        if self.n_sig_figs is not None:
            result["nSigFigs"] = self.n_sig_figs
        return result


def outgoing(method: str, subscription: Subscription | None = None) -> str:
    """The only legal application messages: public subscribe/unsubscribe/ping."""
    if method == "ping" and subscription is None:
        return '{"method":"ping"}'
    if method not in ("subscribe", "unsubscribe") or not isinstance(subscription, Subscription):
        raise StreamValidationError("nonpublic_outgoing_message")
    return json.dumps({"method": method, "subscription": subscription.wire()}, separators=(",", ":"))


@dataclass(frozen=True)
class Trade:
    coin: str
    side: str
    price: Decimal
    size: Decimal
    time_ms: int
    tid: int

    @property
    def identity(self) -> tuple:
        # Legacy default namespace. PublicWSClient uses its selected network;
        # the official economic identity is (block_time, coin, tid).
        return ("mainnet", self.time_ms, self.coin, self.tid)

    @property
    def buffer_bytes(self) -> int:
        # Payload accounting, not an exact CPython heap measurement.
        return 128 + len(self.coin) + len(str(self.price)) + len(str(self.size))


def trade_record(trade: Trade) -> dict:
    """Minimal durable representation; normalized numerical spellings for hashes."""
    from decimal import Context
    def number(value):
        return str(value.normalize(Context(prec=80))).replace("E+", "e").replace("E", "e")
    return {"coin": trade.coin, "side": trade.side, "px": number(trade.price),
            "sz": number(trade.size), "time": trade.time_ms, "tid": trade.tid}


def restore_trade(record: dict) -> Trade:
    """Reuse the existing trade validator at the persistence boundary."""
    return _trade(record, _integer(record.get("time"), minimum=1), frozenset((symbol(record.get("coin")),)))


@dataclass(frozen=True)
class Level:
    price: Decimal
    size: Decimal
    order_count: int


@dataclass(frozen=True)
class Book:
    coin: str
    time_ms: int
    bids: tuple[Level, ...]
    asks: tuple[Level, ...]


@dataclass(frozen=True)
class Ack:
    method: str
    subscription: Subscription


@dataclass(frozen=True)
class Message:
    channel: str
    data: tuple[Trade, ...] | Book | Ack | None


def _trade(raw: Any, now_ms: int, symbols: frozenset[str]) -> Trade:
    raw = _keys(raw, {"coin", "side", "px", "sz", "time", "tid"}, {"hash", "users"})
    coin = symbol(raw["coin"])
    if coin not in symbols or raw["side"] not in ("A", "B"):
        raise StreamValidationError("unexpected_trade_identity")
    if "hash" in raw and (not isinstance(raw["hash"], str) or not re.fullmatch(r"0x[0-9a-fA-F]{64}", raw["hash"])):
        raise StreamValidationError("invalid_trade_metadata")
    if "users" in raw and (not isinstance(raw["users"], list) or len(raw["users"]) != 2
                           or any(not isinstance(user, str) or len(user) > 80 for user in raw["users"])):
        raise StreamValidationError("invalid_trade_metadata")
    return Trade(coin, raw["side"], _positive_decimal(raw["px"]), _positive_decimal(raw["sz"]),
                 _timestamp(raw["time"], now_ms), _integer(raw["tid"], maximum=2**50 - 1))


def _book(raw: Any, now_ms: int, symbols: frozenset[str]) -> Book:
    return normalize_ws_book(raw, now_ms, symbols)


def parse_message(raw: str, symbols: frozenset[str], *, now_ms: int) -> Message:
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_MESSAGE_BYTES:
        raise StreamValidationError("invalid_message_size_or_type")
    def unique(pairs):
        obj = {}
        for key, value in pairs:
            if key in obj:
                raise StreamValidationError("duplicate_json_key")
            obj[key] = value
        return obj
    def constant(_):
        raise StreamValidationError("nonfinite_json")
    try:
        packet = json.loads(raw, object_pairs_hook=unique, parse_constant=constant)
    except (ValueError, RecursionError) as exc:
        if isinstance(exc, StreamValidationError):
            raise
        raise StreamValidationError("invalid_json") from None
    if isinstance(packet, dict) and packet.get("channel") == "error":
        raise StreamValidationError("server_error")
    if isinstance(packet, dict) and packet.get("channel") == "pong":
        _keys(packet, {"channel"})
        return Message("pong", None)
    packet = _keys(packet, {"channel", "data"})
    channel, data = packet["channel"], packet["data"]
    if channel == "trades":
        if not isinstance(data, list) or len(data) > MAX_BATCH_TRADES:
            raise StreamValidationError("invalid_trade_batch")
        return Message(channel, tuple(_trade(row, now_ms, symbols) for row in data))
    if channel == "l2Book":
        return Message(channel, _book(data, now_ms, symbols))
    if channel == "subscriptionResponse":
        data = _keys(data, {"method", "subscription"})
        sub = _keys(data["subscription"], {"type", "coin"}, {"nSigFigs", "mantissa", "fast"})
        if data["method"] not in ("subscribe", "unsubscribe"):
            raise StreamValidationError("invalid_ack_method")
        subscription = Subscription(sub["type"], sub["coin"], sub.get("nSigFigs"))
        # Observed canonical ACK expands the default L2 options. Only defaults
        # matching our request are accepted; never silently switch book modes.
        if subscription.channel == "l2Book":
            if sub.get("mantissa") is not None or sub.get("fast", False) is not False:
                raise StreamValidationError("unexpected_book_options")
        elif "mantissa" in sub or "fast" in sub:
            raise StreamValidationError("unexpected_trade_options")
        if subscription.coin not in symbols:
            raise StreamValidationError("unexpected_ack_symbol")
        return Message(channel, Ack(data["method"], subscription))
    raise StreamValidationError("unexpected_channel")
