"""Deterministic, read-only Hyperliquid account/position/order snapshot."""
from __future__ import annotations

import argparse, json, math, re, sys, time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .account_config import resolve_account_address
from .hl_client import CapturedResponse, HyperliquidAPIError, HyperliquidInfoClient
from .integrity import attach_checksum, verify_checksum

SCHEMA_VERSION = "1.0"
RECENT_FUNDING_WINDOW_MS = 7 * 24 * 60 * 60 * 1000


def _num(value: Any) -> float | None:
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError):
        return None


def _iso(ms: Any) -> str | None:
    try:
        return datetime.fromtimestamp(int(ms) / 1000, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def _issue(issues: list[dict[str, str]], code: str, scope: str, detail: str) -> None:
    issues.append({"code": code, "scope": scope, "detail": detail})


def _leverage(raw: Any) -> dict[str, Any] | None:
    if isinstance(raw, dict):
        return {"type": raw.get("type"), "value": _num(raw.get("value")), "raw_value": raw.get("rawUsd")}
    value = _num(raw)
    return {"type": None, "value": value, "raw_value": None} if value is not None else None


def normalize_order(raw: Any, issues: list[dict[str, str]]) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        _issue(issues, "malformed_order", "open_orders", "order is not an object")
        return None
    symbol = raw.get("coin"); oid = raw.get("oid")
    if not symbol or oid is None:
        _issue(issues, "malformed_order", "open_orders", "order lacks coin or oid")
        return None
    side_code = raw.get("side")
    return {"symbol": symbol, "order_id": oid, "side": "buy" if side_code == "B" else "sell" if side_code == "A" else None,
            "side_code": side_code, "remaining_size": _num(raw.get("sz")), "original_size": _num(raw.get("origSz")),
            "price": _num(raw.get("limitPx")), "trigger_price": _num(raw.get("triggerPx")) if _num(raw.get("triggerPx")) not in (None, 0) else None,
            "order_type": raw.get("orderType"), "trigger_condition": raw.get("triggerCondition"),
            "is_trigger": raw.get("isTrigger"), "is_position_tpsl": raw.get("isPositionTpsl"),
            "reduce_only": raw.get("reduceOnly"), "timestamp_ms": raw.get("timestamp"), "timestamp_utc": _iso(raw.get("timestamp")),
            "status": "open"}


def _protective_kind(order: dict[str, Any], position: dict[str, Any]) -> str | None:
    opposite = (position["side"] == "long" and order["side"] == "sell") or (position["side"] == "short" and order["side"] == "buy")
    if not (opposite and order["reduce_only"] is True and order["is_trigger"] is True and order["trigger_price"] is not None):
        return None
    name = str(order.get("order_type") or "").lower()
    if "take profit" in name or "take-profit" in name or name.startswith("tp"):
        return "take_profit"
    if "stop" in name and "take" not in name:
        return "stop_loss"
    return None


def _distance(side: str, mark: float | None, trigger: float | None, kind: str) -> float | None:
    if not mark or trigger is None: return None
    if kind == "stop_loss": delta = mark - trigger if side == "long" else trigger - mark
    else: delta = trigger - mark if side == "long" else mark - trigger
    return delta / mark * 100


def add_protection(position: dict[str, Any], orders: list[dict[str, Any]], issues: list[dict[str, str]]) -> None:
    matched = []
    for order in orders:
        if order["symbol"] != position["symbol"]: continue
        kind = _protective_kind(order, position)
        if kind:
            item = dict(order); item["protection_kind"] = kind
            item["distance_from_mark_pct"] = _distance(position["side"], position["mark_price"], order["trigger_price"], kind)
            matched.append(item)
    stops = [x for x in matched if x["protection_kind"] == "stop_loss"]
    targets = [x for x in matched if x["protection_kind"] == "take_profit"]
    size = position["size_absolute"] or 0
    stop_size = sum(x["remaining_size"] or 0 for x in stops); target_size = sum(x["remaining_size"] or 0 for x in targets)
    stop_pct = min(stop_size / size * 100, 100) if size else None; target_pct = min(target_size / size * 100, 100) if size else None
    position["protection"] = {"stop_losses": stops, "take_profits": targets,
        "stop_covered_size": min(stop_size, size), "take_profit_covered_size": min(target_size, size),
        "stop_coverage_pct": stop_pct, "take_profit_coverage_pct": target_pct,
        "protection_complete": bool(stop_pct is not None and stop_pct >= 100 and target_pct is not None and target_pct >= 100)}
    if stop_pct is not None and stop_pct < 100: _issue(issues, "incomplete_stop_protection", f"positions.{position['symbol']}", f"stop coverage is {stop_pct:.6g}%")
    if target_pct is not None and target_pct < 100: _issue(issues, "incomplete_take_profit_protection", f"positions.{position['symbol']}", f"take-profit coverage is {target_pct:.6g}%")
    if not stops: _issue(issues, "missing_stop_loss", f"positions.{position['symbol']}", "no explicitly classifiable stop-loss order")
    if not targets: _issue(issues, "missing_take_profit", f"positions.{position['symbol']}", "no explicitly classifiable take-profit order")

    def weighted(items: list[dict[str, Any]]) -> tuple[float | None, float]:
        remaining, total, weighted_sum = size, 0.0, 0.0
        for item in items:
            used = min(item["remaining_size"] or 0, remaining)
            if used <= 0: continue
            weighted_sum += item["trigger_price"] * used; total += used; remaining -= used
        return (weighted_sum / total if total else None, total)
    stop_px, stop_covered = weighted(stops); tp_px, tp_covered = weighted(targets); mark = position["mark_price"]
    stop_dist = _distance(position["side"], mark, stop_px, "stop_loss"); tp_dist = _distance(position["side"], mark, tp_px, "take_profit")
    loss = -(abs(mark - stop_px) * stop_covered) if mark is not None and stop_px is not None else None
    profit = abs(tp_px - mark) * tp_covered if mark is not None and tp_px is not None else None
    rr = profit / abs(loss) if profit is not None and loss not in (None, 0) and stop_covered >= size and tp_covered >= size else None
    position["risk"] = {"position_notional": position["notional"], "stop_reference_price": stop_px,
        "distance_to_stop_pct": stop_dist, "approximate_loss_to_stop": loss, "stop_quantity_used": stop_covered,
        "take_profit_reference_price": tp_px, "distance_to_take_profit_pct": tp_dist,
        "approximate_profit_to_take_profit": profit, "take_profit_quantity_used": tp_covered, "reward_risk": rr}
    if rr is None: _issue(issues, "reward_risk_unavailable", f"positions.{position['symbol']}", "requires full explicit SL and TP coverage with valid prices")


def normalize_position(raw: Any, marks: dict[str, float], issues: list[dict[str, str]]) -> dict[str, Any] | None:
    position = raw.get("position") if isinstance(raw, dict) else None
    if not isinstance(position, dict):
        _issue(issues, "malformed_position", "positions", "position wrapper is malformed"); return None
    symbol, signed = position.get("coin"), _num(position.get("szi"))
    if not symbol or signed in (None, 0): return None
    absolute = abs(signed); notional = _num(position.get("positionValue")); mark = marks.get(symbol)
    if mark is None and notional is not None and absolute: mark = abs(notional) / absolute
    margin = _num(position.get("marginUsed")); pnl = _num(position.get("unrealizedPnl")); roe = _num(position.get("returnOnEquity"))
    if roe is None and pnl is not None and margin: roe = pnl / margin
    return {"symbol": symbol, "side": "long" if signed > 0 else "short", "signed_size": signed, "size_absolute": absolute,
            "entry_price": _num(position.get("entryPx")), "mark_price": mark, "notional": abs(notional) if notional is not None else (mark * absolute if mark else None),
            "leverage": _leverage(position.get("leverage")), "margin_used": margin, "unrealized_pnl": pnl, "roe": roe,
            "liquidation_price": _num(position.get("liquidationPx")), "cum_funding": position.get("cumFunding")}


def normalize_fill(raw: Any, issues: list[dict[str, str]]) -> dict[str, Any] | None:
    if not isinstance(raw, dict) or not raw.get("coin"):
        _issue(issues, "malformed_fill", "recent_history.fills", "fill is malformed"); return None
    return {"symbol": raw.get("coin"), "price": _num(raw.get("px")), "size": _num(raw.get("sz")), "side": "buy" if raw.get("side") == "B" else "sell" if raw.get("side") == "A" else None,
            "direction": raw.get("dir"), "start_position": _num(raw.get("startPosition")), "closed_pnl": _num(raw.get("closedPnl")),
            "fee": _num(raw.get("fee")), "fee_token": raw.get("feeToken"), "time_ms": raw.get("time"), "time_utc": _iso(raw.get("time")),
            "order_id": raw.get("oid"), "trade_id": raw.get("tid"), "hash": raw.get("hash")}


def reconstruct_closed_trades(fills: list[dict[str, Any]], funding: list[Any], issues: list[dict[str, str]], funding_start_ms: int | None = None) -> list[dict[str, Any]]:
    groups, active = [], {}
    for fill in sorted(fills, key=lambda x: x.get("time_ms") or 0):
        start, size = fill.get("start_position"), fill.get("size")
        if start is None or size is None or fill.get("side") is None: continue
        end = start + (size if fill["side"] == "buy" else -size)
        symbol = fill["symbol"]
        if abs(start) < 1e-12: active[symbol] = [fill]
        elif symbol in active: active[symbol].append(fill)
        if symbol in active and abs(end) < 1e-12:
            episode = active.pop(symbol); opened, closed = episode[0]["time_ms"], episode[-1]["time_ms"]
            unsupported_fees = [x for x in episode if x.get("fee") and x.get("fee_token") != "USDC"]
            fees = sum(x["fee"] or 0 for x in episode if x.get("fee_token") == "USDC")
            gross = sum(x["closed_pnl"] or 0 for x in episode)
            funding_total = 0.0; funding_rows = []
            for item in funding:
                if not isinstance(item, dict): continue
                delta = item.get("delta", {}); when = item.get("time")
                if isinstance(delta, dict) and delta.get("coin") == symbol and opened <= (when or 0) <= closed:
                    amount = _num(delta.get("usdc")); funding_total += amount or 0
                    funding_rows.append({"time_ms": when, "time_utc": _iso(when), "usdc": amount, "funding_rate": _num(delta.get("fundingRate"))})
            funding_complete = funding_start_ms is None or opened >= funding_start_ms
            net = gross - fees + funding_total if funding_complete and not unsupported_fees else None
            if not funding_complete: _issue(issues, "incomplete_funding_history", f"recent_history.closed_trades.{symbol}", "position opened before the queried funding window")
            if unsupported_fees: _issue(issues, "unsupported_fee_token", f"recent_history.closed_trades.{symbol}", "net PnL requires conversion of a non-USDC fee")
            groups.append({"symbol": symbol, "side": "long" if (episode[0]["side"] == "buy") else "short",
                           "opened_at_utc": _iso(opened), "closed_at_utc": _iso(closed), "fills": episode,
                           "closed_pnl_gross": gross, "fees_usdc": fees, "funding_usdc": funding_total,
                           "net_pnl_usdc": net, "funding_history_complete": funding_complete, "funding_events": funding_rows,
                           "method": "sum closedPnl - USDC fees + signed userFunding during zero-to-zero position episode"})
    return sorted(groups, key=lambda x: x["closed_at_utc"] or "", reverse=True)


def build_trade_state(user: str | None = None, symbol: str | None = None, client: HyperliquidInfoClient | None = None,
                      clock_ms: Callable[[], int] | None = None) -> dict[str, Any]:
    user = resolve_account_address(user)
    client = client or HyperliquidInfoClient(); clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000); now = clock_ms(); issues = []
    captures = {}
    def query(name: str, call: Callable[[], CapturedResponse], fallback: Any) -> Any:
        try:
            response = call(); captures[name] = {"captured_at_utc": _iso(response.captured_at_ms), "origin": "r102_local_clock_after_response"}; return response.value
        except HyperliquidAPIError as exc:
            captures[name] = {"captured_at_utc": _iso(clock_ms()), "origin": "r102_local_clock_after_error"}; _issue(issues, "endpoint_unavailable", name, str(exc)); return fallback
    state = query("clearinghouseState", lambda: client.clearinghouse_state(user), {})
    raw_orders = query("frontendOpenOrders", lambda: client.frontend_open_orders(user), [])
    raw_fills = query("userFills", lambda: client.user_fills(user, False), [])
    raw_funding = query("userFunding", lambda: client.user_funding(user, now - RECENT_FUNDING_WINDOW_MS, now), [])
    meta = query("metaAndAssetCtxs", client.meta_and_asset_contexts, ({}, []))
    marks = {}
    try:
        universe, contexts = meta[0].get("universe", []), meta[1]
        marks = {asset["name"]: _num(contexts[i].get("markPx")) for i, asset in enumerate(universe) if i < len(contexts) and isinstance(asset, dict) and isinstance(contexts[i], dict)}
    except (AttributeError, TypeError): _issue(issues, "malformed_response", "metaAndAssetCtxs", "unable to map mark prices")
    orders = [x for x in (normalize_order(o, issues) for o in raw_orders) if x and (not symbol or x["symbol"] == symbol.upper())]
    positions = [x for x in (normalize_position(p, marks, issues) for p in state.get("assetPositions", []) if isinstance(state, dict)) if x and (not symbol or x["symbol"] == symbol.upper())]
    for position in positions: add_protection(position, orders, issues)
    fills = [x for x in (normalize_fill(f, issues) for f in raw_fills) if x and (not symbol or x["symbol"] == symbol.upper())]
    funding_window_start = now - RECENT_FUNDING_WINDOW_MS
    closed = reconstruct_closed_trades(fills, raw_funding, issues, funding_window_start)
    margin = state.get("marginSummary", {}) if isinstance(state, dict) else {}
    if not isinstance(margin, dict): margin = {}; _issue(issues, "malformed_response", "account", "marginSummary is not an object")
    account = {"account_value": _num(margin.get("accountValue")), "withdrawable": _num(state.get("withdrawable")) if isinstance(state, dict) else None,
               "total_notional_positions": _num(margin.get("totalNtlPos")), "total_raw_usd": _num(margin.get("totalRawUsd")),
               "total_margin_used": _num(margin.get("totalMarginUsed")), "cross_maintenance_margin_used": _num(state.get("crossMaintenanceMarginUsed")) if isinstance(state, dict) else None}
    for key, value in account.items():
        if value is None: _issue(issues, "missing_field", f"account.{key}", "field absent, null, or malformed")
    snapshot = {"schema_version": SCHEMA_VERSION, "timestamp_utc": _iso(now), "account_address": user,
                "source": {"venue":"Hyperliquid mainnet", "api":"official HTTP Info API", "endpoint":"https://api.hyperliquid.xyz/info", "read_only":True,
                           "request_types":["clearinghouseState","frontendOpenOrders","userFills","userFunding","metaAndAssetCtxs"]},
                "capture": {"sources":captures}, "filter": {"symbol": symbol.upper() if symbol else None}, "account":account,
                "positions":positions, "open_orders":orders, "risk":{"total_exposure_notional":sum(x["notional"] or 0 for x in positions)},
                "recent_history":{"fills":sorted(fills,key=lambda x:x.get("time_ms") or 0,reverse=True)[:100], "closed_trades":closed[:20],
                                  "funding_window_start_utc":_iso(funding_window_start)},
                "data_quality":{"status":"ok" if not issues else "degraded", "issues":issues, "validation_errors":[]}}
    attach_checksum(snapshot); return snapshot


