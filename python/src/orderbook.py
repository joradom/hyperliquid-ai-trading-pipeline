"""Pure public L2 calculations and explicitly different compatibility adapters.

No I/O, persistence, account data or trading policy. Decimal metrics describe
the returned aggregated snapshot, never the entire exchange book.
"""
from __future__ import annotations

import math
from decimal import Context, Decimal, localcontext
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from .stream_types import Book

CALCULATION_VERSION = "1.0"
MATH = Context(prec=192)
BANDS = (Decimal("0.001"), Decimal("0.0025"), Decimal("0.005"), Decimal("0.01"))
ZERO = Decimal(0)


def bbo(book: Book) -> tuple[Decimal | None, Decimal | None]:
    return (book.bids[0].price if book.bids else None,
            book.asks[0].price if book.asks else None)


def midpoint(bid, ask, *, overflow_safe=False):
    # Keep operation order explicit: REST consumers have different float maths.
    return bid / 2 + ask / 2 if overflow_safe else (ask + bid) / 2


def spread_absolute(bid, ask):
    return ask - bid


def spread_pct(absolute, mid):
    return absolute / mid * 100


def spread_bps(absolute, mid):
    return absolute / mid * 10_000


def obi(bid_base, ask_base):
    total = bid_base + ask_base
    return (bid_base - ask_base) / total if total else None


def decimal_text(value: Decimal | None) -> str | None:
    return None if value is None else str(value.normalize(MATH)).replace("E+", "e").replace("E", "e")


def _structure_reasons(book: Book | None) -> list[str]:
    if book is None:
        return ["missing_book"]
    if type(book.time_ms) is not int or not 1 <= book.time_ms <= 2**63 - 1:
        return ["invalid_book_timestamp"]
    for index, side in enumerate((book.bids, book.asks)):
        if not side:
            return ["missing_book_side"]
        if len(side) > 20:
            return ["invalid_book_depth"]
        for level in side:
            if any(not isinstance(value, Decimal) or not value.is_finite() or value <= 0
                   or not -32 <= value.adjusted() <= 32
                   or len(str(value)) > 64 for value in (level.price, level.size)):
                return ["invalid_level_price_or_size"]
            if type(level.order_count) is not int or not 1 <= level.order_count <= 2**63 - 1:
                return ["invalid_order_count"]
        if any((left.price <= right.price if index == 0 else left.price >= right.price)
               for left, right in zip(side, side[1:])):
            return ["unsorted_or_repeated_levels"]
    bid, ask = bbo(book)
    return ["crossed_or_locked_book"] if bid >= ask else []


def _depth(side):
    base = quote = ZERO
    levels = []
    for level in side:
        notional = level.price * level.size
        base += level.size
        quote += notional
        levels.append({"price": decimal_text(level.price), "size_base": decimal_text(level.size),
                       "notional_quote": decimal_text(notional), "order_count": level.order_count,
                       "cumulative_base": decimal_text(base), "cumulative_quote": decimal_text(quote)})
    return {"total_base": decimal_text(base), "total_quote": decimal_text(quote), "levels": levels}


def _band_side(side, lower, upper, *, bid):
    visible = [level for level in side if lower <= level.price <= upper]
    reaches_boundary = side[-1].price <= lower if bid else side[-1].price >= upper
    coverage = "complete" if reaches_boundary else "partial" if visible else "unknown"
    return {"coverage": coverage,
            "coverage_reason": "visible_range_reaches_boundary" if reaches_boundary
                               else "visible_range_stops_inside_band" if visible
                               else "no_in_band_levels_or_completeness_evidence",
            "observed_base": decimal_text(sum((level.size for level in visible), ZERO)),
            "observed_quote": decimal_text(sum((level.price * level.size for level in visible), ZERO)),
            "visible_levels": len(visible), "outermost_visible_price": decimal_text(side[-1].price)}


