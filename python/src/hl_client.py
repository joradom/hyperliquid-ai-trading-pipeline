"""Small, unsigned client for Hyperliquid's official read-only Info API."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

API_URL = "https://api.hyperliquid.xyz/info"


class HyperliquidAPIError(RuntimeError):
    """The Info API returned an error or an invalid response."""


@dataclass(frozen=True)
class CapturedResponse:
    """API value and local wall-clock time recorded after its response arrived."""

    value: Any
    captured_at_ms: int


class HyperliquidInfoClient:
    """POST only to ``/info``; this class cannot sign or submit trades."""

    def __init__(self, api_url: str = API_URL, timeout: float = 20.0) -> None:
        if not api_url.rstrip("/").endswith("/info"):
            raise ValueError("api_url must point to Hyperliquid's /info endpoint")
        self.api_url = api_url
        self.timeout = timeout

    def _post(self, payload: dict[str, Any]) -> CapturedResponse:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        request = urllib.request.Request(
            self.api_url,
            data=body,
            headers={"Content-Type": "application/json", "User-Agent": "r102-market-state/1.0"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                value = json.load(response)
            return CapturedResponse(value=value, captured_at_ms=time.time_ns() // 1_000_000)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")
            raise HyperliquidAPIError(f"HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise HyperliquidAPIError(str(exc)) from exc

    def meta_and_asset_contexts(self) -> CapturedResponse:
        response = self._post({"type": "metaAndAssetCtxs"})
        if not isinstance(response.value, list) or len(response.value) != 2:
            raise HyperliquidAPIError("unexpected metaAndAssetCtxs response")
        meta, contexts = response.value
        if not isinstance(meta, dict) or not isinstance(contexts, list):
            raise HyperliquidAPIError("malformed metaAndAssetCtxs response")
        return CapturedResponse((meta, contexts), response.captured_at_ms)

    def market_context(self, symbol: str) -> CapturedResponse:
        symbol = symbol.upper()
        response = self.meta_and_asset_contexts()
        meta, contexts = response.value
        universe = meta.get("universe", [])
        for index, asset in enumerate(universe):
            if isinstance(asset, dict) and asset.get("name") == symbol and index < len(contexts):
                if not isinstance(contexts[index], dict):
                    raise HyperliquidAPIError(f"malformed market context for {symbol}")
                return CapturedResponse((asset, contexts[index]), response.captured_at_ms)
        raise HyperliquidAPIError(f"unknown perpetual market: {symbol}")

    def candles(self, symbol: str, interval: str, start_ms: int, end_ms: int) -> CapturedResponse:
        response = self._post({
            "type": "candleSnapshot",
            "req": {"coin": symbol.upper(), "interval": interval, "startTime": start_ms, "endTime": end_ms},
        })
        if not isinstance(response.value, list):
            raise HyperliquidAPIError(f"unexpected candleSnapshot response for {symbol} {interval}")
        return response

    def l2_book(self, symbol: str, n_sig_figs: int = 5) -> CapturedResponse:
        response = self._post({"type": "l2Book", "coin": symbol.upper(), "nSigFigs": n_sig_figs})
        if not isinstance(response.value, dict):
            raise HyperliquidAPIError(f"unexpected l2Book response for {symbol}")
        return response

    def funding_history(self, symbol: str, start_ms: int, end_ms: int) -> CapturedResponse:
        response = self._post({
            "type": "fundingHistory", "coin": symbol.upper(), "startTime": start_ms, "endTime": end_ms
        })
        if not isinstance(response.value, list):
            raise HyperliquidAPIError(f"unexpected fundingHistory response for {symbol}")
        return response

    def _user_query(self, request_type: str, user: str, **fields: Any) -> CapturedResponse:
        response = self._post({"type": request_type, "user": user, **fields})
        return response

    def clearinghouse_state(self, user: str) -> CapturedResponse:
        response = self._user_query("clearinghouseState", user)
        if not isinstance(response.value, dict):
            raise HyperliquidAPIError("unexpected clearinghouseState response")
        return response

    def frontend_open_orders(self, user: str) -> CapturedResponse:
        response = self._user_query("frontendOpenOrders", user)
        if not isinstance(response.value, list):
            raise HyperliquidAPIError("unexpected frontendOpenOrders response")
        return response

    def user_fills(self, user: str, aggregate_by_time: bool = False) -> CapturedResponse:
        response = self._user_query("userFills", user, aggregateByTime=aggregate_by_time)
        if not isinstance(response.value, list):
            raise HyperliquidAPIError("unexpected userFills response")
        return response

    def user_funding(self, user: str, start_ms: int, end_ms: int) -> CapturedResponse:
        response = self._user_query("userFunding", user, startTime=start_ms, endTime=end_ms)
        if not isinstance(response.value, list):
            raise HyperliquidAPIError("unexpected userFunding response")
        return response
