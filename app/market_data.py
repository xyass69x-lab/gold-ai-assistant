"""XAUS-backed XAU/USD OHLC adapter with strict freshness and completeness checks.

XAUS exposes free chart OHLC data without an API key. Its chart endpoint is a
Yahoo Finance proxy, so these candles are reference data, not XTB execution quotes.
The engine fails closed whenever bars are stale, malformed, or insufficient.
"""
import math
import time
from datetime import datetime, timezone

import requests

API_URL = "https://query1.finance.yahoo.com/v8/finance/chart/XAUUSD=X"
LIVE_SPOT_URL = "https://xaus.com/api/v1/spot"
SYMBOL = "XAUUSD=X"
HEADERS = {"User-Agent": "GoldAIAssistant/2.1", "Accept": "application/json"}
BASE_INTERVAL = "5m"
BASE_SECONDS = 5 * 60
HTF_INTERVAL = "1h"
HTF_SECONDS = 60 * 60
BASE_RANGE = "3mo"
HTF_RANGE = "3mo"
BASE_CACHE_SECONDS = 60
HTF_CACHE_SECONDS = 60 * 60
MAX_BASE_AGE_SECONDS = 15 * 60
MAX_M15_AGE_SECONDS = 30 * 60
MAX_H1_AGE_SECONDS = 2 * 60 * 60
MAX_H4_AGE_SECONDS = 8 * 60 * 60
MIN_BASE_CANDLES = 500
MIN_M15_CANDLES = 220
MIN_H1_CANDLES = 60
MIN_H4_CANDLES = 50
SOURCE_NAME = "Yahoo Finance direct OHLC (XAUUSD=X)"
LIVE_PRICE_SOURCE = "XAUS live spot API"
INTERVAL_SECONDS = {"5m": BASE_SECONDS, "5min": BASE_SECONDS, "1h": HTF_SECONDS, "60min": HTF_SECONDS}
_CACHE = {}


class MarketDataError(RuntimeError):
    pass


def _next_cache_expiry(interval, now):
    """Expire just after the next UTC candle boundary."""
    period = INTERVAL_SECONDS[interval]
    return (int(now) // period + 1) * period + 5


def _parse_timestamp(value):
    try:
        if isinstance(value, (int, float)):
            stamp = float(value)
            if stamp > 10_000_000_000:
                stamp /= 1000.0
            if not math.isfinite(stamp) or stamp <= 0:
                raise ValueError("invalid unix timestamp")
            return int(stamp)
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.timestamp())
    except (TypeError, ValueError, OverflowError) as exc:
        raise MarketDataError("مزود XAUS أعاد توقيت شمعة غير صالح") from exc


def _point_list(payload):
    """Accept the documented points array and common wrapped chart responses."""
    if not isinstance(payload, dict):
        raise MarketDataError("استجابة XAUS ليست JSON object صالحاً")
    state = payload.get("data_state")
    if isinstance(state, dict) and state.get("status") in ("stale", "unavailable"):
        raise MarketDataError(
            f"حالة بيانات الشموع غير صالحة: {state.get('status')}"
        )

    for key in ("points", "candles", "bars", "data"):
        value = payload.get(key)
        if isinstance(value, list):
            return value

    # Be defensive if the upstream proxy ever returns Yahoo's native chart shape.
    chart = payload.get("chart") or payload
    results = chart.get("result") if isinstance(chart, dict) else None
    if isinstance(results, list) and results:
        result = results[0]
        timestamps = result.get("timestamp") or []
        indicators = result.get("indicators") or {}
        quotes = indicators.get("quote") or []
        quote = quotes[0] if quotes else {}
        output = []
        for i, stamp in enumerate(timestamps):
            try:
                output.append({
                    "t": stamp,
                    "o": quote.get("open", [])[i],
                    "h": quote.get("high", [])[i],
                    "l": quote.get("low", [])[i],
                    "c": quote.get("close", [])[i],
                    "v": (quote.get("volume") or [None] * len(timestamps))[i],
                })
            except (IndexError, TypeError):
                continue
        return output

    raise MarketDataError("استجابة XAUS لا تحتوي على مصفوفة شموع OHLC معروفة")


def _validate_bar(item):
    if not isinstance(item, dict):
        return None
    try:
        timestamp = _parse_timestamp(
            item.get("timestamp", item.get("t", item.get("time", item.get("datetime"))))
        )
        values = [
            float(item.get("open", item.get("o"))),
            float(item.get("high", item.get("h"))),
            float(item.get("low", item.get("l"))),
            float(item.get("close", item.get("c"))),
        ]
        if not all(math.isfinite(v) and v > 0 for v in values):
            return None
        op, high, low, close = values
        if high < max(op, close, low) or low > min(op, close, high):
            return None
        volume = item.get("volume", item.get("v"))
        return {
            "timestamp": timestamp,
            "open": op,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
        }
    except (KeyError, TypeError, ValueError, OverflowError, MarketDataError):
        return None


