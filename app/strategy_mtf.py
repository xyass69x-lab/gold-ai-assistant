"""Multi-timeframe XAU/USD strategy with closed-candle/no-lookahead rules."""
import math
from bisect import bisect_right

LIVE_CONFIG = {
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
    m1_closed = [b for b in data.get("m1", []) if int(b["timestamp"]) + 60 <= now]
    d1_closed = [b for b in data["d1"] if int(b["timestamp"]) + 86400 <= now]
    if len(d1_closed) < 50:
        raise ValueError("شموع M1/D1 غير كافية")
    m5_closed = [b for b in m5 if int(b["timestamp"]) + 300 <= now]
    m15_closed = [b for b in m15 if int(b["timestamp"]) + 900 <= now]
    if len(m5_closed) < 3 or len(m15_closed) < 220:
        raise ValueError("الشموع المغلقة غير كافية للتحليل متعدد الأطر")

    setup_bar = m15_closed[-1]
    setup_close_time = int(setup_bar["timestamp"]) + 900
    h1_bias = _bias(h1, setup_close_time, 3600)
    h4_bias = _bias(h4, setup_close_time, 14400)
    d1_bias = _bias(d1_closed, setup_close_time, 86400)

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
        "timeframe": "M1/M5/M15/H1/H4/D1",
        "price": last_m5["close"],
        "candle_at": __import__("datetime").datetime.fromtimestamp(
            last_m5["timestamp"], __import__("datetime").timezone.utc
        ).isoformat(),
        "checked_at": data["checked_at"],
        "ema20": e20[i], "ema50": e50[i], "ema200": e200[i],
        "rsi14": rsi_values[i], "atr14": m15_atr,
        "candle_age_seconds": data["m5_age_seconds"],
        "m1_close": m1_closed[-1]["close"] if m1_closed else None,
        "m1_age_seconds": data.get("m1_age_seconds"),
        "higher_timeframe_bias": {"h1": h1_bias, "h4": h4_bias, "d1": d1_bias},
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
    if d1_bias in (opposing, "UNAVAILABLE") or h1_bias in (opposing, "UNAVAILABLE") or h4_bias in (opposing, "UNAVAILABLE") or agreeing not in (h1_bias, h4_bias):
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

    # Version 2 score: continuous, component-based and deliberately NOT an
    # entry gate. It ranks setup quality for research; only out-of-sample
    # calibration may justify a future threshold.
    clamp01 = lambda value: max(0.0, min(1.0, float(value)))
    direction = 1.0 if side == "BUY" else -1.0
    trend_separation = abs(e50[i] - e200[i]) / m15_atr
    trend_slope = direction * (e200[i] - e200[i - 10]) / m15_atr
    trend_points = 20.0 * (
        0.5 * clamp01(trend_separation / 0.75)
        + 0.5 * clamp01(trend_slope / 0.50)
    )

    # Reward a properly stacked M15 EMA structure; a price merely sitting
    # above/below EMA200 is not enough to earn a top technical-quality score.
    m15_ema_stack = direction * (e20[i] - e50[i]) / m15_atr
    m15_structure_points = 10.0 * clamp01(m15_ema_stack / 0.35)

    htf_points = 20.0 if h1_bias == agreeing and h4_bias == agreeing else 12.0

    if setup == "PULLBACK":
        touch_distance = (
            abs(current["low"] - e20[i]) if side == "BUY"
            else abs(current["high"] - e20[i])
        ) / m15_atr
        setup_points = 12.0 + 8.0 * clamp01(1.0 - touch_distance / 0.60)
    else:
        breakout_level = max(x["high"] for x in prior) if side == "BUY" else min(x["low"] for x in prior)
        breakout_clearance = direction * (current["close"] - breakout_level) / m15_atr
        setup_points = (
            10.0 + 5.0 * clamp01(breakout_clearance / 0.50)
            + 5.0 * clamp01((body / m15_atr) / 1.00)
        )

    m5_body_ratio = abs(last_m5["close"] - last_m5["open"]) / m5_atr
    m5_clearance = (
        (last_m5["close"] - previous_m5["high"]) if side == "BUY"
        else (previous_m5["low"] - last_m5["close"])
    ) / m5_atr
    trigger_points = (
        10.0 * clamp01(m5_body_ratio / 0.60)
        + 10.0 * clamp01(m5_clearance / 0.40)
    )

    rsi_distance = abs(float(rsi_values[i]) - (60.0 if side == "BUY" else 40.0))
    rsi_points = 10.0 * clamp01(1.0 - rsi_distance / 20.0)

    raw_parts = {
        "trend_strength_and_slope": trend_points,
        "m15_ema_structure": m15_structure_points,
        "h1_h4_alignment": htf_points,
        "m15_setup_quality": setup_points,
        "m5_trigger_strength": trigger_points,
        "rsi_position": rsi_points,
    }
    score_parts = {name: round(value, 1) for name, value in raw_parts.items()}
    score = int(round(sum(score_parts.values())))
    score = max(0, min(100, score))
    snapshot["setup"] = setup
    snapshot["quality_score"] = score
    snapshot["quality_score_type"] = "technical_quality_v3_not_win_probability"
    snapshot["quality_score_method"] = "continuous_component_score_v3"
    snapshot["quality_score_parts"] = score_parts

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
        if (quote_age > 45 or candle_age > 600 or mid <= 0 or entry <= 0
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

    # Preserve the quote precision and round protective/target prices outward.
    # Nearest rounding at 3 decimals could turn an intended 2.00R target into
    # 1.99R, causing the API's strict 1:2 validator to reject a valid setup.
    precision = 10_000
    entry_out = round(float(entry), 4)
    raw_risk = 1.5 * float(m15_atr)
    if side == "BUY":
        stop = math.floor((entry_out - raw_risk) * precision) / precision
        actual_risk = entry_out - stop
        tp1 = round(entry_out + actual_risk, 4)
        tp2 = math.ceil((entry_out + 2.0 * actual_risk) * precision) / precision
        valid_levels = 0 < stop < entry_out < tp1 < tp2
    else:
        stop = math.ceil((entry_out + raw_risk) * precision) / precision
        actual_risk = stop - entry_out
        tp1 = round(entry_out - actual_risk, 4)
        tp2 = math.floor((entry_out - 2.0 * actual_risk) * precision) / precision
        valid_levels = 0 < tp2 < tp1 < entry_out < stop

    actual_reward = abs(tp2 - entry_out)
    if not valid_levels or actual_risk <= 0 or actual_reward / actual_risk < 2.0:
        snapshot["entry_reject_reason"] = "rounded risk/reward levels do not preserve minimum 1:2"
        return snapshot, None

    return snapshot, {
        "symbol": "XAUUSD", "side": side, "entry": entry_out,
        "stop_loss": round(stop, 4), "take_profit_1": round(tp1, 4),
        "take_profit": round(tp2, 4), "timeframe": "M1/M5/M15/H1/H4/D1",
        "confidence": score,
        "reason": (
            f"{setup} على M15؛ اتجاه H1={h1_bias} وH4={h4_bias}؛ "
            f"تأكيد دخول M5؛ جودة فنية {score}/100 (ليست احتمال ربح)"
        ),
    }
