from app.scalp_strategy import analyze_scalp


def _bar(ts, o, h, l, c):
    return {"timestamp": ts, "open": o, "high": h, "low": l, "close": c}


def test_scalp_requires_next_candle_open():
    bars = []
    price = 100.0
    for i in range(80):
        op = price
        price += 0.12
        bars.append(_bar(i * 300, op, price + 0.05, op - 0.05, price))
    now = bars[-1]["timestamp"] + 300 + 10
    data = {"m5": bars, "h1": [], "h4": []}
    snapshot = {"higher_timeframe_bias": {"h1": "BULLISH", "h4": "BULLISH"}}
    assert analyze_scalp(data, now, snapshot) is None


def test_scalp_returns_rr_two_and_valid_levels_when_confirmed():
    bars = []
    price = 100.0
    for i in range(80):
        op = price
        price += 0.12
        bars.append(_bar(i * 300, op, price + 0.05, op - 0.05, price))
    # Strong final completed bullish candle breaks the prior high.
    last = bars[-1]
    last.update({"open": last["close"] - 0.25, "low": last["close"] - 0.30,
                 "close": last["close"] + 0.25, "high": last["close"] + 0.30})
    next_ts = last["timestamp"] + 300
    bars.append(_bar(next_ts, last["close"] + 0.02, last["close"] + 0.10,
                     last["close"] - 0.05, last["close"] + 0.05))
    now = next_ts + 20
    data = {"m5": bars, "h1": [], "h4": []}
    snapshot = {"higher_timeframe_bias": {"h1": "BULLISH", "h4": "BULLISH"}}
    signal = analyze_scalp(data, now, snapshot)
    assert signal is not None
    assert signal["side"] == "BUY"
    assert signal["take_profit"] > signal["entry"] > signal["stop_loss"]
    assert round((signal["take_profit"] - signal["entry"]) /
                 (signal["entry"] - signal["stop_loss"]), 2) == 2.0
