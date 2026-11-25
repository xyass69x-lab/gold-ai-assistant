from app.scalp_strategy import analyze_scalp
from app.main import Signal, validate_signal


def _bar(ts, o, h, l, c):
    return {"timestamp": ts, "open": o, "high": h, "low": l, "close": c}


def test_scalp_requires_next_candle_open():
    bars = []
    price = 100.0
    for i in range(80):
        op = price
        price += (0.16 if i % 2 == 0 else -0.08)
        bars.append(_bar(i * 300, op, max(op, price) + 0.05, min(op, price) - 0.05, price))
    now = bars[-1]["timestamp"] + 300 + 10
    data = {"m5": bars, "h1": [], "h4": []}
    snapshot = {"higher_timeframe_bias": {"h1": "BULLISH", "h4": "BULLISH"}}
    assert analyze_scalp(data, now, snapshot) is None


def test_scalp_returns_rr_two_and_valid_levels_when_confirmed():
    bars = []
    price = 100.0
    for i in range(80):
        op = price
        price += [0.13, -0.08, 0.13, -0.08, -0.08][i % 5]
        bars.append(_bar(i * 300, op, max(op, price) + 0.05, min(op, price) - 0.05, price))
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


def test_scalp_signal_passes_application_schema_and_rr_validation():
    signal = {
        "symbol": "XAUUSD", "side": "BUY", "entry": 3500.0,
        "stop_loss": 3490.0, "take_profit_1": 3510.0,
        "take_profit": 3520.0, "timeframe": "M5 SCALP",
        "reason": "M5 scalp confirmation", "confidence": 84,
    }
    parsed = Signal(**signal)
    assert validate_signal(parsed) == 2.0


def _confirmed_scalp_data():
    bars = []
    price = 100.0
    for i in range(80):
        op = price
        price += [0.13, -0.08, 0.13, -0.08, -0.08][i % 5]
        bars.append(_bar(i * 300, op, max(op, price) + 0.05, min(op, price) - 0.05, price))
    last = bars[-1]
    last.update({
        "open": last["close"] - 0.25,
        "low": last["close"] - 0.30,
        "close": last["close"] + 0.25,
        "high": last["close"] + 0.30,
    })
    next_ts = last["timestamp"] + 300
    return bars, next_ts, last


def test_scalp_uses_fresh_spot_quote_after_90_second_window():
    bars, next_ts, last = _confirmed_scalp_data()
    data = {
        "m5": bars, "h1": [], "h4": [],
        "live_quote": {"price": last["close"] + 0.02, "bid": last["close"] + 0.01, "ask": last["close"] + 0.03, "age_seconds": 15},
    }
    snapshot = {"higher_timeframe_bias": {"h1": "BULLISH", "h4": "BULLISH"}}
    signal = analyze_scalp(data, next_ts + 240, snapshot)
    assert signal is not None
    assert signal["entry"] == round(last["close"] + 0.03, 3)


def test_scalp_rejects_stale_live_spot_quote():
    bars, next_ts, last = _confirmed_scalp_data()
    data = {
        "m5": bars, "h1": [], "h4": [],
        "live_quote": {"price": last["close"] + 0.02, "bid": last["close"] + 0.01, "ask": last["close"] + 0.03, "age_seconds": 120},
    }
    snapshot = {"higher_timeframe_bias": {"h1": "BULLISH", "h4": "BULLISH"}}
    assert analyze_scalp(data, next_ts + 240, snapshot) is None