def calculate_book(book: Book | None) -> dict[str, Any]:
    """Detached metrics; valid OBI here means structural/spatial validity only.

    Consumers needing current data use l2_view, which adds temporal validity.
    Coverage refers to the visible aggregated price range, not hidden liquidity.
    """
    reasons = _structure_reasons(book)
    result = {"calculation_version": CALCULATION_VERSION,
              "structure": {"valid": not reasons, "reasons": reasons},
              "bbo": None, "depth": None,
              "obi_legacy": {"observed": None, "valid": None, "reasons": list(reasons),
                             "scope": "all_returned_levels_base", "entire_book_coverage": "unknown"},
              "bands": {}}
    if reasons:
        for band in BANDS:
            result["bands"][decimal_text(band)] = {
                "fraction": decimal_text(band), "lower_bound": None, "upper_bound": None,
                "coverage": "unknown", "bids": {"coverage": "unknown"}, "asks": {"coverage": "unknown"},
                "obi_observed": None, "obi_valid": None, "reasons": list(reasons)}
        return result
    with localcontext(MATH):
        bid, ask = bbo(book)
        mid = midpoint(bid, ask)
        absolute = spread_absolute(bid, ask)
        result["bbo"] = {"best_bid": decimal_text(bid), "best_ask": decimal_text(ask),
                         "midpoint": decimal_text(mid), "spread_absolute": decimal_text(absolute),
                         "spread_pct": decimal_text(spread_pct(absolute, mid)),
                         "spread_bps": decimal_text(spread_bps(absolute, mid))}
        bid_depth, ask_depth = _depth(book.bids), _depth(book.asks)
        result["depth"] = {"bids": bid_depth, "asks": ask_depth,
                           "levels_per_side": [len(book.bids), len(book.asks)]}
        imbalance = decimal_text(obi(Decimal(bid_depth["total_base"]), Decimal(ask_depth["total_base"])))
        result["obi_legacy"].update(observed=imbalance, valid=imbalance, reasons=[])
        for band in BANDS:
            lower, upper = mid * (1 - band), mid * (1 + band)
            bids = _band_side(book.bids, lower, mid, bid=True)
            asks = _band_side(book.asks, mid, upper, bid=False)
            statuses = (bids["coverage"], asks["coverage"])
            coverage = "complete" if statuses == ("complete", "complete") else "unknown" if "unknown" in statuses else "partial"
            observed = obi(Decimal(bids["observed_base"]), Decimal(asks["observed_base"]))
            band_reasons = []
            if coverage != "complete":
                band_reasons.append("band_coverage_" + coverage)
            if observed is None:
                band_reasons.append("no_visible_liquidity_in_band")
            result["bands"][decimal_text(band)] = {
                "fraction": decimal_text(band), "lower_bound": decimal_text(lower),
                "upper_bound": decimal_text(upper), "coverage": coverage, "bids": bids, "asks": asks,
                "obi_observed": decimal_text(observed),
                "obi_valid": decimal_text(observed) if not band_reasons else None, "reasons": band_reasons}
    return result


