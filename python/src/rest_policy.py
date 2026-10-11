"""Process-local REST admission; standard library, no network or import-time I/O."""
from __future__ import annotations

import math
import os
import threading
import time
from collections import deque
from dataclasses import dataclass, fields
from contextlib import contextmanager
from urllib.parse import urlsplit


@dataclass(frozen=True)
class RESTConfig:
    budget: int = 900
    window_seconds: int = 60
    max_inflight: int = 3
    max_attempts: int = 3
    max_wait_seconds: int = 10
    admission_wait_seconds: int = 180
    request_deadline_seconds: int = 20
    capture_deadline_seconds: int = 25
    context_ttl_seconds: int = 10
    backoff_initial_seconds: int = 1
    backoff_max_seconds: int = 8

    def __post_init__(self):
        for item in fields(self):
            if type(getattr(self, item.name)) is not int or getattr(self, item.name) <= 0:
                raise ValueError(f"rest.{item.name} must be a positive integer")
        if self.budget > 1200 or self.window_seconds < 60:
            raise ValueError("REST budget must be <=1200 and window >=60 seconds")
        if self.backoff_initial_seconds > self.backoff_max_seconds:
            raise ValueError("REST initial backoff exceeds maximum")


class RESTAdmissionError(RuntimeError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


# Documented /info base weights. Unknown types use the documented default.
_LIGHT = {"l2Book", "allMids", "clearinghouseState", "orderStatus",
          "spotClearinghouseState", "exchangeStatus"}
_LISTS = {"recentTrades", "historicalOrders", "userFills", "userFillsByTime",
          "fundingHistory", "userFunding", "nonUserFundingUpdates", "twapHistory",
          "userTwapSliceFills", "userTwapSliceFillsByTime", "delegatorHistory",
          "delegatorRewards", "validatorStats"}
_INTERVALS = {"1m": 60000, "3m": 180000, "5m": 300000, "15m": 900000,
              "30m": 1800000, "1h": 3600000, "2h": 7200000, "4h": 14400000,
              "8h": 28800000, "12h": 43200000, "1d": 86400000,
              "3d": 259200000, "1w": 604800000}


def request_weight(payload, value=None):
    """Reserve ceiling surcharges; reconcile valid list responses by actual size.

    The documentation does not specify partial-block rounding. Using ceilings
    is conservative. Time-range funding requests reserve the documented 500
    item limit; fills/history orders reserve 2000. No private payload is logged.
    """
    kind = payload.get("type")
    base = 2 if kind in _LIGHT else 60 if kind == "userRole" else 20
    if kind == "candleSnapshot":
        req = payload.get("req", {})
        duration = _INTERVALS.get(req.get("interval"))
        count = min(5000, max(0, math.ceil((req.get("endTime", 0) - req.get("startTime", 0)) / duration) + 2)) if duration else 5000
        return base + math.ceil((len(value) if isinstance(value, list) else count) / 60)
    if kind in _LISTS:
        count = 2000 if kind in {"userFills", "userFillsByTime", "historicalOrders"} else 500
        return base + math.ceil((len(value) if isinstance(value, list) else count) / 20)
    return base


class RESTCoordinator:
    """Atomic window with future commitments, stage headroom and HTTP slots.

    No fixed per-request pacing. Commitments reserve future capacity without
    charging it as traffic; each admission converts credit into a window entry.
    All waits use monotonic time and explicit bounds. State is process-local.
    """
    def __init__(self, config=None, *, timer=None, wait=None):
        self.config = config or RESTConfig()
        self.timer = timer or time.monotonic
        self.wait = wait or time.sleep
        self._lock = threading.Lock()
        self._entries = deque()
        self._cooldown_until = 0.0
        self._inflight = 0
        self._commitments = {}
        self._headrooms = {}
        self._local = threading.local()

    def _clean(self, now):
        while self._entries and self._entries[0][0] + self.config.window_seconds <= now:
            self._entries.popleft()

    def _used(self):
        return (sum(entry[1] for entry in self._entries)
                + sum(value[0] for value in self._commitments.values())
                + sum(self._headrooms.values()))

    @property
    def reserved_weight(self):
        """Window consumption plus unspent commitments and protected headroom."""
        with self._lock:
            self._clean(self.timer())
            return self._used()

    def _release_time(self, needed, now):
        for entry in self._entries:
            needed -= entry[1]
            if needed <= 0:
                return entry[0] + self.config.window_seconds
        return None  # Another commitment/headroom must be released first.

    def _dynamic(self):
        return (self._inflight > 0 or any(value[0] for value in self._commitments.values())
                or bool(self._headrooms))

    def _reserve(self, weight, deadline, *, headroom=False):
        if type(weight) is not int or weight <= 0:
            raise ValueError("reservation weight must be a positive integer")
        if weight > self.config.budget:
            raise RESTAdmissionError("request_weight_exceeds_budget")
        while True:
            with self._lock:
                now = self.timer()
                self._clean(now)
                if now >= deadline:
                    raise RESTAdmissionError("admission_wait_exceeded")
                if self._used() + weight <= self.config.budget and now >= self._cooldown_until:
                    token = object()
                    if headroom:
                        self._headrooms[token] = weight
                    else:
                        self._commitments[token] = [weight, weight]
                    return token
                needed = self._used() + weight - self.config.budget
                ready = self._release_time(needed, now) if needed > 0 else now
                # Inflight responses and other scopes may free capacity earlier.
                if self._dynamic() and needed > 0:
                    ready = now + 0.01
                if ready is None:
                    ready = now + 0.01
                ready = max(ready, self._cooldown_until)
                if ready >= deadline:
                    raise RESTAdmissionError("admission_wait_exceeded")
                delay = max(0.001, ready - now)
            self.wait(delay)

    @contextmanager
    def reservation(self, weight, deadline):
        current = getattr(self._local, "token", None)
        with self._lock:
            existing = self._commitments.get(current)
            if existing is not None and weight > existing[0]:
                raise RESTAdmissionError("commitment_weight_exceeds_reservation")
        if existing is not None:
            yield current
            return
        token = self._reserve(weight, deadline)
        self._local.token = token
        try:
            yield token
        finally:
            with self._lock:
                self._commitments.pop(token, None)
            self._local.token = current

    @contextmanager
    def stage_headroom(self, weight, deadline):
        token = self._reserve(weight, deadline, headroom=True)
        try:
            yield
        finally:
            with self._lock:
                self._headrooms.pop(token, None)

    def acquire(self, weight, deadline):
        if weight > self.config.budget:
            raise RESTAdmissionError("request_weight_exceeds_budget")
        while True:
            with self._lock:
                now = self.timer()
                self._clean(now)
                if now >= deadline or self._cooldown_until >= deadline:
                    raise RESTAdmissionError("budget_wait_exceeded")
                commitment = self._commitments.get(getattr(self._local, "token", None))
                credit = min(weight, commitment[0]) if commitment is not None else 0
                needed = self._used() + weight - credit - self.config.budget
                ready = self._cooldown_until
                if needed > 0:
                    release_time = self._release_time(needed, now)
                    if not self._dynamic() and (release_time is None or release_time >= deadline):
                        raise RESTAdmissionError("budget_wait_exceeded")
                    ready = max(ready, now + 0.01 if self._dynamic() else release_time)
                capacity = self._inflight < self.config.max_inflight
                if capacity and needed <= 0 and ready <= now:
                    if commitment is not None:
                        commitment[0] -= credit
                    entry = [now, weight]
                    self._entries.append(entry)
                    self._inflight += 1
                    return entry
                delay = max(0.001, ready - now) if capacity else 0.01
                if now + delay >= deadline:
                    raise RESTAdmissionError("budget_wait_exceeded")
            self.wait(delay)

    def release(self, entry, actual_weight=None):
        with self._lock:
            self._inflight -= 1
            if actual_weight is not None:
                # Success reconciles traffic only. Do not refund it as another
                # future commitment; that would credit the same weight twice.
                extra = max(0, actual_weight - entry[1])
                entry[1] = actual_weight - extra
                if extra:
                    self._entries.append([self.timer(), extra])

    def cooldown(self, seconds):
        with self._lock:
            self._cooldown_until = max(self._cooldown_until, self.timer() + seconds)


_registry = {}
_registry_lock = threading.Lock()


def shared_coordinator(api_url, config=None, *, timer=None, wait=None):
    """New clients share admission by origin and PID; conflicting policy fails."""
    url = urlsplit(api_url)
    key = (os.getpid(), url.scheme.lower(), url.hostname, url.port or (443 if url.scheme.lower() == "https" else 80))
    with _registry_lock:
        coordinator = _registry.get(key)
        if coordinator is None:
            coordinator = RESTCoordinator(config, timer=timer, wait=wait)
            _registry[key] = coordinator
        elif config is not None and coordinator.config != config:
            raise ValueError("conflicting REST policy for this process and API origin")
        if timer is not None and coordinator.timer is not timer:
            raise ValueError("conflicting REST clock; inject an isolated coordinator for tests")
        if wait is not None and coordinator.wait is not wait:
            raise ValueError("conflicting REST wait function; inject an isolated coordinator for tests")
        return coordinator
