import asyncio
import hmac
import hashlib
import math
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field, ValidationError

from app.market_engine import analyze_latest_closed_candle, fetch_candles
from app.trade_tracker import evaluate_trade_candle

load_dotenv()

APP_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.getenv("DATABASE_PATH", str(APP_DIR.parent / "data" / "signals.db")))
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="Gold AI Assistant", version="0.4.0")
LATEST = {"status": "waiting", "message": "لا توجد إشارات بعد", "updated": None}
HISTORY = []
MAX_HISTORY = 200


class Signal(BaseModel):
    symbol: str = Field(default="XAUUSD", min_length=3, max_length=20)
    side: str = Field(min_length=3, max_length=4)
    entry: float = Field(gt=0)
    stop_loss: float = Field(gt=0)
    take_profit: float = Field(gt=0)
    take_profit_1: Optional[float] = Field(default=None, gt=0)
    timeframe: str = Field(default="5m", min_length=1, max_length=12)
    reason: str = Field(default="إشارة من استراتيجية TradingView", max_length=500)
    confidence: Optional[int] = Field(default=None, ge=0, le=100)

    class Config:
        extra = "forbid"


def _db():
    return sqlite3.connect(DB_PATH, timeout=15)


def init_db():
    with closing(_db()) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL, side TEXT NOT NULL,
                entry REAL NOT NULL, stop_loss REAL NOT NULL,
                take_profit REAL NOT NULL, take_profit_1 REAL, timeframe TEXT NOT NULL,
                reason TEXT NOT NULL, rr REAL NOT NULL,
                updated TEXT NOT NULL, telegram INTEGER NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS processed_candles (
                symbol TEXT NOT NULL, timeframe TEXT NOT NULL,
                candle_at TEXT NOT NULL,
                PRIMARY KEY (symbol, timeframe, candle_at)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS notification_outbox (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                signal_id INTEGER NOT NULL,
                symbol TEXT NOT NULL,
                timeframe TEXT NOT NULL,
                candle_at TEXT NOT NULL,
                message TEXT NOT NULL,
                sent INTEGER NOT NULL DEFAULT 0,
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                created_at TEXT NOT NULL,
                sent_at TEXT,
                UNIQUE(symbol, timeframe, candle_at)
            )
        """)
        columns = {row[1] for row in conn.execute("PRAGMA table_info(signals)").fetchall()}
        migrations = {
            "take_profit_1": "REAL",
            "candle_at": "TEXT",
            "status": "TEXT NOT NULL DEFAULT 'UNTRACKED'",
            "tp1_hit": "INTEGER NOT NULL DEFAULT 0",
            "last_checked_candle": "INTEGER",
            "exit_price": "REAL",
            "closed_at": "TEXT",
        }
        for column, definition in migrations.items():
            if column not in columns:
                conn.execute(f"ALTER TABLE signals ADD COLUMN {column} {definition}")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS trade_event_outbox (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                signal_id INTEGER NOT NULL,
                event_key TEXT NOT NULL,
                message TEXT NOT NULL,
                sent INTEGER NOT NULL DEFAULT 0,
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                created_at TEXT NOT NULL,
                sent_at TEXT,
                UNIQUE(signal_id, event_key)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS telegram_chats (
                chat_id TEXT PRIMARY KEY,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            )
        """)
        conn.commit()


def save_telegram_chat(chat_id: str):
    with closing(_db()) as conn:
        conn.execute("""
            INSERT INTO telegram_chats (chat_id, active, created_at)
            VALUES (?, 1, ?)
            ON CONFLICT(chat_id) DO UPDATE SET active=1
        """, (str(chat_id), datetime.now(timezone.utc).isoformat()))
        conn.commit()


def load_history():
    global HISTORY, LATEST
    with closing(_db()) as conn:
        rows = conn.execute("""
            SELECT symbol, side, entry, stop_loss, take_profit, take_profit_1,
                   timeframe, reason, rr, updated, telegram
            FROM signals ORDER BY id DESC LIMIT ?
        """, (MAX_HISTORY,)).fetchall()
    HISTORY = [
        dict(zip(("symbol", "side", "entry", "stop_loss", "take_profit", "take_profit_1",
                  "timeframe", "reason", "rr", "updated", "telegram"), row))
        for row in rows
    ]
    if HISTORY:
        item = HISTORY[0]
        LATEST = {"status": "signal", "message": f'{item["side"]} XAU/USD', **item}


