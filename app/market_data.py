"""Combine fresh BiQuote M5/MT5 entry data with XAUS higher-timeframe context.

XAUS supplies M15/H1/H4 context and historical M5 for backtests. BiQuote supplies
closed live M5 candles plus a fresh bid/ask quote. Neither feed is XTB execution.
"""
import math
import time
from datetime import datetime, timezone

import requests

API_URL = "https://xaus.com/api/v1/chart"
LIVE_SPOT_URL = "https://biquote.io/api/XAUUSD"
SYMBOL = "xau"
HEADERS = {"User-Agent": "GoldAIAssistant/2.1", "Accept": "application/json"}
BASE_INTERVAL = "5m"
BASE_SECONDS = 5 * 60
M15_INTERVAL = "15m"
M15_SECONDS = 15 * 60
HTF_INTERVAL = "1h"
HTF_SECONDS = 60 * 60
BASE_RANGE = "3mo"
HTF_RANGE = "3mo"
BASE_CACHE_SECONDS = 5 * 60
HTF_CACHE_SECONDS = 60 * 60
MAX_BASE_AGE_SECONDS = 15 * 60
MAX_LIVE_M5_AGE_SECONDS = 300
MIN_LIVE_M5_CANDLES = 100
MAX_M15_AGE_SECONDS = 30 * 60
MAX_H1_AGE_SECONDS = 2 * 60 * 60
MAX_H4_AGE_SECONDS = 8 * 60 * 60
MIN_BASE_CANDLES = 500
MIN_M15_CANDLES = 220
MIN_H1_CANDLES = 60
MIN_H4_CANDLES = 50
SOURCE_NAME = "XAUS chart OHLC (Yahoo Finance proxy)"
LIVE_PRICE_SOURCE = "BiQuote XAUUSD MT5 quote"
INTERVAL_SECONDS = {"5m": BASE_SECONDS, "5min": BASE_SECONDS, "15m": M15_SECONDS, "1h": HTF_SECONDS, "60min": HTF_SECONDS, "1m": 60, "1d": 86400}
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
            params={"symbol": SYMBOL, "range": period_range, "interval": interval},
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
        raise MarketDataError(f"تعذر جلب شموع XAUS ({interval}): {str(exc)[:180]}") from exc

    if not isinstance(payload, dict):
        raise MarketDataError("استجابة XAUS ليست JSON صالحاً")
    if payload.get("error"):
        raise MarketDataError(f"XAUS: {str(payload['error'])[:180]}")

    bars = [_validate_bar(point) for point in _point_list(payload)]
    bars = [bar for bar in bars if bar is not None]
    bars.sort(key=lambda bar: bar["timestamp"])
    unique = {bar["timestamp"]: bar for bar in bars}
    bars = list(unique.values())
    if not bars:
        raise MarketDataError(f"لم تصل شموع OHLC صالحة من XAUS ({interval})")

    fetched_at = time.time()
    # Refresh the 5m series once per minute and the 1h series at most every 5 minutes.
    _CACHE[cache_key] = {
        "fetched_at": fetched_at,
        "expires_at": _next_cache_expiry(interval, fetched_at),
        "candles": list(bars),
    }
    return bars



