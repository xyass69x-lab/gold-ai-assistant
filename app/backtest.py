"""Conservative XAU/USD 30m backtest with next-bar entries and R-multiple metrics."""
import time
from datetime import datetime, timezone

from app.market_engine import TIMEFRAME_SECONDS, _atr, _ema, _rsi, fetch_candles


def _simulate_trade(candles, entry_index, side, entry, risk, horizon):
    """50% exits at TP1, 50% at TP2; stop-first when both touch in one bar."""
    if risk <= 0:
        return None

    if side == "BUY":
        stop, tp1, tp2 = entry - risk, entry + risk, entry + 2 * risk
    else:
        stop, tp1, tp2 = entry + risk, entry - risk, entry - 2 * risk

    tp1_hit = False
    tp2_hit = False
    stop_after_tp1 = False
    last_index = min(entry_index + horizon, len(candles) - 1)

    for j in range(entry_index, last_index + 1):
        bar = candles[j]
        if side == "BUY":
            sl_touched = bar["low"] <= stop
            tp1_touched = bar["high"] >= tp1
            tp2_touched = bar["high"] >= tp2
        else:
            sl_touched = bar["high"] >= stop
            tp1_touched = bar["low"] <= tp1
            tp2_touched = bar["low"] <= tp2

        # OHLC candles cannot reveal intrabar order; assume adverse stop first.
        if sl_touched:
            if tp1_hit:
                stop_after_tp1 = True
                gross_r = 0.0  # +1R on half, -1R on half
            else:
                gross_r = -1.0
            return {
                "gross_r": gross_r, "tp1": tp1_hit, "tp2": False,
                "stop_before_tp1": not tp1_hit,
                "stop_after_tp1": stop_after_tp1, "exit_index": j,
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

    # Close remaining exposure at the final close after the 24h horizon.
    final_close = candles[last_index]["close"]
    direction = 1.0 if side == "BUY" else -1.0
    remaining_r = ((final_close - entry) * direction) / risk
    gross_r = 0.5 * 1.0 + 0.5 * remaining_r if tp1_hit else remaining_r
    return {
        "gross_r": gross_r, "tp1": tp1_hit, "tp2": False,
        "stop_before_tp1": False, "stop_after_tp1": False,
        "exit_index": last_index, "time_exit": True,
    }


def run_backtest_report():
    candles = fetch_candles()
    now = time.time()
    candles = [
        c for c in sorted(candles, key=lambda x: x["timestamp"])
        if c["timestamp"] + TIMEFRAME_SECONDS <= now
    ]
    horizon = 48
    if len(candles) < 220 + horizon + 1:
        return f"⚠️ البيانات التاريخية غير كافية: {len(candles)} شمعة مغلقة."

    closes = [c["close"] for c in candles]
    ema20, ema50, ema200 = _ema(closes, 20), _ema(closes, 50), _ema(closes, 200)
    rsi, atr = _rsi(closes, 14), _atr(candles, 14)

    trades = []
    next_allowed = 200
    # Signal is confirmed at candle close; execution is modelled at next candle open.
    for i in range(199, len(candles) - horizon - 1):
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

        entry_index = i + 1
        entry = candles[entry_index]["open"]
        risk = 1.5 * atr[i]
        result = _simulate_trade(candles, entry_index, side, entry, risk, horizon)
        if result is None:
            continue
        result["side"] = side
        trades.append(result)
        next_allowed = result["exit_index"] + 1

    if not trades:
        return (
            "📊 اختبار XAU/USD — 30 دقيقة\n"
            f"الشموع المغلقة: {len(candles)}\n"
            "لم تظهر فرص مطابقة للشروط في العينة."
        )

    total = len(trades)
    wins = sum(t["gross_r"] > 0 for t in trades)
    losses = sum(t["gross_r"] < 0 for t in trades)
    breakeven = total - wins - losses
    tp1_count = sum(t["tp1"] for t in trades)
    tp2_count = sum(t["tp2"] for t in trades)
    sl_before = sum(t["stop_before_tp1"] for t in trades)
    sl_after = sum(t["stop_after_tp1"] for t in trades)
    total_r = sum(t["gross_r"] for t in trades)
    expectancy = total_r / total
    gross_profit = sum(t["gross_r"] for t in trades if t["gross_r"] > 0)
    gross_loss = -sum(t["gross_r"] for t in trades if t["gross_r"] < 0)
    profit_factor = gross_profit / gross_loss if gross_loss else None

    equity = peak = max_drawdown = 0.0
    for trade in trades:
        equity += trade["gross_r"]
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)

    buy_count = sum(t["side"] == "BUY" for t in trades)
    sell_count = total - buy_count
    start = datetime.fromtimestamp(candles[0]["timestamp"], timezone.utc).strftime("%Y-%m-%d")
    end = datetime.fromtimestamp(candles[-horizon - 1]["timestamp"], timezone.utc).strftime("%Y-%m-%d")
    pf_text = f"{profit_factor:.2f}" if profit_factor is not None else "غير محدود (لا توجد خسائر إجمالية)"

    return (
        "📊 تقرير الاختبار المحسّن — XAU/USD\n"
        "الإطار: 30 دقيقة | الدخول: افتتاح الشمعة التالية | أفق: 24 ساعة\n"
        f"الفترة التقريبية: {start} إلى {end}\n"
        f"الصفقات: {total} | شراء: {buy_count} | بيع: {sell_count}\n"
        f"رابحة: {wins} ({wins/total:.1%}) | خاسرة: {losses} ({losses/total:.1%}) | تعادل: {breakeven}\n"
        f"وصل TP1: {tp1_count} ({tp1_count/total:.1%}) | وصل TP2: {tp2_count} ({tp2_count/total:.1%})\n"
        f"وقف قبل TP1: {sl_before} | وقف بعد TP1: {sl_after}\n"
        f"صافي النتيجة الإجمالية: {total_r:+.2f}R | متوسط الصفقة: {expectancy:+.3f}R\n"
        f"عامل الربح: {pf_text} | أقصى تراجع: {max_drawdown:.2f}R\n\n"
        "طريقة الحساب: إغلاق 50% عند TP1 و50% عند TP2؛ إذا وصل الوقف بعد TP1 تُحسب المحصلة 0R. "
        "إذا انتهت 24 ساعة يُغلق المتبقي على إغلاق آخر شمعة. الدخول بافتتاح الشمعة التالية.\n"
        "النتائج إجمالية قبل السبريد والانزلاق والعمولات، والعينة التاريخية محدودة ولا تضمن المستقبل."
    )
