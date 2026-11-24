import unittest

from app.main import Signal, validate_signal


class SignalValidationTests(unittest.TestCase):
    def test_legacy_buy_signal_gets_midpoint_tp1(self):
        signal = Signal(
            symbol="XAUUSD",
            side="BUY",
            entry=4000.0,
            stop_loss=3990.0,
            take_profit=4020.0,
        )

        rr = validate_signal(signal)

        self.assertEqual(rr, 2.0)
        self.assertEqual(signal.take_profit_1, 4010.0)

    def test_legacy_sell_signal_gets_midpoint_tp1(self):
        signal = Signal(
            symbol="XAUUSD",
            side="SELL",
            entry=4000.0,
            stop_loss=4010.0,
            take_profit=3980.0,
        )

        rr = validate_signal(signal)

        self.assertEqual(rr, 2.0)
        self.assertEqual(signal.take_profit_1, 3990.0)

    def test_explicit_tp1_outside_target_range_is_rejected(self):
        signal = Signal(
            symbol="XAUUSD",
            side="BUY",
            entry=4000.0,
            stop_loss=3990.0,
            take_profit_1=4030.0,
            take_profit=4020.0,
        )

        with self.assertRaises(Exception):
            validate_signal(signal)


if __name__ == "__main__":
    unittest.main()