def l2_view(book: Book | None, *, as_of_ms: int, received_at_ms: int | None,
            network: str, symbol: str, book_epoch: int | None, current_epoch: int | None,
            n_sig_figs: int | None = 5, freshness_ms: int = 15_000,
            max_future_skew_ms: int = 5000, conflicting_timestamp_ms: int | None = None) -> dict[str, Any]:
    """Pure timestamped view; no heartbeat or global flow status renews L2.

    Observed metrics survive staleness/disconnection. Valid OBI additionally
    requires usable exchange/receipt times and matching known connection epochs.
    """
    for value in (as_of_ms, freshness_ms, max_future_skew_ms):
        if type(value) is not int or value < 0:
            raise ValueError("invalid L2 evaluation time/tolerance")
    if n_sig_figs is not None and (type(n_sig_figs) is not int or n_sig_figs not in (2, 3, 4, 5)):
        raise ValueError("invalid L2 precision")
    metrics = calculate_book(book)
    reasons = list(metrics["structure"]["reasons"])
    if book is not None and book.coin != symbol:
        reasons.append("book_symbol_mismatch")
    if book is not None and conflicting_timestamp_ms == book.time_ms:
        reasons.append("book_timestamp_conflict")
    if n_sig_figs is None:
        reasons.append("book_precision_unknown")

    def time_quality(stamp, name):
        if type(stamp) is not int or not 1 <= stamp <= 2**63 - 1:
            reasons.append(name + "_time_unknown")
            return {"age_ms": None, "status": "unknown"}
        age = as_of_ms - stamp
        status = "future" if age < -max_future_skew_ms else "stale" if age > freshness_ms else "fresh"
        if status != "fresh":
            reasons.append(name + "_" + status)
        return {"age_ms": age, "status": status}

    exchange = time_quality(book.time_ms if book else None, "book_exchange")
    receipt = time_quality(received_at_ms, "book_receipt")
    if any(type(epoch) is not int or epoch < 1 for epoch in (book_epoch, current_epoch)):
        reasons.append("book_epoch_unknown")
    elif book_epoch != current_epoch:
        reasons.append("book_epoch_mismatch")
    usable = not reasons
    if not usable:
        metrics["obi_legacy"]["valid"] = None
        metrics["obi_legacy"]["reasons"] = list(dict.fromkeys(metrics["obi_legacy"]["reasons"] + reasons))
        for band in metrics["bands"].values():
            band["obi_valid"] = None
            band["reasons"] = list(dict.fromkeys(band["reasons"] + reasons))
    return {"kind": "l2_metrics", "schema_version": "1.0", "read_only": True,
            "execution_authorized": False, "network": network, "symbol": symbol, "as_of_ms": as_of_ms,
            "source": {"channel": "l2Book", "mode": "full_snapshot", "timestamp_origin": "hyperliquid",
                       "received_at_origin": "r102_ws_local_clock", "book_time_ms": book.time_ms if book else None,
                       "received_at_ms": received_at_ms, "connection_epoch": book_epoch,
                       "current_connection_epoch": current_epoch,
                       "conflicting_timestamp_ms": conflicting_timestamp_ms},
            "precision": {"n_sig_figs": n_sig_figs, "mantissa": None, "arithmetic": "Decimal",
                          "decimal_precision": MATH.prec, "max_levels_per_side": 20,
                          "coverage_basis": "returned_aggregated_price_range"},
            "units": {"size": "base", "price": "quote_per_base", "notional": "quote", "obi": "ratio"},
            "freshness": {"exchange": exchange, "receipt": receipt,
                          "tolerance_ms": freshness_ms, "max_future_skew_ms": max_future_skew_ms},
            "quality": {"status": "ok" if usable else "degraded", "usable": usable,
                        "reasons": list(dict.fromkeys(reasons))}, "metrics": metrics}


def normalize_ws_book(raw: Any, now_ms: int, symbols: frozenset[str]) -> Book:
    # Reuse the existing transport-neutral field validators and exact types.
    # Local import avoids a cycle while stream_types delegates its L2 adapter.
    from .stream_types import (Book, Level, StreamValidationError, _keys,
                               _positive_decimal, _integer, _timestamp, symbol)
    raw = _keys(raw, {"coin", "time", "levels"}, {"spread"})
    if "spread" in raw:
        _positive_decimal(raw["spread"], allow_zero=True)
    coin = symbol(raw["coin"])
    if coin not in symbols:
        raise StreamValidationError("unexpected_book_symbol")
    sides = raw["levels"]
    if not isinstance(sides, list) or len(sides) != 2:
        raise StreamValidationError("invalid_book_sides")
    normalized = []
    for index, side in enumerate(sides):
        if not isinstance(side, list) or not 1 <= len(side) <= 20:
            raise StreamValidationError("invalid_book_depth")
        levels = []
        for raw_level in side:
            row = _keys(raw_level, {"px", "sz", "n"})
            levels.append(Level(_positive_decimal(row["px"]), _positive_decimal(row["sz"]),
                                _integer(row["n"], minimum=1)))
        prices = [level.price for level in levels]
        if any((left <= right if index == 0 else left >= right) for left, right in zip(prices, prices[1:])):
            raise StreamValidationError("unsorted_book")
        normalized.append(tuple(levels))
    if normalized[0][0].price >= normalized[1][0].price:
        raise StreamValidationError("crossed_or_locked_book")
    return Book(coin, _timestamp(raw["time"], now_ms), *normalized)


