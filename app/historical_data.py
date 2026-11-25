"""Free AlgoSpecial XAUUSD history for chronological strategy research.

AlgoSpecial publishes broker-server timestamps, not UTC. All timeframes in a
single dataset use that same server clock, so the backtest preserves that clock
consistently and never invents a UTC offset. The free preview can lag the
calendar; the loader detects its newest available M5 date and fetches a full
31-day window ending there.
"""
import math
import time
from datetime import datetime, timedelta, timezone

import requests

from app.market_data import MarketDataError, fetch_market_data

API_URL = "https://www.algospecial.com/api/ohlc.php"
SOURCE_NAME = "AlgoSpecial XAUUSD historical OHLC (broker-server time)"
HEADERS = {"User-Agent": "GoldAIAssistant/2.1", "Accept": "application/json"}
TIMEOUT_SECONDS = 30
HISTORY_DAYS = 31
MAX_ROWS_PER_REQUEST = 1900
MIN_M5_CANDLES = 3000


def _parse_server_time(value):
    if isinstance(value, (int, float)):
        stamp = float(value)
        if not math.isfinite(stamp) or stamp <= 0:
            raise ValueError("invalid timestamp")
        return int(stamp)
    raw = str(value).strip()
    for fmt in ("%Y.%m.%d %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            # This is a numeric encoding of broker-server wall time, not UTC.
            return int(datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc).timestamp())
        except ValueError:
            pass
    parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp())


def _parse_bar(row):
    if not isinstance(row, dict):
        return None
    try:
        stamp = _parse_server_time(row.get("Timestamp", row.get("timestamp", row.get("time"))))
        op = float(row.get("Open", row.get("open")))
        high = float(row.get("High", row.get("high")))
        low = float(row.get("Low", row.get("low")))
        close = float(row.get("Close", row.get("close")))
        if not all(math.isfinite(v) and v > 0 for v in (op, high, low, close)):
            return None
        if high < max(op, close, low) or low > min(op, close, high):
            return None
        volume = row.get("TickVolume", row.get("tickVolume", row.get("volume")))
        return {
            "timestamp": stamp, "open": op, "high": high,
            "low": low, "close": close, "volume": volume,
        }
    except (TypeError, ValueError, OverflowError):
        return None


def _date_chunks(start, end, max_days):
    cursor = start
    while cursor <= end:
        chunk_end = min(end, cursor + timedelta(days=max_days - 1))
        yield cursor, chunk_end
        cursor = chunk_end + timedelta(days=1)


def _fetch_chunk(timeframe, start, end):
    try:
        response = requests.get(
            API_URL,
            params={
                "symbol": "XAUUSD", "tf": timeframe,
                "from": start.isoformat(), "to": end.isoformat(),
                "format": "json",
            },
            headers=HEADERS,
            timeout=TIMEOUT_SECONDS,
        )
        if response.status_code == 413:
            raise MarketDataError(f"AlgoSpecial {timeframe}: طلب التاريخ أكبر من حد الصفوف")
        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After", "غير محدد")
            raise MarketDataError(f"AlgoSpecial حدّ الطلبات؛ Retry-After={retry_after}")
        response.raise_for_status()
        payload = response.json()
    except MarketDataError:
        raise
    except (requests.RequestException, ValueError) as exc:
        raise MarketDataError(
            f"تعذر جلب AlgoSpecial {timeframe} ({start} إلى {end}): {str(exc)[:160]}"
        ) from exc

    if not isinstance(payload, dict) or payload.get("error"):
        detail = payload.get("error") if isinstance(payload, dict) else "JSON غير صالح"
        raise MarketDataError(f"AlgoSpecial {timeframe}: {str(detail)[:160]}")
    rows = payload.get("data")
    if not isinstance(rows, list):
        raise MarketDataError(f"استجابة AlgoSpecial {timeframe} لا تحتوي على data array")
    if len(rows) > MAX_ROWS_PER_REQUEST:
        raise MarketDataError(
            f"AlgoSpecial أعاد {len(rows)} صفاً في {timeframe}، فوق حد الأمان {MAX_ROWS_PER_REQUEST}"
        )
    return [bar for row in rows if (bar := _parse_bar(row)) is not None]


def _fetch_timeframe(timeframe, start, end, max_days):
    bars = []
    for chunk_start, chunk_end in _date_chunks(start, end, max_days):
        bars.extend(_fetch_chunk(timeframe, chunk_start, chunk_end))
    unique = {bar["timestamp"]: bar for bar in bars}
    return [unique[key] for key in sorted(unique)]


