"""Dukascopy XAUUSD minute history for isolated walk-forward backtests.

The archive timestamps are UTC. Each daily BID M1 file is LZMA-alone
compressed, containing 24-byte records. We aggregate only complete M5/M15/H1/H4
buckets and never mix the historical archive with the live quote feed.
"""
import lzma
import math
import struct
from datetime import datetime, timedelta, timezone

import requests

from app.market_data import MarketDataError, resample_candles

BASE_URL = "https://datafeed.dukascopy.com/datafeed"
PRICE_SCALE = 1000.0
RECORD = struct.Struct(">IIIIIf")
TIMEOUT_SECONDS = 25
HISTORY_DAYS = 16


def _decode_m1_file(payload, day):
    if not payload:
        return []
    try:
        raw = lzma.decompress(payload, format=lzma.FORMAT_ALONE)
    except lzma.LZMAError as exc:
        raise MarketDataError(f"ملف Dukascopy M1 غير قابل لفك الضغط: {exc}") from exc
    if len(raw) % RECORD.size:
        raise MarketDataError("حجم ملف Dukascopy لا يطابق سجلات M1 ذات 24 بايت")

    day_start = int(datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp())
    bars = []
    for seconds, op, close, low, high, volume in RECORD.iter_unpack(raw):
        if seconds >= 86400:
            continue
        values = (op / PRICE_SCALE, high / PRICE_SCALE, low / PRICE_SCALE, close / PRICE_SCALE)
        if not all(math.isfinite(value) and value > 0 for value in values):
            continue
        open_price, high_price, low_price, close_price = values[0], values[1], values[2], values[3]
        if high_price < max(open_price, close_price, low_price):
            continue
        if low_price > min(open_price, close_price, high_price):
            continue
        bars.append({
            "timestamp": day_start + int(seconds),
            "open": open_price,
            "high": high_price,
            "low": low_price,
            "close": close_price,
            "volume": float(volume),
        })
    return bars


def _fetch_day(day, side="BID"):
    url = (
        f"{BASE_URL}/XAUUSD/{day.year}/{day.month - 1:02d}/{day.day:02d}/"
        f"{side}_candles_min_1.bi5"
    )
    try:
        response = requests.get(url, timeout=TIMEOUT_SECONDS, headers={
            "User-Agent": "GoldAIAssistant/2.1",
            "Accept": "application/octet-stream",
        })
        if response.status_code == 404:
            return []
        if response.status_code == 429:
            raise MarketDataError("Dukascopy rate-limited the historical request")
        response.raise_for_status()
        return _decode_m1_file(response.content, day)
    except MarketDataError:
        raise
    except requests.RequestException as exc:
        raise MarketDataError(f"تعذر تنزيل شموع Dukascopy ليوم {day}: {str(exc)[:160]}") from exc


def fetch_historical_market_data(now=None):
    """Fetch 16 UTC days of XAUUSD M1, then build closed M5/M15/H1/H4 bars."""
    now_ts = datetime.now(timezone.utc).timestamp() if now is None else float(now)
    now_dt = datetime.fromtimestamp(now_ts, timezone.utc)
    first_day = now_dt.date() - timedelta(days=HISTORY_DAYS - 1)
    m1 = []
    for day_offset in range(HISTORY_DAYS):
        day = first_day + timedelta(days=day_offset)
        m1.extend(_fetch_day(day, "BID"))

    if not m1:
        raise MarketDataError("لم تصل بيانات M1 تاريخية من Dukascopy")
    m1.sort(key=lambda bar: bar["timestamp"])
    m1 = [bar for bar in m1 if bar["timestamp"] + 60 <= now_ts]
    unique = {bar["timestamp"]: bar for bar in m1}
    m1 = list(unique.values())

    m5 = resample_candles(m1, 60, 300)
    m15 = resample_candles(m5, 300, 900)
    h1 = resample_candles(m5, 300, 3600)
    h4 = resample_candles(h1, 3600, 14400)

    if len(m5) < 1200:
        raise MarketDataError(
            f"عينة Dukascopy M5 قصيرة ({len(m5)} شمعة مكتملة)؛ نحتاج 1200 على الأقل"
        )
    if len(m15) < 220 or len(h1) < 60 or len(h4) < 50:
        raise MarketDataError(
            f"بيانات الأطر العليا غير كافية من Dukascopy: M15={len(m15)}, H1={len(h1)}, H4={len(h4)}"
        )

    return {
        "symbol": "XAUUSD",
        "source": "Dukascopy XAUUSD BID M1 -> M5/M15/H1/H4 (UTC)",
        "live_price_source": "Historical backtest — no live quote used",
        "live_quote": None,
        "m5": m5,
        "m15": m15,
        "h1": h1,
        "h4": h4,
        "checked_at": now_dt.isoformat(),
        "m5_age_seconds": int(max(0, now_ts - (m5[-1]["timestamp"] + 300))),
        "m15_age_seconds": int(max(0, now_ts - (m15[-1]["timestamp"] + 900))),
        "h1_age_seconds": int(max(0, now_ts - (h1[-1]["timestamp"] + 3600))),
        "h4_age_seconds": int(max(0, now_ts - (h4[-1]["timestamp"] + 14400))),
    }
