import unittest

from app.strategy import evaluate_strategy_signal, higher_timeframe_biases, resample_2h


class HybridStrategyTests(unittest.TestCase):
    def make_candles(self, count=240):
        bars = []
        for i in range(count):
            close = 2000.0 + i * 0.1
            ts = 1_700_006_400 + i * 1800
            bars.append({
                "timestamp": ts,
                "open": close - 0.02,
                "high": close + 0.10,
                "low": close - 0.10,
                "close": close,
                "volume": 1,
            })
        return bars

    def test_resample_uses_only_complete_four_bar_groups(self):
        bars = self.make_candles(9)
        higher = resample_2h(bars)
        self.assertEqual(len(higher), 2)
        self.assertEqual(higher[0]["open"], bars[0]["open"])
        self.assertEqual(higher[0]["close"], bars[3]["close"])
        self.assertEqual(higher[0]["high"], max(x["high"] for x in bars[:4]))

    def test_incomplete_gap_drops_the_whole_two_hour_bucket(self):
        bars = self.make_candles(8)
        bars.pop(2)
        higher = resample_2h(bars)
        self.assertEqual(len(higher), 1)
        self.assertEqual(higher[0]["timestamp"], bars[3]["timestamp"] // 7200 * 7200)

    def test_higher_timeframe_bias_does_not_use_unclosed_two_hour_bar(self):
        bars = self.make_candles(220)
        biases = higher_timeframe_biases(bars)
        self.assertEqual(len(biases), len(bars))
        # At the first three 30m bars of a new 2h bucket, the current bucket
        # cannot be used as a completed 2h candle.
        self.assertEqual(biases[216], biases[215])

    def test_hybrid_pullback_returns_quality_score(self):
        bars = self.make_candles(220)
        bars[-2]["close"] = 2021.0
        bars[-1].update({"open": 2021.05, "high": 2022.1, "low": 2020.9, "close": 2022.0})
        ema20 = [2021.5] * 220
        ema50 = [2020.0] * 220
        ema200 = [2019.0 + i * 0.001 for i in range(220)]
        rsi = [60.0] * 220
        atr = [1.0] * 220
        indicators = (ema20, ema50, ema200, rsi, atr)

        decision = evaluate_strategy_signal(bars, 219, indicators, "BULLISH")

        self.assertIsNotNone(decision)
        self.assertEqual(decision["side"], "BUY")
        self.assertEqual(decision["setup"], "PULLBACK")
        self.assertGreaterEqual(decision["quality_score"], 75)


    def test_hybrid_breakout_setup_is_detected(self):
        bars = self.make_candles(220)
        bars[-1].update({"open": 2021.0, "high": 2023.0, "low": 2020.9, "close": 2022.5})
        indicators = (
            [1990.0] * 220,
            [2010.0] * 220,
            [2000.0 + i * 0.001 for i in range(220)],
            [60.0] * 220,
            [1.0] * 220,
        )

        decision = evaluate_strategy_signal(bars, 219, indicators, "BULLISH")

        self.assertIsNotNone(decision)
        self.assertEqual(decision["side"], "BUY")
        self.assertEqual(decision["setup"], "BREAKOUT")

    def test_opposite_higher_timeframe_bias_blocks_trade(self):
        bars = self.make_candles(220)
        bars[-2]["close"] = 2021.0
        bars[-1].update({"open": 2021.05, "high": 2022.1, "low": 2020.9, "close": 2022.0})
        indicators = (
            [2021.5] * 220,
            [2020.0] * 220,
            [2019.0 + i * 0.001 for i in range(220)],
            [60.0] * 220,
            [1.0] * 220,
        )
        self.assertIsNone(evaluate_strategy_signal(bars, 219, indicators, "BEARISH"))


if __name__ == "__main__":
    unittest.main()
