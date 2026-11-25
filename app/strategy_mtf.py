"""Multi-timeframe XAU/USD strategy with closed-candle/no-lookahead rules."""
from bisect import bisect_right

LIVE_CONFIG = {
    "min_score": 80,
    "breakout_lookback": 20,
    "pullback_atr_tolerance": 0.15,
    "min_body_atr": 0.25,
}


def ema(values, period):
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


def rsi(values, period=14):
    result = [None] * len(values)
    if len(values) <= period:
        return result
    changes = [values[i] - values[i - 1] for i in range(1, len(values))]
    gain = sum(max(x, 0.0) for x in changes[:period]) / period
    loss = sum(max(-x, 0.0) for x in changes[:period]) / period

    def value(g, l):
        if l == 0:
            return 100.0 if g > 0 else 50.0
        return 100.0 - 100.0 / (1.0 + g / l)

    result[period] = value(gain, loss)
    for i in range(period + 1, len(values)):
        change = changes[i - 1]
        gain = (gain * (period - 1) + max(change, 0.0)) / period
        loss = (loss * (period - 1) + max(-change, 0.0)) / period
        result[i] = value(gain, loss)
    return result


def atr(candles, period=14):
    result = [None] * len(candles)
    if len(candles) < period:
        return result
    ranges = []
    for i, bar in enumerate(candles):
        prev = candles[i - 1]["close"] if i else bar["close"]
        ranges.append(max(
            bar["high"] - bar["low"],
            abs(bar["high"] - prev),
            abs(bar["low"] - prev),
        ))
    current = sum(ranges[:period]) / period
    result[period - 1] = current
    for i in range(period, len(candles)):
        current = (current * (period - 1) + ranges[i]) / period
        result[i] = current
    return result


def _bias(candles, close_time, seconds):
    eligible = [bar for bar in candles if int(bar["timestamp"]) + seconds <= close_time]
    if len(eligible) < 50:
        return "UNAVAILABLE"
    closes = [bar["close"] for bar in eligible]
    e20, e50 = ema(closes, 20), ema(closes, 50)
    i = len(eligible) - 1
    if e20[i] is None or e50[i] is None:
        return "UNAVAILABLE"
    if closes[i] > e50[i] and e20[i] > e50[i]:
        return "BULLISH"
    if closes[i] < e50[i] and e20[i] < e50[i]:
        return "BEARISH"
    return "NEUTRAL"


