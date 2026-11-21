import math
import time
from datetime import datetime, timezone
from urllib.parse import quote

import requests

YAHOO_SYMBOL = "XAUUSD=X"
INTERVAL = "30m"
# Yahoo intraday intervals are generally limited to a rolling 60-day window.
RANGE = "60d"
TIMEFRAME_SECONDS = 30 * 60
MIN_CLOSED_CANDLES = 220
MAX_CANDLE_AGE_SECONDS = 72 * 60 * 60
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; GoldAIAssistant/1.0)",
    "Accept": "application/json",
}


class MarketDataError(Exception):
    pass


def fetch_candles():
    """Fetch XAU/USD 30m candles from Yahoo's unofficial chart endpoint."""
    symbol = quote(YAHOO_SYMBOL, safe="")
    params = {"range": RANGE, "interval": INTERVAL, "events": "history"}
    last_error = None

    for host in ("query1.finance.yahoo.com", "query2.finance.yahoo.com"):
        url = f"https://{host}/v8/finance/chart/{symbol}"
        try:
            response = requests.get(url, params=params, headers=HEADERS, timeout=15)
            response.raise_for_status()
            payload = response.json()
            chart = payload.get("chart") or {}
            if chart.get("error"):
                raise MarketDataError(str(chart["error"]))
            result = (chart.get("result") or [None])[0]
            if not result:
                raise MarketDataError("مزود البيانات لم يُرجع شموعاً للذهب")

            timestamps = result.get("timestamp") or []
            quote_data = (result.get("indicators", {}).get("quote") or [{}])[0]
            opens = quote_data.get("open") or []
            highs = quote_data.get("high") or []
            lows = quote_data.get("low") or []
            closes = quote_data.get("close") or []
            volumes = quote_data.get("volume") or []
            candles = []

            for i, stamp in enumerate(timestamps):
                try:
                    values = [opens[i], highs[i], lows[i], closes[i]]
                    if any(value is None for value in values):
                        continue
                    op, hi, lo, cl = [float(value) for value in values]
                    if not all(math.isfinite(v) and v > 0 for v in (op, hi, lo, cl)):
                        continue
                    if hi < max(op, cl, lo) or lo > min(op, cl, hi):
                        continue
                    candles.append({
                        "timestamp": int(stamp),
                        "open": op,
                        "high": hi,
                        "low": lo,
                        "close": cl,
                        "volume": volumes[i] if i < len(volumes) else None,
                    })
                except (IndexError, TypeError, ValueError, OverflowError):
                    continue

            candles.sort(key=lambda candle: candle["timestamp"])
            # Remove duplicate timestamps from provider responses.
            unique = {}
            for candle in candles:
                unique[candle["timestamp"]] = candle
            candles = list(unique.values())

            if len(candles) < MIN_CLOSED_CANDLES:
                raise MarketDataError(
                    f"البيانات غير كافية: وصل {len(candles)} شمعة، نحتاج {MIN_CLOSED_CANDLES} على الأقل"
                )
            return candles
        except (requests.RequestException, ValueError, KeyError, TypeError, MarketDataError) as exc:
            last_error = exc

    raise MarketDataError(f"تعذر جلب بيانات XAU/USD من Yahoo Finance: {last_error}")


def _ema(values, period):
    """EMA seeded with the first period SMA to reduce initialization bias."""
    result = [None] * len(values)
    if len(values) < period:
        return result
    alpha = 2.0 / (period + 1.0)
    current = sum(values[:period]) / period
    result[period - 1] = current
    for i in range(period, len(values)):
        current = alpha * values[i] + (1.0 - alpha) * current
        result[i] = current
    return result


def _rsi(values, period=14):
    result = [None] * len(values)
    if len(values) <= period:
        return result

    changes = [values[i] - values[i - 1] for i in range(1, len(values))]
    avg_gain = sum(max(change, 0.0) for change in changes[:period]) / period
    avg_loss = sum(max(-change, 0.0) for change in changes[:period]) / period

    def rsi_value(gain, loss):
        if loss == 0:
            return 100.0 if gain > 0 else 50.0
        return 100.0 - (100.0 / (1.0 + gain / loss))

    result[period] = rsi_value(avg_gain, avg_loss)
    for i in range(period + 1, len(values)):
        change = changes[i - 1]
        avg_gain = (avg_gain * (period - 1) + max(change, 0.0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-change, 0.0)) / period
        result[i] = rsi_value(avg_gain, avg_loss)
    return result


