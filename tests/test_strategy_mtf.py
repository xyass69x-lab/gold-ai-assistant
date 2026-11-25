import unittest

from app.strategy_mtf import _bias, analyze_mtf, ema


def bars(start, count, seconds, base=100.0, step=0.1):
    result = []
    for i in range(count):
        open_price = base + i * step
        close_price = open_price + 0.05
        result.append({
            "timestamp": start + i * seconds,
            "open": open_price,
            "high": close_price + 0.20,
            "low": open_price - 0.20,
            "close": close_price,
            "volume": None,
        })
    return result


class MultiTimeframeStrategyTests(unittest.TestCase):
    def setUp(self):
        self.now = 1_800_000_000
        self.data = {
            "symbol": "XAUUSD",
            "source": "unit-test",
            "checked_at": "2027-01-15T08:00:00+00:00",
            "m5_age_seconds": 0,
            "m15_age_seconds": 0,
            "h1_age_seconds": 0,
            "h4_age_seconds": 0,
            "m5": bars(self.now - 3 * 300, 3, 300, base=121.0, step=0.1),
            "m15": bars(self.now - 220 * 900, 220, 900),
            "h1": bars(self.now - 60 * 3600, 60, 3600),
            "h4": bars(self.now - 60 * 14400, 60, 14400),
        }

    def test_ema_warms_up_without_lookahead(self):
        values = ema([1.0, 2.0, 3.0, 4.0], 3)
        self.assertEqual(values[:2], [None, None])
        self.assertEqual(values[2], 2.0)
        self.assertAlmostEqual(values[3], 3.0)

    def test_higher_timeframe_bias_ignores_unclosed_bar(self):
        candles = self.data["h1"]
        baseline = _bias(candles, self.now, 3600)
        future_bar = bars(self.now, 1, 3600, base=10_000.0, step=1.0)[0]
        self.assertEqual(_bias(candles + [future_bar], self.now, 3600), baseline)

    def test_analysis_rejects_insufficient_closed_m15_history(self):
        self.data["m15"] = self.data["m15"][:-1]
        with self.assertRaises(ValueError):
            analyze_mtf(self.data, self.now)

    def test_unclosed_extreme_bars_do_not_change_snapshot(self):
        baseline, _ = analyze_mtf(self.data, self.now)
        with_future = {key: list(value) if key in ("m5", "m15") else value
                       for key, value in self.data.items()}
        with_future["m5"].append(
            bars(self.now, 1, 300, base=10_000.0, step=1.0)[0]
        )
        with_future["m15"].append(
            bars(self.now, 1, 900, base=10_000.0, step=1.0)[0]
        )
        changed, _ = analyze_mtf(with_future, self.now)
        self.assertEqual(changed["price"], baseline["price"])
        self.assertEqual(changed["ema200"], baseline["ema200"])
        self.assertEqual(changed["rsi14"], baseline["rsi14"])

    def test_missing_higher_timeframe_bias_fails_closed(self):
        self.data["h1"] = []
        self.data["h4"] = []
        snapshot, signal = analyze_mtf(self.data, self.now)
        self.assertEqual(snapshot["higher_timeframe_bias"]["h1"], "UNAVAILABLE")
        self.assertEqual(snapshot["higher_timeframe_bias"]["h4"], "UNAVAILABLE")
        self.assertIsNone(signal)


if __name__ == "__main__":
    unittest.main()
