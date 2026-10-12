"""Deterministic per-network/asset order flow on normalized public trades.

Hyperliquid uses B buy aggressor / A sell aggressor. Legacy checkpoints retain
their original calculation until an explicit new connection segment begins.
"""
from __future__ import annotations

import copy
import heapq
from collections import Counter, OrderedDict, deque
from dataclasses import dataclass
from decimal import Decimal, Context, localcontext
from typing import Any

from .stream_types import (Trade, restore_trade, trade_record, symbol, SIDE_SEMANTICS,
                           SIDE_SEMANTICS_VERSION, SYNTHETIC_SIDE_SEMANTICS)

SCHEMA_VERSION = "1.0"
CALCULATION_VERSION = "2.0"
LEGACY_CALCULATION_VERSION = "1.0"
SUPPORTED_CALCULATIONS = frozenset((LEGACY_CALCULATION_VERSION, CALCULATION_VERSION))
WINDOWS = (60, 300, 900)
NETWORKS = frozenset(("mainnet", "testnet", "synthetic"))
GAP_REASONS = frozenset(("disconnect", "capture_end", "restart", "ws_continuity_change", "identity_conflict",
                        "late_trade", "pending_overflow", "stream_stale", "dedup_evicted"))
MATH = Context(prec=192)
ZERO = Decimal(0)


def semantics_for(network, calculation_version):
    if network == "synthetic": return SYNTHETIC_SIDE_SEMANTICS
    return SIDE_SEMANTICS if calculation_version == CALCULATION_VERSION else "unverified"


def decimal_text(value: Decimal | None) -> str | None:
    return None if value is None else str(value.normalize(MATH)).replace("E+", "e").replace("E", "e")


@dataclass(frozen=True)
class Operation:
    kind: str
    network: str
    coin: str
    at_ms: int
    trade: Trade | None = None
    contact_ms: int | None = None
    source_epoch: int = 0
    source_continuity: int = 0
    pre_connection: bool = False
    reason: str | None = None

    def __post_init__(self):
        if self.network not in NETWORKS or self.kind not in ("connect", "disconnect", "gap", "advance", "trade"):
            raise ValueError("invalid flow operation")
        symbol(self.coin)
        for value in (self.at_ms, self.source_epoch, self.source_continuity):
            if type(value) is not int or value < 0: raise ValueError("invalid flow timestamp/epoch")
        if self.contact_ms is not None and (type(self.contact_ms) is not int or self.contact_ms < 0):
            raise ValueError("invalid contact timestamp")
        if type(self.pre_connection) is not bool: raise ValueError("invalid history flag")
        if self.reason is not None and self.reason not in GAP_REASONS: raise ValueError("invalid gap reason")
        if self.kind == "trade" and (not isinstance(self.trade, Trade) or self.trade.coin != self.coin):
            raise ValueError("requires normalized matching trade")
        if self.kind == "trade" and restore_trade(trade_record(self.trade)) != self.trade:
            raise ValueError("invalid normalized trade")
        if self.kind != "trade" and self.trade is not None: raise ValueError("unexpected trade")

    @classmethod
    def from_received(cls, event, network="mainnet"):
        return cls("trade", network, event.trade.coin, event.received_at_ms, event.trade,
                   source_epoch=event.connection_epoch, source_continuity=event.continuity_epoch,
                   pre_connection=event.pre_connection)

    def record(self):
        return {"kind": self.kind, "network": self.network, "coin": self.coin, "at_ms": self.at_ms,
                "trade": trade_record(self.trade) if self.trade else None, "contact_ms": self.contact_ms,
                "source_epoch": self.source_epoch, "source_continuity": self.source_continuity,
                "pre_connection": self.pre_connection, "reason": self.reason}

    @classmethod
    def restore(cls, record):
        row = dict(record)
        if row["trade"] is not None: row["trade"] = restore_trade(row["trade"])
        return cls(**row)


