import unittest

from app.market_data import BASE_CACHE_SECONDS, resample_candles
from app.strategy_mtf import _bias, analyze_mtf


class MultiTimeframeTests(unittest.TestCase):
    def test_m5_cache_is_shorter_than_entry_confirmation_window(self):
        self.assertLessEqual(BASE_CACHE_SECONDS, 90)
        self.assertGreaterEqual(BASE_CACHE_SECONDS, 30)

    def test_resample_requires_complete_bucket(self):
        bars = [
            {"timestamp": 0, "open": 10, "high": 11, "low": 9, "close": 10.5},
            {"timestamp": 300, "open": 10.5, "high": 12, "low": 10, "close": 11.5},
            {"timestamp": 600, "open": 11.5, "high": 13, "low": 11, "close": 12.5},
            {"timestamp": 1200, "open": 12.5, "high": 14, "low": 12, "close": 13.5},
        ]
        result = resample_candles(bars, 300, 900)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["timestamp"], 0)
        self.assertEqual(result[0]["open"], 10)
        self.assertEqual(result[0]["high"], 13)
        self.assertEqual(result[0]["low"], 9)
        self.assertEqual(result[0]["close"], 12.5)

    def test_bias_ignores_future_higher_timeframe_candle(self):
        bars = []
        for i in range(60):
            close = 100 + i
            bars.append({
                "timestamp": i * 14400, "open": close - 0.5,
                "high": close + 1, "low": close - 1, "close": close,
            })
        cutoff = bars[-1]["timestamp"] + 14400
        bars.append({
            "timestamp": cutoff, "open": 1, "high": 2, "low": 0.5, "close": 1,
        })
        self.assertEqual(_bias(bars, cutoff, 14400), "BULLISH")

    def test_mtf_analyzer_accepts_closed_series_without_lookahead(self):
        m5 = []
        for i in range(3000):
            close = 2000 + i * 0.02
            m5.append({
                "timestamp": i * 300, "open": close - 0.01,
                "high": close + 0.03, "low": close - 0.03, "close": close,
            })
        h4 = []
        for i in range(70):
            close = 2000 + i * 4
            h4.append({
                "timestamp": i * 14400, "open": close - 0.5,
                "high": close + 1, "low": close - 1, "close": close,
            })
        now = m5[-1]["timestamp"] + 301
        data = {
            "m5": m5,
            "m15": resample_candles(m5, 300, 900),
            "h1": resample_candles(m5, 300, 3600),
            "h4": h4,
            "source": "test",
            "checked_at": "test",
            "m5_age_seconds": 1,
            "h4_age_seconds": 1,
        }
        snapshot, signal = analyze_mtf(data, now)
        self.assertEqual(snapshot["timeframe"], "M5/M15/H1/H4")
        self.assertEqual(snapshot["higher_timeframe_bias"]["h4"], "BULLISH")
        self.assertIsNone(signal)


if __name__ == "__main__":
    unittest.main()
