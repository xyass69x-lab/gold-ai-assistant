"""Short-horizon M5 scalp setups; closed candles only, manual execution only."""
import math

from app.strategy_mtf import ema, rsi, atr


def analyze_scalp(data, now, snapshot=None):
    """Return a conservative M5 scalp signal or None.

    Signal is confirmed on a completed M5 candle and uses the next M5 open.
    It never sends orders. H1/H4 are used only as directional guards.
    """
    bars = [b for b in data["m5"] if int(b["timestamp"]) + 300 <= now]
    if len(bars) < 60:
        return None

    closes = [float(b["close"]) for b in bars]
    e20, e50 = ema(closes, 20), ema(closes, 50)
    rsi_values = rsi(closes, 14)
    atr_values = atr(bars, 14)
    i = len(bars) - 1
    current, previous = bars[i], bars[i - 1]
    values = (e20[i], e50[i], rsi_values[i], atr_values[i])
    if any(v is None for v in values) or atr_values[i] <= 0:
        return None

    bias = (snapshot or {}).get("higher_timeframe_bias", {})
    h1, h4 = bias.get("h1"), bias.get("h4")
    side = None
    quality = 0
    bullish_trend = closes[i] > e20[i] > e50[i]
    bearish_trend = closes[i] < e20[i] < e50[i]

    # Momentum continuation or a confirmed EMA20 rejection.
    buy_trigger = (
        current["close"] > current["open"]
        and (current["close"] > previous["high"] or
             (current["low"] <= e20[i] and current["close"] > e20[i]))
        and 50 < rsi_values[i] < 68
    )
    sell_trigger = (
        current["close"] < current["open"]
        and (current["close"] < previous["low"] or
             (current["high"] >= e20[i] and current["close"] < e20[i]))
        and 32 < rsi_values[i] < 50
    )
    if bullish_trend and buy_trigger and "BEARISH" not in (h1, h4):
        side = "BUY"
        quality = 72 + (6 if h1 == "BULLISH" else 0) + (6 if h4 == "BULLISH" else 0)
    elif bearish_trend and sell_trigger and "BULLISH" not in (h1, h4):
        side = "SELL"
        quality = 72 + (6 if h1 == "BEARISH" else 0) + (6 if h4 == "BEARISH" else 0)
    if side is None:
        return None

    live_quote = data.get("live_quote")
    if live_quote is not None:
        quote_age = float(live_quote.get("age_seconds", float("inf")))
        candle_age = max(0.0, now - (int(current["timestamp"]) + 300))
        mid = float(live_quote.get("price", 0.0))
        entry = float(live_quote.get("ask") if side == "BUY" else live_quote.get("bid"))
        max_extension = max(0.5 * float(atr_values[i]), 1.0)
        if (quote_age > 45 or candle_age > 300 or mid <= 0 or entry <= 0
                or abs(mid - float(current["close"])) > max_extension):
            return None
    else:
        next_ts = int(current["timestamp"]) + 300
        next_bar = next((b for b in data["m5"] if int(b["timestamp"]) == next_ts), None)
        if next_bar is None or not (next_ts <= now < next_ts + 300) or now - next_ts > 90:
            return None
        entry = float(next_bar["open"])

    # Round protective/target levels outward so precision cannot reduce RR below 1:2.
    precision = 10_000
    entry_out = round(float(entry), 3)
    raw_risk = 1.2 * float(atr_values[i])
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

    if not valid_levels or actual_risk <= 0 or abs(tp2 - entry_out) / actual_risk < 2.0:
        return None

    return {
        "symbol": "XAUUSD", "side": side, "entry": entry_out,
        "stop_loss": round(stop, 4), "take_profit_1": round(tp1, 4),
        "take_profit": round(tp2, 4), "timeframe": "M5 SCALP",
        "reason": (
            f"فرصة سكالبينغ M5؛ EMA20/50 مع تأكيد شمعة مغلقة؛ "
            f"RSI={rsi_values[i]:.1f}؛ H1={h1} H4={h4}؛ "
            f"جودة فنية {min(100, quality)}/100"
        ),
        "confidence": min(100, quality),
    }
