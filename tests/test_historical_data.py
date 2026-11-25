import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from app import historical_data


class AlgoSpecialHistoricalDataTests(unittest.TestCase):
    def test_parses_broker_server_timestamp_and_ohlc(self):
        bar = historical_data._parse_bar({
            "Timestamp": "2026.11.25 14:00:00",
            "Open": "4000.1", "High": "4002.0",
            "Low": "3999.5", "Close": "4001.2",
            "TickVolume": 15,
        })
        self.assertIsNotNone(bar)
        self.assertEqual(bar["open"], 4000.1)
        self.assertEqual(bar["volume"], 15)

    def test_rejects_invalid_ohlc(self):
        self.assertIsNone(historical_data._parse_bar({
            "Timestamp": "2026.11.25 14:00:00",
            "Open": 4000, "High": 3990, "Low": 3980, "Close": 4001,
        }))

    def test_date_chunks_stay_within_provider_row_cap(self):
        start = datetime(2026, 10, 26).date()
        end = start + timedelta(days=30)
        chunks = list(historical_data._date_chunks(start, end, 6))
        self.assertEqual(len(chunks), 6)
        self.assertTrue(all((right - left).days < 6 for left, right in chunks))
        self.assertEqual(chunks[0][0], start)
        self.assertEqual(chunks[-1][1], end)

    def test_infers_broker_server_offset_from_latest_closed_m5(self):
        now = datetime(2026, 11, 25, 12, 0, tzinfo=timezone.utc).timestamp()
        # Provider timestamps are UTC+2; last M5 opened at 11:55 UTC.
        raw_open = int(now - 300 + 7200)
        offset, age = historical_data._infer_server_utc_offset(
            [{"timestamp": raw_open}], now
        )
        self.assertEqual(offset, 7200)
        self.assertEqual(age, 0)

    def test_refuses_ambiguous_or_old_server_time(self):
        now = datetime(2026, 11, 25, 12, 0, tzinfo=timezone.utc).timestamp()
        with self.assertRaises(historical_data.MarketDataError):
            historical_data._infer_server_utc_offset(
                [{"timestamp": int(now - 4 * 3600)}], now
            )

    def test_fetches_extended_closed_history_and_never_uses_live_quote(self):
        now = datetime(2026, 11, 25, 12, 0, tzinfo=timezone.utc).timestamp()
        offset = 7200

        def bars(count, seconds):
            result = []
            for i in range(count):
                utc_open = now - (count - i) * seconds
                raw_open = int(utc_open + offset)
                op = 4000.0 + i * 0.01
                result.append({
                    "timestamp": raw_open,
                    "open": op, "high": op + 1.0,
                    "low": op - 1.0, "close": op + 0.5,
                    "volume": 10,
                })
            return result

        m5 = bars(31 * 24 * 12, 300)
        m15 = bars(31 * 24 * 4, 900)
        h1 = bars(31 * 24, 3600)
        h4 = bars(31 * 6, 14400)

        def fake_fetch(tf, start, end, max_days):
            return {"M5": m5, "M15": m15, "H1": h1, "H4": h4}[tf]

        with patch.object(historical_data, "_fetch_timeframe", side_effect=fake_fetch):
            data = historical_data.fetch_historical_market_data(now)

        self.assertGreaterEqual(len(data["m5"]), 3000)
        self.assertGreaterEqual(len(data["m15"]), 220)
        self.assertGreaterEqual(len(data["h1"]), 60)
        self.assertGreaterEqual(len(data["h4"]), 50)
        self.assertEqual(data["server_utc_offset_seconds"], offset)
        self.assertIsNone(data["live_quote"])
        self.assertIn("AlgoSpecial", data["source"])
        self.assertTrue(all(bar["timestamp"] + 300 <= now for bar in data["m5"]))


if __name__ == "__main__":
    unittest.main()