def save_snapshot(snapshot: dict[str, Any], directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True); stamp = datetime.fromisoformat(snapshot["timestamp_utc"].replace("Z","+00:00")).strftime("%Y%m%d_%H%M%S")
    path = directory / f"{stamp}_trade_state.json"; path.write_text(json.dumps(snapshot,indent=2,sort_keys=True,allow_nan=False)+"\n",encoding="utf-8"); return path


def load_and_verify(path: Path) -> bool:
    with path.open(encoding="utf-8") as handle: return verify_checksum(json.load(handle))


def main() -> int:
    parser=argparse.ArgumentParser(description=__doc__); parser.add_argument("--user"); parser.add_argument("--symbol"); parser.add_argument("--output-dir",type=Path,default=Path(__file__).resolve().parents[2]/"data"/"snapshots"); parser.add_argument("--verify",type=Path,metavar="SNAPSHOT")
    args=parser.parse_args()
    if args.verify:
        try: valid=load_and_verify(args.verify)
        except (OSError,json.JSONDecodeError) as exc: print(f"error: {exc}",file=sys.stderr); return 1
        print(f"checksum={'valid' if valid else 'INVALID'}: {args.verify}"); return 0 if valid else 1
    try: snapshot=build_trade_state(args.user,args.symbol); path=save_snapshot(snapshot,args.output_dir)
    except (ValueError,HyperliquidAPIError,OSError) as exc: print(f"error: {exc}",file=sys.stderr); return 1
    print(f"Trade State v{SCHEMA_VERSION} | account={snapshot['account_address']} | {snapshot['timestamp_utc']}")
    print(f"equity={snapshot['account']['account_value']} withdrawable={snapshot['account']['withdrawable']} positions={len(snapshot['positions'])} orders={len(snapshot['open_orders'])} exposure={snapshot['risk']['total_exposure_notional']}")
    latest=snapshot["recent_history"]["closed_trades"]
    if latest: print(f"latest close: {latest[0]['symbol']} {latest[0]['side']} net={latest[0]['net_pnl_usdc']} USDC")
    print(f"quality={snapshot['data_quality']['status']} issues={len(snapshot['data_quality']['issues'])} checksum={snapshot['integrity']['checksum']}")
    print(f"saved: {path}"); return 0


if __name__ == "__main__": raise SystemExit(main())
