"""Small, unsigned client for Hyperliquid's official read-only Info API."""

from __future__ import annotations

import json
import math
import threading
from contextlib import contextmanager, nullcontext
from email.utils import parsedate_to_datetime
import random
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from .rest_policy import RESTAdmissionError, request_weight, shared_coordinator

API_URL = "https://api.hyperliquid.xyz/info"


class HyperliquidAPIError(RuntimeError):
    """The Info API returned an error or an invalid response."""

    def __init__(self, message: str, *, status_code: int | None = None,
                 request_type: str | None = None, retry_after_seconds: float | None = None,
                 attempts: int = 0, code: str = "api_error") -> None:
        super().__init__(message)
        self.status_code = status_code
        self.request_type = request_type
        self.retry_after_seconds = retry_after_seconds
        self.attempts = attempts
        self.code = code


@dataclass(frozen=True)
class CapturedResponse:
    """API value and local wall-clock time recorded after its response arrived."""

    value: Any
    captured_at_ms: int


class HyperliquidInfoClient:
    """POST only to ``/info``; this class cannot sign or submit trades."""

    def __init__(self, api_url: str = API_URL, timeout: float = 20.0, *,
                 timer=None, wait=None, rng=None, transport=None, clock_ms=None,
                 coordinator=None, rest_config=None) -> None:
        if not api_url.rstrip("/").endswith("/info"):
            raise ValueError("api_url must point to Hyperliquid's /info endpoint")
        self.coordinator = coordinator or shared_coordinator(api_url, rest_config, timer=timer, wait=wait)
        if coordinator is not None and rest_config is not None and coordinator.config != rest_config:
            raise ValueError("conflicting injected REST policy")
        if timer is not None and timer is not self.coordinator.timer:
            raise ValueError("client and coordinator must use the same monotonic clock")
        self.api_url = api_url
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be positive and finite")
        self.timeout = timeout
        self.timer = timer or self.coordinator.timer
        self.wait = wait or self.coordinator.wait
        self.rng = rng or random.random
        self.transport = transport
        self.clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)
        self._scope = threading.local()

    @contextmanager
    def request_scope(self, *, deadline_seconds: float, max_attempts: int | None = None):
        """Thread-local deadline; nesting can shorten but never extend a scope."""
        if isinstance(deadline_seconds, bool) or not isinstance(deadline_seconds, (int, float)) or not math.isfinite(deadline_seconds) or deadline_seconds < 0:
            raise ValueError("deadline_seconds must be finite and nonnegative")
        if max_attempts is not None and (type(max_attempts) is not int or max_attempts <= 0):
            raise ValueError("max_attempts must be positive")
        previous = getattr(self._scope, "limits", (float("inf"), self.coordinator.config.max_attempts))
        self._scope.limits = (min(previous[0], self.timer() + deadline_seconds),
                              min(previous[1], max_attempts or previous[1]))
        try:
            yield
        finally:
            self._scope.limits = previous

    @contextmanager
    def reserve_requests(self, weight, *, deadline_seconds=None):
        """Bounded admission before starting request/capture deadlines."""
        seconds = self.coordinator.config.admission_wait_seconds if deadline_seconds is None else deadline_seconds
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not math.isfinite(seconds) or seconds < 0:
            raise ValueError("admission deadline must be finite and nonnegative")
        expires = self.timer() + min(seconds, self.coordinator.config.admission_wait_seconds)
        try:
            with self.coordinator.reservation(weight, expires):
                yield
        except RESTAdmissionError as exc:
            raise HyperliquidAPIError(str(exc), code=exc.code) from exc

    @contextmanager
    def protect_stage(self, weight, *, deadline_seconds=None):
        seconds = self.coordinator.config.admission_wait_seconds if deadline_seconds is None else deadline_seconds
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not math.isfinite(seconds) or seconds < 0:
            raise ValueError("admission deadline must be finite and nonnegative")
        try:
            with self.coordinator.stage_headroom(weight, self.timer() + min(seconds, self.coordinator.config.admission_wait_seconds)):
                yield
        except RESTAdmissionError as exc:
            raise HyperliquidAPIError(str(exc), code=exc.code) from exc

    def _retry_after(self, value):
        if value is None:
            return None
        try:
            seconds = float(value)
        except (ValueError, TypeError):
            try:
                seconds = parsedate_to_datetime(value).timestamp() - self.clock_ms() / 1000
            except (ValueError, TypeError, OverflowError):
                return None
        return max(0.0, seconds) if math.isfinite(seconds) else None

    def _post(self, payload: dict[str, Any]) -> CapturedResponse:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        request = urllib.request.Request(
            self.api_url, data=body,
            headers={"Content-Type": "application/json", "User-Agent": "r102-market-state/1.0"},
            method="POST",
        )
        policy = self.coordinator.config
        scoped_deadline, attempts = getattr(self._scope, "limits", (float("inf"), policy.max_attempts))
        expires = min(scoped_deadline, self.timer() + policy.request_deadline_seconds)
        waited = 0.0
        for attempt in range(1, attempts + 1):
            if self.timer() >= expires:
                raise HyperliquidAPIError("request deadline exceeded", request_type=payload.get("type"),
                                         attempts=attempt - 1, code="deadline_exceeded")
            begin = self.timer()
            try:
                ticket = self.coordinator.acquire(request_weight(payload),
                    min(expires, begin + max(0, policy.max_wait_seconds - waited)))
            except RESTAdmissionError as exc:
                raise HyperliquidAPIError(str(exc), request_type=payload.get("type"),
                                         attempts=attempt - 1, code=exc.code) from exc
            waited += self.timer() - begin
            actual_weight = None
            error = None
            try:
                remaining = expires - self.timer()
                if remaining <= 0:
                    raise HyperliquidAPIError("request deadline exceeded", request_type=payload.get("type"),
                                             attempts=attempt - 1, code="deadline_exceeded")
                with (self.transport or urllib.request.urlopen)(request, timeout=min(self.timeout, remaining)) as response:
                    value = json.load(response)
                actual_weight = request_weight(payload, value)
                if self.timer() >= expires:
                    raise HyperliquidAPIError("request deadline exceeded", request_type=payload.get("type"),
                                             attempts=attempt, code="deadline_exceeded")
                return CapturedResponse(value=value, captured_at_ms=self.clock_ms())
            except urllib.error.HTTPError as exc:
                # Keep legacy HTTP prefix/cause; bound body size and expose no headers.
                detail = exc.read(4096).decode(errors="replace")
                retry_after = self._retry_after(exc.headers.get("Retry-After")) if exc.headers else None
                error = HyperliquidAPIError(f"HTTP {exc.code}: {detail}", status_code=exc.code,
                    request_type=payload.get("type"), attempts=attempt, retry_after_seconds=retry_after)
                error.__cause__ = exc
                if exc.code != 429:
                    raise error from exc
                backoff = min(policy.backoff_max_seconds, policy.backoff_initial_seconds * 2 ** (attempt - 1))
                delay = max(retry_after or 0, backoff) + 0.25 * max(0, min(1, self.rng()))
                self.coordinator.cooldown(delay)
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                raise HyperliquidAPIError(str(exc), request_type=payload.get("type"), attempts=attempt,
                                         code="invalid_json" if isinstance(exc, json.JSONDecodeError) else "network_error") from exc
            finally:
                self.coordinator.release(ticket, actual_weight)
            if attempt == attempts:
                raise error
            if delay > policy.max_wait_seconds - waited or self.timer() + delay >= expires:
                error.code = "retry_wait_exceeded"
                raise error
            self.wait(delay)
            waited += delay

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


def request_scope_for(client, *, deadline_seconds, max_attempts=None):
    """Optional capability: legacy fake clients keep their original methods."""
    if callable(getattr(type(client), "request_scope", None)):
        return client.request_scope(deadline_seconds=deadline_seconds, max_attempts=max_attempts)
    return nullcontext()


def reservation_scope_for(client, weight, *, deadline_seconds=None):
    """Optional capability; legacy clients keep their original query methods."""
    if callable(getattr(type(client), "reserve_requests", None)):
        return client.reserve_requests(weight, deadline_seconds=deadline_seconds)
    return nullcontext()


def stage_headroom_for(client, weight, *, deadline_seconds=None):
    if weight and callable(getattr(type(client), "protect_stage", None)):
        return client.protect_stage(weight, deadline_seconds=deadline_seconds)
    return nullcontext()
