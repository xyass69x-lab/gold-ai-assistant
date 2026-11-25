import lzma
import struct
import unittest
from datetime import date, datetime, timezone
from unittest.mock import patch

from app import historical_data


class DukascopyHistoricalDataTests(unittest.TestCase):
    def test_decode_lzma_m1_record(self):
        day = date(2026, 11, 25)
        packed = struct.pack(">IIIIIf", 60, 4_000_000, 4_001_000, 3_999_000, 4_002_000, 12.5)
        payload = lzma.compress(packed, format=lzma.FORMAT_ALONE)
        bars = historical_data._decode_m1_file(payload, day)
        self.assertEqual(len(bars), 1)
        self.assertEqual(bars[0]["open"], 4000.0)
        self.assertEqual(bars[0]["close"], 4001.0)
        self.assertEqual(bars[0]["low"], 3999.0)
        self.assertEqual(bars[0]["high"], 4002.0)
        self.assertEqual(bars[0]["volume"], 12.5)

    def test_bad_record_size_fails_closed(self):
        day = date(2026, 11, 25)
        payload = lzma.compress(b"bad", format=lzma.FORMAT_ALONE)
        with self.assertRaises(historical_data.MarketDataError):
            historical_data._decode_m1_file(payload, day)

    def test_builds_sufficient_complete_mtf_history_without_live_quote(self):
        now = datetime(2026, 11, 25, 23, 59, tzinfo=timezone.utc)
        def fake_day(day, side="BID"):
            start = int(datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp())
            rows = []
            for minute in range(1440):
                op = 4000.0 + minute * 0.001
                close = op + 0.0005
                rows.append({
                    "timestamp": start + minute * 60,
                    "open": op,
                    "high": close + 0.01,
                    "low": op - 0.01,
                    "close": close,
                    "volume": 1.0,
                })
            return rows

        with patch.object(historical_data, "_fetch_day", side_effect=fake_day):
            data = historical_data.fetch_historical_market_data(now.timestamp())

        self.assertGreaterEqual(len(data["m5"]), 1200)
        self.assertGreaterEqual(len(data["m15"]), 220)
        self.assertGreaterEqual(len(data["h1"]), 60)
        self.assertGreaterEqual(len(data["h4"]), 50)
        self.assertIsNone(data["live_quote"])
        self.assertIn("Dukascopy", data["source"])
        self.assertTrue(all(bar["timestamp"] + 300 <= now.timestamp() for bar in data["m5"]))


if __name__ == "__main__":
    unittest.main()