def fetch_live_price(now=None, max_age_seconds=45):
    """Fetch a separate live MT5 quote; reject closed, stale, or malformed quotes."""
    now = time.time() if now is None else float(now)
    try:
        response = requests.get(
            LIVE_SPOT_URL,
            params={"allowStale": "false"},
            headers=HEADERS,
            timeout=10,
        )
        if response.status_code == 404:
            raise MarketDataError("BiQuote لا يملك سعراً حياً حديثاً للذهب حالياً")
        if response.status_code == 429:
            raise MarketDataError("تجاوز مصدر السعر BiQuote حد الطلبات؛ أوقفنا الإشارات مؤقتاً")
        if response.status_code == 503:
            raise MarketDataError("مصدر السعر BiQuote غير متاح حالياً (503)")
        response.raise_for_status()
        payload = response.json()
    except MarketDataError:
        raise
    except (requests.RequestException, ValueError) as exc:
        raise MarketDataError(f"تعذر جلب السعر اللحظي من BiQuote: {str(exc)[:160]}") from exc

    if not isinstance(payload, dict):
        raise MarketDataError("استجابة BiQuote ليست JSON صالحاً")
    try:
        if payload.get("stale") or payload.get("marketState") != "open":
            raise MarketDataError("سوق BiQuote مغلق أو أن آخر سعر معلّم كقديم")
        age = float(payload.get("quoteAgeSeconds"))
        timestamp = payload.get("lastQuoteAt") or payload.get("timestamp")
        source_time = _parse_timestamp(timestamp)
        bid = float(payload.get("bid"))
        ask = float(payload.get("ask"))
        mid = float(payload.get("mid"))
    except MarketDataError:
        raise
    except (TypeError, ValueError) as exc:
        raise MarketDataError("بيانات Bid/Ask أو توقيتها من BiQuote غير مكتملة") from exc

    measured_age = max(age, now - source_time)
    if not all(math.isfinite(value) and value > 0 for value in (bid, ask, mid)):
        raise MarketDataError("أسعار Bid/Ask/Mid من BiQuote غير صالحة")
    if ask < bid or not bid <= mid <= ask:
        raise MarketDataError("ترتيب أسعار Bid/Ask/Mid من BiQuote غير منطقي")
    if measured_age < -5 or measured_age > max_age_seconds:
        raise MarketDataError(f"سعر BiQuote قديم منذ {int(max(0, measured_age))} ثانية")
    return {
        "price": mid,
        "bid": bid,
        "ask": ask,
        "spread": ask - bid,
        "age_seconds": int(max(0, measured_age)),
        "as_of": datetime.fromtimestamp(source_time, timezone.utc).isoformat(),
        "source": payload.get("source") or LIVE_PRICE_SOURCE,
        "provider": LIVE_PRICE_SOURCE,
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



def _fetch_biquote_m5(now, min_bars=MIN_LIVE_M5_CANDLES):
    """Fetch closed, real-time-aggregated M5 bars from the independent MT5 OHLC feed."""
    try:
        response = requests.get(
            f"{LIVE_SPOT_URL}/ohlc",
            params={"interval": "5m", "limit": 288},
            headers=HEADERS,
            timeout=10,
        )
        if response.status_code == 429:
            raise MarketDataError("BiQuote OHLC تجاوز حد الطلبات")
        if response.status_code == 503:
            raise MarketDataError("BiQuote OHLC غير متاح حالياً")
        response.raise_for_status()
        payload = response.json()
    except MarketDataError:
        raise
    except (requests.RequestException, ValueError) as exc:
        raise MarketDataError(f"تعذر جلب شموع M5 الحديثة من BiQuote: {str(exc)[:160]}") from exc

    rows = payload.get("bars") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise MarketDataError("استجابة BiQuote OHLC لا تحتوي على bars array")
    bars = []
    for row in rows:
        if not isinstance(row, dict) or row.get("isOpen") is True:
            continue
        bar = _validate_bar({
            "timestamp": row.get("openTime"),
            "open": row.get("open"),
            "high": row.get("high"),
            "low": row.get("low"),
            "close": row.get("close"),
            "volume": row.get("tickVolume", row.get("volume")),
        })
        if bar is not None:
            bars.append(bar)
    bars.sort(key=lambda bar: bar["timestamp"])
    unique = {bar["timestamp"]: bar for bar in bars}
    bars = list(unique.values())
    bars = _closed_only(bars, BASE_SECONDS, now)
    if len(bars) < min_bars:
        raise MarketDataError(
            f"شموع BiQuote M5 غير كافية: {len(bars)} شمعة مغلقة؛ نحتاج {min_bars}"
        )
    age = now - (bars[-1]["timestamp"] + BASE_SECONDS)
    if age < 0 or age > MAX_LIVE_M5_AGE_SECONDS:
        raise MarketDataError(
            f"شموع BiQuote M5 متأخرة منذ {int(max(0, age))} ثانية؛ أوقفنا الإشارات"
        )
    return bars, int(max(0, age))


def fetch_market_data(force_refresh=False, now=None, include_live_quote=True, use_biquote_m5=None):
    """Use fresh BiQuote M5 candles for live entries and XAUS for higher timeframes."""
    now = time.time() if now is None else float(now)
    if use_biquote_m5 is None:
        use_biquote_m5 = bool(include_live_quote)

    h1_raw = _fetch_chart(HTF_INTERVAL, HTF_RANGE, HTF_CACHE_SECONDS, force_refresh)
    h1_closed = _closed_only(h1_raw, HTF_SECONDS, now)
    if not h1_closed:
        raise MarketDataError("لا توجد شموع H1 مغلقة من XAUS")

    if use_biquote_m5:
        m5_raw, m5_age = _fetch_biquote_m5(now)
        # Rebuild recent higher timeframes from the fresh closed M5 feed.
        # Retain XAUS historical depth, but never use its stale tail as live data.
        live_h1 = resample_candles(m5_raw, BASE_SECONDS, HTF_SECONDS)
        live_m15 = resample_candles(m5_raw, BASE_SECONDS, M15_SECONDS)
        h1_raw = sorted({b["timestamp"]: b for b in h1_raw + live_h1}.values(), key=lambda b: b["timestamp"])
        m15_raw = _fetch_chart(M15_INTERVAL, HTF_RANGE, 15 * 60, force_refresh)
        m15_raw = sorted({b["timestamp"]: b for b in m15_raw + live_m15}.values(), key=lambda b: b["timestamp"])
        h1_closed = _closed_only(h1_raw, HTF_SECONDS, now)
        source = "BiQuote M5 OHLC + XAUS M15/H1/H4 OHLC"
        min_m5 = MIN_LIVE_M5_CANDLES
    else:
        m5_raw = _fetch_chart(BASE_INTERVAL, BASE_RANGE, BASE_CACHE_SECONDS, force_refresh)
        m5_closed = _closed_only(m5_raw, BASE_SECONDS, now)
        if not m5_closed:
            raise MarketDataError("لا توجد شموع M5 مغلقة من XAUS")
        m5_age = now - (m5_closed[-1]["timestamp"] + BASE_SECONDS)
        if m5_age < 0 or m5_age > MAX_BASE_AGE_SECONDS:
            raise MarketDataError(
                f"بيانات M5 من XAUS قديمة منذ {int(max(0, m5_age) / 60)} دقيقة؛ أوقفنا الإشارات."
            )
        m15_raw = resample_candles(m5_raw, BASE_SECONDS, M15_SECONDS)
        source = SOURCE_NAME
        min_m5 = MIN_BASE_CANDLES

    m5_closed = _closed_only(m5_raw, BASE_SECONDS, now)
    if len(m5_closed) < min_m5:
        raise MarketDataError(
            f"بيانات M5 غير كافية: {len(m5_closed)} شمعة، نحتاج {min_m5}."
        )

    h1_age = now - (h1_closed[-1]["timestamp"] + HTF_SECONDS)
    if h1_age < 0 or h1_age > MAX_H1_AGE_SECONDS:
        # A cached chart may remain stale after the upstream feed updates.
        # Retry once without cache; never relax the freshness guard.
        h1_raw = _fetch_chart(HTF_INTERVAL, HTF_RANGE, HTF_CACHE_SECONDS, force=True)
        h1_closed = _closed_only(h1_raw, HTF_SECONDS, now)
        if not h1_closed:
            raise MarketDataError("لا توجد شموع H1 مغلقة من XAUS")
        h1_age = now - (h1_closed[-1]["timestamp"] + HTF_SECONDS)
        if h1_age < 0 or h1_age > MAX_H1_AGE_SECONDS:
            raise MarketDataError(
                f"آخر شمعة H1 من XAUS قديمة ({int(max(0, h1_age) / 60)} دقيقة)؛ أوقفنا الإشارات."
            )
    if len(h1_closed) < MIN_H1_CANDLES:
        raise MarketDataError(
            f"بيانات H1 غير كافية: {len(h1_closed)} شمعة، نحتاج {MIN_H1_CANDLES}."
        )

    # Build H4 directly from the fresh, closed M5 feed. This avoids
    # incompatible H1 timestamp grids and stale historical H4 tails.
    h4_raw = resample_candles(h1_raw, HTF_SECONDS, 4 * HTF_SECONDS)
    if use_biquote_m5:
        # Use the most recent 48 contiguous closed M5 bars even when the
        # upstream feed is offset from UTC H4 boundaries.
        live_h4 = resample_candles(m5_closed, BASE_SECONDS, 4 * HTF_SECONDS)
        if not live_h4 and len(m5_closed) >= 48:
            tail = m5_closed[-48:]
            if all(int(tail[i]["timestamp"]) - int(tail[i-1]["timestamp"]) == BASE_SECONDS for i in range(1, len(tail))):
                live_h4 = [{
                    "timestamp": tail[0]["timestamp"],
                    "open": tail[0]["open"],
                    "high": max(b["high"] for b in tail),
                    "low": min(b["low"] for b in tail),
                    "close": tail[-1]["close"],
                    "volume": None,
                }]
        h4_raw = sorted(
            {b["timestamp"]: b for b in h4_raw + live_h4}.values(),
            key=lambda b: b["timestamp"],
        )
    m15_closed = _closed_only(m15_raw, M15_SECONDS, now)
    h4_closed = _closed_only(h4_raw, 4 * HTF_SECONDS, now)
    if not m15_closed:
        raise MarketDataError("لا توجد شموع M15 مغلقة من مصدر الشموع")
    if not h4_closed:
        raise MarketDataError("تعذر تكوين شموع H4 كاملة من بيانات XAUS")

    m15_age = now - (m15_closed[-1]["timestamp"] + M15_SECONDS)
    h4_age = now - (h4_closed[-1]["timestamp"] + 4 * HTF_SECONDS)
    if m15_age < 0 or m15_age > MAX_M15_AGE_SECONDS:
        raise MarketDataError("آخر شمعة M15 قديمة؛ أوقفنا الإشارات.")
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

    # Additional context; reject stale or incomplete data downstream.
    m1 = _closed_only(_fetch_chart("1m", "5d", 60, force_refresh), 60, now)
    d1 = _closed_only(_fetch_chart("1d", "2y", 86400, force_refresh), 86400, now)
    if not m1 or not d1:
        raise MarketDataError("لا توجد شموع M1/D1 مغلقة")
    if now - (m1[-1]["timestamp"] + 60) > 180:
        m1 = []  # XAUS M1 unavailable/stale: never use stale candles
    if now - (d1[-1]["timestamp"] + 86400) > 345600:
        raise MarketDataError("آخر شمعة D1 قديمة")
    return {
        "m1": m1, "d1": d1,
        "m1_age_seconds": int(max(0, now - (m1[-1]["timestamp"] + 60))) if m1 else None,
        "symbol": "XAUUSD",
        "source": source,
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