def _fetch_chart(interval, period_range, cache_seconds, force=False):
    now = time.time()
    cache_key = (interval, period_range)
    cached = _CACHE.get(cache_key)
    if not force and cached and now < cached["expires_at"]:
        return list(cached["candles"])

    try:
        response = requests.get(
            API_URL,
            params={"range": period_range, "interval": interval},
            headers=HEADERS,
            timeout=20,
        )
        if response.status_code == 429:
            raise MarketDataError("وصل XAUS إلى حد الطلبات المؤقت؛ أوقفنا الإشارات لحين التعافي.")
        if response.status_code == 503:
            raise MarketDataError("مصدر XAUS لا يملك بيانات سوق متاحة حالياً (503).")
        response.raise_for_status()
        payload = response.json()
    except MarketDataError:
        raise
    except (requests.RequestException, ValueError) as exc:
        raise MarketDataError(f"تعذر جلب شموع Yahoo Finance ({interval}): {str(exc)[:180]}") from exc

    if not isinstance(payload, dict):
        raise MarketDataError("استجابة XAUS ليست JSON صالحاً")
    if payload.get("error"):
        raise MarketDataError(f"Yahoo Finance: {str(payload['error'])[:180]}")

    bars = [_validate_bar(point) for point in _point_list(payload)]
    bars = [bar for bar in bars if bar is not None]
    bars.sort(key=lambda bar: bar["timestamp"])
    unique = {bar["timestamp"]: bar for bar in bars}
    bars = list(unique.values())
    if not bars:
        raise MarketDataError(f"لم تصل شموع OHLC صالحة من Yahoo Finance ({interval})")

    fetched_at = time.time()
    # Refresh the 5m series once per minute and the 1h series at most every 5 minutes.
    _CACHE[cache_key] = {
        "fetched_at": fetched_at,
        "expires_at": _next_cache_expiry(interval, fetched_at),
        "candles": list(bars),
    }
    return bars



def fetch_live_price(now=None, max_age_seconds=90):
    """Fetch an independently sourced, timestamp-validated XAUS spot quote."""
    now = time.time() if now is None else float(now)
    try:
        response = requests.get(
            LIVE_SPOT_URL,
            params={"compact": "1"},
            headers=HEADERS,
            timeout=10,
        )
        if response.status_code == 503:
            raise MarketDataError("مصدر السعر اللحظي XAUS غير متاح (503)")
        if response.status_code == 429:
            raise MarketDataError("تجاوز مصدر السعر اللحظي حد الطلبات؛ أوقفنا الإشارات مؤقتاً")
        response.raise_for_status()
        payload = response.json()
    except MarketDataError:
        raise
    except (requests.RequestException, ValueError) as exc:
        raise MarketDataError(f"تعذر جلب السعر اللحظي من XAUS: {str(exc)[:160]}") from exc

    if not isinstance(payload, dict):
        raise MarketDataError("استجابة السعر اللحظي ليست JSON صالحاً")
    state = payload.get("data_state") or {}
    if state.get("status") != "fresh":
        raise MarketDataError(f"السعر اللحظي غير حديث: {state.get('status', 'unknown')}")
    try:
        price = float(payload.get("spot_usd_oz", (payload.get("xau") or {}).get("price")))
        age = float(state.get("age_seconds"))
        as_of = payload.get("price_as_of") or state.get("as_of")
        source_time = _parse_timestamp(as_of)
    except (TypeError, ValueError, MarketDataError) as exc:
        raise MarketDataError("بيانات السعر اللحظي أو توقيتها غير مكتملة") from exc
    measured_age = max(age, now - source_time)
    if not math.isfinite(price) or price <= 0:
        raise MarketDataError("السعر اللحظي غير صالح")
    if measured_age < -5 or measured_age > max_age_seconds:
        raise MarketDataError(f"السعر اللحظي قديم منذ {int(max(0, measured_age))} ثانية")
    return {
        "price": price,
        "age_seconds": int(max(0, measured_age)),
        "as_of": datetime.fromtimestamp(source_time, timezone.utc).isoformat(),
        "source": LIVE_PRICE_SOURCE,
    }

def _closed_only(candles, seconds, now):
    return [
        bar for bar in candles
        if int(bar["timestamp"]) + seconds <= now
    ]


