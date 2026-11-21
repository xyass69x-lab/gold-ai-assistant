import unittest

from app.trade_tracker import evaluate_trade_candle


class TradeTrackerTests(unittest.TestCase):
    def candle(self, low, high):
        return {"low": low, "high": high}

    def test_buy_hits_tp1_and_moves_to_break_even_mode(self):
        result = evaluate_trade_candle("BUY", 100, 98, 102, 104, False, self.candle(99, 102.2))
        self.assertEqual(result[:3], ("TP1_HIT", True, ["TP1"]))

    def test_buy_stop_before_tp1_is_stop_loss(self):
        result = evaluate_trade_candle("BUY", 100, 98, 102, 104, False, self.candle(97.9, 102.1))
        self.assertEqual(result, ("STOP_LOSS", False, ["STOP_LOSS"], 98))

    def test_buy_after_tp1_stop_is_breakeven(self):
        result = evaluate_trade_candle("BUY", 100, 98, 102, 104, True, self.candle(99.9, 103))
        self.assertEqual(result, ("BREAKEVEN", True, ["BREAKEVEN"], 100))

    def test_sell_hits_both_targets(self):
        result = evaluate_trade_candle("SELL", 100, 102, 98, 96, False, self.candle(95.9, 101))
        self.assertEqual(result, ("TP2_HIT", True, ["TP1", "TP2"], 96))

    def test_sell_after_tp1_hits_tp2(self):
        result = evaluate_trade_candle("SELL", 100, 102, 98, 96, True, self.candle(95.9, 100.5))
        self.assertEqual(result, ("TP2_HIT", True, ["TP2"], 96))


if __name__ == "__main__":
    unittest.main()
