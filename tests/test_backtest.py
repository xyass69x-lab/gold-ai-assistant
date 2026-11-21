import unittest

from app.backtest import _metrics, _signal_side, _simulate_trade


class BacktestSimulationTests(unittest.TestCase):
    def bar(self, low, high, close=100):
        return {"low": low, "high": high, "close": close}

    def test_buy_stop_before_tp1_is_minus_one_r(self):
        result = _simulate_trade([self.bar(98.0, 100.5)], 0, "BUY", 100.0, 1.0, 48)
        self.assertEqual(result["gross_r"], -1.0)
        self.assertTrue(result["stop_before_tp1"])

    def test_buy_tp2_is_plus_one_point_five_r(self):
        result = _simulate_trade([self.bar(100.0, 102.1)], 0, "BUY", 100.0, 1.0, 48)
        self.assertEqual(result["gross_r"], 1.5)
        self.assertTrue(result["tp2"])

    def test_buy_tp1_then_stop_is_zero_r(self):
        candles = [self.bar(100.0, 101.1), self.bar(98.9, 100.5)]
        result = _simulate_trade(candles, 0, "BUY", 100.0, 1.0, 48)
        self.assertEqual(result["gross_r"], 0.0)
        self.assertTrue(result["stop_after_tp1"])

    def test_sell_tp2_is_plus_one_point_five_r(self):
        result = _simulate_trade([self.bar(97.9, 100.0)], 0, "SELL", 100.0, 1.0, 48)
        self.assertEqual(result["gross_r"], 1.5)
        self.assertTrue(result["tp2"])

    def test_metrics_split_wins_losses_and_breakeven(self):
        trades = [
            {"gross_r": 1.5, "tp1": True, "tp2": True},
            {"gross_r": -1.0, "tp1": False, "tp2": False},
            {"gross_r": 0.0, "tp1": True, "tp2": False},
        ]
        result = _metrics(trades)
        self.assertEqual(result["count"], 3)
        self.assertEqual(result["wins"], 1)
        self.assertEqual(result["losses"], 1)
        self.assertEqual(result["breakeven"], 1)
        self.assertAlmostEqual(result["net_r"], 0.5)

    def test_strict_rsi_filter_blocks_weak_buy(self):
        candles = [{"open": 99.0, "high": 101.0, "low": 99.5, "close": 100.5}]
        indicators = (
            [99.0], [99.0], [98.0], [53.0], [1.0],
        )
        config = {"slope_filter": False, "rsi_buy": 55, "rsi_sell": 45, "body_atr": 0.0}
        self.assertIsNone(_signal_side(candles, 0, indicators, config))


if __name__ == "__main__":
    unittest.main()