def _closed(bars, seconds, cutoff):
    return [bar for bar in bars if int(bar["timestamp"]) + seconds <= cutoff]


def _short_spot_fallback(now, reason):
    data = fetch_market_data(now=now, include_live_quote=False, use_biquote_m5=False)
    data = dict(data)
    data["live_quote"] = None
    data["source"] = (
        f"{data['source']} — short spot fallback ({len(data['m5'])} M5 bars); "
        f"AlgoSpecial unavailable: {str(reason)[:100]}"
    )
    data["historical_sample_warning"] = (
        "No out-of-sample claim below 3000 M5 bars; AlgoSpecial history unavailable"
    )
    return data


def fetch_historical_market_data(now=None):
    """Fetch a 31-day closed MTF sample ending at the newest date actually published."""
    now_ts = time.time() if now is None else float(now)
    current_date = datetime.fromtimestamp(now_ts, timezone.utc).date()
    initial_start = current_date - timedelta(days=HISTORY_DAYS - 1)

    try:
        # Probe the current calendar window. Public preview data may be delayed.
        initial_m5 = _fetch_timeframe("M5", initial_start, current_date, max_days=6)
        if not initial_m5:
            raise MarketDataError("AlgoSpecial لم يُرجع شموع M5 ضمن نافذة التاريخ الحالية")

        latest_timestamp = max(bar["timestamp"] for bar in initial_m5)
        latest_server_date = datetime.fromtimestamp(latest_timestamp, timezone.utc).date()

        if len(initial_m5) >= MIN_M5_CANDLES:
            window_end = current_date
            m5_raw = initial_m5
        else:
            # Backfill the full preview window ending at the latest date in the
            # provider, instead of assuming the provider is current.
            window_end = latest_server_date
            window_start = window_end - timedelta(days=HISTORY_DAYS - 1)
            m5_raw = _fetch_timeframe("M5", window_start, window_end, max_days=6)

        if len(m5_raw) < MIN_M5_CANDLES:
            raise MarketDataError(
                f"عينة AlgoSpecial M5 قصيرة ({len(m5_raw)} شمعة خام)؛ نحتاج 3000 لاختبار OOS"
            )

        window_start = window_end - timedelta(days=HISTORY_DAYS - 1)
        m15_raw = _fetch_timeframe("M15", window_start, window_end, max_days=19)
        h1_raw = _fetch_timeframe("H1", window_start, window_end, max_days=31)
        h4_raw = _fetch_timeframe("H4", window_start, window_end, max_days=31)

        # Treat the newest M5 bar as potentially open. The previous bar's close
        # is the cutoff for all four timeframes, preserving the provider clock.
        latest_m5_open = max(bar["timestamp"] for bar in m5_raw)
        m5_cutoff = latest_m5_open
        m5 = _closed(m5_raw, 300, m5_cutoff)
        m15 = _closed(m15_raw, 900, m5_cutoff)
        h1 = _closed(h1_raw, 3600, m5_cutoff)
        h4 = _closed(h4_raw, 14400, m5_cutoff)

        if len(m5) < MIN_M5_CANDLES:
            raise MarketDataError(
                f"عينة AlgoSpecial M5 المغلقة قصيرة ({len(m5)})؛ نحتاج 3000"
            )
        if len(m15) < 220 or len(h1) < 60 or len(h4) < 50:
            raise MarketDataError(
                f"أطر AlgoSpecial العليا غير كافية: M15={len(m15)}, H1={len(h1)}, H4={len(h4)}"
            )

        checked_at = datetime.fromtimestamp(m5_cutoff, timezone.utc).isoformat()
        return {
            "symbol": "XAUUSD",
            "source": SOURCE_NAME,
            "live_price_source": "Historical backtest — no live quote used",
            "live_quote": None,
            "m5": m5, "m15": m15, "h1": h1, "h4": h4,
            "checked_at": checked_at,
            "m5_age_seconds": 0, "m15_age_seconds": 0,
            "h1_age_seconds": 0, "h4_age_seconds": 0,
            "server_time_note": "Provider broker-server time; no UTC offset was guessed",
            "history_window_start": window_start.isoformat(),
            "history_window_end": window_end.isoformat(),
        }
    except MarketDataError as exc:
        # If the historical provider is unavailable, preserve a transparent
        # short spot diagnostic; the report must not pretend it is OOS.
        return _short_spot_fallback(now_ts, exc)
