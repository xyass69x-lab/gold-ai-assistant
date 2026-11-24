"""Chronological train/validation research for conservative XAU/USD 30m strategy."""
import time
from datetime import datetime, timezone

from app.market_engine import TIMEFRAME_SECONDS, _atr, _ema, _rsi, fetch_candles
from app.strategy import LIVE_CONFIG, evaluate_strategy_signal, higher_timeframe_biases


HORIZON = 48
TRAIN_FRACTION = 0.70
MIN_TRAIN_TRADES_FOR_SELECTION = 8

# Fixed, small hypothesis set. No parameter is selected using validation results.
CANDIDATES = (
    {"name": "BASELINE", "slope_filter": False, "rsi_buy": 52, "rsi_sell": 48, "body_atr": 0.0},
    {"name": "EMA200_SLOPE", "slope_filter": True, "rsi_buy": 52, "rsi_sell": 48, "body_atr": 0.0},
    {"name": "SLOPE_RSI_55_45", "slope_filter": True, "rsi_buy": 55, "rsi_sell": 45, "body_atr": 0.0},
    {"name": "SLOPE_RSI_BODY", "slope_filter": True, "rsi_buy": 55, "rsi_sell": 45, "body_atr": 0.15},
    {**LIVE_CONFIG, "name": "HYBRID_PULLBACK_BREAKOUT"},
)


def _simulate_trade(candles, entry_index, side, entry, risk, horizon):
    """50% exits at TP1 and 50% at TP2; adverse stop-first OHLC assumption."""
    if risk <= 0:
        return None
    if side == "BUY":
        stop, tp1, tp2 = entry - risk, entry + risk, entry + 2 * risk
    else:
        stop, tp1, tp2 = entry + risk, entry - risk, entry - 2 * risk

    tp1_hit = False
    last_index = min(entry_index + horizon, len(candles) - 1)
    for j in range(entry_index, last_index + 1):
        bar = candles[j]
        # The live tracker applies the break-even stop from the candle AFTER
        # TP1 is confirmed. Keep that same candle-by-candle transition here.
        active_stop = entry if tp1_hit else stop
        if side == "BUY":
            sl_touched, tp1_touched, tp2_touched = (
                bar["low"] <= active_stop, bar["high"] >= tp1, bar["high"] >= tp2
            )
        else:
            sl_touched, tp1_touched, tp2_touched = (
                bar["high"] >= active_stop, bar["low"] <= tp1, bar["low"] <= tp2
            )

        # If the active stop and a target are both touched in one OHLC candle,
        # assume the stop happened first. After TP1, half the position has
        # already banked +1R, so a break-even exit on the remainder is +0.5R.
        if sl_touched:
            return {
                "gross_r": 0.5 if tp1_hit else -1.0,
                "tp1": tp1_hit, "tp2": False,
                "stop_before_tp1": not tp1_hit,
                "stop_after_tp1": tp1_hit, "exit_index": j,
                "time_exit": False,
            }
        if tp2_touched:
            return {
                "gross_r": 1.5, "tp1": True, "tp2": True,
                "stop_before_tp1": False, "stop_after_tp1": False,
                "exit_index": j, "time_exit": False,
            }
        if tp1_touched:
            tp1_hit = True

    final_close = candles[last_index]["close"]
    direction = 1.0 if side == "BUY" else -1.0
    remaining_r = ((final_close - entry) * direction) / risk
    gross_r = 0.5 * 1.0 + 0.5 * remaining_r if tp1_hit else remaining_r
    return {
        "gross_r": gross_r, "tp1": tp1_hit, "tp2": False,
        "stop_before_tp1": False, "stop_after_tp1": False,
        "exit_index": last_index, "time_exit": True,
    }


