"""Universe screening and 24h volume acceleration for Hyperliquid perpetuals."""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Callable

from .hl_client import HyperliquidAPIError, HyperliquidInfoClient

SCHEMA_VERSION = "1.0"
DEFAULT_MIN_VOLUME = 10_000_000.0
DEFAULT_TOP_N = 50
DEFAULT_LIMIT = 10
DEFAULT_MAX_WORKERS = 8

MEME_BLACKLIST = {
    "PUMP", "TRUMP", "PEPE", "kPEPE", "DOGE", "SHIB", "kSHIB", "BONK", "kBONK",
    "WIF", "FLOKI", "FARTCOIN", "POPCAT", "MEW", "GRIFFAIN", "SPX", "MOG", "TURBO",
    "NEIRO", "kNEIRO", "NEIROETH", "BRETT", "PNUT", "ACT", "GOAT", "MOODENG", "BOME",
    "HIPPO", "TOSHI", "SLERF", "MYRO", "WEN", "LADYS", "SUNDOG", "kDOGS", "CATI",
    "GIGA", "CHILLGUY", "PONS", "AI16Z", "ELIZA", "ZEREBRO", "CASHCAT", "MEME",
}


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def screen_market(
    *,
    client: HyperliquidInfoClient | None = None,
    top_n: int = DEFAULT_TOP_N,
    limit: int = DEFAULT_LIMIT,
    sort_by: str = "vol_delta",
    min_volume: float = DEFAULT_MIN_VOLUME,
    max_workers: int = DEFAULT_MAX_WORKERS,
    clock_ms: Callable[[], int] | None = None,
    timer: Callable[[], float] | None = None,
) -> dict[str, Any]:
    """Discover, filter and calculate 24h volume acceleration for perpetual markets."""
    if top_n <= 0:
        raise ValueError("top_n must be greater than 0")
    if limit <= 0:
        raise ValueError("limit must be greater than 0")
    valid_sorts = ("vol_delta", "volume", "oi", "px_change", "momentum")
    if sort_by not in valid_sorts:
        raise ValueError(f"sort_by must be one of {valid_sorts}")

    client = client or HyperliquidInfoClient()
    clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)
    timer = timer or time.perf_counter

    started = timer()
    now_ms = clock_ms()
    started_at = _iso(now_ms)

    # 1. Fetch market contexts (1 HTTP request)
    meta_response = client.meta_and_asset_contexts()
    meta_data = meta_response.value if hasattr(meta_response, "value") else meta_response
    if not isinstance(meta_data, (list, tuple)) or len(meta_data) != 2:
        raise HyperliquidAPIError("invalid metaAndAssetCtxs structure")
    universe, contexts = meta_data[0].get("universe", []), meta_data[1]

    raw_assets = []
    for index, asset in enumerate(universe):
        if not isinstance(asset, dict) or index >= len(contexts):
            continue
        ctx = contexts[index]
        if not isinstance(ctx, dict):
            continue

        name = asset.get("name", "")
        if not name or name in MEME_BLACKLIST:
            continue

        try:
            mark = float(ctx.get("markPx") or 0.0)
            prev_px = float(ctx.get("prevDayPx") or 0.0)
            vol = float(ctx.get("dayNtlVlm") or 0.0)
            oi_tokens = float(ctx.get("openInterest") or 0.0)
            funding = float(ctx.get("funding") or 0.0)
        except (ValueError, TypeError):
            continue

        px_change_24h = ((mark - prev_px) / prev_px * 100.0) if prev_px > 0 else 0.0
        oi_usd = oi_tokens * mark
        funding_ann = funding * 100.0 * 3.0 * 365.0

        if vol < min_volume:
            continue

        raw_assets.append({
            "symbol": name,
            "price": mark,
            "price_change_pct_24h": round(px_change_24h, 2),
            "volume_24h_usd": round(vol, 2),
            "oi_usd": round(oi_usd, 2),
            "funding_rate": funding,
            "funding_annualized_pct": round(funding_ann, 2),
        })

    # Sort initial universe by volume to select candidate pool
    raw_assets.sort(key=lambda x: x["volume_24h_usd"], reverse=True)
    candidate_pool = raw_assets[:top_n]

    # 2. Fetch 48h candle history concurrently to calculate volume acceleration
    def analyze_asset_volume(item: dict[str, Any]) -> dict[str, Any]:
        sym = item["symbol"]
        result = dict(item)
        try:
            candles_resp = client.candles(sym, "1h", now_ms - 49 * 3600_000, now_ms)
            candles = candles_resp.value if hasattr(candles_resp, "value") else candles_resp
            if not isinstance(candles, list):
                candles = []
            closed_candles = [c for c in candles if isinstance(c, dict) and int(c.get("T", 0)) <= now_ms]
            closed_candles.sort(key=lambda c: int(c.get("t", 0)))

            if len(closed_candles) >= 48:
                today_vol = sum(float(c.get("v", 0)) * float(c.get("c", 0)) for c in closed_candles[-24:])
                prev_vol = sum(float(c.get("v", 0)) * float(c.get("c", 0)) for c in closed_candles[-48:-24])
                vol_delta = ((today_vol / prev_vol) - 1.0) * 100.0 if prev_vol > 0 else (0.0 if today_vol == 0 else 100.0)
            elif len(closed_candles) >= 24:
                today_vol = sum(float(c.get("v", 0)) * float(c.get("c", 0)) for c in closed_candles[-24:])
                prev_vol = None
                vol_delta = None
            else:
                today_vol = item["volume_24h_usd"]
                prev_vol = None
                vol_delta = None

            result["volume_today_usd"] = round(today_vol, 2)
            result["volume_prev_24h_usd"] = round(prev_vol, 2) if prev_vol is not None else None
            result["vol_delta_pct_24h"] = round(vol_delta, 2) if vol_delta is not None else None
            result["vol_oi_ratio"] = round(item["volume_24h_usd"] / item["oi_usd"], 2) if item["oi_usd"] > 0 else None
            result["error"] = None
        except Exception as exc:
            result["volume_today_usd"] = item["volume_24h_usd"]
            result["volume_prev_24h_usd"] = None
            result["vol_delta_pct_24h"] = None
            result["vol_oi_ratio"] = round(item["volume_24h_usd"] / item["oi_usd"], 2) if item["oi_usd"] > 0 else None
            result["error"] = f"{type(exc).__name__}: {exc}"
        return result

    workers = min(max_workers, len(candidate_pool)) if candidate_pool else 1
    with ThreadPoolExecutor(max_workers=workers) as pool:
        analyzed = list(pool.map(analyze_asset_volume, candidate_pool))

    # 3. Sort final results according to requested strategy
    def sort_key(x: dict[str, Any]) -> Any:
        if sort_by == "vol_delta":
            has_delta = x["vol_delta_pct_24h"] is not None
            return (has_delta, x["vol_delta_pct_24h"] if has_delta else -float("inf"))
        elif sort_by == "volume":
            return x["volume_24h_usd"]
        elif sort_by == "oi":
            return x["oi_usd"]
        elif sort_by == "px_change":
            return abs(x["price_change_pct_24h"])
        elif sort_by == "momentum":
            return abs(x["price_change_pct_24h"]) * math.sqrt(max(x["volume_24h_usd"], 0.0))
        return 0

    analyzed.sort(key=sort_key, reverse=True)
    final_results = analyzed[:limit]

    completed_at = _iso(clock_ms())
    total_seconds = timer() - started

    return {
        "kind": "market_screener",
        "schema_version": SCHEMA_VERSION,
        "read_only": True,
        "status": "ok",
        "started_at_utc": started_at,
        "completed_at_utc": completed_at,
        "total_seconds": round(total_seconds, 6),
        "parameters": {
            "top_n": top_n,
            "limit": limit,
            "sort_by": sort_by,
            "min_volume": min_volume,
        },
        "universe_size": len(universe),
        "screened_count": len(candidate_pool),
        "returned_count": len(final_results),
        "symbols": [x["symbol"] for x in final_results],
        "results": final_results,
    }