class Aggregate:
    def __init__(self, signed=False):
        self.count = 0
        self.base = self.quote = ZERO
        self.buy_base = self.sell_base = ZERO if signed else None
        self.buy_quote = self.sell_quote = ZERO if signed else None

    def add_trade(self, trade):
        with localcontext(MATH):
            quote = trade.price * trade.size
            self.count += 1; self.base += trade.size; self.quote += quote
            if self.buy_base is not None:
                if trade.side == "B": self.buy_base += trade.size; self.buy_quote += quote
                else: self.sell_base += trade.size; self.sell_quote += quote

    def add(self, other):
        with localcontext(MATH):
            self.count += other.count; self.base += other.base; self.quote += other.quote
            if self.buy_base is not None:
                self.buy_base += other.buy_base; self.sell_base += other.sell_base
                self.buy_quote += other.buy_quote; self.sell_quote += other.sell_quote

    def delta(self, unit="base"):
        with localcontext(MATH):
            buy, sell = getattr(self, "buy_"+unit), getattr(self, "sell_"+unit)
            return None if buy is None else buy - sell

    def record(self):
        return {"count": self.count, **{name: decimal_text(getattr(self, name)) for name in
                ("base", "quote", "buy_base", "sell_base", "buy_quote", "sell_quote")}}

    @classmethod
    def restore(cls, record, *, expected_signed=None):
        signed = record["buy_base"] is not None
        if expected_signed is not None and signed != expected_signed:
            raise ValueError("aggregate side semantics mismatch")
        if type(record["count"]) is not int or record["count"] < 0:
            raise ValueError("invalid aggregate count")
        result = cls(record["buy_base"] is not None)
        result.count = record["count"]
        for name in ("base", "quote", "buy_base", "sell_base", "buy_quote", "sell_quote"):
            setattr(result, name, Decimal(record[name]) if record[name] is not None else None)
        for name in ("base", "quote", "buy_base", "sell_base", "buy_quote", "sell_quote"):
            value = getattr(result, name)
            required = name in ("base", "quote") or signed
            if required and (value is None or not value.is_finite() or value < 0):
                raise ValueError("invalid aggregate value")
            if not required and value is not None:
                raise ValueError("mixed signed aggregate")
        if signed:
            with localcontext(MATH):
                if result.buy_base+result.sell_base != result.base or result.buy_quote+result.sell_quote != result.quote:
                    raise ValueError("aggregate volume mismatch")
        return result


class AssetFlow:
    def __init__(self, network, coin):
        self.network, self.coin = network, coin
        self.calculation_version = CALCULATION_VERSION
        self.side_semantics_version = SIDE_SEMANTICS_VERSION
        self.signed = True
        self.connected = False
        self.segment_id = 0
        self.segment_start_ms = self.segment_end_ms = None
        self.source_epoch = self.source_continuity = 0
        self.coverage_through_ms = self.evaluated_at_ms = 0
        self.watermark_ms = 0
        self.contact_ms = self.last_received_ms = None
        self.max_event_ms = None
        self.last_trade = None
        self.closed = Aggregate(self.signed)
        self.observed = Aggregate(False)  # Unsigned lifetime observation, never joined CVD.
        self.pending = []
        self.buckets: OrderedDict[int, Aggregate] = OrderedDict()
        self.seen: OrderedDict[tuple, Trade] = OrderedDict()
        self.counts, self.reasons = Counter(), Counter()
        self.closed_segments = deque(maxlen=8)
        self.retention_floor_ms = -1


