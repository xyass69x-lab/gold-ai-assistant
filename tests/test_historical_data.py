import unittest
from unittest.mock import Mock, patch

from app import historical_data
from app.market_data import MarketDataError


class HistoricalGoldProxyTests(unittest.TestCase):
    def test_yahoo_gc_chart_payload_is_normalized_and_resampled(self):
        start = 1_800_000_000
        count = 4000
        timestamps = [start + i * 300 for i in range(count)]
        opens = [4000.0 + i * 0.01 for i in range(count)]
        closes = [value + 0.1 for value in opens]
        payload = {
            "chart": {
                "result": [{
                    "timestamp": timestamps,
                    "indicators": {"quote": [{
                        "open": opens,
                        "high": [value + 0.2 for value in closes],
                        "low": [value - 0.2 for value in opens],
                        "close": closes,
                        "volume": [10] * count,
                    }]},
                }],
                "error": None,
            }
        }
        response = Mock()
        response.status_code = 200
        response.json.return_value = payload
        now = timestamps[-1] + 300 + 30
        with patch.object(historical_data.requests, "get", return_value=response):
            data = historical_data._fetch_yahoo_gc_history(now)

        self.assertEqual(data["symbol"], "GC=F")
        self.assertGreaterEqual(len(data["m5"]), 3000)
        self.assertGreaterEqual(len(data["m15"]), 220)
        self.assertGreaterEqual(len(data["h1"]), 60)
        self.assertGreaterEqual(len(data["h4"]), 50)
        self.assertIsNone(data["live_quote"])
        self.assertIn("not spot", data["source"])

    def test_falls_back_to_real_xaus_spot_sample_if_proxy_unavailable(self):
        spot_data = {
            "m5": [{"timestamp": i * 300, "open": 1, "high": 2, "low": 1, "close": 2} for i in range(1072)],
            "m15": [], "h1": [], "h4": [],
            "source": "XAUS chart OHLC",
            "live_quote": None,
        }
        with patch.object(
            historical_data, "_fetch_yahoo_gc_history",
            side_effect=MarketDataError("Yahoo unavailable"),
        ), patch.object(historical_data, "fetch_market_data", return_value=spot_data):
            data = historical_data.fetch_historical_market_data(now=1_800_000_000)
        self.assertIn("short spot fallback", data["source"])
        self.assertIn("No out-of-sample claim", data["historical_sample_warning"])

    def test_closed_bar_filter_does_not_include_future_candle(self):
        start = 1_800_000_000
        count = 10
        timestamps = [start + i * 300 for i in range(count)]
        opens = [4000.0] * count
        payload = {
            "chart": {"result": [{
                "timestamp": timestamps,
                "indicators": {"quote": [{
                    "open": opens, "high": [4001.0] * count,
                    "low": [3999.0] * count, "close": [4000.5] * count,
                    "volume": [1] * count,
                }]},
            }], "error": None},
        }
        response = Mock()
        response.status_code = 200
        response.json.return_value = payload
        now = timestamps[-1] + 100
        with patch.object(historical_data.requests, "get", return_value=response):
            with self.assertRaises(MarketDataError):
                historical_data._fetch_yahoo_gc_history(now)


if __name__ == "__main__":
    unittest.main()
