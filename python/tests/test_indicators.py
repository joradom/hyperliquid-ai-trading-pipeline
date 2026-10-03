import math
import unittest

from src.indicators import calculate, ema_series, rsi_wilder


class IndicatorTests(unittest.TestCase):
    def test_ema_constant_series(self):
        self.assertEqual(ema_series([10.0] * 10, 8)[-1], 10.0)

    def test_rsi_edges(self):
        self.assertEqual(rsi_wilder([1.0] * 15), 50.0)
        self.assertEqual(rsi_wilder(list(map(float, range(15)))), 100.0)

    def test_complete_indicator_set_is_finite(self):
        candles = [{"t": i, "o": 100 + i, "h": 102 + i, "l": 99 + i, "c": 101 + i, "v": 1000 + i} for i in range(150)]
        result = calculate(candles)
        self.assertIsNotNone(result["ema"]["120"])
        self.assertTrue(math.isfinite(result["macd"]["histogram"]))
        self.assertTrue(math.isfinite(result["bollinger_20_2"]["upper"]))
        self.assertTrue(math.isfinite(result["atr_14"]))

    def test_exactly_minimum_and_below_minimum(self):
        values = list(map(float, range(120)))
        self.assertIsNotNone(ema_series(values, 120)[-1])
        self.assertIsNone(ema_series(values[:-1], 120)[-1])

    def test_zero_volume_and_constant_series(self):
        candles = [{"t": i, "o": 10, "h": 10, "l": 10, "c": 10, "v": 0} for i in range(120)]
        result = calculate(candles)
        self.assertEqual(result["ema"]["120"], 10)
        self.assertEqual(result["rsi_14"], 50)
        self.assertEqual(result["atr_14"], 0)
        self.assertIsNone(result["volume_relative_20"])

    def test_reasonable_extreme_values_remain_finite(self):
        candles = [{"t": i, "o": 1e12+i, "h": 1e12+i+10, "l": 1e12+i-10, "c": 1e12+i+1, "v": 1e15} for i in range(150)]
        result = calculate(candles)
        self.assertTrue(math.isfinite(result["ema"]["120"]))
        self.assertTrue(math.isfinite(result["bollinger_20_2"]["upper"]))


if __name__ == "__main__":
    unittest.main()
