"""Free-tier Twelve Data adapter for validated multi-timeframe XAU/USD candles.

The live application is switched to this module only after its provider access is
verified. It deliberately fails closed: stale, incomplete, malformed, or
insufficient data never becomes a trade signal.
"""
import math
import os
import time
from datetime import datetime, timezone

import requests

API_URL = "https://api.twelvedata.com/time_series"
SYMBOL = "XAU/USD"
HEADERS = {"User-Agent": "GoldAIAssistant/2.0", "Accept": "application/json"}
BASE_INTERVAL = "5min"
BASE_SECONDS = 300
SETUP_INTERVAL = "15min"
SETUP_SECONDS = 15 * 60
HTF_INTERVAL = "4h"
HTF_SECONDS = 4 * 60 * 60
BASE_CACHE_SECONDS = 5 * 60
SETUP_CACHE_SECONDS = 15 * 60
HTF_CACHE_SECONDS = 4 * 60 * 60
INTERVAL_SECONDS = {BASE_INTERVAL: BASE_SECONDS, SETUP_INTERVAL: SETUP_SECONDS, HTF_INTERVAL: HTF_SECONDS}
MAX_BASE_AGE_SECONDS = 15 * 60
MAX_HTF_AGE_SECONDS = 8 * 60 * 60
MIN_BASE_CANDLES = 500
MIN_H4_CANDLES = 60
_CACHE = {}
_LAST_REQUEST_AT = 0.0
_RATE_LIMIT_UNTIL = 0.0


class MarketDataError(RuntimeError):
    pass


