import unittest
from unittest.mock import patch

from app import backtest_mtf


class BacktestTimingTests(unittest.TestCase):
    @staticmethod
    def _data():
        start = 1_800_000_000
        candles = [{
            "timestamp": start + i * 300,
            "open": 4000.0,
            "high": 4002.0,
            "low": 3998.0,
            "close": 4001.0,
            "volume": None,
        } for i in range(1200)]
        return {
            "m5": candles,
            "m15": [],
            "h1": [],
            "h4": [],
            "source": "unit-test",
        }

    def test_backtest_evaluates_after_next_candle_opens(self):
        data = self._data()
        with patch.object(backtest_mtf, "fetch_historical_market_data", return_value=data), \
             patch.object(backtest_mtf, "analyze_mtf", return_value=({"setup": None}, None)) as analyze:
            backtest_mtf.run_backtest_report()

        first_data = analyze.call_args_list[0].args[0]
        first_now = analyze.call_args_list[0].args[1]
        self.assertNotIn("live_quote", first_data)
        self.assertEqual(first_now, data["m5"][701]["timestamp"] + 30)

    def test_stop_wins_when_stop_and_target_touch_same_candle(self):
        candles = [{
            "timestamp": 1_800_000_000,
            "open": 100.0,
            "high": 112.0,
            "low": 88.0,
            "close": 105.0,
        }]
        result = backtest_mtf._simulate(
            candles, 0, "BUY", 100.0, 90.0, 110.0, 120.0
        )
        self.assertEqual(result["r"], -1.0)
        self.assertFalse(result["tp1"])


if __name__ == "__main__":
    unittest.main()
