import math
import time
from datetime import datetime, timezone
from urllib.parse import quote

import requests

YAHOO_SYMBOL = "XAUUSD=X"
INTERVAL = "30m"
RANGE = "1mo"
TIMEFRAME_SECONDS = 30 * 60
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; GoldAIAssistant/1.0)",
    "Accept": "application/json",
}


class MarketDataError(Exception):
    pass


def fetch_candles():
    """Fetch Yahoo Finance XAU/USD chart candles. Yahoo's chart endpoint is unofficial."""
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
            candles = []
            for i, stamp in enumerate(timestamps):
                try:
                    o = quote_data["open"][i]
                    h = quote_data["high"][i]
                    l = quote_data["low"][i]
                    c = quote_data["close"][i]
                    if None in (o, h, l, c):
                        continue
                    values = [float(o), float(h), float(l), float(c)]
                    if not all(math.isfinite(v) and v > 0 for v in values):
                        continue
                    op, hi, lo, cl = values
                    if hi < max(op, cl, lo) or lo > min(op, cl, hi):
                        continue
                    candles.append({
                        "timestamp": int(stamp),
                        "open": op, "high": hi, "low": lo, "close": cl,
                        "volume": ((quote_data.get("volume") or [None] * len(timestamps))[i]),
                    })
                except (KeyError, IndexError, TypeError, ValueError):
                    continue
            if len(candles) < 220:
                raise MarketDataError(
                    f"البيانات غير كافية للتحليل: وصل {len(candles)} شمعة، نحتاج 220 على الأقل"
                )
            return candles
        except (requests.RequestException, ValueError, KeyError, MarketDataError) as exc:
            last_error = exc
    raise MarketDataError(f"تعذر جلب بيانات XAU/USD من Yahoo Finance: {last_error}")


def _ema(values, period):
    alpha = 2.0 / (period + 1.0)
    result = []
    current = None
    for value in values:
        current = value if current is None else alpha * value + (1.0 - alpha) * current
        result.append(current)
    return result


def _rsi(values, period=14):
    result = [None] * len(values)
    if len(values) <= period:
        return result
    gains = []
    losses = []
    for i in range(1, period + 1):
        change = values[i] - values[i - 1]
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    def value(gain, loss):
        if loss == 0:
            return 100.0 if gain > 0 else 50.0
        return 100.0 - (100.0 / (1.0 + gain / loss))
    result[period] = value(avg_gain, avg_loss)
    for i in range(period + 1, len(values)):
        change = values[i] - values[i - 1]
        avg_gain = (avg_gain * (period - 1) + max(change, 0.0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-change, 0.0)) / period
        result[i] = value(avg_gain, avg_loss)
    return result


def _atr(candles, period=14):
    true_ranges = []
    for i, candle in enumerate(candles):
        previous_close = candles[i - 1]["close"] if i else candle["close"]
        true_ranges.append(max(
            candle["high"] - candle["low"],
            abs(candle["high"] - previous_close),
            abs(candle["low"] - previous_close),
        ))
    result = [None] * len(candles)
    if len(candles) < period:
        return result
    current = sum(true_ranges[:period]) / period
    result[period - 1] = current
    for i in range(period, len(candles)):
        current = ((current * (period - 1)) + true_ranges[i]) / period
        result[i] = current
    return result


def analyze_latest_closed_candle(candles, now=None):
    """Return market snapshot and a signal only when the latest candle is closed."""
    now = now or time.time()
    closed = [c for c in candles if c["timestamp"] + TIMEFRAME_SECONDS <= now]
    if len(closed) < 200:
        raise MarketDataError("لا توجد شموع مغلقة كافية لحساب EMA 200")
    closes = [c["close"] for c in closed]
    ema20 = _ema(closes, 20)
    ema50 = _ema(closes, 50)
    ema200 = _ema(closes, 200)
    rsi = _rsi(closes, 14)
    atr = _atr(closed, 14)
    i = len(closed) - 1
    candle = closed[i]
    snapshot = {
        "symbol": "XAUUSD",
        "source": "Yahoo Finance chart (unofficial)",
        "timeframe": "30m",
        "price": candle["close"],
        "candle_at": datetime.fromtimestamp(candle["timestamp"], timezone.utc).isoformat(),
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "ema20": ema20[i], "ema50": ema50[i], "ema200": ema200[i],
        "rsi14": rsi[i], "atr14": atr[i],
    }
    if rsi[i] is None or atr[i] is None or atr[i] <= 0:
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

    signal = {
        "symbol": "XAUUSD",
        "side": side,
        "entry": round(entry, 3),
        "stop_loss": round(stop, 3),
        "take_profit_1": round(target_1, 3),
        "take_profit": round(target_2, 3),
        "timeframe": "30m",
        "reason": reason,
    }
    return snapshot, signal
