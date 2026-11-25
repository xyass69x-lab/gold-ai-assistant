"""Chronological, no-lookahead backtest for the live M5/M15/H1/H4 strategy."""
import os
from datetime import datetime, timezone

from app.historical_data import fetch_historical_market_data
from app.strategy_mtf import analyze_mtf

HORIZON_BARS = 288  # 24 hours on M5
TRAIN_FRACTION = 0.70


def _simulate(candles, entry_index, side, entry, stop, tp1, tp2):
    risk = abs(entry - stop)
    if risk <= 0:
        return None
    tp1_hit = False
    end = min(entry_index + HORIZON_BARS, len(candles) - 1)
    for j in range(entry_index, end + 1):
        bar = candles[j]
        if side == "BUY":
            stopped = bar["low"] <= (entry if tp1_hit else stop)
            first = bar["high"] >= tp1
            second = bar["high"] >= tp2
        else:
            stopped = bar["high"] >= (entry if tp1_hit else stop)
            first = bar["low"] <= tp1
            second = bar["low"] <= tp2
        if stopped:
            return {"r": 0.5 if tp1_hit else -1.0, "exit": j, "tp1": tp1_hit, "tp2": False}
        if second:
            return {"r": 1.5, "exit": j, "tp1": True, "tp2": True}
        if first:
            tp1_hit = True
    direction = 1.0 if side == "BUY" else -1.0
    remaining = (candles[end]["close"] - entry) * direction / risk
    result = 0.5 + 0.5 * remaining if tp1_hit else remaining
    return {"r": result, "exit": end, "tp1": tp1_hit, "tp2": False}


def _metrics(trades, cost_r=0.0):
    values = [t["r"] - cost_r for t in trades]
    if not values:
        return {"count": 0, "wins": 0, "net": 0.0, "avg": 0.0, "pf": None, "dd": 0.0}
    wins = sum(v > 0 for v in values)
    gains = sum(v for v in values if v > 0)
    losses = -sum(v for v in values if v < 0)
    equity = peak = drawdown = 0.0
    for value in values:
        equity += value
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    return {
        "count": len(values), "wins": wins, "net": sum(values),
        "avg": sum(values) / len(values),
        "pf": gains / losses if losses else None, "dd": drawdown,
    }


def _fmt(label, trades, cost_r):
    m = _metrics(trades, cost_r)
    pf = f"{m['pf']:.2f}" if m["pf"] is not None else "غير محسوب"
    win_rate = 100.0 * m["wins"] / m["count"] if m["count"] else 0.0
    return (
        f"{label}: {m['count']} صفقة | الفوز {win_rate:.1f}% | "
        f"الصافي {m['net']:+.2f}R | المتوسط {m['avg']:+.3f}R | "
        f"Profit Factor {pf} | Drawdown {m['dd']:.2f}R"
    )


def run_backtest_report(data=None):
    # Backtests prefer extended Yahoo GC=F futures history and fall back to XAUS spot;
    # they never fetch or reuse the live BiQuote quote.
    if data is None:
        data = fetch_historical_market_data()
    # Never allow the current live quote to leak into historical simulation.
    data = dict(data)
    data.pop("live_quote", None)
    candles = data["m5"]
    if len(candles) < 1000:
        return f"⚠️ عينة M5 غير كافية ({len(candles)} شمعة). نحتاج 1000 على الأقل حتى لا نصدر حكماً مضللاً."

    trades = []
    i = 700
    split = int(len(candles) * TRAIN_FRACTION) if len(candles) >= 3000 else None
    while i < len(candles) - 1:
        # Evaluate at the start of the next M5 candle, after it has opened.
        # Candle i is closed; candle i+1 supplies only its opening price.
        # This mirrors the live 90-second rule without using its H/L/C.
        next_candle = candles[i + 1]
        now = int(next_candle["timestamp"]) + 30
        snapshot, signal = analyze_mtf(data, now)
        if not signal:
            i += 1
            continue
        entry_index = i + 1
        trade = _simulate(
            candles, entry_index, signal["side"], signal["entry"],
            signal["stop_loss"], signal["take_profit_1"], signal["take_profit"],
        )
        if trade is None:
            i += 1
            continue
        trade.update({
            "side": signal["side"], "signal_index": i,
            "setup": snapshot.get("setup"),
        })
        trades.append(trade)
        i = trade["exit"] + 1

    if split is None:
        train = []
        test = []
    else:
        train = [t for t in trades if t["signal_index"] < split and t["exit"] < split]
        test = [t for t in trades if t["signal_index"] >= split]
    raw_cost = os.getenv("BACKTEST_ROUND_TURN_COST_R", "").strip()
    try:
        cost_r = max(0.0, float(raw_cost)) if raw_cost else 0.0
    except ValueError:
        cost_r = 0.0

    start = datetime.fromtimestamp(candles[0]["timestamp"], timezone.utc).strftime("%Y-%m-%d")
    end = datetime.fromtimestamp(candles[-1]["timestamp"], timezone.utc).strftime("%Y-%m-%d")
    split_at = datetime.fromtimestamp(candles[split]["timestamp"], timezone.utc).strftime("%Y-%m-%d") if split is not None else None
    lines = [
        "🧪 اختبار GOLD AI متعدد الأطر — M5/M15/H1/H4",
        f"مصدر البيانات: {data['source']} | الفترة المتاحة: {start} إلى {end}",
        f"الشموع: M5={len(data['m5'])} | M15={len(data['m15'])} | H1={len(data['h1'])} | H4={len(data['h4'])}",
        (f"تقسيم زمني: 70% تطوير حتى {split_at} / 30% اختبار مستقل" if split_at else "⚠️ عينة قصيرة: فحص استكشافي فقط، دون ادعاء اختبار OOS أو صلاحية إحصائية"),
        f"تكلفة التنفيذ المفترضة: {cost_r:.4f}R لكل صفقة (اضبط BACKTEST_ROUND_TURN_COST_R لمعايرة XTB)",
        "",
        *([_fmt("التطوير", train, cost_r), _fmt("الاختبار المستقل OOS", test, cost_r)] if split is not None else []),
        _fmt("كل العينة بعد الإحماء", trades, cost_r),
        "",
        "GC=F عقود ذهب COMEX وليست XAUUSD spot؛ تُستخدم كمرجع تاريخي فقط. النتائج ليست ضماناً للأداء،",
        "وبيانات المزود قد تختلف عن XTB، كما لا يحاكي الاختبار السبريد والانزلاق بدقة ما لم تُعاير تكلفة R.",
        "والاختبار لا يمثل التنفيذ الفعلي أو السبريد والانزلاق بدقة ما لم تُعاير تكلفة R.",
        "عند أقل من 3000 شمعة، لا تُعرض نتيجة OOS لأن فترة الإحماء تستهلك معظم العينة؛ نحتاج تاريخاً أطول قبل اعتماد الأداء.",
    ]
    return "\n".join(lines)