init_db()
load_history()

ENGINE_STATE = {
    "status": "starting",
    "message": "يتم تهيئة محرك تحليل الذهب",
    "source": "Twelve Data XAU/USD spot (Gold Spot / US Dollar)",
    "timeframe": "30m",
    "price": None,
    "last_candle": None,
    "last_checked": None,
    "last_error": None,
    "indicators": {},
}
ENGINE_TASK = None
ENGINE_POLL_SECONDS = max(120, min(900, int(os.getenv("ENGINE_POLL_SECONDS", "180"))))


def validate_signal(s: Signal) -> float:
    if s.symbol.upper().replace("/", "").replace(" ", "") not in ("XAUUSD", "GOLD"):
        raise HTTPException(400, "هذا المشروع مخصص للذهب XAU/USD فقط")

    side = s.side.upper()
    if side not in ("BUY", "SELL"):
        raise HTTPException(400, "side must be BUY or SELL")

    prices = (s.entry, s.stop_loss, s.take_profit) + ((s.take_profit_1,) if s.take_profit_1 is not None else ())
    if not all(math.isfinite(v) and v > 0 for v in prices):
        raise HTTPException(400, "يجب أن تكون جميع الأسعار أرقاماً موجبة وصالحة")

    if side == "BUY" and not (s.stop_loss < s.entry < s.take_profit):
        raise HTTPException(400, "ترتيب أسعار الشراء غير صحيح")
    if side == "SELL" and not (s.take_profit < s.entry < s.stop_loss):
        raise HTTPException(400, "ترتيب أسعار البيع غير صحيح")

    if s.take_profit_1 is not None:
        if side == "BUY" and not (s.entry < s.take_profit_1 < s.take_profit):
            raise HTTPException(400, "ترتيب أهداف الشراء غير صحيح")
        if side == "SELL" and not (s.take_profit < s.take_profit_1 < s.entry):
            raise HTTPException(400, "ترتيب أهداف البيع غير صحيح")

    risk = abs(s.entry - s.stop_loss)
    reward = abs(s.take_profit - s.entry)
    if risk <= 0:
        raise HTTPException(400, "مسافة وقف الخسارة غير صالحة")
    rr = reward / risk
    if not math.isfinite(rr) or rr < 2:
        raise HTTPException(400, "تم رفض الإشارة: العائد إلى المخاطرة أقل من 1:2")
    return rr


def configure_telegram_webhook():
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        return False, "TELEGRAM_BOT_TOKEN is missing"
    url = os.getenv("TELEGRAM_WEBHOOK_URL", "https://gold-ai-assistant.onrender.com/telegram/webhook").strip()
    try:
        response = requests.post(
            f"https://api.telegram.org/bot{token}/setWebhook",
            json={"url": url, "allowed_updates": ["message"], "drop_pending_updates": False, "secret_token": hashlib.sha256(token.encode()).hexdigest()},
            timeout=15,
        )
        response.raise_for_status()
        data = response.json()
        return bool(data.get("ok")), data.get("description", "webhook configured")
    except (requests.RequestException, ValueError) as exc:
        return False, str(exc)[:250]