def _next_cache_expiry(interval, now):
    """Expire just after the next UTC candle boundary to refresh once per bar."""
    period = INTERVAL_SECONDS[interval]
    return (int(now) // period + 1) * period + 5


def _parse_time(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.timestamp())
    except (TypeError, ValueError, OverflowError) as exc:
        raise MarketDataError("مزود البيانات أعاد توقيت شمعة غير صالح") from exc


def _validate_bar(item):
    try:
        values = [float(item[key]) for key in ("open", "high", "low", "close")]
        if not all(math.isfinite(v) and v > 0 for v in values):
            return None
        op, high, low, close = values
        if high < max(op, close, low) or low > min(op, close, high):
            return None
        return {
            "timestamp": _parse_time(item["datetime"]),
            "open": op, "high": high, "low": low, "close": close,
            "volume": item.get("volume"),
        }
    except (KeyError, TypeError, ValueError, OverflowError):
        return None


def _fetch_interval(interval, outputsize, cache_seconds, force=False):
    global _LAST_REQUEST_AT, _RATE_LIMIT_UNTIL
    now = time.time()
    cached = _CACHE.get(interval)
    if not force and cached and now < cached.get("expires_at", cached["fetched_at"] + cache_seconds):
        return list(cached["candles"])

    if now < _RATE_LIMIT_UNTIL:
        remaining = max(1, int(_RATE_LIMIT_UNTIL - now))
        raise MarketDataError(f"حصة Twelve Data موقوفة مؤقتاً؛ أعد المحاولة بعد {remaining} ثانية.")

    api_key = os.getenv("TWELVE_DATA_API_KEY", "").strip()
    if not api_key:
        raise MarketDataError(
            "مفتاح TWELVE_DATA_API_KEY غير مضبوط في Render Environment."
        )

    # Keep requests well below the free Basic plan's per-minute ceiling.
    wait = 8.0 - (now - _LAST_REQUEST_AT)
    if wait > 0:
        time.sleep(wait)

    params = {
        "symbol": SYMBOL, "interval": interval, "outputsize": outputsize,
        "timezone": "UTC", "apikey": api_key,
    }
    try:
        response = requests.get(API_URL, params=params, headers=HEADERS, timeout=25)
        _LAST_REQUEST_AT = time.time()
        if response.status_code == 429:
            retry = response.headers.get("Retry-After", "")
            try:
                cooldown = max(60, min(3600, int(retry)))
            except (TypeError, ValueError):
                cooldown = 900
            _RATE_LIMIT_UNTIL = time.time() + cooldown
            raise MarketDataError(
                "وصلنا حد طلبات Twelve Data المجاني (429). "
                f"تم إيقاف إعادة المحاولة لمدة {cooldown // 60} دقيقة لحماية الحصة."
            )
        response.raise_for_status()
        payload = response.json()
    except MarketDataError:
        raise
    except (requests.RequestException, ValueError) as exc:
        raise MarketDataError(f"تعذر جلب بيانات {interval}: {str(exc)[:180]}") from exc

    if not isinstance(payload, dict):
        raise MarketDataError("استجابة مزود البيانات ليست JSON صالحاً")
    if payload.get("status") == "error" or payload.get("code"):
        message = payload.get("message") or payload.get("message_detail") or str(payload)
        raise MarketDataError(f"Twelve Data ({interval}): {str(message)[:220]}")

    meta = payload.get("meta") or {}
    symbol = str(meta.get("symbol") or SYMBOL).upper().replace(" ", "")
    if symbol not in ("XAU/USD", "XAUUSD"):
        raise MarketDataError(f"رمز غير متوقع من مزود البيانات: {symbol}")

    bars = [_validate_bar(item) for item in (payload.get("values") or [])]
    bars = [bar for bar in bars if bar is not None]
    bars.sort(key=lambda bar: bar["timestamp"])
    unique = {bar["timestamp"]: bar for bar in bars}
    bars = list(unique.values())
    if not bars:
        raise MarketDataError(f"لم تصل شموع صالحة من Twelve Data ({interval})")

    fetched_at = time.time()
    _CACHE[interval] = {
        "fetched_at": fetched_at,
        "expires_at": _next_cache_expiry(interval, fetched_at),
        "candles": list(bars),
    }
    return bars


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


def fetch_market_data(force_refresh=False, now=None):
    """Return raw M5/M15/H4 bars plus closed-bar validation and derived H1."""
    now = time.time() if now is None else float(now)
    m5_raw = _fetch_interval(BASE_INTERVAL, 5000, BASE_CACHE_SECONDS, force_refresh)
    m15_raw = _fetch_interval(SETUP_INTERVAL, 5000, SETUP_CACHE_SECONDS, force_refresh)
    h4_raw = _fetch_interval(HTF_INTERVAL, 500, HTF_CACHE_SECONDS, force_refresh)
    m5_closed = _closed_only(m5_raw, BASE_SECONDS, now)
    m15_closed = _closed_only(m15_raw, SETUP_SECONDS, now)
    h4_closed = _closed_only(h4_raw, HTF_SECONDS, now)

    if not m5_closed:
        raise MarketDataError("لا توجد شموع M5 مغلقة")
    m5_age = now - (m5_closed[-1]["timestamp"] + BASE_SECONDS)
    if m5_age < 0 or m5_age > MAX_BASE_AGE_SECONDS:
        raise MarketDataError(
            f"شموع M5 قديمة منذ {int(max(0, m5_age) / 60)} دقيقة؛ أوقفنا الإشارات."
        )
    if len(m5_closed) < MIN_BASE_CANDLES:
        raise MarketDataError(
            f"بيانات M5 غير كافية: {len(m5_closed)} شمعة، نحتاج {MIN_BASE_CANDLES}."
        )
    m15_age = now - (m15_closed[-1]["timestamp"] + SETUP_SECONDS) if m15_closed else float("inf")
    if not m15_closed or m15_age < 0 or m15_age > 30 * 60:
        raise MarketDataError("بيانات M15 غير متاحة أو قديمة؛ أوقفنا الإشارات.")
    if len(m15_closed) < 220:
        raise MarketDataError(f"بيانات M15 غير كافية: {len(m15_closed)} شمعة، نحتاج 220.")
    if not h4_closed:
        raise MarketDataError("لا توجد شموع H4 مغلقة")
    h4_age = now - (h4_closed[-1]["timestamp"] + HTF_SECONDS)
    if h4_age < 0 or h4_age > MAX_HTF_AGE_SECONDS:
        raise MarketDataError("آخر شمعة H4 قديمة؛ أوقفنا الإشارات.")
    if len(h4_closed) < MIN_H4_CANDLES:
        raise MarketDataError(
            f"بيانات H4 غير كافية: {len(h4_closed)} شمعة، نحتاج {MIN_H4_CANDLES}."
        )

    h1 = resample_candles(m15_raw, SETUP_SECONDS, 60 * 60)
    if len(m15_closed) < 220 or len(h1) < 60:
        raise MarketDataError(
            f"التغطية متعددة الأطر غير كافية (M15={len(m15_closed)}, H1={len(h1)})."
        )

    return {
        "symbol": "XAUUSD",
        "source": "Twelve Data XAU/USD spot",
        "m5": m5_raw, "m15": m15_raw, "h1": h1, "h4": h4_raw,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "m5_age_seconds": int(max(0, m5_age)),
        "m15_age_seconds": int(max(0, m15_age)),
        "h4_age_seconds": int(max(0, h4_age)),
    }
