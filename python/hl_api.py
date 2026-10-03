#!/usr/bin/env python3
"""Read-only CLI for Hyperliquid's official HTTP Info API."""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from statistics import mean
from typing import Any

from src.account_config import resolve_account_address

API_URL = "https://api.hyperliquid.xyz/info"


class ApiError(RuntimeError):
    """A request to the Hyperliquid Info API failed."""


def post_info(payload: dict[str, Any], timeout: float = 20.0) -> Any:
    body = json.dumps(payload, separators=(",", ":")).encode()
    request = urllib.request.Request(
        API_URL,
        data=body,
        headers={"Content-Type": "application/json", "User-Agent": "hl-readonly-cli/0.1"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        raise ApiError(f"HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise ApiError(str(exc)) from exc


def iso_time(value: Any) -> str:
    if value is None:
        return "-"
    try:
        return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc).isoformat(timespec="seconds")
    except (TypeError, ValueError, OSError):
        return str(value)


def table(rows: list[list[Any]], headers: list[str]) -> None:
    rendered = [["-" if value is None else str(value) for value in row] for row in rows]
    widths = [len(header) for header in headers]
    for row in rendered:
        for index, value in enumerate(row):
            widths[index] = max(widths[index], len(value))
    print("  ".join(header.ljust(widths[i]) for i, header in enumerate(headers)))
    print("  ".join("-" * width for width in widths))
    for row in rendered:
        print("  ".join(value.ljust(widths[i]) for i, value in enumerate(row)))


def filter_oids(items: list[dict[str, Any]], oids: list[int], nested: bool = False) -> list[dict[str, Any]]:
    if not oids:
        return items
    wanted = set(oids)
    return [item for item in items if (item.get("order", {}) if nested else item).get("oid") in wanted]


def account(user: str, raw: bool) -> None:
    perp = post_info({"type": "clearinghouseState", "user": user})
    spot = post_info({"type": "spotClearinghouseState", "user": user})
    if raw:
        print(json.dumps({"perpetuals": perp, "spot": spot}, indent=2))
        return
    summary = perp.get("marginSummary", {})
    print(f"User: {user}")
    table(
        [[summary.get("accountValue"), summary.get("totalNtlPos"), summary.get("totalMarginUsed"), perp.get("withdrawable")]],
        ["accountValue", "totalNtlPos", "totalMarginUsed", "withdrawable"],
    )
    positions = perp.get("assetPositions", [])
    if positions:
        print("\nPerpetual positions:")
        table(
            [[p.get("position", {}).get(k) for k in ("coin", "szi", "entryPx", "positionValue", "unrealizedPnl", "returnOnEquity")] for p in positions],
            ["coin", "size", "entryPx", "positionValue", "unrealizedPnl", "returnOnEquity"],
        )
    balances = spot.get("balances", [])
    if balances:
        print("\nSpot balances:")
        table([[b.get("coin"), b.get("total"), b.get("hold"), b.get("entryNtl")] for b in balances], ["coin", "total", "hold", "entryNtl"])


def fills(user: str, raw: bool, oids: list[int]) -> None:
    data = post_info({"type": "userFills", "user": user, "aggregateByTime": False})
    data = filter_oids(data, oids)
    if raw:
        print(json.dumps(data, indent=2))
        return
    table(
        [[iso_time(f.get("time")), f.get("coin"), f.get("dir", f.get("side")), f.get("px"), f.get("sz"), f.get("oid"), f.get("fee"), f.get("feeToken"), f.get("closedPnl")] for f in data],
        ["time (UTC)", "asset", "direction", "price", "size", "order ID", "fee", "fee token", "closedPnl"],
    )


def orders(user: str, raw: bool, oids: list[int]) -> None:
    data = post_info({"type": "historicalOrders", "user": user})
    data = filter_oids(data, oids, nested=True)
    if raw:
        print(json.dumps(data, indent=2))
        return
    rows = []
    for item in data:
        order = item.get("order", {})
        rows.append([iso_time(order.get("timestamp")), order.get("coin"), order.get("side"), order.get("limitPx"), order.get("origSz"), order.get("oid"), item.get("status"), iso_time(item.get("statusTimestamp"))])
    table(rows, ["order time (UTC)", "asset", "side", "limitPx", "origSz", "order ID", "status", "status time (UTC)"])


def funding(user: str, raw: bool, start_time: int, end_time: int | None) -> None:
    payload: dict[str, Any] = {"type": "userFunding", "user": user, "startTime": start_time}
    if end_time is not None:
        payload["endTime"] = end_time
    data = post_info(payload)
    if raw:
        print(json.dumps(data, indent=2))
        return
    rows = []
    for item in data:
        delta = item.get("delta", {})
        rows.append([iso_time(item.get("time")), delta.get("coin"), delta.get("usdc"), delta.get("szi"), delta.get("fundingRate"), item.get("hash")])
    table(rows, ["time (UTC)", "asset", "usdc", "position size", "funding rate", "hash"])


def pnl(user: str, raw: bool) -> None:
    portfolio = post_info({"type": "portfolio", "user": user})
    state = post_info({"type": "clearinghouseState", "user": user})
    if raw:
        print(json.dumps({"portfolio": portfolio, "clearinghouseState": state}, indent=2))
        return
    print("PnL history (latest value returned by Hyperliquid):")
    rows = []
    for period, values in portfolio:
        history = values.get("pnlHistory", [])
        latest_time, latest_pnl = history[-1] if history else (None, None)
        rows.append([period, iso_time(latest_time), latest_pnl, values.get("vlm")])
    table(rows, ["period", "latest time (UTC)", "pnl", "volume"])
    positions = state.get("assetPositions", [])
    if positions:
        print("\nOpen-position PnL fields returned by Hyperliquid:")
        table(
            [[p.get("position", {}).get(k) for k in ("coin", "szi", "unrealizedPnl", "returnOnEquity")] for p in positions],
            ["asset", "size", "unrealizedPnl", "returnOnEquity"],
        )


def ema(values: list[float], period: int) -> float | None:
    if len(values) < period:
        return None
    value = mean(values[:period])
    alpha = 2 / (period + 1)
    for item in values[period:]:
        value = item * alpha + value * (1 - alpha)
    return value


def rsi(values: list[float], period: int = 14) -> float | None:
    if len(values) <= period:
        return None
    changes = [right - left for left, right in zip(values, values[1:])]
    gains = [max(change, 0.0) for change in changes[-period:]]
    losses = [max(-change, 0.0) for change in changes[-period:]]
    avg_gain, avg_loss = mean(gains), mean(losses)
    if avg_loss == 0:
        return 100.0
    return 100 - 100 / (1 + avg_gain / avg_loss)


def candle_metrics(candles: list[dict[str, Any]]) -> dict[str, Any]:
    candles = sorted(candles, key=lambda item: int(item["t"]))
    closes = [float(item["c"]) for item in candles]
    highs = [float(item["h"]) for item in candles]
    lows = [float(item["l"]) for item in candles]
    volumes = [float(item["v"]) for item in candles]
    true_ranges = []
    for index, candle in enumerate(candles):
        high, low = float(candle["h"]), float(candle["l"])
        previous = closes[index - 1] if index else float(candle["o"])
        true_ranges.append(max(high - low, abs(high - previous), abs(low - previous)))
    last = closes[-1]
    recent_volumes = volumes[-21:-1]
    return {
        "lastCandleTime": candles[-1]["t"],
        "open": float(candles[-1]["o"]),
        "high": highs[-1],
        "low": lows[-1],
        "close": last,
        "ema20": ema(closes, 20),
        "ema50": ema(closes, 50),
        "rsi14": rsi(closes),
        "atr14": mean(true_ranges[-14:]) if len(true_ranges) >= 14 else None,
        "atr14Pct": mean(true_ranges[-14:]) / last * 100 if len(true_ranges) >= 14 else None,
        "return1Pct": (last / closes[-2] - 1) * 100 if len(closes) >= 2 else None,
        "return6Pct": (last / closes[-7] - 1) * 100 if len(closes) >= 7 else None,
        "return24Pct": (last / closes[-25] - 1) * 100 if len(closes) >= 25 else None,
        "volume": volumes[-1],
        "volume20Avg": mean(recent_volumes) if recent_volumes else None,
        "volumeRatio": volumes[-1] / mean(recent_volumes) if recent_volumes and mean(recent_volumes) else None,
        "recentHigh20": max(highs[-20:]),
        "recentLow20": min(lows[-20:]),
        "previousHigh20": max(highs[-21:-1]),
        "previousLow20": min(lows[-21:-1]),
    }


def market(coins: list[str], raw: bool, candle_count: int, book_levels: int) -> None:
    now_ms = int(datetime.now(tz=timezone.utc).timestamp() * 1000)
    meta, contexts = post_info({"type": "metaAndAssetCtxs"})
    universe = meta.get("universe", [])
    by_coin = {asset.get("name"): (asset, contexts[index]) for index, asset in enumerate(universe)}
    interval_ms = {"15m": 15 * 60_000, "1h": 60 * 60_000, "4h": 4 * 60 * 60_000}
    output: dict[str, Any] = {"asOf": now_ms, "coins": {}}
    for coin in coins:
        if coin not in by_coin:
            raise ApiError(f"unknown perpetual market: {coin}")
        asset, context = by_coin[coin]
        coin_data: dict[str, Any] = {"meta": asset, "context": context, "timeframes": {}}
        for interval, duration in interval_ms.items():
            candles = post_info({
                "type": "candleSnapshot",
                "req": {"coin": coin, "interval": interval, "startTime": now_ms - duration * candle_count, "endTime": now_ms},
            })
            coin_data["timeframes"][interval] = {"metrics": candle_metrics(candles), "candles": candles if raw else candles[-30:]}
        coin_data["fundingHistory"] = post_info({"type": "fundingHistory", "coin": coin, "startTime": now_ms - 72 * 60 * 60_000, "endTime": now_ms})
        book = post_info({"type": "l2Book", "coin": coin, "nSigFigs": 5})
        if not raw:
            book["levels"] = [side[:book_levels] for side in book.get("levels", [])]
        coin_data["l2Book"] = book
        output["coins"][coin] = coin_data
    print(json.dumps(output, indent=2))


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--user", help="public 42-character account address")
    result.add_argument("--json", action="store_true", help="print API data as JSON")
    sub = result.add_subparsers(dest="command", required=True)
    sub.add_parser("account")
    fills_parser = sub.add_parser("fills")
    fills_parser.add_argument("--oid", action="append", type=int, default=[], help="filter by order ID (repeatable)")
    orders_parser = sub.add_parser("orders")
    orders_parser.add_argument("--oid", action="append", type=int, default=[], help="filter by order ID (repeatable)")
    funding_parser = sub.add_parser("funding")
    funding_parser.add_argument("--start-time", type=int, default=0, help="inclusive epoch milliseconds")
    funding_parser.add_argument("--end-time", type=int, help="inclusive epoch milliseconds")
    sub.add_parser("pnl")
    market_parser = sub.add_parser("market")
    market_parser.add_argument("--coins", nargs="+", default=["ETH", "HYPE"], help="perpetual symbols")
    market_parser.add_argument("--candle-count", type=int, default=200, help="candles per timeframe")
    market_parser.add_argument("--book-levels", type=int, default=10, help="L2 levels per side unless --json")
    return result


def main() -> int:
    args = parser().parse_args()
    if args.command != "market":
        try:
            args.user = resolve_account_address(args.user)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
    try:
        if args.command == "account":
            account(args.user, args.json)
        elif args.command == "fills":
            fills(args.user, args.json, args.oid)
        elif args.command == "orders":
            orders(args.user, args.json, args.oid)
        elif args.command == "funding":
            funding(args.user, args.json, args.start_time, args.end_time)
        elif args.command == "pnl":
            pnl(args.user, args.json)
        elif args.command == "market":
            market([coin.upper() for coin in args.coins], args.json, args.candle_count, args.book_levels)
    except (ApiError, BrokenPipeError) as exc:
        if not isinstance(exc, BrokenPipeError):
            print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
