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
    take_profit: float = Field(gt=0)
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
                take_profit REAL NOT NULL, timeframe TEXT NOT NULL,
                reason TEXT NOT NULL, rr REAL NOT NULL,
                updated TEXT NOT NULL, telegram INTEGER NOT NULL
            )
        """)
        conn.commit()


def load_history():
    global HISTORY, LATEST
    with closing(_db()) as conn:
        rows = conn.execute("""
            SELECT symbol, side, entry, stop_loss, take_profit,
                   timeframe, reason, rr, updated, telegram
            FROM signals ORDER BY id DESC LIMIT ?
        """, (MAX_HISTORY,)).fetchall()
    HISTORY = [
        dict(zip(("symbol", "side", "entry", "stop_loss", "take_profit",
                  "timeframe", "reason", "rr", "updated", "telegram"), row))
        for row in rows
    ]
    if HISTORY:
        item = HISTORY[0]
        LATEST = {"status": "signal", "message": f'{item["side"]} XAU/USD', **item}


init_db()
load_history()


def validate_signal(s: Signal) -> float:
    if s.symbol.upper().replace("/", "").replace(" ", "") not in ("XAUUSD", "GOLD"):
        raise HTTPException(400, "هذا المشروع مخصص للذهب XAU/USD فقط")

    side = s.side.upper()
    if side not in ("BUY", "SELL"):
        raise HTTPException(400, "side must be BUY or SELL")

    prices = (s.entry, s.stop_loss, s.take_profit)
    if not all(math.isfinite(v) and v > 0 for v in prices):
        raise HTTPException(400, "يجب أن تكون جميع الأسعار أرقاماً موجبة وصالحة")

    if side == "BUY" and not (s.stop_loss < s.entry < s.take_profit):
        raise HTTPException(400, "ترتيب أسعار الشراء غير صحيح")
    if side == "SELL" and not (s.take_profit < s.entry < s.stop_loss):
        raise HTTPException(400, "ترتيب أسعار البيع غير صحيح")

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
            (symbol, side, entry, stop_loss, take_profit, timeframe,
             reason, rr, updated, telegram)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            item["symbol"], item["side"], item["entry"], item["stop_loss"],
            item["take_profit"], item["timeframe"], item["reason"],
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
    return {"latest": LATEST, "history": HISTORY[:10], "mode": "signals_only"}


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
        f"الهدف: {signal.take_profit}\n"
        f"العائد/المخاطرة: 1:{rr:.2f}\n"
        f"السبب: {signal.reason}\n\n"
        "⚠️ تنبيه تحليلي فقط — راجع السعر والسبريد وحجم العقد في XTB قبل أي تنفيذ. لا يوجد ضمان للربح."
    )
    sent, note = telegram_send(message)
    item = {
        "symbol": signal.symbol.upper().replace("/", ""),
        "side": side, "entry": signal.entry,
        "stop_loss": signal.stop_loss, "take_profit": signal.take_profit,
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
    return {"ok": True, "service": "Gold AI Assistant", "version": "0.2.0"}
