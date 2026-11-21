"""Conservative historical test for the live 30-minute XAU/USD rules."""
import time

from app.market_engine import (
    TIMEFRAME_SECONDS, _atr, _ema, _rsi, fetch_candles,
)


def run_backtest_report():
    candles = fetch_candles()
    now = time.time()
    candles = [
        c for c in sorted(candles, key=lambda x: x["timestamp"])
        if c["timestamp"] + TIMEFRAME_SECONDS <= now
    ]
    horizon = 48  # 24 hours of 30-minute bars
    if len(candles) < 220 + horizon:
        return f"⚠️ البيانات التاريخية غير كافية للاختبار: {len(candles)} شمعة مغلقة."

    closes = [c["close"] for c in candles]
    ema20, ema50, ema200 = _ema(closes, 20), _ema(closes, 50), _ema(closes, 200)
    rsi, atr = _rsi(closes, 14), _atr(candles, 14)

    trades = []
    next_allowed = 200
    for i in range(199, len(candles) - horizon):
        if i < next_allowed:
            continue
        c = candles[i]
        if any(v[i] is None for v in (ema20, ema50, ema200, rsi, atr)) or atr[i] <= 0:
            continue

        up = c["close"] > ema200[i] and ema50[i] > ema200[i]
        down = c["close"] < ema200[i] and ema50[i] < ema200[i]
        side = None
        if up and c["low"] <= ema20[i] and c["close"] > ema20[i] and rsi[i] > 52 and c["close"] > c["open"]:
            side = "BUY"
        elif down and c["high"] >= ema20[i] and c["close"] < ema20[i] and rsi[i] < 48 and c["close"] < c["open"]:
            side = "SELL"
        if not side:
            continue

        entry = c["close"]
        risk = 1.5 * atr[i]
        if side == "BUY":
            stop, tp1, tp2 = entry - risk, entry + risk, entry + 2 * risk
        else:
            stop, tp1, tp2 = entry + risk, entry - risk, entry - 2 * risk

        first_hit = False
        second_hit = False
        stop_hit = False
        exit_index = i + horizon
        for j in range(i + 1, i + horizon + 1):
            bar = candles[j]
            if side == "BUY":
                sl_touched = bar["low"] <= stop
                tp1_touched = bar["high"] >= tp1
                tp2_touched = bar["high"] >= tp2
            else:
                sl_touched = bar["high"] >= stop
                tp1_touched = bar["low"] <= tp1
                tp2_touched = bar["low"] <= tp2

            # Conservative intrabar assumption: if stop and a target share a candle,
            # assume the stop was reached first.
            if sl_touched:
                stop_hit = True
                exit_index = j
                break
            if tp2_touched:
                first_hit = True
                second_hit = True
                exit_index = j
                break
            if tp1_touched:
                first_hit = True
            if first_hit and j == i + horizon:
                exit_index = j

        trades.append({
            "tp1": first_hit,
            "tp2": second_hit,
            "sl_before_tp1": stop_hit and not first_hit,
            "stop_after_tp1": stop_hit and first_hit,
        })
        next_allowed = exit_index + 1

    if not trades:
        return (
            f"📊 اختبار تاريخي XAU/USD — 30 دقيقة\n"
            f"الشموع المستخدمة: {len(candles)}\n"
            "لم تظهر فرص مطابقة لشروط الاستراتيجية في هذه العينة. هذا لا يثبت وجود عطل."
        )

    total = len(trades)
    tp1_count = sum(t["tp1"] for t in trades)
    tp2_count = sum(t["tp2"] for t in trades)
    sl_before = sum(t["sl_before_tp1"] for t in trades)
    sl_after = sum(t["stop_after_tp1"] for t in trades)
    neither = total - tp1_count - sl_before
    start = candles[0]["timestamp"]
    end = candles[-horizon - 1]["timestamp"]
    from datetime import datetime, timezone
    start_text = datetime.fromtimestamp(start, timezone.utc).strftime("%Y-%m-%d")
    end_text = datetime.fromtimestamp(end, timezone.utc).strftime("%Y-%m-%d")

    return (
        "📊 نتيجة الاختبار التاريخي — XAU/USD\n"
        "الإطار: 30 دقيقة | أفق الصفقة: 48 شمعة (حتى 24 ساعة)\n"
        f"الفترة التقريبية: {start_text} إلى {end_text}\n"
        f"عدد الصفقات غير المتداخلة: {total}\n"
        f"وصل TP1: {tp1_count} ({tp1_count / total:.1%})\n"
        f"وصل TP2: {tp2_count} ({tp2_count / total:.1%})\n"
        f"وقف قبل TP1: {sl_before} ({sl_before / total:.1%})\n"
        f"وقف بعد TP1 وقبل TP2: {sl_after}\n"
        f"لم يصل هدف أو وقف خلال 24 ساعة: {neither}\n\n"
        "تنبيه: اختبار أولي فقط؛ لا يحتسب السبريد أو الانزلاق أو العمولة، "
        "وعند لمس الوقف والهدف في الشمعة نفسها يفترض الوقف أولاً. النتائج السابقة لا تضمن المستقبل."
    )
