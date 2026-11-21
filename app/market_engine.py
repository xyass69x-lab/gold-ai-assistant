import math
import os
import time
from datetime import datetime, timezone

import requests

# Twelve Data's XAU/USD instrument is Gold Spot / US Dollar (not COMEX futures).
API_URL = "https://api.twelvedata.com/time_series"
SYMBOL = "XAU/USD"
INTERVAL = "30min"
TIMEFRAME_SECONDS = 30 * 60
MIN_CLOSED_CANDLES = 220
MAX_CANDLE_AGE_SECONDS = 90 * 60
HEADERS = {"User-Agent": "GoldAIAssistant/1.0", "Accept": "application/json"}


class MarketDataError(Exception):
    pass


def _parse_datetime(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.timestamp())
    except (TypeError, ValueError, OverflowError) as exc:
        raise MarketDataError(f"وقت شمعة غير صالح من مزود البيانات: {value}") from exc


def fetch_candles():
    """Fetch real spot-gold XAU/USD 30-minute OHLC candles from Twelve Data."""
    api_key = os.getenv("TWELVE_DATA_API_KEY", "").strip()
    if not api_key:
        raise MarketDataError(
            "مفتاح Twelve Data غير مضبوط. أضف TWELVE_DATA_API_KEY في Render Environment."
        )

    params = {
        "symbol": SYMBOL,
        "interval": INTERVAL,
        "outputsize": 5000,
        "timezone": "UTC",
        "apikey": api_key,
    }
    try:
        response = requests.get(API_URL, params=params, headers=HEADERS, timeout=20)
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise MarketDataError(f"تعذر الاتصال بمزود بيانات XAU/USD: {str(exc)[:180]}") from exc

    if not isinstance(payload, dict):
        raise MarketDataError("استجابة مزود البيانات غير صالحة")
    if payload.get("status") == "error" or payload.get("code"):
        message = payload.get("message") or payload.get("message_detail") or str(payload)
        raise MarketDataError(f"Twelve Data: {message[:220]}")

    meta = payload.get("meta") or {}
    returned_symbol = str(meta.get("symbol") or SYMBOL).upper().replace(" ", "")
    if returned_symbol not in ("XAU/USD", "XAUUSD"):
        raise MarketDataError(f"رمز غير متوقع من مزود البيانات: {returned_symbol}")

    values = payload.get("values") or []
    candles = []
    for item in values:
        try:
            op, hi, lo, cl = [float(item[key]) for key in ("open", "high", "low", "close")]
            if not all(math.isfinite(v) and v > 0 for v in (op, hi, lo, cl)):
                continue
            if hi < max(op, cl, lo) or lo > min(op, cl, hi):
                continue
            candles.append({
                "timestamp": _parse_datetime(item["datetime"]),
                "open": op,
                "high": hi,
                "low": lo,
                "close": cl,
                "volume": item.get("volume"),
            })
        except (KeyError, TypeError, ValueError, OverflowError):
            continue

    candles.sort(key=lambda candle: candle["timestamp"])
    unique = {candle["timestamp"]: candle for candle in candles}
    candles = list(unique.values())
    if len(candles) < MIN_CLOSED_CANDLES:
        raise MarketDataError(
            f"بيانات XAU/USD غير كافية: وصل {len(candles)} شمعة، نحتاج {MIN_CLOSED_CANDLES} على الأقل. "
            "تحقق من صلاحية خطة Twelve Data ورمز XAU/USD."
        )
    return candles


def _ema(values, period):
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
    """Analyze only completed spot XAU/USD candles; never use the live candle."""
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
        "source": "Twelve Data XAU/USD spot (Gold Spot / US Dollar)",
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
        trend_up and candle["low"] <= ema20[i] and candle["close"] > ema20[i]
        and rsi[i] > 52 and candle["close"] > candle["open"]
    )
    sell = (
        trend_down and candle["high"] >= ema20[i] and candle["close"] < ema20[i]
        and rsi[i] < 48 and candle["close"] < candle["open"]
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
