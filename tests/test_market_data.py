import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from app import market_data


class MarketDataParsingTests(unittest.TestCase):
    def test_parse_compact_xaus_ohlc_point(self):
        bar = market_data._validate_bar({
            "t": 1_800_000_000,
            "o": 4000,
            "h": 4010,
            "l": 3995,
            "c": 4005,
            "v": None,
        })
        self.assertEqual(bar["timestamp"], 1_800_000_000)
        self.assertEqual(bar["open"], 4000)
        self.assertEqual(bar["high"], 4010)
        self.assertEqual(bar["low"], 3995)
        self.assertEqual(bar["close"], 4005)

    def test_parse_millisecond_timestamp(self):
        bar = market_data._validate_bar({
            "timestamp": 1_800_000_000_000,
            "open": 4000,
            "high": 4010,
            "low": 3990,
            "close": 4002,
        })
        self.assertEqual(bar["timestamp"], 1_800_000_000)

    def test_reject_invalid_ohlc(self):
        self.assertIsNone(market_data._validate_bar({
            "t": 1_800_000_000, "o": 4000, "h": 3990,
            "l": 3980, "c": 4001,
        }))

    def test_documented_points_array(self):
        points = [{"t": 1_800_000_000, "o": 1, "h": 2, "l": 1, "c": 2}]
        self.assertEqual(market_data._point_list({"points": points}), points)

    def test_reject_stale_provider_state(self):
        with self.assertRaises(market_data.MarketDataError):
            market_data._point_list({
                "points": [],
                "data_state": {"status": "stale"},
            })


class MarketDataAggregationTests(unittest.TestCase):
    def test_only_complete_buckets_are_resampled(self):
        bars = []
        start = 1_800_000_000
        for i in (0, 300, 600, 1200):
            bars.append({
                "timestamp": start + i,
                "open": 100 + i / 300,
                "high": 102 + i / 300,
                "low": 99 + i / 300,
                "close": 101 + i / 300,
                "volume": None,
            })
        result = market_data.resample_candles(bars, 300, 900)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["timestamp"], (start // 900) * 900)


class MarketDataFetchTests(unittest.TestCase):
    @staticmethod
    def _bars(start, count, seconds):
        return [{
            "timestamp": start + i * seconds,
            "open": 4000 + i * 0.01,
            "high": 4002 + i * 0.01,
            "low": 3998 + i * 0.01,
            "close": 4001 + i * 0.01,
            "volume": None,
        } for i in range(count)]

    def test_builds_all_timeframes_from_xaus_history(self):
        now = datetime(2026, 11, 25, tzinfo=timezone.utc).timestamp()
        m5 = self._bars(int(now) - 1200 * 300, 1200, 300)
        h1 = self._bars(int(now) - 240 * 3600, 240, 3600)

        def fake_fetch(interval, period_range, cache_seconds, force=False):
            return m5 if interval == "5m" else h1

        with patch.object(market_data, "_fetch_chart", side_effect=fake_fetch):
            data = market_data.fetch_market_data(now=now, include_live_quote=False)

        self.assertEqual(data["source"], market_data.SOURCE_NAME)
        self.assertGreaterEqual(len(data["m5"]), 500)
        self.assertGreaterEqual(len(data["m15"]), 220)
        self.assertGreaterEqual(len(data["h1"]), 60)
        self.assertGreaterEqual(len(data["h4"]), 50)
        self.assertEqual(data["m5_age_seconds"], 0)

    def test_stale_m5_data_fails_closed(self):
        now = datetime(2026, 11, 25, tzinfo=timezone.utc).timestamp()
        m5 = self._bars(int(now) - 1200 * 300 - 3600, 1200, 300)
        h1 = self._bars(int(now) - 240 * 3600, 240, 3600)

        def fake_fetch(interval, period_range, cache_seconds, force=False):
            return m5 if interval == "5m" else h1

        with patch.object(market_data, "_fetch_chart", side_effect=fake_fetch):
            with self.assertRaises(market_data.MarketDataError):
                market_data.fetch_market_data(now=now)


    def test_fresh_independent_spot_quote_is_parsed(self):
        now = 1_800_000_000
        as_of = datetime.fromtimestamp(now - 12, timezone.utc).isoformat()
        response = unittest.mock.Mock()
        response.status_code = 200
        response.json.return_value = {
            "spot_usd_oz": 4012.5,
            "price_as_of": as_of,
            "data_state": {"status": "fresh", "age_seconds": 12, "as_of": as_of},
        }
        with patch.object(market_data.requests, "get", return_value=response):
            quote = market_data.fetch_live_price(now=now)
        self.assertEqual(quote["price"], 4012.5)
        self.assertLessEqual(quote["age_seconds"], 12)
        self.assertEqual(quote["source"], market_data.LIVE_PRICE_SOURCE)

    def test_stale_independent_spot_quote_is_rejected(self):
        now = 1_800_000_000
        as_of = datetime.fromtimestamp(now - 240, timezone.utc).isoformat()
        response = unittest.mock.Mock()
        response.status_code = 200
        response.json.return_value = {
            "spot_usd_oz": 4012.5,
            "price_as_of": as_of,
            "data_state": {"status": "stale", "age_seconds": 240, "as_of": as_of},
        }
        with patch.object(market_data.requests, "get", return_value=response):
            with self.assertRaises(market_data.MarketDataError):
                market_data.fetch_live_price(now=now)


if __name__ == "__main__":
    unittest.main()
