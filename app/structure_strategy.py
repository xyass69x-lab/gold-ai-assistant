"""Closed-candle swing breakout engine. No future bars."""
def analyze_structure(data, now, snapshot):
    bars = [b for b in data["m5"] if int(b["timestamp"]) + 300 <= now]
    if len(bars) < 35:
        return None
    quote = data.get("live_quote") or {}
    if float(quote.get("age_seconds", 9999)) > 45:
        return None
    # Swing points confirmed by two closed candles on either side.
    history = bars[-32:-1]
    highs, lows = [], []
    for i in range(2, len(history)-2):
        window = history[i-2:i+3]
        b = history[i]
        if b["high"] == max(x["high"] for x in window):
            highs.append(float(b["high"]))
        if b["low"] == min(x["low"] for x in window):
            lows.append(float(b["low"]))
    if not highs or not lows:
        return None
    bar, prev = bars[-1], bars[-2]
    o, c = float(bar["open"]), float(bar["close"])
    resistance, support = highs[-1], lows[-1]
    if c > resistance and o <= resistance and c > o:
        side, level = "BUY", resistance
        entry = float(quote.get("ask", 0))
        stop = min(support, float(bar["low"]))
        risk = entry - stop
        targets = (entry + risk, entry + 2*risk)
    elif c < support and o >= support and c < o:
        side, level = "SELL", support
        entry = float(quote.get("bid", 0))
        stop = max(resistance, float(bar["high"]))
        risk = stop - entry
        targets = (entry - risk, entry - 2*risk)
    else:
        return None
    if entry <= 0 or risk <= 0 or targets[0] <= 0:
        return None
    # Reject stale/distant entries rather than chase a broken level.
    if abs(entry-c) > max(1.0, (float(bar["high"])-float(bar["low"]))):
        return None
    return {
        "symbol": "XAUUSD", "side": side, "entry": round(entry, 3),
        "stop_loss": round(stop, 3), "take_profit_1": round(targets[0], 3),
        "take_profit": round(targets[1], 3), "timeframe": "M5 STRUCTURE",
        "reason": f"كسر مستوى مؤكد {level:.3f} بإغلاق شمعة M5",
    }