@app.post("/telegram/webhook")
async def telegram_webhook(request: Request):
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    expected_secret = hashlib.sha256(token.encode()).hexdigest() if token else ""
    supplied_secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not token or not hmac.compare_digest(supplied_secret, expected_secret):
        raise HTTPException(503, "Telegram webhook authentication failed")
    try:
        update = await request.json()
        message = update.get("message") or {}
        chat = message.get("chat") or {}
        chat_id = str(chat.get("id", ""))
        # Telegram chat IDs are learned from incoming updates; do not require a preconfigured chat ID for /start.
        if not chat_id:
            return {"ok": True, "ignored": True}
        text = str(message.get("text", "")).strip()
        command = text.split()[0].split("@")[0].lower() if text else ""
        if command == "/start":
            save_telegram_chat(chat_id)
            reply = (
                "مرحباً بك في Gold AI Assistant 🟡\n"
                "تم ربط المحادثة بنجاح. ستصلك هنا إشعارات إشارات الذهب عند توفرها.\n"
                "الأوامر المتاحة: /status و /help"
            )
        elif command == "/help":
            reply = "هذا البوت يرسل إشعارات تحليل الذهب فقط، ولا ينفذ صفقات. الأوامر: /status للحالة، /backtest للاختبار التاريخي، /testalert لاختبار التنبيه."
        elif command == "/status":
            state = ENGINE_STATE.get("status", "unknown")
            detail = ENGINE_STATE.get("message", "")
            reply = f"حالة محرك الذهب: {state}\n{detail}\nالمصدر: {ENGINE_STATE.get('source', 'غير متاح')}."
        elif text.lower() == "test":
            reply = "تم اختبار اتصال Telegram بنجاح ✅"
        elif command == "/testalert":
            test_sent, test_note = telegram_send(
                "🧪 اختبار تجريبي لإشعارات Gold AI Assistant\n"
                "هذه رسالة اختبار فقط وليست توصية تداول.\n"
                "لا توجد صفقة حقيقية أو دخول أو أهداف في هذا الاختبار."
            )
            reply = (
                "✅ تم إرسال إشعار الاختبار إلى المحادثات المسجلة."
                if test_sent else f"❌ تعذر إرسال إشعار الاختبار: {test_note}"
            )
        elif command == "/backtest":
            try:
                from app.backtest import run_backtest_report
                reply = await asyncio.to_thread(run_backtest_report)
            except Exception as exc:
                reply = f"⚠️ تعذر إكمال الاختبار التاريخي: {str(exc)[:250]}"
        else:
            return {"ok": True, "ignored": True}
        sent, note = telegram_send_to_chat(chat_id, reply)
        if not sent:
            raise HTTPException(502, f"Telegram send failed: {note}")
        return {"ok": True}
    except HTTPException:
        raise
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(400, "Invalid Telegram update")


def telegram_send_to_chat(chat_id: str, message: str):
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        return False, "TELEGRAM_BOT_TOKEN is missing"
    try:
        response = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": message},
            timeout=10,
        )
        response.raise_for_status()
        data = response.json()
        return (True, "sent") if data.get("ok") else (False, "Telegram rejected message")
    except (requests.RequestException, ValueError) as exc:
        return False, str(exc)[:250]


def telegram_send(message: str):
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        return False, "TELEGRAM_BOT_TOKEN is missing"

    with closing(_db()) as conn:
        rows = conn.execute(
            "SELECT chat_id FROM telegram_chats WHERE active=1"
        ).fetchall()
    chat_ids = [str(row[0]) for row in rows]

    # Optional fallback for an explicitly configured chat ID.
    if not chat_ids:
        fallback = os.getenv("TELEGRAM_CHAT_ID", "").strip()
        if fallback:
            chat_ids = [fallback]
    if not chat_ids:
        return False, "No Telegram chats registered; send /start to the bot."

    results = [telegram_send_to_chat(chat_id, message) for chat_id in chat_ids]
    successful = sum(1 for sent, _ in results if sent)
    if successful == len(chat_ids):
        return True, f"sent to {successful} chat(s)"
    errors = [note for sent, note in results if not sent]
    return False, "; ".join(errors)[:250]


def _signal_message(signal: Signal, rr: float, source: str):
    return (
        "🟡 GOLD AI ASSISTANT — تحليل مستقل\n"
        f"إشارة {signal.side.upper()} — XAU/USD\n"
        f"الإطار: {signal.timeframe}\n"
        f"الدخول المرجعي: {signal.entry}\n"
        f"وقف الخسارة: {signal.stop_loss}\n"
        f"TP1: {signal.take_profit_1 if signal.take_profit_1 is not None else 'غير محدد'}\n"
        f"TP2: {signal.take_profit}\n"
        f"العائد/المخاطرة: 1:{rr:.2f}\n"
        f"السبب: {signal.reason}\n"
        f"المصدر: {source}\n"
        "تنبيه: الإشارة تحليلية وليست ضماناً للربح؛ تحقق من السعر الحالي والسبريد قبل التنفيذ."
    )


