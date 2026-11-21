import asyncio
import hmac
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

load_dotenv()

APP_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.getenv("DATABASE_PATH", str(APP_DIR.parent / "data" / "signals.db")))
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="Gold AI Assistant", version="0.2.0")
LATEST = {"status": "waiting", "message": "لا توجد إشارات بعد", "updated": None}
HISTORY = []
MAX_HISTORY = 200


class Signal(BaseModel):
    symbol: str = Field(default="XAUUSD", min_length=3, max_length=20)
    side: str = Field(min_length=3, max_length=4)
    entry: float = Field(gt=0)
    stop_loss: float = Field(gt=0)
    take_profit: float = Field(gt=0)  # TP2 (RR >= 1:2)
    take_profit_1: Optional[float] = Field(default=None, gt=0)  # TP1 (RR 1:1)
    timeframe: str = Field(default="5m", min_length=1, max_length=12)
    reason: str = Field(default="إشارة من استراتيجية TradingView", max_length=500)
    confidence: Optional[int] = Field(default=None, ge=0, le=100)

    class Config:
        extra = "forbid"


def _db():
    return sqlite3.connect(DB_PATH, timeout=10)


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
        columns = {row[1] for row in conn.execute("PRAGMA table_info(signals)").fetchall()}
        if "take_profit_1" not in columns:
            conn.execute("ALTER TABLE signals ADD COLUMN take_profit_1 REAL")
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
    "source": "Yahoo Finance chart (unofficial)",
    "timeframe": "30m",
    "price": None,
    "last_candle": None,
    "last_checked": None,
    "last_error": None,
}
ENGINE_TASK = None
ENGINE_POLL_SECONDS = max(120, int(os.getenv("ENGINE_POLL_SECONDS", "180")))


def candle_was_processed(candle_at: str) -> bool:
    with closing(_db()) as conn:
        row = conn.execute(
            "SELECT 1 FROM processed_candles WHERE symbol=? AND timeframe=? AND candle_at=?",
            ("XAUUSD", "30m", candle_at),
        ).fetchone()
    return row is not None


def mark_candle_processed(candle_at: str):
    with closing(_db()) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO processed_candles(symbol,timeframe,candle_at) VALUES(?,?,?)",
            ("XAUUSD", "30m", candle_at),
        )
        conn.commit()


def run_engine_once():
    global ENGINE_STATE, HISTORY, LATEST
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
    candle_at = snapshot["candle_at"]
    if candle_was_processed(candle_at):
        return

    if not raw_signal:
        mark_candle_processed(candle_at)
        return

    signal = Signal(**raw_signal)
    rr = validate_signal(signal)
    now = datetime.now(timezone.utc).isoformat()
    message = (
        "🟡 GOLD AI ASSISTANT — تحليل مستقل\n"
        f"إشارة {signal.side} — XAU/USD\n"
        f"الإطار: {signal.timeframe}\n"
        f"الدخول: {signal.entry}\nوقف الخسارة: {signal.stop_loss}\n"
        f"TP1: {signal.take_profit_1 if signal.take_profit_1 is not None else 'غير محدد'}\n"
        f"TP2: {signal.take_profit}\nالعائد/المخاطرة: 1:{rr:.2f}\n"
        f"السبب: {signal.reason}\nالمصدر: Yahoo Finance (غير رسمي)"
    )
    sent, _ = telegram_send(message)
    item = {
        "symbol": "XAUUSD", "side": signal.side, "entry": signal.entry,
        "stop_loss": signal.stop_loss, "take_profit": signal.take_profit,
        "take_profit_1": signal.take_profit_1,
        "timeframe": signal.timeframe, "reason": signal.reason,
        "rr": round(rr, 2), "updated": now, "telegram": sent,
    }
    save_signal(item)
    HISTORY.insert(0, item)
    del HISTORY[MAX_HISTORY:]
    LATEST.clear()
    LATEST.update({"status": "signal", "message": f"{signal.side} XAU/USD", **item})
    mark_candle_processed(candle_at)


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
        await asyncio.sleep(ENGINE_POLL_SECONDS)


@app.on_event("startup")
async def start_engine():
    global ENGINE_TASK
    ENGINE_TASK = asyncio.create_task(engine_loop())


@app.on_event("shutdown")
async def stop_engine():
    if ENGINE_TASK:
        ENGINE_TASK.cancel()
        try:
            await ENGINE_TASK
        except asyncio.CancelledError:
            pass


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


def telegram_send(message: str):
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        return False, "بيانات تلجرام غير مكتملة"
    try:
        response = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": message},
            timeout=8,
        )
        response.raise_for_status()
        data = response.json()
        if not data.get("ok"):
            return False, "رفض Telegram الرسالة"
        return True, "sent"
    except (requests.RequestException, ValueError):
        return False, "تعذر إرسال رسالة تلجرام"


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
    return {
        "latest": LATEST,
        "history": HISTORY[:10],
        "mode": "autonomous_signals_only",
        "engine": ENGINE_STATE,
    }


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
    message = (
        "🟡 GOLD AI ASSISTANT\n"
        f"إشارة {side} — {signal.symbol}\n"
        f"الإطار: {signal.timeframe}\n"
        f"الدخول: {signal.entry}\n"
        f"وقف الخسارة: {signal.stop_loss}\n"
        f"TP1: {signal.take_profit_1 if signal.take_profit_1 is not None else 'غير محدد'}\n"
        f"TP2: {signal.take_profit}\n"
        f"العائد/المخاطرة: 1:{rr:.2f}\n"
        f"السبب: {signal.reason}\n\n"
        "⚠️ تنبيه تحليلي فقط — راجع السعر والسبريد وحجم العقد في XTB قبل أي تنفيذ. لا يوجد ضمان للربح."
    )
    sent, note = telegram_send(message)
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
    return {"ok": True, "service": "Gold AI Assistant", "version": "0.3.0", "engine": ENGINE_STATE["status"]}