def _metrics(trades):
    total = len(trades)
    if not total:
        return {
            "count": 0, "wins": 0, "losses": 0, "breakeven": 0,
            "net_r": 0.0, "avg_r": 0.0, "profit_factor": None,
            "max_dd_r": 0.0, "tp1": 0, "tp2": 0,
        }
    wins = sum(t["gross_r"] > 0 for t in trades)
    losses = sum(t["gross_r"] < 0 for t in trades)
    net_r = sum(t["gross_r"] for t in trades)
    positive = sum(t["gross_r"] for t in trades if t["gross_r"] > 0)
    negative = -sum(t["gross_r"] for t in trades if t["gross_r"] < 0)
    equity = peak = max_dd = 0.0
    for t in trades:
        equity += t["gross_r"]
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    return {
        "count": total, "wins": wins, "losses": losses,
        "breakeven": total - wins - losses,
        "net_r": net_r, "avg_r": net_r / total,
        "profit_factor": positive / negative if negative else None,
        "max_dd_r": max_dd,
        "tp1": sum(t["tp1"] for t in trades),
        "tp2": sum(t["tp2"] for t in trades),
    }


def _signal_side(candles, i, indicators, config, htf_bias=None):
    if config.get("mode") == "HYBRID":
        decision = evaluate_strategy_signal(candles, i, indicators, htf_bias, config)
        return decision["side"] if decision else None
    ema20, ema50, ema200, rsi, atr = indicators
    c = candles[i]

    # Explicitly validate every indicator used in comparisons. Early indicator
    # warm-up values are None by design and must never reach numeric operators.
    current_values = (ema20[i], ema50[i], ema200[i], rsi[i], atr[i])
    if not all(isinstance(value, (int, float)) for value in current_values):
        return None
    if atr[i] <= 0:
        return None

    up = c["close"] > ema200[i] and ema50[i] > ema200[i]
    down = c["close"] < ema200[i] and ema50[i] < ema200[i]

    if config["slope_filter"]:
        if i < 10:
            return None
        prior_ema200 = ema200[i - 10]
        if not isinstance(prior_ema200, (int, float)):
            return None
        up = up and ema200[i] > prior_ema200
        down = down and ema200[i] < prior_ema200

    body = abs(c["close"] - c["open"])
    body_ok = body >= config["body_atr"] * atr[i]
    buy = (
        up and c["low"] <= ema20[i] and c["close"] > ema20[i]
        and rsi[i] > config["rsi_buy"] and c["close"] > c["open"] and body_ok
    )
    sell = (
        down and c["high"] >= ema20[i] and c["close"] < ema20[i]
        and rsi[i] < config["rsi_sell"] and c["close"] < c["open"] and body_ok
    )
    if buy:
        return "BUY"
    if sell:
        return "SELL"
    return None


def _run_segment(candles, indicators, config, start, end, htf_biases=None):
    """Only include trades whose full 24h outcome fits inside [start, end)."""
    trades = []
    i = max(start, 200)
    last_signal = end - HORIZON - 2
    while i <= last_signal:
        htf_bias = htf_biases[i] if htf_biases is not None else None
        side = _signal_side(candles, i, indicators, config, htf_bias)
        if side is None:
            i += 1
            continue
        entry_index = i + 1
        entry = candles[entry_index]["open"]
        risk = 1.5 * indicators[4][i]
        trade = _simulate_trade(candles, entry_index, side, entry, risk, HORIZON)
        if trade is None:
            i += 1
            continue
        trade["side"] = side
        trades.append(trade)
        i = trade["exit_index"] + 1
    return trades


def _format_metrics(label, trades):
    m = _metrics(trades)
    if not m["count"]:
        return f"{label}: لا توجد صفقات كافية"
    pf = f'{m["profit_factor"]:.2f}' if m["profit_factor"] is not None else "∞"
    buy = _metrics([t for t in trades if t["side"] == "BUY"])
    sell = _metrics([t for t in trades if t["side"] == "SELL"])
    return (
        f"{label}: {m['count']} صفقة | صافي {m['net_r']:+.2f}R | "
        f"متوسط {m['avg_r']:+.3f}R | PF {pf} | DD {m['max_dd_r']:.2f}R\n"
        f"  BUY: {buy['count']} صفقة، {buy['net_r']:+.2f}R، متوسط {buy['avg_r']:+.3f}R\n"
        f"  SELL: {sell['count']} صفقة، {sell['net_r']:+.2f}R، متوسط {sell['avg_r']:+.3f}R"
    )


