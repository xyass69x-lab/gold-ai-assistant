import unittest

from unittest.mock import patch

from app.backtest import _metrics, _run_segment, _signal_side, _simulate_trade, _with_cost


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

    def test_buy_tp1_then_break_even_is_plus_half_r(self):
        candles = [self.bar(100.0, 101.1), self.bar(98.9, 100.5)]
        result = _simulate_trade(candles, 0, "BUY", 100.0, 1.0, 48)
        self.assertEqual(result["gross_r"], 0.5)
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

    def test_round_trip_cost_is_deducted_from_each_trade(self):
        trades = [
            {"gross_r": 1.5, "side": "BUY"},
            {"gross_r": -1.0, "side": "SELL"},
        ]
        adjusted = _with_cost(trades, 0.1)
        self.assertAlmostEqual(adjusted[0]["net_r"], 1.4)
        self.assertAlmostEqual(adjusted[1]["net_r"], -1.1)
        metrics = _metrics(adjusted, "net_r")
        self.assertAlmostEqual(metrics["net_r"], 0.3)
        self.assertAlmostEqual(metrics["avg_r"], 0.15)

    def test_sell_only_candidate_filters_out_buy_signals(self):
        candles = [{"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0}
                   for _ in range(300)]
        indicators = ([100.0] * 300,) * 5

        def fake_signal(_candles, index, _indicators, _config, _htf_bias):
            return {200: "BUY", 201: "SELL"}.get(index)

        def fake_trade(_candles, entry_index, side, _entry, _risk, _horizon):
            return {
                "gross_r": 1.0, "tp1": False, "tp2": False,
                "exit_index": entry_index, "side": side,
            }

        config = {"side_filter": "SELL"}
        with patch("app.backtest._signal_side", side_effect=fake_signal), patch(
            "app.backtest._simulate_trade", side_effect=fake_trade
        ):
            trades = _run_segment(candles, indicators, config, 200, 300)

        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["side"], "SELL")

    def test_strict_rsi_filter_blocks_weak_buy(self):
        candles = [{"open": 99.0, "high": 101.0, "low": 99.5, "close": 100.5}]
        indicators = (
            [99.0], [99.0], [98.0], [53.0], [1.0],
        )
        config = {"slope_filter": False, "rsi_buy": 55, "rsi_sell": 45, "body_atr": 0.0}
        self.assertIsNone(_signal_side(candles, 0, indicators, config))

    def test_sell_tp1_then_break_even_is_plus_half_r(self):
        candles = [self.bar(98.9, 100.0), self.bar(99.5, 101.1)]
        result = _simulate_trade(candles, 0, "SELL", 100.0, 1.0, 48)
        self.assertEqual(result["gross_r"], 0.5)
        self.assertTrue(result["stop_after_tp1"])

    def test_original_stop_wins_if_stop_and_tp1_touched_same_candle(self):
        result = _simulate_trade([self.bar(98.9, 101.1)], 0, "BUY", 100.0, 1.0, 48)
        self.assertEqual(result["gross_r"], -1.0)
        self.assertFalse(result["tp1"])
        self.assertTrue(result["stop_before_tp1"])

    def test_tp1_and_tp2_same_candle_is_plus_one_point_five_r(self):
        result = _simulate_trade([self.bar(99.5, 102.1)], 0, "BUY", 100.0, 1.0, 48)
        self.assertEqual(result["gross_r"], 1.5)
        self.assertTrue(result["tp1"])
        self.assertTrue(result["tp2"])


if __name__ == "__main__":
    unittest.main()
