import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from app import historical_data
from app.market_data import MarketDataError


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

    def test_uses_latest_published_date_and_backfills_full_31_day_window(self):
        now = datetime(2026, 11, 25, 12, 0, tzinfo=timezone.utc).timestamp()
        latest_open = datetime(2026, 10, 30, 22, 0, tzinfo=timezone.utc).timestamp()

        def make_bars(count, seconds, last_open):
            result = []
            for i in range(count):
                ts = int(last_open - (count - 1 - i) * seconds)
                op = 4000.0 + i * 0.01
                result.append({
                    "timestamp": ts, "open": op, "high": op + 1.0,
                    "low": op - 1.0, "close": op + 0.5, "volume": 10,
                })
            return result

        initial_m5 = make_bars(5 * 24 * 12, 300, latest_open)
        full_m5 = make_bars(31 * 24 * 12, 300, latest_open)
        m15 = make_bars(31 * 24 * 4, 900, latest_open)
        h1 = make_bars(31 * 24, 3600, latest_open)
        h4 = make_bars(31 * 6, 14400, latest_open - 7200)
        m5_calls = []

        def fake_fetch(tf, start, end, max_days):
            if tf == "M5":
                m5_calls.append((start, end, max_days))
                return initial_m5 if len(m5_calls) == 1 else full_m5
            return {"M15": m15, "H1": h1, "H4": h4}[tf]

        with patch.object(historical_data, "_fetch_timeframe", side_effect=fake_fetch):
            data = historical_data.fetch_historical_market_data(now)

        self.assertEqual(len(m5_calls), 2)
        self.assertEqual(data["history_window_end"], "2026-10-30")
        self.assertEqual(data["history_window_start"], "2026-09-29")
        self.assertGreaterEqual(len(data["m5"]), 3000)
        self.assertGreaterEqual(len(data["m15"]), 220)
        self.assertGreaterEqual(len(data["h1"]), 60)
        self.assertGreaterEqual(len(data["h4"]), 50)
        self.assertIsNone(data["live_quote"])
        self.assertIn("AlgoSpecial", data["source"])
        self.assertIn("broker-server time", data["server_time_note"])
        self.assertTrue(all(bar["timestamp"] + 300 <= latest_open for bar in data["m5"]))

    def test_historical_provider_failure_falls_back_without_claiming_oos(self):
        now = datetime(2026, 11, 25, 12, 0, tzinfo=timezone.utc).timestamp()
        fallback = {
            "symbol": "XAUUSD", "source": "XAUS short sample",
            "m5": [{"timestamp": 1, "open": 1, "high": 2, "low": 1, "close": 2}] * 1100,
            "m15": [], "h1": [], "h4": [], "live_quote": {"price": 2},
        }
        with patch.object(
            historical_data, "_fetch_timeframe",
            side_effect=MarketDataError("provider unavailable"),
        ), patch.object(historical_data, "fetch_market_data", return_value=fallback):
            data = historical_data.fetch_historical_market_data(now)
        self.assertIsNone(data["live_quote"])
        self.assertIn("short spot fallback", data["source"])
        self.assertIn("No out-of-sample claim", data["historical_sample_warning"])


if __name__ == "__main__":
    unittest.main()