def market_state_book(raw: dict[str, Any], issues: list[dict[str, str]], *, number, display_number, iso) -> dict[str, Any]:
    """Market State 1.1 adapter: tolerant floats, 12-place display rounding.

    Existing conversion/ISO callbacks preserve even legacy exception behaviour.
    Do not tighten validation or add quality fields to this contract.
    """
    def issue(code, detail):
        issues.append({"code": code, "scope": "orderbook", "detail": detail})
    levels = raw.get("levels", [])
    if not isinstance(levels, list):
        levels = []; issue("malformed_response", "levels is not a list")
    bids = levels[0] if len(levels) > 0 and isinstance(levels[0], list) else []
    asks = levels[1] if len(levels) > 1 and isinstance(levels[1], list) else []
    if not bids or not asks:
        issue("missing_orderbook_side", "bid or ask levels are absent")
    def norm(side):
        result = []
        for level in side:
            if not isinstance(level, dict):
                issue("malformed_response", "level is not an object"); continue
            result.append({"price": display_number(level.get("px")), "size": number(level.get("sz")), "order_count": level.get("n")})
        return result
    bids, asks = norm(bids), norm(asks)
    best_bid = bids[0]["price"] if bids else None; best_ask = asks[0]["price"] if asks else None
    bid_depth = sum(x["size"] or 0 for x in bids); ask_depth = sum(x["size"] or 0 for x in asks)
    absolute = display_number(spread_absolute(best_bid, best_ask)) if best_bid is not None and best_ask is not None else None
    mid = display_number(midpoint(best_bid, best_ask)) if best_bid is not None and best_ask is not None else None
    try:
        source_ms = int(raw["time"]) if raw.get("time") is not None else None
    except (TypeError, ValueError):
        source_ms = None; issue("invalid_timestamp", "orderbook time is not an integer")
    return {"timestamp_ms": source_ms, "timestamp_utc": iso(source_ms), "best_bid": best_bid, "best_ask": best_ask,
            "midpoint": mid, "spread_absolute": absolute,
            "spread_pct_of_mid": spread_pct(absolute, mid) if absolute is not None and mid else None,
            "depth": {"bid_total_size": bid_depth, "ask_total_size": ask_depth, "levels_per_side": [len(bids), len(asks)]},
            "imbalance": {"value": obi(bid_depth, ask_depth),
                          "formula": "(bid_total_size - ask_total_size) / (bid_total_size + ask_total_size)"},
            "levels": {"bids": bids, "asks": asks}}


def revalidation_book(raw: Any, symbol: str, *, number) -> dict[str, Any]:
    """Fast Revalidation adapter: strict floats, unchanged errors and ordering."""
    if not isinstance(raw, dict) or raw.get("coin", symbol) != symbol:
        raise ValueError("wrong/malformed book identity")
    stamp, sides = raw.get("time"), raw.get("levels")
    if type(stamp) is not int or not isinstance(sides, list) or len(sides) != 2:
        raise ValueError("book timestamp/sides malformed")
    normalized = []
    for index, side in enumerate(sides):
        if not isinstance(side, list) or not side:
            raise ValueError("missing book side")
        levels = []
        for level in side:
            if not isinstance(level, dict):
                raise ValueError("malformed level")
            price, size = number(level.get("px")), number(level.get("sz"))
            if price is None or price <= 0 or size is None or size < 0:
                raise ValueError("invalid level price/size")
            levels.append({"price": price, "size": size})
        for left, right in zip(levels, levels[1:]):
            if (index == 0 and left["price"] < right["price"]) or (index == 1 and left["price"] > right["price"]):
                raise ValueError("unsorted book")
        normalized.append(levels)
    bid, ask = normalized[0][0]["price"], normalized[1][0]["price"]
    depths = [sum(level["size"] for level in side) for side in normalized]
    if bid > ask or any(not math.isfinite(depth) or depth <= 0 for depth in depths) or not math.isfinite(sum(depths)):
        raise ValueError("crossed book or unusable depth")
    mid = midpoint(bid, ask, overflow_safe=True)
    absolute = spread_absolute(bid, ask)
    return {"source_timestamp_ms": stamp, "best_bid": bid, "best_ask": ask,
            "l2_midpoint": mid, "spread_absolute": absolute,
            "spread_bps": spread_bps(absolute, mid),
            "depth": {"bid_total_size": depths[0], "ask_total_size": depths[1],
                      "levels_per_side": [len(side) for side in normalized]},
            "orderbook_imbalance": obi(depths[0], depths[1])}