class OrderFlow:
    def __init__(self, *, lateness_ms=2000, max_pending=4096, max_buckets=960,
                 max_seen=100_000, freshness_ms=30_000, max_assets=64):
        for value in (max_pending, max_buckets, max_seen, freshness_ms, max_assets):
            if type(value) is not int or value < 1: raise ValueError("invalid flow limit")
        if type(lateness_ms) is not int or lateness_ms < 0: raise ValueError("invalid lateness")
        self.config = dict(lateness_ms=lateness_ms, max_pending=max_pending, max_buckets=max_buckets,
                           max_seen=max_seen, freshness_ms=freshness_ms, max_assets=max_assets)
        self.assets: dict[tuple, AssetFlow] = {}
        self.durable_dedup = False

    def asset(self, network, coin):
        if network not in NETWORKS: raise ValueError("invalid network")
        symbol(coin)
        key = (network, coin)
        if key not in self.assets:
            if len(self.assets) >= self.config["max_assets"]: raise ValueError("asset capacity reached")
            self.assets[key] = AssetFlow(network, coin)
        return self.assets[key]

    def _end(self, asset, at_ms):
        if asset.connected:
            asset.closed_segments.append({"segment_id": asset.segment_id, "from_ms": asset.segment_start_ms,
                                          "through_ms": asset.watermark_ms, "ended_at_ms": at_ms,
                                          "calculation_version":asset.calculation_version,
                                          "side_semantics_version":asset.side_semantics_version,
                                          "side_semantics":semantics_for(asset.network,asset.calculation_version),
                                          "observed": asset.closed.record(), "pending_abandoned": len(asset.pending)})
        asset.counts["pending_abandoned"] += len(asset.pending)
        asset.pending.clear()
        asset.connected = False; asset.segment_end_ms = at_ms
        asset.evaluated_at_ms=max(asset.evaluated_at_ms,at_ms)

    def _connect(self, asset, op):
        if asset.connected: return
        if asset.calculation_version == LEGACY_CALCULATION_VERSION:
            # Keep historical values unsigned; only fresh segment aggregates
            # use the new semantics. No replay or DB migration is implicit.
            for segment in asset.closed_segments:
                segment.setdefault("calculation_version", LEGACY_CALCULATION_VERSION)
                segment.setdefault("side_semantics_version", None)
                segment.setdefault("side_semantics", semantics_for(asset.network, LEGACY_CALCULATION_VERSION))
            asset.calculation_version = CALCULATION_VERSION
            asset.side_semantics_version = SIDE_SEMANTICS_VERSION
            asset.signed = True
        asset.connected = True; asset.segment_id += 1
        asset.segment_start_ms = op.at_ms; asset.segment_end_ms = None
        asset.source_epoch, asset.source_continuity = op.source_epoch, op.source_continuity
        asset.coverage_through_ms = op.at_ms; asset.contact_ms = op.at_ms
        asset.evaluated_at_ms = max(asset.evaluated_at_ms, op.at_ms)
        asset.watermark_ms = (op.at_ms - self.config["lateness_ms"]) // 1000 * 1000
        asset.closed = Aggregate(asset.signed); asset.buckets.clear(); asset.pending.clear()

    def _gap(self, asset, at_ms, reason):
        connected = asset.connected
        asset.counts["gaps"] += 1; asset.reasons[reason] += 1
        self._end(asset, at_ms)
        if connected:
            self._connect(asset, Operation("connect", asset.network, asset.coin, at_ms,
                          source_epoch=asset.source_epoch, source_continuity=asset.source_continuity))

    def note_duplicate(self, network, coin):
        self.asset(network, coin).counts["duplicates"] += 1

    def note_conflict(self, network, coin, at_ms):
        asset = self.asset(network, coin)
        asset.counts["conflicts"] += 1
        self._gap(asset, max(at_ms, asset.evaluated_at_ms), "identity_conflict")

    def _trade(self, asset, op):
        trade = op.trade
        key = (trade.time_ms, trade.tid)
        if key in asset.seen:
            if asset.seen[key] == trade: self.note_duplicate(asset.network, asset.coin); return "duplicate"
            self.note_conflict(asset.network, asset.coin, op.at_ms); return "conflict"
        if trade.time_ms <= asset.retention_floor_ms:
            asset.counts["outside_retention"] += 1
            if asset.connected and trade.time_ms>=asset.segment_start_ms:
                self._gap(asset,max(op.at_ms,asset.evaluated_at_ms),"late_trade")
            return "outside_retention"
        if len(asset.seen) >= self.config["max_seen"]:
            asset.seen.popitem(last=False); asset.counts["seen_evictions"] += 1
            if not self.durable_dedup: self._gap(asset, max(op.at_ms, asset.evaluated_at_ms), "dedup_evicted")
        asset.seen[key] = trade
        asset.counts["unique_trades"] += 1; asset.observed.add_trade(trade)
        asset.last_received_ms = max(op.at_ms, asset.last_received_ms or 0)
        if asset.max_event_ms is None or trade.time_ms >= asset.max_event_ms:
            asset.max_event_ms = trade.time_ms; asset.last_trade = trade
        else: asset.counts["out_of_order"] += 1
        if (not asset.connected or op.source_epoch != asset.source_epoch
                or op.source_continuity != asset.source_continuity):
            asset.counts["outside_active_epoch"] += 1; return "prior_epoch"
        if op.pre_connection or trade.time_ms < asset.segment_start_ms:
            asset.counts["bootstrap_trades"] += 1; return "bootstrap"
        if trade.time_ms < asset.watermark_ms:
            asset.counts["late_trades"] += 1
            self._gap(asset, max(op.at_ms, asset.evaluated_at_ms), "late_trade")
            return "late"
        if len(asset.pending) >= self.config["max_pending"]:
            asset.counts["pending_overflow"] += 1
            self._gap(asset, max(op.at_ms, asset.evaluated_at_ms), "pending_overflow")
            return "overflow"
        heapq.heappush(asset.pending, (trade.time_ms, trade.tid, op))
        return "accepted"

    def _advance(self, asset, op):
        if op.at_ms < asset.evaluated_at_ms: raise ValueError("evaluation time regressed")
        asset.evaluated_at_ms = op.at_ms
        if not asset.connected: return
        contact = op.contact_ms if op.contact_ms is not None else asset.contact_ms
        if contact is None or not -5000 <= op.at_ms - contact <= self.config["freshness_ms"]:
            asset.counts["gaps"] += 1; asset.reasons["stream_stale"] += 1
            self._end(asset, op.at_ms); return
        asset.contact_ms = contact; asset.coverage_through_ms = op.at_ms
        watermark = (op.at_ms - self.config["lateness_ms"]) // 1000 * 1000
        while asset.pending and asset.pending[0][0] < watermark:
            _, _, event = heapq.heappop(asset.pending)
            second = event.trade.time_ms // 1000 * 1000
            if second not in asset.buckets: asset.buckets[second] = Aggregate(asset.signed)
            asset.buckets[second].add_trade(event.trade); asset.closed.add_trade(event.trade)
            asset.counts["closed_trades"] += 1
        asset.buckets = OrderedDict(sorted(asset.buckets.items()))
        floor = watermark - self.config["max_buckets"] * 1000
        while asset.buckets and next(iter(asset.buckets)) < floor: asset.buckets.popitem(last=False)
        asset.watermark_ms = watermark

    def apply(self, op: Operation):
        asset = self.asset(op.network, op.coin)
        if op.kind == "connect": self._connect(asset, op)
        elif op.kind == "disconnect":
            if asset.connected:
                asset.counts["segment_boundaries" if op.reason=="capture_end" else "gaps"] += 1
                asset.reasons[op.reason or "disconnect"] += 1
                self._end(asset, op.at_ms)
        elif op.kind == "gap": self._gap(asset, max(op.at_ms, asset.evaluated_at_ms), op.reason or "ws_continuity_change")
        elif op.kind == "advance": self._advance(asset, op)
        else: return self._trade(asset, op)
        return op.kind

    def _slope(self, asset, start, end, unit):
        with localcontext(MATH):
            n = (end-start)//1000; y = sy = sxy = ZERO
            for index, second in enumerate(range(start, end, 1000)):
                bucket = asset.buckets.get(second)
                y += bucket.delta(unit) if bucket else ZERO
                sy += y; sxy += index*y
            sx = n*(n-1)//2; sxx = n*(n-1)*(2*n-1)//6
            return (n*sxy-sx*sy)/(n*sxx-sx*sx) if n > 1 else None

    def snapshot(self, network, coin, *, as_of_ms=None):
        asset = self.asset(network, coin)
        earliest=max(asset.evaluated_at_ms,asset.last_received_ms or 0,asset.segment_end_ms or 0)
        as_of = earliest if as_of_ms is None else as_of_ms
        if type(as_of) is not int or as_of < earliest: raise ValueError("invalid snapshot time")
        fresh = asset.connected and asset.contact_ms is not None and -5000 <= as_of-asset.contact_ms <= self.config["freshness_ms"]
        windows = {}
        for seconds in WINDOWS:
            end = asset.watermark_ms; start = end-seconds*1000
            covered = max(0, end-max(start, asset.segment_start_ms if asset.segment_start_ms is not None else end))
            ready = fresh and asset.segment_start_ms is not None and start >= asset.segment_start_ms and seconds <= self.config["max_buckets"]
            reasons = []
            if not asset.connected: reasons.append("disconnected")
            elif not fresh: reasons.append("stream_stale")
            if covered < seconds*1000: reasons.append("warmup")
            if seconds > self.config["max_buckets"]: reasons.append("history_retention")
            if not asset.signed: reasons.append("side_semantics_unverified")
            total = Aggregate(asset.signed)
            for second, bucket in asset.buckets.items():
                if start <= second < end: total.add(bucket)
            signed = ready and asset.signed
            with localcontext(MATH):
                ratio = total.buy_base/total.sell_base if signed and total.sell_base else None
                frequency = Decimal(total.count)/seconds if ready else None
            if signed and not total.sell_base: reasons.append("zero_sell_denominator")
            delta_base, delta_quote = (total.delta("base"),total.delta("quote")) if signed else (None,None)
            windows[str(seconds//60)+"m"] = {
                "from_ms": start, "to_ms": end, "coverage_ms": covered,
                "coverage_ratio": covered/(seconds*1000), "coverage_basis": "known_connectivity_not_exchange_completeness",
                "exchange_completeness": "unknown", "ready": ready, "signed_available": signed,
                "trades": total.count if ready else None, "trades_per_second": decimal_text(frequency),
                "volume_base": decimal_text(total.base) if ready else None,
                "volume_quote": decimal_text(total.quote) if ready else None,
                "observed_partial": {"trades":total.count,"volume_base":decimal_text(total.base),"volume_quote":decimal_text(total.quote)},
                "buy_volume_base":decimal_text(total.buy_base) if signed else None,
                "sell_volume_base":decimal_text(total.sell_base) if signed else None,
                "buy_volume_quote":decimal_text(total.buy_quote) if signed else None,
                "sell_volume_quote":decimal_text(total.sell_quote) if signed else None,
                "delta_base":decimal_text(delta_base), "delta_quote":decimal_text(delta_quote),
                "cvd_window_base":decimal_text(delta_base), "cvd_window_quote":decimal_text(delta_quote),
                "cvd_slope_base_per_second":decimal_text(self._slope(asset,start,end,"base")) if signed else None,
                "cvd_slope_quote_per_second":decimal_text(self._slope(asset,start,end,"quote")) if signed else None,
                "buy_sell_ratio_base":decimal_text(ratio), "quality_reasons":reasons}
        cvd_available = fresh and asset.signed and asset.segment_start_ms is not None and asset.watermark_ms >= asset.segment_start_ms
        quality_reasons=[]
        if not asset.signed:quality_reasons.append("side_semantics_unverified")
        if not asset.connected:quality_reasons.append("disconnected")
        elif not fresh:quality_reasons.append("stream_stale")
        if any(not window["ready"] for window in windows.values()):quality_reasons.append("window_warmup_or_unusable")
        if asset.counts["gaps"]:quality_reasons.append("historical_gaps")
        observed_available=asset.observed.count>0 or asset.segment_id>0
        return {"kind":"order_flow", "schema_version":SCHEMA_VERSION, "calculation_version":asset.calculation_version,
                "network":network,"symbol":coin,"as_of_ms":as_of,"read_only":True,"execution_authorized":False,
                "side_semantics":semantics_for(network,asset.calculation_version),
                "side_semantics_version":asset.side_semantics_version,
                "connection":{"connected":asset.connected,"fresh":fresh,"last_contact_ms":asset.contact_ms,
                              "coverage_from_ms":asset.segment_start_ms,"coverage_through_ms":asset.coverage_through_ms},
                "last_trade":trade_record(asset.last_trade) if asset.last_trade else None,
                "last_received_ms":asset.last_received_ms,
                "freshness":{"contact_age_ms":as_of-asset.contact_ms if asset.contact_ms is not None else None,
                             "trade_event_age_ms":as_of-asset.max_event_ms if asset.max_event_ms is not None else None,
                             "trade_receipt_age_ms":as_of-asset.last_received_ms if asset.last_received_ms is not None else None,
                             "contact_tolerance_ms":self.config["freshness_ms"]},
                "observed_total":{"available":observed_available,"unique_trades":asset.observed.count if observed_available else None,
                                  "volume_base":decimal_text(asset.observed.base) if observed_available else None,
                                  "volume_quote":decimal_text(asset.observed.quote) if observed_available else None,
                                  "includes_bootstrap_and_prior_epochs":True},
                "cvd":{"segment_id":asset.segment_id,"from_ms":asset.segment_start_ms,"through_ms":asset.watermark_ms,
                       "base":decimal_text(asset.closed.delta()) if cvd_available else None,
                       "quote":decimal_text(asset.closed.delta("quote")) if cvd_available else None,
                       "pending_trades":len(asset.pending),"available":cvd_available,"quality_reasons":quality_reasons},
                "quality":{"status":"degraded" if quality_reasons else "observed","reasons":quality_reasons,
                           "exchange_completeness":"unknown"},
                "windows":windows,"counts":dict(asset.counts),"gap_reasons":dict(asset.reasons),
                "memory":{"pending":len(asset.pending),"buckets":len(asset.buckets),"seen":len(asset.seen)},
                "dedup_retention_floor_ms":asset.retention_floor_ms,"exchange_completeness":"unknown"}

    def export_asset(self, network, coin, *, include_seen=True):
        asset = self.asset(network, coin)
        result = {"schema_version":SCHEMA_VERSION,"calculation_version":asset.calculation_version,"config":dict(self.config),
                  "network":network,"coin":coin,"signed":asset.signed}
        if asset.calculation_version == CALCULATION_VERSION:
            result.update(side_semantics=semantics_for(network,asset.calculation_version),
                          side_semantics_version=asset.side_semantics_version)
        for key in ("connected","segment_id","segment_start_ms","segment_end_ms","source_epoch","source_continuity",
                    "coverage_through_ms","evaluated_at_ms","watermark_ms","contact_ms","last_received_ms","max_event_ms","retention_floor_ms"):
            result[key] = getattr(asset,key)
        result.update(closed=asset.closed.record(),observed=asset.observed.record(),counts=dict(asset.counts),reasons=dict(asset.reasons),
                      last_trade=trade_record(asset.last_trade) if asset.last_trade else None,
                      pending=[item[2].record() for item in sorted(asset.pending)],
                      buckets=[[stamp,value.record()] for stamp,value in asset.buckets.items()],closed_segments=list(asset.closed_segments))
        if include_seen: result["seen"] = [trade_record(value) for value in asset.seen.values()]
        return copy.deepcopy(result)

    def import_asset(self, record):
        version = record["calculation_version"]
        if record["schema_version"] != SCHEMA_VERSION or version not in SUPPORTED_CALCULATIONS or record["config"] != self.config:
            raise ValueError("unsupported flow checkpoint configuration/version")
        expected_signed = version == CALCULATION_VERSION or record["network"] == "synthetic"
        if type(record["signed"]) is not bool or record["signed"] != expected_signed:
            raise ValueError("checkpoint side semantics mismatch")
        if version == CALCULATION_VERSION and (
                record.get("side_semantics_version") != SIDE_SEMANTICS_VERSION or
                record.get("side_semantics") != semantics_for(record["network"], version)):
            raise ValueError("unsupported checkpoint side semantics")
        if version == LEGACY_CALCULATION_VERSION and (
                record.get("side_semantics_version") is not None or "side_semantics" in record):
            raise ValueError("legacy checkpoint cannot claim new semantics")
        self.asset(record["network"],record["coin"])
        asset = AssetFlow(record["network"], record["coin"])
        asset.calculation_version = version
        asset.side_semantics_version = SIDE_SEMANTICS_VERSION if version == CALCULATION_VERSION else None
        asset.signed = expected_signed
        for key in ("connected","segment_id","segment_start_ms","segment_end_ms","source_epoch","source_continuity",
                    "coverage_through_ms","evaluated_at_ms","watermark_ms","contact_ms","last_received_ms","max_event_ms","retention_floor_ms"):
            setattr(asset,key,record[key])
        asset.closed = Aggregate.restore(record["closed"], expected_signed=asset.signed)
        asset.observed = Aggregate.restore(record["observed"], expected_signed=False)
        asset.counts, asset.reasons = Counter(record["counts"]), Counter(record["reasons"])
        asset.last_trade = restore_trade(record["last_trade"]) if record["last_trade"] else None
        for row in record["pending"]:
            op = Operation.restore(row)
            if op.kind != "trade" or (op.network,op.coin) != (asset.network,asset.coin):
                raise ValueError("pending trade belongs to another asset")
            heapq.heappush(asset.pending,(op.trade.time_ms,op.trade.tid,op))
        asset.buckets = OrderedDict((stamp,Aggregate.restore(value,expected_signed=asset.signed)) for stamp,value in record["buckets"])
        asset.closed_segments = deque(copy.deepcopy(record["closed_segments"]),maxlen=8)
        for segment in asset.closed_segments:
            archived_version = segment.get("calculation_version", LEGACY_CALCULATION_VERSION)
            if archived_version not in SUPPORTED_CALCULATIONS:
                raise ValueError("unsupported archived calculation")
            if archived_version == CALCULATION_VERSION and (
                    segment.get("side_semantics_version") != SIDE_SEMANTICS_VERSION or
                    segment.get("side_semantics") != semantics_for(asset.network, archived_version)):
                raise ValueError("unsupported archived side semantics")
            if archived_version == LEGACY_CALCULATION_VERSION and (
                    segment.get("side_semantics_version") is not None or
                    segment.get("side_semantics",semantics_for(asset.network,archived_version)) != semantics_for(asset.network,archived_version)):
                raise ValueError("legacy archive cannot claim new semantics")
            Aggregate.restore(segment["observed"], expected_signed=(
                archived_version == CALCULATION_VERSION or asset.network == "synthetic"))
        for row in record.get("seen",[]):
            trade = restore_trade(row); asset.seen[(trade.time_ms,trade.tid)] = trade
        if (len(asset.pending)>self.config["max_pending"] or len(asset.buckets)>self.config["max_buckets"]
                or len(asset.seen)>self.config["max_seen"]): raise ValueError("checkpoint exceeds limits")
        self.assets[(asset.network,asset.coin)] = asset
