import unittest

from app.backtest import _simulate_trade


class BacktestSimulationTests(unittest.TestCase):
    def bar(self, low, high, close=100):
        return {"low": low, "high": high, "close": close}

    def test_buy_stop_before_tp1_is_minus_one_r(self):
        candles = [self.bar(98.0, 100.5)]
        result = _simulate_trade(candles, 0, "BUY", 100.0, 1.0, 48)
        self.assertEqual(result["gross_r"], -1.0)
        self.assertTrue(result["stop_before_tp1"])

    def test_buy_tp2_is_plus_one_point_five_r(self):
        candles = [self.bar(100.0, 102.1)]
        result = _simulate_trade(candles, 0, "BUY", 100.0, 1.0, 48)
        self.assertEqual(result["gross_r"], 1.5)
        self.assertTrue(result["tp2"])

    def test_buy_tp1_then_stop_is_zero_r(self):
        candles = [
            self.bar(100.0, 101.1),
            self.bar(98.9, 100.5),
        ]
        result = _simulate_trade(candles, 0, "BUY", 100.0, 1.0, 48)
        self.assertEqual(result["gross_r"], 0.0)
        self.assertTrue(result["stop_after_tp1"])

    def test_sell_tp2_is_plus_one_point_five_r(self):
        candles = [self.bar(97.9, 100.0)]
        result = _simulate_trade(candles, 0, "SELL", 100.0, 1.0, 48)
        self.assertEqual(result["gross_r"], 1.5)
        self.assertTrue(result["tp2"])


if __name__ == "__main__":
    unittest.main()
