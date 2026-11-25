import unittest
from datetime import datetime, timezone

from app import historical_data


class HistoricalDataNormalizationTests(unittest.TestCase):
    def test_parse_algospecial_broker_timestamp_and_ohlc(self):
        bar = historical_data._parse_bar({
            "Timestamp": "2026.11.25 14:00:00",
            "Open": "4000.1",
            "High": "4002.0",
            "Low": "3999.5",
            "Close": "4001.2",
            "TickVolume": 15,
        })
        self.assertIsNotNone(bar)
        self.assertEqual(bar["open"], 4000.1)
        self.assertEqual(bar["volume"], 15)

    def test_infers_broker_server_offset_against_latest_closed_m5(self):
        now = datetime(2026, 11, 25, 12, 0, tzinfo=timezone.utc).timestamp()
        # Broker server is UTC+2; the 11:55 UTC M5 bar is stamped 13:55 server time.
        server_open = datetime.fromtimestamp(now - 300 + 7200, timezone.utc)
        raw_stamp = int(server_open.timestamp())
        bars = [{"timestamp": raw_stamp}]
        offset, age = historical_data._infer_server_offset(bars, now)
        self.assertEqual(offset, 7200)
        self.assertEqual(age, 0)

    def test_refuses_to_guess_when_latest_bar_is_too_old(self):
        now = datetime(2026, 11, 25, 12, 0, tzinfo=timezone.utc).timestamp()
        old_bar = int(now - 4 * 3600)
        with self.assertRaises(historical_data.MarketDataError):
            historical_data._infer_server_offset([{"timestamp": old_bar}], now)

    def test_rejects_invalid_ohlc(self):
        bar = historical_data._parse_bar({
            "Timestamp": "2026.11.25 14:00:00",
            "Open": 4000, "High": 3990, "Low": 3980, "Close": 4001,
        })
        self.assertIsNone(bar)


if __name__ == "__main__":
    unittest.main()
