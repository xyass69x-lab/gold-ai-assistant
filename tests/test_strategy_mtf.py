import unittest
from unittest.mock import patch

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


    def _make_live_setup(self, quote_age=10):
        self.data["m15"][-1].update({
            "open": 121.4, "high": 122.1, "low": 120.0, "close": 122.0,
        })
        self.data["m5"][-2].update({
            "open": 120.3, "high": 120.8, "low": 120.2, "close": 120.5,
        })
        self.data["m5"][-1].update({
            "open": 120.5, "high": 121.2, "low": 120.4, "close": 121.0,
        })
        self.data["live_quote"] = {
            "price": 121.05, "bid": 121.04, "ask": 121.06,
            "age_seconds": quote_age, "source": "test spot feed",
        }
        self.data["m5_age_seconds"] = 271

    def _patch_indicators(self):
        def fake_ema(values, period):
            if period == 20:
                return [120.0] * len(values)
            if period == 50:
                return [119.0] * len(values)
            return [100.0 + i * 0.01 for i in range(len(values))]
        return (
            patch("app.strategy_mtf.ema", side_effect=fake_ema),
            patch("app.strategy_mtf.rsi", side_effect=lambda values, period=14: [60.0] * len(values)),
            patch("app.strategy_mtf.atr", side_effect=lambda candles, period=14: [1.0] * len(candles)),
            patch("app.strategy_mtf._bias", return_value="BULLISH"),
        )

    def test_live_quote_allows_actionable_entry_after_90_seconds(self):
        self._make_live_setup()
        patches = self._patch_indicators()
        with patches[0], patches[1], patches[2], patches[3]:
            snapshot, signal = analyze_mtf(self.data, self.now + 271)
        self.assertIsNotNone(signal, snapshot)
        self.assertEqual(signal["entry"], 121.06)
        self.assertEqual(snapshot["entry_source"], "test spot feed")
        self.assertEqual(signal["confidence"], snapshot["quality_score"])
        self.assertEqual(snapshot["quality_score_type"], "technical_quality_v3_not_win_probability")
        self.assertAlmostEqual(sum(snapshot["quality_score_parts"].values()), snapshot["quality_score"], delta=0.6)
        self.assertEqual(set(snapshot["quality_score_parts"]), {
            "trend_strength_and_slope", "m15_ema_structure", "h1_h4_alignment", "m15_setup_quality",
            "m5_trigger_strength", "rsi_position",
        })
        self.assertGreaterEqual(signal["confidence"], 80)
        self.assertLessEqual(signal["confidence"], 100)


    def test_quality_score_rewards_directional_m15_ema_stack(self):
        self._make_live_setup()
        patches = self._patch_indicators()
        with patches[0], patches[1], patches[2], patches[3]:
            baseline, signal = analyze_mtf(self.data, self.now + 271)
        self.assertIsNotNone(signal)
        # Keep the rest of the setup fixed while making EMA20 less aligned
        # with EMA50 in the direction of the BUY.
        original_ema = __import__("app.strategy_mtf", fromlist=["ema"]).ema
        def weaker_stack(values, period):
            result = original_ema(values, period)
            if period == 20:
                return [120.0] * len(values)
            if period == 50:
                return [121.0] * len(values)
            return [100.0 + i * 0.01 for i in range(len(values))]
        with patch("app.strategy_mtf.ema", side_effect=weaker_stack), \
             patches[1], patches[2], patches[3]:
            weaker, weaker_signal = analyze_mtf(self.data, self.now + 271)
        self.assertIsNotNone(weaker_signal)
        self.assertGreater(
            baseline["quality_score_parts"]["m15_ema_structure"],
            weaker["quality_score_parts"]["m15_ema_structure"],
        )

    def test_quality_score_rewards_stronger_m5_trigger(self):
        self._make_live_setup()
        patches = self._patch_indicators()
        with patches[0], patches[1], patches[2], patches[3]:
            baseline, signal = analyze_mtf(self.data, self.now + 271)
        self.assertIsNotNone(signal)
        self.data["m5"][-1].update({"open": 120.2, "close": 121.4, "high": 121.5})
        with patches[0], patches[1], patches[2], patches[3]:
            stronger, stronger_signal = analyze_mtf(self.data, self.now + 271)
        self.assertIsNotNone(stronger_signal)
        self.assertGreater(
            stronger["quality_score_parts"]["m5_trigger_strength"],
            baseline["quality_score_parts"]["m5_trigger_strength"],
        )

    def test_stale_live_quote_blocks_mtf_signal(self):
        self._make_live_setup(quote_age=120)
        patches = self._patch_indicators()
        with patches[0], patches[1], patches[2], patches[3]:
            _, signal = analyze_mtf(self.data, self.now + 120)
        self.assertIsNone(signal)

    def test_rounded_signal_levels_preserve_minimum_two_to_one_rr(self):
        self._make_live_setup()
        self.data["live_quote"].update({
            "price": 121.061, "bid": 121.060, "ask": 121.061,
        })
        patches = self._patch_indicators()
        with patches[0], patches[1], patches[2], patches[3]:
            _, signal = analyze_mtf(self.data, self.now + 271)
        self.assertIsNotNone(signal)
        risk = abs(signal["entry"] - signal["stop_loss"])
        reward = abs(signal["take_profit"] - signal["entry"])
        self.assertGreaterEqual(reward / risk, 2.0)
        self.assertLess(signal["entry"], signal["take_profit_1"])
        self.assertLess(signal["take_profit_1"], signal["take_profit"])

        # Exercise the same strict API validator used by the live engine.
        from app.main import Signal, validate_signal
        validated_rr = validate_signal(Signal(**signal))
        self.assertGreaterEqual(validated_rr, 2.0)


    def test_mtf_rejects_live_price_diverged_from_higher_timeframe_candle(self):
        self._make_live_setup()
        self.data["m15"][-1].update({
            "open": 122.8, "high": 123.6, "low": 122.7, "close": 123.5,
        })
        patches = self._patch_indicators()
        with patches[0], patches[1], patches[2], patches[3]:
            snapshot, signal = analyze_mtf(self.data, self.now + 120)
        self.assertIsNone(signal)
        self.assertIn("diverged", snapshot["entry_reject_reason"])


if __name__ == "__main__":
    unittest.main()