def format_table(report: dict[str, Any]) -> str:
    headers = ["#", "SYMBOL", "PRICE", "PX 24H (%)", "VOL 24H (USD)", "VOL PREV (USD)", "VOL DELTA (%)", "OI (USD)", "VOL/OI"]
    rows = []
    for i, r in enumerate(report["results"], 1):
        sym = r["symbol"]
        mark = f"${r['price']:,.4f}" if r["price"] < 10 else f"${r['price']:,.2f}"
        px_ch = f"{r['price_change_pct_24h']:+.2f}%"
        vol = f"${r['volume_24h_usd']:,.0f}"
        vol_prev = f"${r['volume_prev_24h_usd']:,.0f}" if r["volume_prev_24h_usd"] is not None else "-"
        vol_delta = f"{r['vol_delta_pct_24h']:+.2f}%" if r["vol_delta_pct_24h"] is not None else "-"
        oi = f"${r['oi_usd']:,.0f}"
        ratio = f"{r['vol_oi_ratio']:.2f}" if r["vol_oi_ratio"] is not None else "-"
        rows.append([str(i), sym, mark, px_ch, vol, vol_prev, vol_delta, oi, ratio])

    widths = [len(h) for h in headers]
    for row in rows:
        for idx, val in enumerate(row):
            widths[idx] = max(widths[idx], len(val))

    header_line = "  ".join(h.ljust(widths[i]) if i in (0, 1) else h.rjust(widths[i]) for i, h in enumerate(headers))
    separator = "  ".join("-" * widths[i] for i in range(len(headers)))
    data_lines = [
        "  ".join(val.ljust(widths[i]) if i in (0, 1) else val.rjust(widths[i]) for i, val in enumerate(row))
        for row in rows
    ]

    summary = (
        f"Screened {report['screened_count']}/{report['universe_size']} markets in {report['total_seconds']:.2f}s "
        f"(sorted by {report['parameters']['sort_by']}) | Candidates: {' '.join(report['symbols'])}"
    )
    return "\n".join([header_line, separator] + data_lines + ["", summary])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--top-n", type=int, default=DEFAULT_TOP_N, help="initial universe pool by 24h volume (default: 50)")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="number of top candidates to output (default: 10)")
    parser.add_argument(
        "--sort-by",
        choices=["vol_delta", "volume", "oi", "px_change", "momentum"],
        default="vol_delta",
        help="ranking criterion for final candidate selection (default: vol_delta)",
    )
    parser.add_argument(
        "--min-volume",
        type=float,
        default=DEFAULT_MIN_VOLUME,
        help="minimum 24h notional volume filter in USD (default: 10,000,000)",
    )
    parser.add_argument("--json", action="store_true", help="emit structured JSON report on stdout")
    parser.add_argument(
        "--symbols-only",
        action="store_true",
        help="emit space-separated symbols only (suitable for piping into scanner or analysis_pipeline)",
    )

    args = parser.parse_args(argv)

    try:
        report = screen_market(
            top_n=args.top_n,
            limit=args.limit,
            sort_by=args.sort_by,
            min_volume=args.min_volume,
        )
    except Exception as exc:
        sys.stderr.write(f"error: {type(exc).__name__}: {exc}\n")
        return 1

    if args.symbols_only:
        print(" ".join(report["symbols"]))
    elif args.json:
        print(json.dumps(report, separators=(",", ":")))
    else:
        print(format_table(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
