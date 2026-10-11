"""Ephemeral public context provider; no persistence or account queries."""
from __future__ import annotations

import copy
import math
import threading
import time

from .hl_client import CapturedResponse, HyperliquidAPIError, HyperliquidInfoClient, request_scope_for
from .rest_policy import RESTConfig


class MarketContextProvider:
    """One concurrent refresh, original receipt timestamp and bounded TTL.

    Clocks are local: TTL does not claim an exchange source timestamp. Expired
    data is never served on refresh failure. Each consumer receives a copy.
    """
    def __init__(self, client=None, *, ttl_seconds=None, rest_config=None,
                 timer=None, clock_ms=None, wait=None):
        policy = getattr(getattr(client, "coordinator", None), "config", None)
        self.config = rest_config or (policy if isinstance(policy, RESTConfig) else RESTConfig())
        self.ttl_seconds = self.config.context_ttl_seconds if ttl_seconds is None else ttl_seconds
        if isinstance(self.ttl_seconds, bool) or not isinstance(self.ttl_seconds, (int, float)) or not math.isfinite(self.ttl_seconds) or self.ttl_seconds <= 0:
            raise ValueError("context TTL must be positive and finite")
        self._client = client
        self.timer = timer or getattr(client, "timer", time.monotonic)
        self.clock_ms = clock_ms or getattr(client, "clock_ms", lambda: time.time_ns() // 1_000_000)
        self.wait = wait or getattr(client, "wait", time.sleep)
        self._lock = threading.Lock()
        self._response = None
        self._received = None

    @property
    def client(self):
        # Lazy construction keeps dependency-injected pipelines free of I/O.
        if self._client is not None:
            return self._client
        with self._lock:
            if self._client is None:
                self._client = HyperliquidInfoClient(rest_config=self.config)
            return self._client

    def get(self, *, deadline_seconds=None):
        if deadline_seconds is not None and (isinstance(deadline_seconds, bool) or not isinstance(deadline_seconds, (int, float)) or not math.isfinite(deadline_seconds) or deadline_seconds < 0):
            raise ValueError("context deadline must be finite and nonnegative")
        deadline = self.timer() + (self.config.request_deadline_seconds if deadline_seconds is None else deadline_seconds)
        lock_deadline = min(deadline, self.timer() + self.config.max_wait_seconds)
        while not self._lock.acquire(blocking=False):
            remaining = lock_deadline - self.timer()
            if remaining <= 0.01:
                raise HyperliquidAPIError("context refresh wait exceeded", code="budget_wait_exceeded")
            self.wait(0.01)
        try:
            if self.timer() >= deadline:
                raise HyperliquidAPIError("context deadline exceeded", code="deadline_exceeded")
            if self._response is not None:
                age_ms = self.clock_ms() - self._response.captured_at_ms
                elapsed = self.timer() - self._received
                if 0 <= age_ms <= self.ttl_seconds * 1000 and 0 <= elapsed <= self.ttl_seconds:
                    return copy.deepcopy(self._response)
            if self._client is None:
                self._client = HyperliquidInfoClient(rest_config=self.config)
            with request_scope_for(self._client, deadline_seconds=max(0, deadline - self.timer())):
                response = self._client.meta_and_asset_contexts()
            if self.timer() >= deadline:
                raise HyperliquidAPIError("context deadline exceeded", code="deadline_exceeded")
            if (not isinstance(response, CapturedResponse) or type(response.captured_at_ms) is not int
                    or not isinstance(response.value, (tuple, list)) or len(response.value) != 2
                    or not isinstance(response.value[0], dict) or not isinstance(response.value[1], list)
                    or not isinstance(response.value[0].get("universe"), list)):
                raise HyperliquidAPIError("malformed shared market context")
            age_ms = self.clock_ms() - response.captured_at_ms
            if not 0 <= age_ms <= self.ttl_seconds * 1000:
                raise HyperliquidAPIError("shared context receipt is stale or in the future", code="stale_context")
            self._response = copy.deepcopy(response)
            self._received = self.timer()
            return copy.deepcopy(response)
        finally:
            self._lock.release()

    def market_context(self, symbol, *, deadline_seconds=None):
        response = self.get(deadline_seconds=deadline_seconds)
        meta, contexts = response.value
        for index, asset in enumerate(meta["universe"]):
            if isinstance(asset, dict) and asset.get("name") == symbol.upper() and index < len(contexts):
                if not isinstance(contexts[index], dict):
                    raise HyperliquidAPIError("malformed shared market context row")
                return CapturedResponse((asset, contexts[index]), response.captured_at_ms)
        raise HyperliquidAPIError(f"unknown perpetual market: {symbol.upper()}")
