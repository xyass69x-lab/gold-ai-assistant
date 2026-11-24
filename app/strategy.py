"""Shared, no-lookahead XAU/USD strategy rules used by live signals and research."""

from bisect import bisect_right

TIMEFRAME_SECONDS = 30 * 60
HTF_SECONDS = 4 * TIMEFRAME_SECONDS


def _ema(values, period):
    result = [None] * len(values)
    if len(values) < period:
        return result
    alpha = 2.0 / (period + 1.0)
    current = sum(values[:period]) / period
    result[period - 1] = current
    for index in range(period, len(values)):
        current = alpha * values[index] + (1.0 - alpha) * current
        result[index] = current
    return result
LIVE_CONFIG = {
    "mode": "HYBRID",
    "min_score": 75,
    "min_body_atr": 0.25,
    "breakout_lookback": 20,
    "pullback_atr_tolerance": 0.15,
}


def resample_2h(candles):
    """Build only complete UTC-aligned 2h candles from four consecutive 30m bars."""
    buckets = {}
    for index, bar in enumerate(candles):
        bucket_start = (int(bar["timestamp"]) // HTF_SECONDS) * HTF_SECONDS
        buckets.setdefault(bucket_start, []).append((index, bar))

    result = []
    for bucket_start in sorted(buckets):
        items = sorted(buckets[bucket_start], key=lambda item: item[1]["timestamp"])
        expected = [bucket_start + n * TIMEFRAME_SECONDS for n in range(4)]
        if [item[1]["timestamp"] for item in items] != expected:
            continue
        bars = [item[1] for item in items]
        result.append({
            "timestamp": bucket_start,
            "open": bars[0]["open"],
            "high": max(bar["high"] for bar in bars),
            "low": min(bar["low"] for bar in bars),
            "close": bars[-1]["close"],
            "source_end_index": items[-1][0],
        })
    return result


def higher_timeframe_biases(candles):
    """Return the latest fully closed 2h EMA20/EMA50 bias for each 30m bar."""
    higher = resample_2h(candles)
    if not higher:
        return [None] * len(candles)

    closes = [bar["close"] for bar in higher]
    ema20 = _ema(closes, 20)
    ema50 = _ema(closes, 50)
    completed_at = [bar["timestamp"] + HTF_SECONDS for bar in higher]
    biases = []
    pointer = -1

    for bar in candles:
        close_time = int(bar["timestamp"]) + TIMEFRAME_SECONDS
        pointer = max(pointer, bisect_right(completed_at, close_time) - 1)
        if pointer < 49 or ema20[pointer] is None or ema50[pointer] is None:
            biases.append(None)
            continue
        if closes[pointer] > ema50[pointer] and ema20[pointer] > ema50[pointer]:
            biases.append("BULLISH")
        elif closes[pointer] < ema50[pointer] and ema20[pointer] < ema50[pointer]:
            biases.append("BEARISH")
        else:
            biases.append("NEUTRAL")
    return biases


def evaluate_strategy_signal(candles, index, indicators, htf_bias=None, config=None):
    """Score pullback and 20-bar breakout setups without looking beyond index."""
    config = config or LIVE_CONFIG
    ema20, ema50, ema200, rsi, atr = indicators
    if index < 200 or index >= len(candles) or index < 10:
        return None

    current = candles[index]
    values = (ema20[index], ema50[index], ema200[index], rsi[index], atr[index])
    prior_ema200 = ema200[index - 10] if index >= 10 else None
    if (
        not all(isinstance(value, (int, float)) for value in values)
        or not isinstance(prior_ema200, (int, float))
        or atr[index] <= 0
    ):
        # EMA200 needs 200 values to initialize, then another 10 bars are
        # required before its slope can be compared safely.
        return None

    close = current["close"]
    body = abs(close - current["open"])
    volatility = atr[index]
    trend_up = close > ema200[index] and ema50[index] > ema200[index]
    trend_down = close < ema200[index] and ema50[index] < ema200[index]
    slope_up = ema200[index] > prior_ema200
    slope_down = ema200[index] < prior_ema200

    if trend_up:
        side = "BUY"
        if htf_bias == "BEARISH":
            return None
        rsi_ok = 52 < rsi[index] < 70
        rsi_strong = 55 <= rsi[index] <= 65
        trend_slope_ok = slope_up
        pullback = (
            current["low"] <= ema20[index] + config["pullback_atr_tolerance"] * volatility
            and close > ema20[index]
            and close > current["open"]
            and close > candles[index - 1]["close"]
        )
        prior_high = max(bar["high"] for bar in candles[max(0, index - config["breakout_lookback"]):index])
        breakout = (
            close > prior_high and close > current["open"]
            and body >= 0.5 * volatility
        )
    elif trend_down:
        side = "SELL"
        if htf_bias == "BULLISH":
            return None
        rsi_ok = 30 < rsi[index] < 48
        rsi_strong = 35 <= rsi[index] <= 45
        trend_slope_ok = slope_down
        pullback = (
            current["high"] >= ema20[index] - config["pullback_atr_tolerance"] * volatility
            and close < ema20[index]
            and close < current["open"]
            and close < candles[index - 1]["close"]
        )
        prior_low = min(bar["low"] for bar in candles[max(0, index - config["breakout_lookback"]):index])
        breakout = (
            close < prior_low and close < current["open"]
            and body >= 0.5 * volatility
        )
    else:
        return None

    if not rsi_ok or not trend_slope_ok:
        return None

    setup = "PULLBACK" if pullback else "BREAKOUT" if breakout else None
    if setup is None or body < config["min_body_atr"] * volatility:
        return None

    score = 25  # 30m trend alignment: close and EMA50 relative to EMA200
    score += 15  # EMA200 slope agrees with the trade direction
    score += 20 if htf_bias == ("BULLISH" if side == "BUY" else "BEARISH") else 5
    score += 15 if rsi_strong else 10
    score += 10 if body >= 0.35 * volatility else 5
    score += 15  # closed-candle pullback confirmation or 20-bar breakout
    if score < config["min_score"]:
        return None

    return {
        "side": side,
        "setup": setup,
        "quality_score": score,
        "higher_timeframe_bias": htf_bias or "UNAVAILABLE",
    }