def analyze_mtf(data, now):
    """Return (snapshot, signal); no signal unless every timeframe is valid."""
    m5, m15, h1, h4 = (data[k] for k in ("m5", "m15", "h1", "h4"))
    m5_closed = [b for b in m5 if int(b["timestamp"]) + 300 <= now]
    m15_closed = [b for b in m15 if int(b["timestamp"]) + 900 <= now]
    if len(m5_closed) < 3 or len(m15_closed) < 220:
        raise ValueError("الشموع المغلقة غير كافية للتحليل متعدد الأطر")

    setup_bar = m15_closed[-1]
    setup_close_time = int(setup_bar["timestamp"]) + 900
    h1_bias = _bias(h1, setup_close_time, 3600)
    h4_bias = _bias(h4, setup_close_time, 14400)

    closes = [bar["close"] for bar in m15_closed]
    e20, e50, e200 = ema(closes, 20), ema(closes, 50), ema(closes, 200)
    rsi_values = rsi(closes, 14)
    atr_values = atr(m15_closed, 14)
    i = len(m15_closed) - 1
    current = m15_closed[i]
    last_m5 = m5_closed[-1]
    previous_m5 = m5_closed[-2]
    m5_atr = atr(m5_closed, 14)[-1]
    m15_atr = atr_values[i]

    snapshot = {
        "symbol": "XAUUSD", "source": data["source"],
        "live_price_source": data.get("live_price_source"),
        "live_price": (data.get("live_quote") or {}).get("price"),
        "live_price_age_seconds": (data.get("live_quote") or {}).get("age_seconds"),
        "live_bid": (data.get("live_quote") or {}).get("bid"),
        "live_ask": (data.get("live_quote") or {}).get("ask"),
        "timeframe": "M5/M15/H1/H4",
        "price": last_m5["close"],
        "candle_at": __import__("datetime").datetime.fromtimestamp(
            last_m5["timestamp"], __import__("datetime").timezone.utc
        ).isoformat(),
        "checked_at": data["checked_at"],
        "ema20": e20[i], "ema50": e50[i], "ema200": e200[i],
        "rsi14": rsi_values[i], "atr14": m15_atr,
        "candle_age_seconds": data["m5_age_seconds"],
        "higher_timeframe_bias": {"h1": h1_bias, "h4": h4_bias},
    }

    values = (e20[i], e50[i], e200[i], rsi_values[i], m15_atr, m5_atr)
    if any(v is None for v in values) or m15_atr <= 0 or m5_atr <= 0:
        return snapshot, None

    side = None
    if current["close"] > e200[i] and e50[i] > e200[i] and e200[i] > e200[i - 10]:
        side = "BUY"
    elif current["close"] < e200[i] and e50[i] < e200[i] and e200[i] < e200[i - 10]:
        side = "SELL"
    if side is None:
        snapshot["setup"] = None
        return snapshot, None

    opposing = "BEARISH" if side == "BUY" else "BULLISH"
    agreeing = "BULLISH" if side == "BUY" else "BEARISH"
    if h1_bias in (opposing, "UNAVAILABLE") or h4_bias in (opposing, "UNAVAILABLE") or agreeing not in (h1_bias, h4_bias):
        snapshot["setup"] = None
        return snapshot, None

    body = abs(current["close"] - current["open"])
    prior = m15_closed[max(0, i - LIVE_CONFIG["breakout_lookback"]):i]
    if not prior:
        return snapshot, None
    if side == "BUY":
        pullback = (
            current["low"] <= e20[i] + LIVE_CONFIG["pullback_atr_tolerance"] * m15_atr
            and current["close"] > e20[i] and current["close"] > current["open"]
            and current["close"] > m15_closed[i - 1]["close"]
        )
        breakout = current["close"] > max(x["high"] for x in prior) and body >= 0.5 * m15_atr
        trigger = (
            last_m5["close"] > last_m5["open"]
            and last_m5["close"] > previous_m5["high"]
            and abs(last_m5["close"] - last_m5["open"]) >= 0.15 * m5_atr
        )
        rsi_ok = 52 < rsi_values[i] < 70
        rsi_strong = 55 <= rsi_values[i] <= 65
    else:
        pullback = (
            current["high"] >= e20[i] - LIVE_CONFIG["pullback_atr_tolerance"] * m15_atr
            and current["close"] < e20[i] and current["close"] < current["open"]
            and current["close"] < m15_closed[i - 1]["close"]
        )
        breakout = current["close"] < min(x["low"] for x in prior) and body >= 0.5 * m15_atr
        trigger = (
            last_m5["close"] < last_m5["open"]
            and last_m5["close"] < previous_m5["low"]
            and abs(last_m5["close"] - last_m5["open"]) >= 0.15 * m5_atr
        )
        rsi_ok = 30 < rsi_values[i] < 48
        rsi_strong = 35 <= rsi_values[i] <= 45

    setup = "PULLBACK" if pullback else "BREAKOUT" if breakout else None
    if not setup or not trigger or not rsi_ok or body < LIVE_CONFIG["min_body_atr"] * m15_atr:
        snapshot["setup"] = setup
        return snapshot, None

    score = 25 + 15 + 15
    score += 20 if h1_bias == agreeing and h4_bias == agreeing else 10
    score += 15 if rsi_strong else 10
    score += 10 if body >= 0.35 * m15_atr else 5
    snapshot["setup"] = setup
    snapshot["quality_score"] = min(100, score)
    if score < LIVE_CONFIG["min_score"]:
        return snapshot, None

    # Live mode uses an independent, timestamp-validated spot quote. Historical
    # tests/backtests intentionally omit live_quote and use the next candle open.
    live_quote = data.get("live_quote")
    if live_quote is not None:
        quote_age = float(live_quote.get("age_seconds", float("inf")))
        candle_age = max(0.0, now - (int(last_m5["timestamp"]) + 300))
        mid = float(live_quote.get("price", 0.0))
        entry = float(live_quote.get("ask") if side == "BUY" else live_quote.get("bid"))
        max_extension = max(0.5 * float(m5_atr), 1.0)
        max_m15_extension = max(1.5 * float(m15_atr), 2.0)
        if (quote_age > 45 or candle_age > 180 or mid <= 0 or entry <= 0
                or abs(mid - float(last_m5["close"])) > max_extension
                or abs(mid - float(current["close"])) > max_m15_extension):
            snapshot["entry_pending"] = True
            snapshot["entry_reject_reason"] = "live quote stale or price diverged from M5/M15 setup"
            return snapshot, None
        snapshot["live_price"] = mid
        snapshot["live_price_age_seconds"] = int(quote_age)
        snapshot["live_bid"] = live_quote.get("bid")
        snapshot["live_ask"] = live_quote.get("ask")
        snapshot["entry_source"] = live_quote.get("source", "independent spot quote")
    else:
        next_ts = int(last_m5["timestamp"]) + 300
        next_bar = next((bar for bar in m5 if int(bar["timestamp"]) == next_ts), None)
        if next_bar is None or not (next_ts <= now < next_ts + 300) or now - next_ts > 90:
            snapshot["entry_pending"] = next_bar is None and now < next_ts + 300
            return snapshot, None
        entry = float(next_bar["open"])

    risk = 1.5 * m15_atr
    if side == "BUY":
        stop, tp1, tp2 = entry - risk, entry + risk, entry + 2 * risk
    else:
        stop, tp1, tp2 = entry + risk, entry - risk, entry - 2 * risk

    return snapshot, {
        "symbol": "XAUUSD", "side": side, "entry": round(entry, 3),
        "stop_loss": round(stop, 3), "take_profit_1": round(tp1, 3),
        "take_profit": round(tp2, 3), "timeframe": "M5/M15",
        "reason": (
            f"{setup} على M15؛ اتجاه H1={h1_bias} وH4={h4_bias}؛ "
            f"تأكيد دخول M5؛ جودة فنية {score}/100"
        ),
    }
