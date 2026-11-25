"""Free AlgoSpecial XAUUSD historical OHLC loader for isolated backtests.

The provider publishes broker-server timestamps without a timezone. The loader
infers one unique UTC offset from the newest completed M5 bar while the market
is open, then applies that same offset to every timeframe. It fails closed if
the offset cannot be established. Historical candles never use live quotes.
"""
import math
import time
from datetime import date, datetime, timedelta, timezone

import requests

from app.market_data import MarketDataError

API_URL = "https://www.algospecial.com/api/ohlc.php"
SOURCE_NAME = "AlgoSpecial XAUUSD historical OHLC"
HEADERS = {"User-Agent": "GoldAIAssistant/2.1", "Accept": "application/json"}
TIMEOUT_SECONDS = 30
HISTORY_DAYS = 31
MAX_M5_AGE_SECONDS = 15 * 60
MAX_ROWS_PER_REQUEST = 1900


def _parse_server_time(value):
    if isinstance(value, (int, float)):
        stamp = float(value)
        if not math.isfinite(stamp) or stamp <= 0:
            raise ValueError("invalid timestamp")
        return int(stamp)
    raw = str(value).strip()
    for fmt in ("%Y.%m.%d %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            # Provider explicitly documents naive timestamps as broker-server time.
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
            "timestamp": stamp,
            "open": op,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
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
                "symbol": "XAUUSD",
                "tf": timeframe,
                "from": start.isoformat(),
                "to": end.isoformat(),
                "format": "json",
            },
            headers=HEADERS,
            timeout=TIMEOUT_SECONDS,
        )
        if response.status_code == 413:
            raise MarketDataError(
                f"عينة AlgoSpecial {timeframe} أكبر من الحد؛ قلّصنا الفترات لكن المزود رفضها"
            )
        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After", "غير محدد")
            raise MarketDataError(
                f"AlgoSpecial حدّ عدد الطلبات؛ Retry-After={retry_after}"
            )
        response.raise_for_status()
        payload = response.json()
    except MarketDataError:
        raise
    except (requests.RequestException, ValueError) as exc:
        raise MarketDataError(
            f"تعذر جلب بيانات AlgoSpecial {timeframe} ({start} إلى {end}): {str(exc)[:160]}"
        ) from exc

    if not isinstance(payload, dict) or payload.get("error"):
        detail = payload.get("error") if isinstance(payload, dict) else "استجابة JSON غير صالحة"
        raise MarketDataError(f"AlgoSpecial {timeframe}: {str(detail)[:160]}")
    rows = payload.get("data")
    if not isinstance(rows, list):
        raise MarketDataError(f"استجابة AlgoSpecial {timeframe} لا تحتوي على data array")
    bars = [_parse_bar(row) for row in rows]
    return [bar for bar in bars if bar is not None]


def _fetch_timeframe(timeframe, start, end, max_days):
    bars = []
    for chunk_start, chunk_end in _date_chunks(start, end, max_days):
        part = _fetch_chunk(timeframe, chunk_start, chunk_end)
        if len(part) > MAX_ROWS_PER_REQUEST:
            raise MarketDataError(
                f"AlgoSpecial أعاد {len(part)} صفاً في {timeframe}، فوق حد الأمان {MAX_ROWS_PER_REQUEST}"
            )
        bars.extend(part)
    unique = {bar["timestamp"]: bar for bar in bars}
    return [unique[key] for key in sorted(unique)]


def _infer_server_utc_offset(m5_server_bars, now):
    """Infer a unique half-hour-aligned server offset from a recent completed M5 bar."""
    candidates = []
    for steps in range(-24, 29):
        offset = steps * 1800
        close_times = [
            int(bar["timestamp"]) - offset + 300
            for bar in m5_server_bars
        ]
        latest_close = max(close_times, default=0)
        age = now - latest_close
        if 0 <= age <= MAX_M5_AGE_SECONDS:
            candidates.append((age, offset))

    candidates.sort()
    if not candidates:
        raise MarketDataError(
            "تعذر تحديد توقيت خادم AlgoSpecial من آخر شمعة M5؛ لا نخمن UTC offset"
        )
    if len(candidates) != 1:
        raise MarketDataError("توقيت خادم AlgoSpecial ملتبس؛ أوقفنا الاختبار لحماية الترتيب الزمني")
    return candidates[0][1], int(candidates[0][0])


def _to_utc(bars, offset):
    return [
        {**bar, "timestamp": int(bar["timestamp"]) - offset}
        for bar in bars
    ]


def _closed(bars, seconds, now):
    return [
        bar for bar in bars
        if int(bar["timestamp"]) + seconds <= now
    ]


def fetch_historical_market_data(now=None):
    """Fetch 31 days of M5/M15/H1/H4 history without touching live quote APIs."""
    now_ts = time.time() if now is None else float(now)
    now_dt = datetime.fromtimestamp(now_ts, timezone.utc)
    end = now_dt.date()
    start = end - timedelta(days=HISTORY_DAYS - 1)

    # M5: six calendar days per request (<= 1,728 expected bars).
    m5_server = _fetch_timeframe("M5", start, end, max_days=6)
    offset, _ = _infer_server_utc_offset(m5_server, now_ts)

    # M15: 19 days per request (< 1,900 expected bars); H1/H4 fit one window.
    m15_server = _fetch_timeframe("M15", start, end, max_days=19)
    h1_server = _fetch_timeframe("H1", start, end, max_days=31)
    h4_server = _fetch_timeframe("H4", start, end, max_days=31)

    m5 = _closed(_to_utc(m5_server, offset), 300, now_ts)
    m15 = _closed(_to_utc(m15_server, offset), 900, now_ts)
    h1 = _closed(_to_utc(h1_server, offset), 3600, now_ts)
    h4 = _closed(_to_utc(h4_server, offset), 14400, now_ts)

    if len(m5) < 3000:
        raise MarketDataError(
            f"عينة AlgoSpecial M5 قصيرة ({len(m5)} شمعة مغلقة)؛ نحتاج 3000 لاختبار تطوير/OOS"
        )
    if len(m15) < 220 or len(h1) < 60 or len(h4) < 50:
        raise MarketDataError(
            f"بيانات الأطر العليا غير كافية من AlgoSpecial: "
            f"M15={len(m15)}, H1={len(h1)}, H4={len(h4)}"
        )

    return {
        "symbol": "XAUUSD",
        "source": SOURCE_NAME,
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
        "server_utc_offset_seconds": offset,
    }