def _atr(candles, period=14):
    result = [None] * len(candles)
    if len(candles) < period:
        return result

    true_ranges = []
    for i, candle in enumerate(candles):
        previous_close = candles[i - 1]["close"] if i else candle["close"]
        true_ranges.append(max(
            candle["high"] - candle["low"],
            abs(candle["high"] - previous_close),
            abs(candle["low"] - previous_close),
        ))

    current = sum(true_ranges[:period]) / period
    result[period - 1] = current
    for i in range(period, len(candles)):
        current = ((current * (period - 1)) + true_ranges[i]) / period
        result[i] = current
    return result


def analyze_latest_closed_candle(candles, now=None):
    """Analyze only completed candles; never form a signal from an open candle."""
    now = time.time() if now is None else now
    ordered = sorted(candles, key=lambda candle: candle["timestamp"])
    closed = [
        candle for candle in ordered
        if candle["timestamp"] + TIMEFRAME_SECONDS <= now
    ]
    if len(closed) < MIN_CLOSED_CANDLES:
        raise MarketDataError(
            f"الشموع المغلقة غير كافية: {len(closed)} من أصل {MIN_CLOSED_CANDLES}"
        )

    candle = closed[-1]
    age = now - (candle["timestamp"] + TIMEFRAME_SECONDS)
    if age > MAX_CANDLE_AGE_SECONDS:
        raise MarketDataError(
            f"آخر شمعة مغلقة قديمة منذ {int(age / 3600)} ساعة؛ تم إيقاف الإشارات لحماية الحساب"
        )

    closes = [item["close"] for item in closed]
    ema20 = _ema(closes, 20)
    ema50 = _ema(closes, 50)
    ema200 = _ema(closes, 200)
    rsi = _rsi(closes, 14)
    atr = _atr(closed, 14)
    i = len(closed) - 1

    snapshot = {
        "symbol": "XAUUSD",
        "source": "Yahoo Finance chart (unofficial)",
        "timeframe": "30m",
        "price": candle["close"],
        "candle_at": datetime.fromtimestamp(candle["timestamp"], timezone.utc).isoformat(),
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "ema20": ema20[i],
        "ema50": ema50[i],
        "ema200": ema200[i],
        "rsi14": rsi[i],
        "atr14": atr[i],
        "candle_age_seconds": max(0, int(age)),
    }

    if any(value is None for value in (ema20[i], ema50[i], ema200[i], rsi[i], atr[i])) or atr[i] <= 0:
        return snapshot, None

    trend_up = candle["close"] > ema200[i] and ema50[i] > ema200[i]
    trend_down = candle["close"] < ema200[i] and ema50[i] < ema200[i]
    buy = (
        trend_up
        and candle["low"] <= ema20[i]
        and candle["close"] > ema20[i]
        and rsi[i] > 52
        and candle["close"] > candle["open"]
    )
    sell = (
        trend_down
        and candle["high"] >= ema20[i]
        and candle["close"] < ema20[i]
        and rsi[i] < 48
        and candle["close"] < candle["open"]
    )

    if buy:
        entry = candle["close"]
        side = "BUY"
        stop = entry - 1.5 * atr[i]
        target_1 = entry + 1.5 * atr[i]
        target_2 = entry + 3.0 * atr[i]
        reason = "اتجاه صاعد + إعادة اختبار EMA20 + زخم RSI"
    elif sell:
        entry = candle["close"]
        side = "SELL"
        stop = entry + 1.5 * atr[i]
        target_1 = entry - 1.5 * atr[i]
        target_2 = entry - 3.0 * atr[i]
        reason = "اتجاه هابط + إعادة اختبار EMA20 + زخم RSI"
    else:
        return snapshot, None

    return snapshot, {
        "symbol": "XAUUSD",
        "side": side,
        "entry": round(entry, 3),
        "stop_loss": round(stop, 3),
        "take_profit_1": round(target_1, 3),
        "take_profit": round(target_2, 3),
        "timeframe": "30m",
        "reason": reason,
    }
