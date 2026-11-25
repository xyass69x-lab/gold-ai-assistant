"""Independent historical XAUUSD OHLC loader for walk-forward backtests.

AlgoSpecial returns broker-server timestamps without a timezone suffix. The
loader infers the current server-to-UTC offset from the newest completed M5
bar, accepts only a unique recent offset, and fails closed if it cannot.
"""
import math
from datetime import datetime, timedelta, timezone

import requests

from app.market_data import MarketDataError

API_URL = "https://www.algospecial.com/api/ohlc.php"
SOURCE_NAME = "AlgoSpecial XAUUSD historical OHLC"
HEADERS = {"User-Agent": "GoldAIAssistant/2.1", "Accept": "application/json"}
TIMEOUT_SECONDS = 30
MAX_M5_AGE_SECONDS = 20 * 60


def _parse_server_timestamp(value):
    if isinstance(value, (int, float)):
        stamp = float(value)
        if not math.isfinite(stamp) or stamp <= 0:
            raise ValueError("invalid timestamp")
        return int(stamp)
    raw = str(value).strip()
    for fmt in ("%Y.%m.%d %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            # Deliberately naive: provider documents broker-server time.
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
        timestamp = _parse_server_timestamp(
            row.get("Timestamp", row.get("timestamp", row.get("time")))
        )
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
            "timestamp": timestamp,
            "open": op,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
        }
    except (TypeError, ValueError, OverflowError):
        return None


def _fetch_timeframe(timeframe, start_date, end_date):
    try:
        response = requests.get(
            API_URL,
            params={
                "symbol": "XAUUSD",
                "tf": timeframe,
                "from": start_date,
                "to": end_date,
                "format": "json",
            },
            headers=HEADERS,
            timeout=TIMEOUT_SECONDS,
        )
        if response.status_code == 429:
            raise MarketDataError("AlgoSpecial تجاوز حد الطلبات؛ أعد المحاولة لاحقاً")
        if response.status_code == 413:
            raise MarketDataError(f"عينة AlgoSpecial {timeframe} أكبر من الحد المجاني للطلب")
        response.raise_for_status()
        payload = response.json()
    except MarketDataError:
        raise
    except (requests.RequestException, ValueError) as exc:
        raise MarketDataError(
            f"تعذر جلب بيانات AlgoSpecial {timeframe}: {str(exc)[:160]}"
        ) from exc

    if not isinstance(payload, dict) or payload.get("error"):
        detail = payload.get("error") if isinstance(payload, dict) else "invalid JSON"
        raise MarketDataError(f"AlgoSpecial {timeframe}: {str(detail)[:160]}")
    rows = payload.get("data")
    if not isinstance(rows, list):
        raise MarketDataError(f"استجابة AlgoSpecial {timeframe} لا تحتوي على data array")
    bars = [_parse_bar(row) for row in rows]
    bars = [bar for bar in bars if bar is not None]
    bars.sort(key=lambda bar: bar["timestamp"])
    unique = {bar["timestamp"]: bar for bar in bars}
    result = list(unique.values())
    if not result:
        raise MarketDataError(f"لا توجد شموع {timeframe} صالحة من AlgoSpecial")
    return result


def _infer_server_offset(m5_server_bars, now):
    """Infer a unique half-hour-aligned broker UTC offset from recent M5 closes."""
    candidates = []
    for offset_steps in range(-24, 29):
        offset_seconds = offset_steps * 1800
        closed = [
            int(bar["timestamp"]) - offset_seconds
            for bar in m5_server_bars
            if int(bar["timestamp"]) - offset_seconds + 300 <= now
        ]
        if not closed:
            continue
        last_close = max(closed) + 300
        age = now - last_close
        if 0 <= age <= MAX_M5_AGE_SECONDS:
            candidates.append((age, offset_seconds))

    candidates.sort()
    if not candidates:
        raise MarketDataError(
            "تعذر تحديد توقيت خادم شموع AlgoSpecial من آخر شمعة M5؛ أوقفنا الاختبار بدلاً من تخمين التوقيت"
        )
    if len(candidates) > 1 and candidates[1][0] - candidates[0][0] < 20 * 60:
        raise MarketDataError("توقيت خادم AlgoSpecial ملتبس؛ لا يمكن توحيد الشموع بأمان")
    return candidates[0][1], int(candidates[0][0])


def _to_utc(bars, offset_seconds):
    output = []
    for bar in bars:
        item = dict(bar)
        item["timestamp"] = int(item["timestamp"]) - offset_seconds
        output.append(item)
    return sorted(output, key=lambda bar: bar["timestamp"])


def fetch_historical_market_data(now=None):
    """Fetch enough independent XAUUSD history for chronological MTF backtests."""
    now = datetime.now(timezone.utc) if now is None else datetime.fromtimestamp(
        float(now), timezone.utc
    )
    end_date = now.date()
    # Eight inclusive calendar days normally provide >1,200 M5 bars while
    # staying below the public preview row ceiling.
    m5_start = (end_date - timedelta(days=7)).isoformat()
    # Fifteen days provide enough higher-timeframe context for EMA50 and H4 bias.
    htf_start = (end_date - timedelta(days=14)).isoformat()
    end = end_date.isoformat()

    m5_server = _fetch_timeframe("M5", m5_start, end)
    offset_seconds, m5_age = _infer_server_offset(m5_server, now.timestamp())
    m5 = _to_utc(m5_server, offset_seconds)
    m15 = _to_utc(_fetch_timeframe("M15", htf_start, end), offset_seconds)
    h1 = _to_utc(_fetch_timeframe("H1", htf_start, end), offset_seconds)
    h4 = _to_utc(_fetch_timeframe("H4", htf_start, end), offset_seconds)

    m5 = [bar for bar in m5 if bar["timestamp"] + 300 <= now.timestamp()]
    m15 = [bar for bar in m15 if bar["timestamp"] + 900 <= now.timestamp()]
    h1 = [bar for bar in h1 if bar["timestamp"] + 3600 <= now.timestamp()]
    h4 = [bar for bar in h4 if bar["timestamp"] + 14400 <= now.timestamp()]

    if len(m5) < 1200:
        raise MarketDataError(
            f"عينة AlgoSpecial M5 قصيرة ({len(m5)} شمعة بعد استبعاد الشموع غير المغلقة)؛ نحتاج 1200 على الأقل"
        )
    if len(m15) < 220 or len(h1) < 60 or len(h4) < 50:
        raise MarketDataError(
            f"بيانات الأطر العليا غير كافية من AlgoSpecial: M15={len(m15)}, H1={len(h1)}, H4={len(h4)}"
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
        "checked_at": now.isoformat(),
        "m5_age_seconds": m5_age,
        "m15_age_seconds": int(max(0, now.timestamp() - (m15[-1]["timestamp"] + 900))),
        "h1_age_seconds": int(max(0, now.timestamp() - (h1[-1]["timestamp"] + 3600))),
        "h4_age_seconds": int(max(0, now.timestamp() - (h4[-1]["timestamp"] + 14400))),
        "server_utc_offset_seconds": offset_seconds,
    }