def persist_engine_result(candle_at: str, signal: Optional[Signal], rr: Optional[float], source: str):
    """Atomically claim a candle and, when applicable, persist its signal and notification."""
    with closing(_db()) as conn:
        conn.execute("BEGIN IMMEDIATE")
        claimed = conn.execute(
            "INSERT OR IGNORE INTO processed_candles(symbol,timeframe,candle_at) VALUES(?,?,?)",
            ("XAUUSD", "30m", candle_at),
        )
        if claimed.rowcount == 0:
            conn.commit()
            return False

        if signal is not None and rr is not None:
            now = datetime.now(timezone.utc).isoformat()
            message = _signal_message(signal, rr, source)
            cursor = conn.execute("""
                INSERT INTO signals
                (symbol, side, entry, stop_loss, take_profit, take_profit_1, timeframe,
                 reason, rr, updated, telegram, candle_at, status, tp1_hit)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, 'OPEN', 0)
            """, (
                "XAUUSD", signal.side.upper(), signal.entry, signal.stop_loss,
                signal.take_profit, signal.take_profit_1, signal.timeframe,
                signal.reason, rr, now, candle_at,
            ))
            conn.execute("""
                INSERT OR IGNORE INTO notification_outbox
                (signal_id, symbol, timeframe, candle_at, message, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (cursor.lastrowid, "XAUUSD", "30m", candle_at, message, now))
        conn.commit()
        return True


def track_open_trades(candles, now=None):
    """Evaluate only completed candles and queue each trade-state alert once."""
    now = datetime.now(timezone.utc).timestamp() if now is None else now
    completed = sorted(
        [bar for bar in candles if bar["timestamp"] + 30 * 60 <= now],
        key=lambda bar: bar["timestamp"],
    )
    with closing(_db()) as conn:
        rows = conn.execute("""
            SELECT id, side, entry, stop_loss, take_profit_1, take_profit,
                   candle_at, status, tp1_hit, last_checked_candle
            FROM signals WHERE status IN ('OPEN', 'TP1_HIT') AND candle_at IS NOT NULL
        """).fetchall()

    for row in rows:
        signal_id, side, entry, stop, tp1, tp2, candle_at, status, tp1_hit, last_checked = row
        try:
            signal_ts = datetime.fromisoformat(candle_at.replace("Z", "+00:00")).timestamp()
        except (TypeError, ValueError):
            continue
        current_status = status
        current_tp1 = bool(tp1_hit)
        last_ts = last_checked or int(signal_ts)
        for bar in completed:
            ts = bar["timestamp"]
            if ts <= signal_ts or ts <= last_ts:
                continue
            new_status, new_tp1, events, exit_price = evaluate_trade_candle(
                side, entry, stop, tp1, tp2, current_tp1, bar
            )
            with closing(_db()) as conn:
                conn.execute("""
                    UPDATE signals SET status=?, tp1_hit=?, last_checked_candle=?,
                        exit_price=COALESCE(?, exit_price),
                        closed_at=CASE WHEN ? IN ('TP2_HIT','STOP_LOSS','BREAKEVEN') THEN ? ELSE closed_at END
                    WHERE id=?
                """, (
                    new_status, int(new_tp1), ts, exit_price, new_status,
                    datetime.now(timezone.utc).isoformat(), signal_id,
                ))
                for event in events:
                    labels = {
                        "TP1": f"🎯 تحقق الهدف الأول TP1 للصفقة #{signal_id}. تم إغلاق 50% ونقل الوقف إلى نقطة الدخول.",
                        "TP2": f"🏁 تحقق الهدف الثاني TP2 للصفقة #{signal_id}. اكتملت الصفقة.",
                        "STOP_LOSS": f"🛑 تم ضرب وقف الخسارة للصفقة #{signal_id}. أُغلقت الصفقة.",
                        "BREAKEVEN": f"↩️ عادت الصفقة #{signal_id} إلى نقطة الدخول بعد TP1. أُغلقت الكمية المتبقية دون ربح/خسارة سعرية.",
                    }
                    event_key = f"{event}:{ts}"
                    conn.execute("""
                        INSERT OR IGNORE INTO trade_event_outbox
                        (signal_id, event_key, message, created_at)
                        VALUES (?, ?, ?, ?)
                    """, (signal_id, event_key, labels[event], datetime.now(timezone.utc).isoformat()))
                conn.commit()
            current_status, current_tp1, last_ts = new_status, new_tp1, ts
            if current_status not in ("OPEN", "TP1_HIT"):
                break
            if ts - signal_ts >= 48 * 30 * 60:
                current_status = "EXPIRED"
                with closing(_db()) as conn:
                    conn.execute("""
                        UPDATE signals SET status='EXPIRED', closed_at=?
                        WHERE id=? AND status IN ('OPEN','TP1_HIT')
                    """, (datetime.now(timezone.utc).isoformat(), signal_id))
                    conn.execute("""
                        INSERT OR IGNORE INTO trade_event_outbox
                        (signal_id, event_key, message, created_at)
                        VALUES (?, ?, ?, ?)
                    """, (
                        signal_id, f"EXPIRED:{ts}",
                        f"⌛ انتهت صلاحية الصفقة #{signal_id} بعد 24 ساعة دون وصولها إلى الوقف أو الأهداف.",
                        datetime.now(timezone.utc).isoformat(),
                    ))
                    conn.commit()
                break


def deliver_pending_trade_events(limit: int = 20):
    with closing(_db()) as conn:
        pending = conn.execute("""
            SELECT id, message FROM trade_event_outbox
            WHERE sent=0 ORDER BY id LIMIT ?
        """, (limit,)).fetchall()
    for event_id, message in pending:
        sent, note = telegram_send(message)
        with closing(_db()) as conn:
            conn.execute("""
                UPDATE trade_event_outbox SET sent=?, attempts=attempts+1,
                    last_error=?, sent_at=? WHERE id=? AND sent=0
            """, (
                int(sent), None if sent else note,
                datetime.now(timezone.utc).isoformat() if sent else None,
                event_id,
            ))
            conn.commit()


def deliver_pending_notifications(limit: int = 10):
    with closing(_db()) as conn:
        # Never send alerts for a candle that is no longer actionable.
        conn.execute("""
            UPDATE notification_outbox
            SET sent=1, last_error='expired: market candle older than 90 minutes'
            WHERE sent=0 AND datetime(candle_at) < datetime('now', '-90 minutes')
        """)
        conn.commit()
        pending = conn.execute("""
            SELECT id, signal_id, message FROM notification_outbox
            WHERE sent=0 AND datetime(candle_at) >= datetime('now', '-90 minutes')
            ORDER BY id LIMIT ?
        """, (limit,)).fetchall()

    for outbox_id, signal_id, message in pending:
        sent, note = telegram_send(message)
        with closing(_db()) as conn:
            conn.execute("""
                UPDATE notification_outbox
                SET sent=?, attempts=attempts+1, last_error=?, sent_at=?
                WHERE id=? AND sent=0
            """, (
                int(sent), None if sent else note,
                datetime.now(timezone.utc).isoformat() if sent else None,
                outbox_id,
            ))
            if sent:
                conn.execute("UPDATE signals SET telegram=1 WHERE id=?", (signal_id,))
            conn.commit()


def run_engine_once():
    global ENGINE_STATE
    candles = fetch_candles()
    snapshot, raw_signal = analyze_latest_closed_candle(candles)
    ENGINE_STATE = {
        "status": "online",
        "message": "محرك التحليل يعمل — بانتظار تحقق شروط الإشارة" if not raw_signal else f"تم رصد إشارة {raw_signal['side']}",
        "source": snapshot["source"],
        "timeframe": snapshot["timeframe"],
        "price": snapshot["price"],
        "last_candle": snapshot["candle_at"],
        "last_checked": snapshot["checked_at"],
        "last_error": None,
        "indicators": {k: snapshot[k] for k in ("ema20", "ema50", "ema200", "rsi14", "atr14")},
    }

    signal = Signal(**raw_signal) if raw_signal else None
    rr = validate_signal(signal) if signal else None
    persist_engine_result(snapshot["candle_at"], signal, rr, snapshot["source"])
    track_open_trades(candles)
    load_history()
    deliver_pending_notifications()
    deliver_pending_trade_events()


async def engine_loop():
    while True:
        try:
            await asyncio.to_thread(run_engine_once)
        except Exception as exc:
            ENGINE_STATE.update({
                "status": "error",
                "message": "تعذر تحديث بيانات الذهب — سيُعاد المحاولة تلقائياً",
                "last_error": str(exc)[:300],
                "last_checked": datetime.now(timezone.utc).isoformat(),
            })
            # Retry the durable notification queue even while market data is unavailable.
            try:
                await asyncio.to_thread(deliver_pending_notifications)
                await asyncio.to_thread(deliver_pending_trade_events)
            except Exception:
                pass
        await asyncio.sleep(ENGINE_POLL_SECONDS)


@app.on_event("startup")
async def start_engine():
    global ENGINE_TASK
    webhook_ok, webhook_note = await asyncio.to_thread(configure_telegram_webhook)
    print(f"Telegram webhook setup: {'ok' if webhook_ok else 'failed'} - {webhook_note}")
    ENGINE_TASK = asyncio.create_task(engine_loop())


@app.on_event("shutdown")
async def stop_engine():
    if ENGINE_TASK:
        ENGINE_TASK.cancel()
        try:
            await ENGINE_TASK
        except asyncio.CancelledError:
            pass


def save_signal(item: dict):
    with closing(_db()) as conn:
        conn.execute("""
            INSERT INTO signals
            (symbol, side, entry, stop_loss, take_profit, take_profit_1, timeframe,
             reason, rr, updated, telegram)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            item["symbol"], item["side"], item["entry"], item["stop_loss"],
            item["take_profit"], item.get("take_profit_1"), item["timeframe"], item["reason"],
            item["rr"], item["updated"], int(item["telegram"])
        ))
        conn.commit()