def run_backtest_report():
    candles = fetch_candles()
    now = time.time()
    candles = [
        c for c in sorted(candles, key=lambda x: x["timestamp"])
        if c["timestamp"] + TIMEFRAME_SECONDS <= now
    ]
    if len(candles) < 500:
        return f"⚠️ العينة قصيرة ({len(candles)} شمعة مغلقة). نحتاج 500 شمعة على الأقل."

    closes = [c["close"] for c in candles]
    indicators = (
        _ema(closes, 20), _ema(closes, 50), _ema(closes, 200),
        _rsi(closes, 14), _atr(candles, 14),
    )
    htf_biases = higher_timeframe_biases(candles)
    split = int(len(candles) * TRAIN_FRACTION)
    # Purge one 24h horizon at the split to prevent outcome leakage.
    train_end = split - HORIZON
    test_start = split
    start_text = datetime.fromtimestamp(candles[0]["timestamp"], timezone.utc).strftime("%Y-%m-%d")
    split_text = datetime.fromtimestamp(candles[split]["timestamp"], timezone.utc).strftime("%Y-%m-%d")
    end_text = datetime.fromtimestamp(candles[-1]["timestamp"], timezone.utc).strftime("%Y-%m-%d")

    results = []
    for config in CANDIDATES:
        train = _run_segment(candles, indicators, config, 200, train_end, htf_biases)
        test = _run_segment(candles, indicators, config, test_start, len(candles), htf_biases)
        results.append((config, train, test))

    # Select using training data only; require a minimum sample and positive expectancy.
    eligible = [
        row for row in results
        if len(row[1]) >= MIN_TRAIN_TRADES_FOR_SELECTION
        and _metrics(row[1])["avg_r"] > 0
        and _metrics(row[1])["profit_factor"] is not None
        and _metrics(row[1])["profit_factor"] > 1.0
    ]
    selected = max(
        eligible,
        key=lambda row: (_metrics(row[1])["avg_r"], _metrics(row[1])["profit_factor"]),
        default=None,
    )

    baseline = results[0]
    lines = [
        "🧪 بحث تطوير الاستراتيجية — XAU/USD 30m",
        f"الفترة: {start_text} إلى {end_text} | تقسيم زمني 70% تطوير / 30% اختبار مستقل",
        f"بداية الاختبار المستقل: {split_text} | فجوة منع تسرب: 24 ساعة",
        "",
        "النسخة الأساسية — بيانات التطوير:",
        _format_metrics("BASELINE", baseline[1]),
        "",
        "مقارنة الفلاتر على بيانات التطوير:",
    ]
    for config, train, _ in results[1:]:
        lines.append(_format_metrics(config["name"], train))

    lines += ["", "الاختبار المستقل (لم يُستخدم لاختيار الفلتر):"]
    if selected:
        config, train, test = selected
        lines.append(f"الفلتر المختار من بيانات التطوير فقط: {config['name']}")
        lines.append(_format_metrics("BASELINE OOS", baseline[2]))
        lines.append(_format_metrics(f"{config['name']} OOS", test))
    else:
        lines.append("لم تحقق أي نسخة شروط الاختيار: 8 صفقات تطوير على الأقل، متوسط موجب، وعامل ربح أكبر من 1.")
        lines.append(_format_metrics("BASELINE OOS", baseline[2]))
        lines.append("لا يوجد فلتر مطوّر مؤهل للاختبار المستقل.")

    lines += [
        "",
        "النسخة الهجينة الحية: ارتداد EMA20 أو اختراق أعلى/أدنى 20 شمعة؛ ميل EMA200؛ تأكيد اتجاه 2h عند توفره؛ RSI غير متطرف؛ تقييم جودة لا يقل عن 75/100.",
        "النسخة الهجينة في الاختبار تستخدم نفس دالة القرار المستخدمة في المحرك الحي؛ لا تُستخدم بيانات شموع مستقبلية لحساب اتجاه 2h.",
        "الفلاتر المرجعية الأخرى: ميل EMA200 خلال 10 شموع؛ تشديد RSI إلى 55/45؛ قوة جسم الشمعة 0.15 ATR.",
        "الدخول عند افتتاح الشمعة التالية، خروج 50% عند TP1 و50% عند TP2، ووقف أولاً عند غموض ترتيب اللمس داخل الشمعة.",
        "لا تشمل النتائج السبريد أو الانزلاق أو العمولة. العينة قصيرة نسبياً؛ لا تغيّر إعدادات التنبيهات الحية بناءً على هذا التقرير وحده.",
    ]
    return "\n".join(lines)
