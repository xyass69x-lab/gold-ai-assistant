import unittest

from app.market_engine import YAHOO_SYMBOL, _atr, _ema, _rsi, analyze_latest_closed_candle


class MarketEngineTests(unittest.TestCase):
    def test_yahoo_gold_symbol_uses_comex_future_proxy(self):
        self.assertEqual(YAHOO_SYMBOL, "GC=F")

    def make_candles(self, count=240, start=1_700_000_000):
        candles = []
        for index in range(count):
            close = 2000.0 + index * 0.1
            candles.append({
                "timestamp": start + index * 1800,
                "open": close - 0.02,
                "high": close + 0.10,
                "low": close - 0.10,
                "close": close,
                "volume": 1,
            })
        return candles

    def test_ema_uses_sma_seed(self):
        result = _ema([1.0, 2.0, 3.0, 4.0, 5.0], 3)
        self.assertIsNone(result[0])
        self.assertIsNone(result[1])
        self.assertAlmostEqual(result[2], 2.0)

    def test_flat_rsi_is_neutral(self):
        result = _rsi([10.0] * 20, 14)
        self.assertAlmostEqual(result[-1], 50.0)

    def test_atr_is_positive_for_nonzero_ranges(self):
        candles = self.make_candles(30)
        result = _atr(candles, 14)
        self.assertGreater(result[-1], 0)

    def test_analysis_uses_only_closed_candles(self):
        candles = self.make_candles(240)
        expected_timestamp = candles[-1]["timestamp"]
        now = expected_timestamp + 1800 + 1
        open_candle = dict(candles[-1])
        open_candle["timestamp"] = now
        open_candle["close"] += 100
        candles.append(open_candle)

        snapshot, signal = analyze_latest_closed_candle(candles, now=now)
        from datetime import datetime, timezone
        self.assertEqual(
            snapshot["candle_at"],
            datetime.fromtimestamp(expected_timestamp, timezone.utc).isoformat(),
        )
        self.assertIsNone(signal)
        self.assertIsNotNone(snapshot["ema200"])
        self.assertIsNotNone(snapshot["rsi14"])
        self.assertIsNotNone(snapshot["atr14"])


if __name__ == "__main__":
    unittest.main()
