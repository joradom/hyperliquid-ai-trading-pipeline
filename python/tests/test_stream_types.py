"""Public WS fixtures; no network, credentials or user histories."""
import copy
import json
import unittest
from decimal import Decimal

from src.stream_types import (SIDE_SEMANTICS, MAX_MESSAGE_BYTES, StreamValidationError,
                              Subscription, Trade, Book, outgoing, parse_message)

NOW = 2_000_000_000_000
SYMBOLS = frozenset(("ETH", "HYPE"))


def trade(coin="ETH", tid=1, time_ms=NOW, side="B", px="101", sz="1"):
    return {"coin": coin, "tid": tid, "time": time_ms, "side": side, "px": px, "sz": sz,
            "hash": "0x" + "0" * 64, "users": ["not-retained-buyer", "not-retained-seller"]}


def book(coin="ETH", time_ms=NOW-100, bid="100", ask="101"):
    return {"coin": coin, "time": time_ms,
            "levels": [[{"px": bid, "sz": "2", "n": 1}], [{"px": ask, "sz": "3", "n": 2}]]}


def packet(channel, data):
    return json.dumps({"channel": channel, "data": data})


def ack(sub, method="subscribe"):
    return packet("subscriptionResponse", {"method": method, "subscription": sub.wire()})


class StreamTypesTests(unittest.TestCase):
    def parse(self, value):
        return parse_message(value, SYMBOLS, now_ms=NOW)

    def test_formats_and_documented_identity_no_personal_metadata(self):
        original = trade()
        before = copy.deepcopy(original)
        result = self.parse(packet("trades", [original])).data[0]
        self.assertEqual(result.identity, ("mainnet", NOW, "ETH", 1))
        self.assertEqual(result.price, Decimal("101"))
        self.assertEqual(result.size, Decimal("1"))
        self.assertEqual(original, before)
        self.assertNotIn("users", result.__dict__)
        self.assertNotIn("hash", result.__dict__)
        self.assertEqual(SIDE_SEMANTICS, "documented_hyperliquid_aggressor")
        self.assertEqual(result, self.parse(packet("trades", [trade(px="101.000")])).data[0])

    def test_books_are_immutable_snapshots_and_preserve_native_units(self):
        result = self.parse(packet("l2Book", book())).data
        self.assertIsInstance(result, Book)
        self.assertEqual(result.bids[0].size, Decimal("2"))
        with self.assertRaises(AttributeError): result.coin = "HYPE"
        self.assertEqual(self.parse(packet("trades", [])).data, ())

    def test_subscriptions_ack_and_heartbeat(self):
        for coin in SYMBOLS:
            for channel in ("trades", "l2Book"):
                sub = Subscription(channel, coin, 5 if channel == "l2Book" else None)
                self.assertEqual(self.parse(ack(sub)).data.subscription, sub)
                for method in ("subscribe", "unsubscribe"):
                    self.assertEqual(json.loads(outgoing(method, sub)), {"method": method, "subscription": sub.wire()})
        self.assertEqual(outgoing("ping"), '{"method":"ping"}')
        self.assertEqual(self.parse('{"channel":"pong"}').channel, "pong")

    def test_every_outgoing_financial_or_account_message_is_rejected(self):
        for channel in ("userFills", "orderUpdates", "clearinghouseState", "exchange", [], "trades;open"):
            with self.subTest(channel=channel), self.assertRaises(StreamValidationError): Subscription(channel, "ETH")
        for method in ("post", "exchange", "order", "cancel", "modify", "sign", "ping"):
            with self.subTest(method=method), self.assertRaises(StreamValidationError): outgoing(method, Subscription("trades", "ETH"))
        for coin in ("ETH\n", "", "x/y", "<address>", "0x" + "a" * 100):
            with self.assertRaises(StreamValidationError): Subscription("trades", coin)

    def test_malformed_envelopes_are_fixed_codes_without_raw_data(self):
        cases = ["not-json-private", "[]", "{}", b"binary", '"private"',
                 '{"channel":"pong","channel":"trades"}', '{"channel":"pong","extra":1}',
                 '{"channel":"trades","data":NaN}', packet("private-data", {}),
                 packet("error", "private-server-error"), "x" * (MAX_MESSAGE_BYTES + 1),
                 packet("trades", {}), packet("trades", [trade()] * 4097)]
        for value in cases:
            with self.subTest(case=str(value)[:50]), self.assertRaises(StreamValidationError) as caught:
                self.parse(value)
            self.assertNotIn("private", str(caught.exception))

    def test_trade_validation_and_whole_batch_atomicity(self):
        mutations = (("coin", "BTC"), ("side", "S"), ("px", "NaN"), ("px", True),
                     ("sz", "0"), ("sz", "-1"), ("px", "1e100"), ("time", True),
                     ("time", NOW+5001), ("tid", 2**50), ("tid", 1.1), ("users", "private"),
                     ("hash", "private"), ("extra", 1))
        for field, value in mutations:
            row = trade(); row[field] = value
            with self.subTest(field=field, value=value), self.assertRaises(StreamValidationError):
                self.parse(packet("trades", [trade(tid=2), row]))
        row = trade(); del row["tid"]
        with self.assertRaises(StreamValidationError): self.parse(packet("trades", [row]))
        self.assertIsInstance(self.parse(packet("trades", [trade(time_ms=NOW+5000)])).data[0], Trade)

    def test_book_rejects_unsorted_crossed_empty_and_invalid_levels(self):
        cases = []
        for levels in ([], [[], []], [[{"px":"100", "sz":"2", "n":1}]],
                       [book()["levels"][0]*21, book()["levels"][1]],
                       [[{"px":"99","sz":"1","n":1}, {"px":"100","sz":"1","n":1}], book()["levels"][1]]):
            row = book(); row["levels"] = levels; cases.append(row)
        cases += [book(bid="102"), book(bid="101")]
        for field, value in (("px","Inf"), ("sz","0"), ("n",True), ("n",-1)):
            row = book(); row["levels"][0][0][field] = value; cases.append(row)
        for row in cases:
            with self.assertRaises(StreamValidationError): self.parse(packet("l2Book", row))

    def test_unknown_or_financial_ack_fields_rejected(self):
        for raw_sub in ({"type":"trades","coin":"BTC"}, {"type":[],"coin":"ETH"},
                        {"type":"trades","coin":"ETH","user":"private"},
                        {"type":"trades","coin":"ETH","nSigFigs":5}):
            with self.assertRaises(StreamValidationError):
                self.parse(packet("subscriptionResponse", {"method":"subscribe","subscription":raw_sub}))

    def test_current_mainnet_canonical_l2_ack_and_extra_spread_field(self):
        subscription = {"type":"l2Book", "coin":"ETH", "nSigFigs":5, "mantissa":None, "fast":False}
        self.assertEqual(self.parse(packet("subscriptionResponse", {"method":"subscribe", "subscription":subscription})).data.subscription,
                         Subscription("l2Book", "ETH", 5))
        row = book(); row["spread"] = "0.0001"
        self.assertEqual(self.parse(packet("l2Book", row)).data, self.parse(packet("l2Book", book())).data)
        for field, value in (("fast",True),("fast",0),("mantissa",1)):
            bad = dict(subscription); bad[field] = value
            with self.assertRaises(StreamValidationError): self.parse(packet("subscriptionResponse", {"method":"subscribe", "subscription":bad}))
        for value in ("private", "NaN", "-1", True, 0.1):
            row["spread"] = value
            with self.assertRaises(StreamValidationError): self.parse(packet("l2Book", row))
