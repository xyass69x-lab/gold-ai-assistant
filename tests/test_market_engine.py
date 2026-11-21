import unittest
from datetime import datetime, timezone

from app.market_engine import (
    INTERVAL,
    SYMBOL,
    TIMEFRAME_SECONDS,
    MarketDataError,
    _atr,
    _ema,
    _parse_datetime,
    _rsi,
    analyze_latest_closed_candle,
)


class MarketEngineTests(unittest.TestCase):
    def make_candles(self, count=240, start=1_700_000_000):
        candles = []
        for index in range(count):
            close = 2000.0 + index * 0.1
            candles.append({
                "timestamp": start + index * TIMEFRAME_SECONDS,
                "open": close - 0.02,
                "high": close + 0.10,
                "low": close - 0.10,
                "close": close,
                "volume": 1,
            })
        return candles

    def test_market_source_is_spot_gold(self):
        self.assertEqual(SYMBOL, "XAU/USD")
        self.assertEqual(INTERVAL, "30min")

    def test_parse_datetime_accepts_utc_iso(self):
        parsed = _parse_datetime("2026-11-21T12:30:00Z")
        expected = int(datetime(2026, 11, 21, 12, 30, tzinfo=timezone.utc).timestamp())
        self.assertEqual(parsed, expected)

    def test_parse_datetime_rejects_invalid_value(self):
        with self.assertRaises(MarketDataError):
            _parse_datetime("not-a-date")

    def test_ema_uses_sma_seed(self):
        result = _ema([1.0, 2.0, 3.0, 4.0, 5.0], 3)
        self.assertIsNone(result[0])
        self.assertIsNone(result[1])
        self.assertAlmostEqual(result[2], 2.0)

    def test_flat_rsi_is_neutral(self):
        result = _rsi([10.0] * 20, 14)
        self.assertAlmostEqual(result[-1], 50.0)

    def test_atr_is_positive_for_nonzero_ranges(self):
        result = _atr(self.make_candles(30), 14)
        self.assertGreater(result[-1], 0)

    def test_stale_market_candles_are_rejected(self):
        candles = self.make_candles(240)
        now = candles[-1]["timestamp"] + TIMEFRAME_SECONDS + 91 * 60
        with self.assertRaises(MarketDataError):
            analyze_latest_closed_candle(candles, now=now)

    def test_analysis_uses_only_closed_candles(self):
        candles = self.make_candles(240)
        expected_timestamp = candles[-1]["timestamp"]
        now = expected_timestamp + TIMEFRAME_SECONDS + 1
        open_candle = dict(candles[-1])
        open_candle["timestamp"] = now
        open_candle["close"] += 100
        candles.append(open_candle)

        snapshot, signal = analyze_latest_closed_candle(candles, now=now)
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
