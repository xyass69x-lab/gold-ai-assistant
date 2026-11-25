"""Extended no-key historical gold futures proxy for backtests only.

Yahoo GC=F is COMEX gold futures, not XAUUSD spot. It is used only to enlarge
historical pattern testing. Live signals use BiQuote M5 OHLC and XAUS higher
timeframes. If the proxy is unavailable, the backtest transparently falls back
to the shorter XAUS spot sample and disables any out-of-sample claim.
"""
import time
import requests

from app.market_data import (
    MarketDataError,
    _closed_only,
    _point_list,
    _validate_bar,
    fetch_market_data,
    resample_candles,
)

YAHOO_URL = "https://query1.finance.yahoo.com/v8/finance/chart/GC=F"
YAHOO_RANGE = "3mo"
YAHOO_INTERVAL = "5m"
MIN_PROXY_CANDLES = 1000


def _fetch_yahoo_gc_history(now):
    try:
        response = requests.get(
            YAHOO_URL,
            params={"range": YAHOO_RANGE, "interval": YAHOO_INTERVAL, "events": "history"},
            headers={
                "User-Agent": "Mozilla/5.0 GoldAIAssistant/2.1",
                "Accept": "application/json",
            },
            timeout=20,
        )
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise MarketDataError(f"Yahoo GC=F historical request failed: {str(exc)[:160]}") from exc

    bars = [_validate_bar(row) for row in _point_list(payload)]
    bars = [bar for bar in bars if bar is not None]
    bars.sort(key=lambda bar: bar["timestamp"])
    bars = _closed_only(bars, 300, now)
    if len(bars) < MIN_PROXY_CANDLES:
        raise MarketDataError(f"Yahoo GC=F history is short ({len(bars)} closed M5 bars)")
    m15 = resample_candles(bars, 300, 900)
    h1 = resample_candles(bars, 300, 3600)
    h4 = resample_candles(h1, 3600, 14400)
    if len(m15) < 220 or len(h1) < 60 or len(h4) < 50:
        raise MarketDataError(
            f"Yahoo GC=F higher-timeframe history is insufficient: "
            f"M15={len(m15)}, H1={len(h1)}, H4={len(h4)}"
        )
    return {
        "symbol": "GC=F",
        "source": "Yahoo Finance COMEX Gold Futures GC=F — historical proxy, not spot",
        "live_price_source": "Historical backtest — no live quote used",
        "live_quote": None,
        "m5": bars,
        "m15": m15,
        "h1": h1,
        "h4": h4,
        "checked_at": None,
        "m5_age_seconds": int(max(0, now - (bars[-1]["timestamp"] + 300))),
    }


def fetch_historical_market_data(now=None):
    """Prefer a longer no-key futures history; transparently fall back to XAUS spot."""
    now = time.time() if now is None else float(now)
    try:
        return _fetch_yahoo_gc_history(now)
    except MarketDataError as proxy_error:
        data = fetch_market_data(now=now, include_live_quote=False, use_biquote_m5=False)
        data = dict(data)
        data["live_quote"] = None
        data["source"] = (
            f"{data['source']} — short spot fallback ({len(data['m5'])} M5 bars); "
            f"GC=F unavailable: {str(proxy_error)[:100]}"
        )
        data["historical_sample_warning"] = (
            "No out-of-sample claim below 3000 M5 bars; futures proxy not available"
        )
        return data