@app.get("/", response_class=HTMLResponse)
def dashboard():
    path = APP_DIR / "dashboard.html"
    if not path.is_file():
        raise HTTPException(500, "ملف لوحة التحكم غير موجود")
    return HTMLResponse(path.read_text(encoding="utf-8"))


@app.get("/api/status")
def status():
    latest = LATEST
    if ENGINE_STATE["status"] != "online":
        latest = {
            "status": "waiting",
            "message": "لا توجد إشارة حديثة؛ بيانات السوق قديمة أو السوق مغلق",
        }
    return {
        "latest": latest,
        "history": HISTORY[:10],
        "mode": "autonomous_signals_only",
        "engine": ENGINE_STATE,
        "notifications_pending": pending_notification_count(),
    }


def pending_notification_count():
    with closing(_db()) as conn:
        row = conn.execute("SELECT COUNT(*) FROM notification_outbox WHERE sent=0").fetchone()
    return int(row[0])


@app.post("/webhook/{token}")
async def tradingview_webhook(token: str, request: Request):
    expected = os.getenv("WEBHOOK_TOKEN", "").strip()
    if not expected or not hmac.compare_digest(token, expected):
        raise HTTPException(401, "Unauthorized")

    content_length = request.headers.get("content-length")
    if content_length and content_length.isdigit() and int(content_length) > 16_384:
        raise HTTPException(413, "Payload too large")

    try:
        payload = await request.json()
        if not isinstance(payload, dict):
            raise ValueError("JSON object required")
        signal = Signal(**payload)
    except (ValueError, TypeError, ValidationError):
        raise HTTPException(400, "JSON غير صالح أو حقول الإشارة ناقصة أو غير مدعومة")

    rr = validate_signal(signal)
    now = datetime.now(timezone.utc).isoformat()
    side = signal.side.upper()
    sent, note = telegram_send(_signal_message(signal, rr, "TradingView webhook"))
    item = {
        "symbol": signal.symbol.upper().replace("/", ""),
        "side": side, "entry": signal.entry,
        "stop_loss": signal.stop_loss, "take_profit": signal.take_profit,
        "take_profit_1": signal.take_profit_1,
        "timeframe": signal.timeframe, "reason": signal.reason,
        "rr": round(rr, 2), "updated": now, "telegram": sent,
    }
    save_signal(item)
    HISTORY.insert(0, item)
    del HISTORY[MAX_HISTORY:]
    LATEST.clear()
    LATEST.update({"status": "signal", "message": f"{side} XAU/USD", **item})
    return JSONResponse({
        "accepted": True, "telegram_sent": sent,
        "telegram_status": note, "signal": item
    })


@app.get("/health")
def health():
    return {
        "ok": True,
        "service": "Gold AI Assistant",
        "version": "0.3.0",
        "engine": ENGINE_STATE["status"],
        "notifications_pending": pending_notification_count(),
    }
