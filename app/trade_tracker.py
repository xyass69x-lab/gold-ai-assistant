"""Conservative OHLC trade-state transitions for signal-only monitoring."""


def evaluate_trade_candle(side, entry, stop_loss, take_profit_1, take_profit_2, tp1_hit, candle):
    """Return (new_state, new_tp1_hit, events, exit_price).

    If stop and target are both touched in one OHLC candle, stop is assumed first.
    After TP1, the protective stop is moved to entry (break-even).
    """
    side = side.upper()
    if side not in ("BUY", "SELL"):
        raise ValueError("side must be BUY or SELL")

    if side == "BUY":
        stop_touched = candle["low"] <= (entry if tp1_hit else stop_loss)
        tp1_touched = candle["high"] >= take_profit_1
        tp2_touched = candle["high"] >= take_profit_2
    else:
        stop_touched = candle["high"] >= (entry if tp1_hit else stop_loss)
        tp1_touched = candle["low"] <= take_profit_1
        tp2_touched = candle["low"] <= take_profit_2

    if stop_touched:
        return ("BREAKEVEN" if tp1_hit else "STOP_LOSS", bool(tp1_hit),
                ["BREAKEVEN" if tp1_hit else "STOP_LOSS"], entry if tp1_hit else stop_loss)

    if tp2_touched:
        events = ["TP2"] if tp1_hit else ["TP1", "TP2"]
        return "TP2_HIT", True, events, take_profit_2

    if tp1_touched and not tp1_hit:
        return "TP1_HIT", True, ["TP1"], None

    return ("TP1_HIT" if tp1_hit else "OPEN"), bool(tp1_hit), [], None