def resample_candles(candles, source_seconds, target_seconds):
    """Aggregate only complete, UTC-aligned buckets with every source bar present."""
    if target_seconds <= source_seconds or target_seconds % source_seconds:
        raise ValueError("target_seconds must be an integer multiple of source_seconds")
    expected_count = target_seconds // source_seconds
    buckets = {}
    for bar in candles:
        start = (int(bar["timestamp"]) // target_seconds) * target_seconds
        buckets.setdefault(start, []).append(bar)

    output = []
    for start in sorted(buckets):
        bars = sorted(buckets[start], key=lambda item: item["timestamp"])
        expected = [start + i * source_seconds for i in range(expected_count)]
        if [int(bar["timestamp"]) for bar in bars] != expected:
            continue
        output.append({
            "timestamp": start,
            "open": bars[0]["open"],
            "high": max(bar["high"] for bar in bars),
            "low": min(bar["low"] for bar in bars),
            "close": bars[-1]["close"],
            "volume": None,
        })
    return output


def fetch_market_data(force_refresh=False, now=None, include_live_quote=True):
    """Return independent Yahoo OHLC candles and an optional fresh XAUS spot quote."""
    now = time.time() if now is None else float(now)
    m5_raw = _fetch_chart(BASE_INTERVAL, BASE_RANGE, BASE_CACHE_SECONDS, force_refresh)
    h1_raw = _fetch_chart(HTF_INTERVAL, HTF_RANGE, HTF_CACHE_SECONDS, force_refresh)

    m5_closed = _closed_only(m5_raw, BASE_SECONDS, now)
    h1_closed = _closed_only(h1_raw, HTF_SECONDS, now)
    if not m5_closed:
        raise MarketDataError("لا توجد شموع M5 مغلقة من XAUS")
    if not h1_closed:
        raise MarketDataError("لا توجد شموع H1 مغلقة من XAUS")

    m5_age = now - (m5_closed[-1]["timestamp"] + BASE_SECONDS)
    h1_age = now - (h1_closed[-1]["timestamp"] + HTF_SECONDS)
    if m5_age < 0 or m5_age > MAX_BASE_AGE_SECONDS:
        raise MarketDataError(
            f"بيانات M5 من XAUS قديمة منذ {int(max(0, m5_age) / 60)} دقيقة؛ أوقفنا الإشارات."
        )
    if h1_age < 0 or h1_age > MAX_H1_AGE_SECONDS:
        raise MarketDataError("آخر شمعة H1 من XAUS قديمة؛ أوقفنا الإشارات.")
    if len(m5_closed) < MIN_BASE_CANDLES:
        raise MarketDataError(
            f"بيانات M5 غير كافية: {len(m5_closed)} شمعة، نحتاج {MIN_BASE_CANDLES}."
        )
    if len(h1_closed) < MIN_H1_CANDLES:
        raise MarketDataError(
            f"بيانات H1 غير كافية: {len(h1_closed)} شمعة، نحتاج {MIN_H1_CANDLES}."
        )

    m15_raw = resample_candles(m5_raw, BASE_SECONDS, 15 * 60)
    h4_raw = resample_candles(h1_raw, HTF_SECONDS, 4 * HTF_SECONDS)
    m15_closed = _closed_only(m15_raw, 15 * 60, now)
    h4_closed = _closed_only(h4_raw, 4 * HTF_SECONDS, now)
    if not m15_closed:
        raise MarketDataError("تعذر تكوين شموع M15 كاملة من بيانات XAUS")
    if not h4_closed:
        raise MarketDataError("تعذر تكوين شموع H4 كاملة من بيانات XAUS")

    m15_age = now - (m15_closed[-1]["timestamp"] + 15 * 60)
    h4_age = now - (h4_closed[-1]["timestamp"] + 4 * HTF_SECONDS)
    if m15_age < 0 or m15_age > MAX_M15_AGE_SECONDS:
        raise MarketDataError("آخر شمعة M15 مشتقة قديمة؛ أوقفنا الإشارات.")
    if h4_age < 0 or h4_age > MAX_H4_AGE_SECONDS:
        raise MarketDataError("آخر شمعة H4 مشتقة قديمة؛ أوقفنا الإشارات.")
    if len(m15_closed) < MIN_M15_CANDLES:
        raise MarketDataError(
            f"بيانات M15 غير كافية: {len(m15_closed)} شمعة، نحتاج {MIN_M15_CANDLES}."
        )
    if len(h4_closed) < MIN_H4_CANDLES:
        raise MarketDataError(
            f"بيانات H4 غير كافية: {len(h4_closed)} شمعة، نحتاج {MIN_H4_CANDLES}."
        )

    return {
        "symbol": "XAUUSD",
        "source": SOURCE_NAME,
        "live_price_source": LIVE_PRICE_SOURCE,
        "live_quote": fetch_live_price(now=now) if include_live_quote else None,
        "m5": m5_raw,
        "m15": m15_raw,
        "h1": h1_raw,
        "h4": h4_raw,
        "checked_at": datetime.fromtimestamp(now, timezone.utc).isoformat(),
        "m5_age_seconds": int(max(0, m5_age)),
        "m15_age_seconds": int(max(0, m15_age)),
        "h1_age_seconds": int(max(0, h1_age)),
        "h4_age_seconds": int(max(0, h4_age)),
    }
