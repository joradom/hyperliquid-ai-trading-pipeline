"""Deterministic technical indicators calculated from OHLCV candles."""

from __future__ import annotations

import math
from statistics import fmean, pstdev
from typing import Any


def ema_series(values: list[float], period: int) -> list[float | None]:
    result: list[float | None] = [None] * len(values)
    if period <= 0 or len(values) < period:
        return result
    current = fmean(values[:period])
    result[period - 1] = current
    alpha = 2.0 / (period + 1.0)
    for index in range(period, len(values)):
        current = values[index] * alpha + current * (1.0 - alpha)
        result[index] = current
    return result


def rsi_wilder(values: list[float], period: int = 14) -> float | None:
    if len(values) <= period:
        return None
    changes = [right - left for left, right in zip(values, values[1:])]
    avg_gain = fmean(max(change, 0.0) for change in changes[:period])
    avg_loss = fmean(max(-change, 0.0) for change in changes[:period])
    for change in changes[period:]:
        avg_gain = (avg_gain * (period - 1) + max(change, 0.0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-change, 0.0)) / period
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    return 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)


def _last(values: list[float | None]) -> float | None:
    return values[-1] if values else None


def calculate(candles: list[dict[str, Any]]) -> dict[str, Any]:
    """Return latest indicators. EMA/MACD use SMA seeding; RSI/ATR use Wilder smoothing."""
    if not candles:
        return {"error": "no candles"}
    ordered = sorted(candles, key=lambda candle: int(candle["t"]))
    closes = [float(candle["c"]) for candle in ordered]
    highs = [float(candle["h"]) for candle in ordered]
    lows = [float(candle["l"]) for candle in ordered]
    volumes = [float(candle["v"]) for candle in ordered]
    last_close = closes[-1]
    emas = {period: _last(ema_series(closes, period)) for period in (8, 21, 50, 120)}

    ema12, ema26 = ema_series(closes, 12), ema_series(closes, 26)
    macd_points: list[float] = []
    for fast, slow in zip(ema12, ema26):
        if fast is not None and slow is not None:
            macd_points.append(fast - slow)
    signal = _last(ema_series(macd_points, 9))
    macd_value = macd_points[-1] if macd_points else None

    bb_window = closes[-20:]
    bb_middle = fmean(bb_window) if len(bb_window) == 20 else None
    bb_std = pstdev(bb_window) if len(bb_window) == 20 else None

    true_ranges: list[float] = []
    for index, (high, low) in enumerate(zip(highs, lows)):
        previous_close = closes[index - 1] if index else float(ordered[index]["o"])
        true_ranges.append(max(high - low, abs(high - previous_close), abs(low - previous_close)))
    atr = None
    if len(true_ranges) >= 14:
        atr = fmean(true_ranges[:14])
        for value in true_ranges[14:]:
            atr = (atr * 13 + value) / 14

    volume_window = volumes[-20:]
    avg_volume = fmean(volume_window) if volume_window else None
    return {
        "ema": {str(period): value for period, value in emas.items()},
        "price_distance_to_ema_pct": {
            str(period): ((last_close / value) - 1.0) * 100.0 if value else None
            for period, value in emas.items()
        },
        "rsi_14": rsi_wilder(closes),
        "macd": {
            "line": macd_value,
            "signal_9": signal,
            "histogram": macd_value - signal if macd_value is not None and signal is not None else None,
        },
        "bollinger_20_2": {
            "middle": bb_middle,
            "upper": bb_middle + 2 * bb_std if bb_middle is not None and bb_std is not None else None,
            "lower": bb_middle - 2 * bb_std if bb_middle is not None and bb_std is not None else None,
        },
        "atr_14": atr,
        "atr_14_pct": atr / last_close * 100.0 if atr is not None and last_close else None,
        "volume_average_20": avg_volume,
        "volume_relative_20": volumes[-1] / avg_volume if avg_volume else None,
    }


def contains_only_finite_numbers(value: Any) -> bool:
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, dict):
        return all(contains_only_finite_numbers(item) for item in value.values())
    if isinstance(value, list):
        return all(contains_only_finite_numbers(item) for item in value)
    return True

