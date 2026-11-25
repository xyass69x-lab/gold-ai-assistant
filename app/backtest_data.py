"""Isolated historical XAUUSD dataset for reproducible backtests.

AlgoSpecial timestamps are broker-server wall time. We encode the wall-clock
sequence as UTC solely to preserve interval ordering/alignment; this is NOT a
claim that the source timestamps are UTC. This dataset is research-only and
must never feed live signals or execution.
"""
from datetime import datetime, timezone

import requests

from app.market_data import resample_candles

API_URL = "https://www.algospecial.com/api/ohlc.php"
SOURCE = "AlgoSpecial XAUUSD historical OHLC (broker-server timestamps; research only)"
TIMEOUT = 30


def _parse_bar(item):
    try:
        stamp = datetime.strptime(item["Timestamp"], "%Y.%m.%d %H:%M:%S")
        # Preserve broker-wall-clock interval math without asserting its timezone.
        timestamp = int(stamp.replace(tzinfo=timezone.utc).timestamp())
        op, high, low, close = (
            float(item[k]) for k in ("Open", "High", "Low", "Close")
        )
        if min(op, high, low, close) <= 0 or high < max(op, low, close) or low > min(op, high, close):
            return None
        return {
            "timestamp": timestamp, "open": op, "high": high,
            "low": low, "close": close, "volume": item.get("TickVolume"),
        }
    except (KeyError, TypeError, ValueError, OverflowError):
        return None


def fetch_historical_bars(timeframe, start_date, end_date):
    response = requests.get(
        API_URL,
        params={
            "symbol": "XAUUSD", "tf": timeframe,
            "from": start_date, "to": end_date, "format": "json",
        },
        headers={"User-Agent": "GoldAIAssistant-Research/1.0"},
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    payload = response.json()
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise ValueError(f"AlgoSpecial returned no data array for {timeframe}")
    bars = [_parse_bar(row) for row in rows]
    bars = [bar for bar in bars if bar is not None]
    bars.sort(key=lambda bar: bar["timestamp"])
    unique = {bar["timestamp"]: bar for bar in bars}
    return list(unique.values())


def fetch_backtest_dataset():
    # The public preview is limited; these windows provide >1200 M5 bars
    # and enough H1 history for the strategy's H4 bias calculation.
    m5 = fetch_historical_bars("M5", "2026-10-23", "2026-10-30")
    h1 = fetch_historical_bars("H1", "2026-10-20", "2026-10-30")
    m15 = resample_candles(m5, 300, 900)
    h4 = resample_candles(h1, 3600, 14400)
    if len(m5) < 1200:
        raise ValueError(f"Historical M5 sample too short: {len(m5)}; need at least 1200")
    if len(m15) < 220 or len(h1) < 60 or len(h4) < 50:
        raise ValueError(
            f"Insufficient aligned history: M15={len(m15)}, H1={len(h1)}, H4={len(h4)}"
        )
    return {
        "symbol": "XAUUSD", "source": SOURCE,
        "m5": m5, "m15": m15, "h1": h1, "h4": h4,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "m5_age_seconds": 0, "m15_age_seconds": 0,
        "h1_age_seconds": 0, "h4_age_seconds": 0,
    }
