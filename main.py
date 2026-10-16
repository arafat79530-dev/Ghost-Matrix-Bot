"""GHOST MATRIX V2.0 — cautious Telegram market-analysis bot.

Uses Twelve Data candles as an EXTERNAL data source. It does not connect to or
verify prices against Quotex. No trade outcome or win rate is fabricated.
"""
from __future__ import annotations

import base64
import json
import io
from contextlib import contextmanager
import logging
import os
import threading
import secrets
import hashlib
import smtplib
from email.message import EmailMessage
from functools import wraps
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from werkzeug.security import generate_password_hash, check_password_hash
import time
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from typing import Any, Optional

import requests
from flask import Flask, jsonify, request, send_from_directory, session
from PIL import Image, ImageDraw, ImageFont

try:
    import psycopg2
    from psycopg2.extras import RealDictCursor
except Exception:  # Database support is optional at import time; status explains if absent.
    psycopg2 = None
    RealDictCursor = None

# --------------------------- Configuration ---------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()
TWELVE_DATA_API_KEY = os.getenv("TWELVE_DATA_API_KEY", "").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
FLASK_SECRET_KEY = os.getenv("FLASK_SECRET_KEY", "").strip()
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "").strip().lower()
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
APP_BASE_URL = os.getenv("APP_BASE_URL", "").strip().rstrip("/")
SMTP_HOST = os.getenv("SMTP_HOST", "").strip()
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "").strip()
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
SMTP_FROM = os.getenv("SMTP_FROM", "").strip()
AI_API_KEY = os.getenv("AI_API_KEY", "").strip()  # Optional Groq-compatible key
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile").strip()
GROQ_VISION_MODEL = os.getenv("GROQ_VISION_MODEL", "qwen/qwen3.6-27b").strip()
# Optional comma-separated Twelve Data symbols for non-FX Quotex assets.
# Example format depends on Twelve Data's own symbol catalog; these are NOT
# proof that the same instrument exists on Quotex.
DEFAULT_REAL_MARKETS = [
    "AUD/USD", "AUD/CHF", "AUD/JPY", "AUD/CAD", "CAD/JPY", "CHF/JPY",
    "EUR/AUD", "EUR/CHF", "EUR/JPY", "EUR/GBP", "EUR/CAD", "EUR/USD",
    "GBP/AUD", "GBP/CAD", "GBP/CHF", "GBP/USD", "GBP/JPY", "USD/JPY",
]
REAL_MARKET_SYMBOLS = ",".join(DEFAULT_REAL_MARKETS)  # Fixed allowlist: exactly the 18 user-selected pairs
BANGLADESH_TZ = ZoneInfo("Asia/Dhaka")

# The account's reported Twelve Data limit is 8 credits/minute and 800/day.
# Automatic signal delivery is intentionally OFF. /future scans the allowlist
# manually, pacing requests so it does not exceed 8 requests/minute.
DAILY_CREDIT_BUDGET = max(100, min(int(os.getenv("DAILY_CREDIT_BUDGET", "700")), 780))
SCAN_INTERVAL_SECONDS = max(60, int(os.getenv("SCAN_INTERVAL_SECONDS", "90")))  # legacy setting; scanner is disabled
HISTORY_OUTPUTSIZE = 5000  # Twelve Data's documented per-request maximum is plan-dependent.
MAX_SIGNAL_AGE_SECONDS = 180

TWELVE_BASE = "https://api.twelvedata.com"
TELEGRAM_BASE = f"https://api.telegram.org/bot{BOT_TOKEN}" if BOT_TOKEN else ""
GROQ_BASE = "https://api.groq.com/openai/v1/chat/completions"

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("ghost-matrix")
app = Flask(__name__)
@app.route("/manifest.json")
def serve_manifest():
    return send_from_directory(
        os.path.dirname(os.path.abspath(__file__)),
        "manifest.json",
        mimetype="application/manifest+json"
    )
    @app.route("/icons/<path:filename>")
def serve_icons(filename):
    return send_from_directory(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "icons"),
        filename
    )

app.secret_key = FLASK_SECRET_KEY or secrets.token_urlsafe(48)
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax", SESSION_COOKIE_SECURE=bool(APP_BASE_URL.startswith("https://")), PERMANENT_SESSION_LIFETIME=timedelta(hours=12))
_auth_serializer = URLSafeTimedSerializer(app.secret_key, salt="ghost-matrix-email-verification-v1")

http_session = requests.Session()
http_session.headers.update({"User-Agent": "GHOST-MATRIX-V2/2.0"})
state_lock = threading.Lock()
state = {
    "last_scan_utc": None,
    "last_error": None,
    "catalog_count": 0,
    "next_symbol_index": 0,
    "memory_api_calls": {},
    "memory_results": {"win": 0, "loss": 0},
    "memory_signals": [],
    "next_memory_signal_id": 1,
    "auto_scanner_enabled": False,
    # Fallback learning progress when PostgreSQL/Supabase is unavailable.
    "learn_progress": {},
}

# ------------------------------ Database -----------------------------
def db_connect():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not configured")
    if psycopg2 is None:
        raise RuntimeError("psycopg2-binary is missing; add it to existing requirements.txt")
    return psycopg2.connect(DATABASE_URL, connect_timeout=8, sslmode="require")



@contextmanager
def db_connection():
    conn = db_connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_database() -> tuple[bool, str]:
    try:
        with db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS gm_signals (
                        id BIGSERIAL PRIMARY KEY,
                        symbol TEXT NOT NULL,
                        direction TEXT NOT NULL,
                        candle_time TIMESTAMPTZ,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        entry_price DOUBLE PRECISION,
                        reason TEXT NOT NULL,
                        source TEXT NOT NULL DEFAULT 'Twelve Data external feed',
                        verified_quotex BOOLEAN NOT NULL DEFAULT FALSE,
                        status TEXT NOT NULL DEFAULT 'PENDING'
                    )
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS gm_trade_results (
                        id BIGSERIAL PRIMARY KEY,
                        result TEXT NOT NULL CHECK (result IN ('WIN','LOSS')),
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS gm_api_usage (
                        id BIGSERIAL PRIMARY KEY,
                        usage_day DATE NOT NULL,
                        calls INTEGER NOT NULL DEFAULT 0,
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        UNIQUE (usage_day)
                    )
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS gm_market_catalog (
                        id BIGSERIAL PRIMARY KEY,
                        symbol TEXT NOT NULL UNIQUE,
                        currency_group TEXT,
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS gm_learning_progress (
                        chat_id TEXT PRIMARY KEY,
                        next_lesson INTEGER NOT NULL DEFAULT 0 CHECK (next_lesson >= 0),
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                """)
                cur.execute("ALTER TABLE gm_signals ADD COLUMN IF NOT EXISTS result_updated_at TIMESTAMPTZ")
                cur.execute("CREATE INDEX IF NOT EXISTS gm_signals_created_idx ON gm_signals(created_at DESC)")
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS gm_users (
                        id BIGSERIAL PRIMARY KEY,
                        email TEXT NOT NULL UNIQUE,
                        password_hash TEXT NOT NULL,
                        is_admin BOOLEAN NOT NULL DEFAULT FALSE,
                        email_verified BOOLEAN NOT NULL DEFAULT FALSE,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        verified_at TIMESTAMPTZ
                    )
                """)
                cur.execute("CREATE INDEX IF NOT EXISTS gm_users_email_idx ON gm_users(LOWER(email))")
        return True, "Supabase/PostgreSQL connected"
    except Exception as exc:
        log.warning("Database init failed: %s", exc)
        return False, str(exc)


def db_api_calls_today() -> int:
    today = datetime.now(timezone.utc).date()
    try:
        with db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT calls FROM gm_api_usage WHERE usage_day=%s", (today,))
                row = cur.fetchone()
                return int(row[0]) if row else 0
    except Exception:
        return int(state["memory_api_calls"].get(str(today), 0))


def record_api_call() -> None:
    today = datetime.now(timezone.utc).date()
    try:
        with db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO gm_api_usage (usage_day, calls, updated_at)
                    VALUES (%s, 1, NOW())
                    ON CONFLICT (usage_day)
                    DO UPDATE SET calls = gm_api_usage.calls + 1, updated_at = NOW()
                """, (today,))
    except Exception:
        key = str(today)
        state["memory_api_calls"][key] = int(state["memory_api_calls"].get(key, 0)) + 1


def save_signal(symbol: str, direction: str, candle_time: datetime, price: float, reason: str) -> Optional[int]:
    try:
        with db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO gm_signals
                    (symbol, direction, candle_time, entry_price, reason, source, verified_quotex)
                    VALUES (%s, %s, %s, %s, %s, %s, FALSE) RETURNING id
                """, (symbol, direction, candle_time, price, reason, "Twelve Data external feed"))
                row = cur.fetchone()
                return int(row[0]) if row else None
    except Exception as exc:
        log.warning("Could not save signal to database: %s", exc)
        with state_lock:
            signal_id = state["next_memory_signal_id"]
            state["next_memory_signal_id"] += 1
            state["memory_signals"].append({
                "id": signal_id, "symbol": symbol, "direction": direction,
                "candle_time": candle_time, "created_at": datetime.now(timezone.utc),
                "time": candle_time.isoformat(), "entry_price": price, "price": price,
                "reason": reason, "source": "Twelve Data external feed",
                "verified_quotex": False, "status": "PENDING",
            })
            state["memory_signals"] = state["memory_signals"][-100:]
            return signal_id


def fetch_recent_signals(limit: int = 10) -> list[dict[str, Any]]:
    try:
        with db_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""
                    SELECT id, symbol, direction, candle_time, created_at, entry_price, reason,
                           source, verified_quotex, status
                    FROM gm_signals ORDER BY created_at DESC LIMIT %s
                """, (limit,))
                return [dict(r) for r in cur.fetchall()]
    except Exception:
        with state_lock:
            return list(reversed(state["memory_signals"][-limit:]))


def update_signal_result(result: str, signal_id: Optional[int] = None) -> tuple[bool, Optional[dict[str, Any]]]:
    """Attach a manually confirmed WIN/LOSS to a specific or latest pending signal."""
    result = result.upper()
    if result not in ("WIN", "LOSS"):
        return False, None
    try:
        with db_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                if signal_id is not None:
                    cur.execute("""
                        UPDATE gm_signals SET status=%s, result_updated_at=NOW()
                        WHERE id=%s AND status='PENDING'
                        RETURNING id, symbol, direction, status
                    """, (result, signal_id))
                else:
                    cur.execute("""
                        UPDATE gm_signals SET status=%s, result_updated_at=NOW()
                        WHERE id=(SELECT id FROM gm_signals WHERE status='PENDING' ORDER BY created_at DESC LIMIT 1)
                        RETURNING id, symbol, direction, status
                    """, (result,))
                row = cur.fetchone()
                return (True, dict(row)) if row else (False, None)
    except Exception as exc:
        log.warning("Could not update signal result in database: %s", exc)
        with state_lock:
            pending = [x for x in state["memory_signals"] if x.get("status", "PENDING") == "PENDING"]
            target = next((x for x in reversed(pending) if signal_id is None or x.get("id") == signal_id), None)
            if not target:
                return False, None
            target["status"] = result
            return True, {"id": target["id"], "symbol": target["symbol"], "direction": target["direction"], "status": result}


def get_stats() -> tuple[int, int, int]:
    try:
        with db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT COUNT(*) FILTER (WHERE status='WIN'),
                           COUNT(*) FILTER (WHERE status='LOSS'),
                           COUNT(*) FILTER (WHERE status IN ('WIN','LOSS'))
                    FROM gm_signals
                """)
                row = cur.fetchone()
                return int(row[0] or 0), int(row[1] or 0), int(row[2] or 0)
    except Exception:
        with state_lock:
            wins = sum(1 for x in state["memory_signals"] if x.get("status") == "WIN")
            losses = sum(1 for x in state["memory_signals"] if x.get("status") == "LOSS")
            return wins, losses, wins + losses


def get_symbol_stats(symbol: str) -> tuple[int, int, int]:
    try:
        with db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT COUNT(*) FILTER (WHERE status='WIN'),
                           COUNT(*) FILTER (WHERE status='LOSS'),
                           COUNT(*) FILTER (WHERE status IN ('WIN','LOSS'))
                    FROM gm_signals WHERE symbol=%s
                """, (symbol,))
                row = cur.fetchone()
                return int(row[0] or 0), int(row[1] or 0), int(row[2] or 0)
    except Exception:
        with state_lock:
            matching = [x for x in state["memory_signals"] if x.get("symbol") == symbol]
            wins = sum(1 for x in matching if x.get("status") == "WIN")
            losses = sum(1 for x in matching if x.get("status") == "LOSS")
            return wins, losses, wins + losses

# ---------------------------- Telegram API ---------------------------
def telegram_call(method: str, payload: Optional[dict] = None, timeout: int = 20):
    if not TELEGRAM_BASE:
        raise RuntimeError("BOT_TOKEN is not configured")
    response = http_session.post(f"{TELEGRAM_BASE}/{method}", json=payload or {}, timeout=timeout)
    response.raise_for_status()
    data = response.json()
    if not data.get("ok"):
        raise RuntimeError(str(data))
    return data.get("result")


def send_message(
    text: str,
    chat_id: Optional[str] = None,
    parse_mode: Optional[str] = None,
) -> None:
    """Send a Telegram text message; HTML formatting is optional per message."""
    target = str(chat_id or CHAT_ID).strip()
    if not target or not BOT_TOKEN:
        log.info("Telegram message skipped: BOT_TOKEN/CHAT_ID missing")
        return

    # Keep messages under Telegram's 4096-character limit. HTML-formatted
    # news cards are short enough to send in one piece without splitting tags.
    if len(text) > 3900 and parse_mode:
        log.error("Refusing to split a formatted Telegram message; message too long")
        return

    chunks = [text] if parse_mode else [text[i:i + 3900] for i in range(0, len(text), 3900)]
    for chunk in chunks:
        payload = {
            "chat_id": target,
            "text": chunk,
            "disable_web_page_preview": True,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        try:
            telegram_call("sendMessage", payload, timeout=20)
        except Exception as exc:
            log.error("Telegram send failed: %s", exc)
            break


def is_allowed_chat(chat_id: Any) -> bool:
    return not CHAT_ID or str(chat_id) == str(CHAT_ID)

# --------------------------- Twelve Data -----------------------------
def twelve_get(endpoint: str, params: dict, count_credit: bool = True) -> dict:
    if not TWELVE_DATA_API_KEY:
        raise RuntimeError("TWELVE_DATA_API_KEY is not configured")
    if count_credit and db_api_calls_today() >= DAILY_CREDIT_BUDGET:
        raise RuntimeError(f"Daily safety budget reached ({DAILY_CREDIT_BUDGET} API credits).")
    query = dict(params)
    query["apikey"] = TWELVE_DATA_API_KEY
    response = http_session.get(f"{TWELVE_BASE}/{endpoint.lstrip('/')}", params=query, timeout=25)
    if count_credit:
        record_api_call()
    response.raise_for_status()
    data = response.json()
    if data.get("status") == "error" or data.get("code"):
        raise RuntimeError(data.get("message", str(data)))
    return data


def get_catalog(force: bool = False) -> list[dict[str, str]]:
    """Return only the user's explicitly selected REAL-market candidates; never all 1399 symbols."""
    symbols = [x.strip().upper() for x in REAL_MARKET_SYMBOLS.split(",") if x.strip()]
    # Stable unique list while preserving user order.
    symbols = list(dict.fromkeys(symbols))
    catalog = [{"symbol": symbol, "group": "User-selected Quotex REAL-market candidate"} for symbol in symbols]
    state["catalog_count"] = len(catalog)
    return catalog


def merge_custom_symbols(catalog: list[dict[str, str]]) -> list[dict[str, str]]:
    # Kept for compatibility with older code paths; the allowlist is authoritative.
    return get_catalog()


def parse_candles(data: dict) -> list[dict[str, Any]]:
    values = data.get("values") or []
    candles = []
    for item in values:
        try:
            stamp = datetime.strptime(item["datetime"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            candles.append({
                "time": stamp,
                "open": float(item["open"]),
                "high": float(item["high"]),
                "low": float(item["low"]),
                "close": float(item["close"]),
            })
        except (KeyError, TypeError, ValueError):
            continue
    candles.sort(key=lambda c: c["time"])
    return candles


def ema(values: list[float], period: int) -> list[Optional[float]]:
    result: list[Optional[float]] = [None] * len(values)
    if len(values) < period:
        return result
    multiplier = 2.0 / (period + 1)
    current = sum(values[:period]) / period
    result[period - 1] = current
    for i in range(period, len(values)):
        current = (values[i] - current) * multiplier + current
        result[i] = current
    return result


def rsi(values: list[float], period: int = 14) -> list[Optional[float]]:
    result: list[Optional[float]] = [None] * len(values)
    if len(values) <= period:
        return result
    gains = []
    losses = []
    for i in range(1, period + 1):
        change = values[i] - values[i - 1]
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    result[period] = 100.0 if avg_loss == 0 else 100 - 100 / (1 + avg_gain / avg_loss)
    for i in range(period + 1, len(values)):
        change = values[i] - values[i - 1]
        avg_gain = (avg_gain * (period - 1) + max(change, 0.0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-change, 0.0)) / period
        result[i] = 100.0 if avg_loss == 0 else 100 - 100 / (1 + avg_gain / avg_loss)
    return result


def analyze_candles(symbol: str, candles: list[dict[str, Any]]) -> dict[str, Any]:
    if len(candles) < 50:
        return {"direction": "NO TRADE", "reason": f"শুধু {len(candles)}টি candle; বিশ্লেষণের জন্য যথেষ্ট নয়।"}
    closes = [c["close"] for c in candles]
    fast = ema(closes, 9)
    slow = ema(closes, 21)
    rsis = rsi(closes, 14)
    idx = len(candles) - 1
    latest = candles[idx]
    age = (datetime.now(timezone.utc) - latest["time"]).total_seconds()
    if age < -120 or age > MAX_SIGNAL_AGE_SECONDS:
        return {"direction": "NO TRADE", "reason": f"সর্বশেষ M1 candle পুরোনো/সময় অসামঞ্জস্যপূর্ণ ({int(age)} sec)।", "candle": latest}
    if fast[idx] is None or slow[idx] is None or rsis[idx] is None:
        return {"direction": "NO TRADE", "reason": "Indicator data incomplete.", "candle": latest}
    # Conservative filter: trend alignment + RSI confirmation + last-candle momentum.
    previous = candles[idx - 1]
    direction = "NO TRADE"
    reason = "EMA/RSI/momentum একই দিকে নিশ্চিত নয়।"
    if fast[idx] > slow[idx] and 52 <= rsis[idx] <= 68 and latest["close"] > previous["close"] and latest["close"] > latest["open"]:
        direction = "CALL"
        reason = f"দাম EMA9 ও EMA21-এর ওপরে আছে; RSI {rsis[idx]:.1f}; শেষ ১ মিনিটের candle-এ বন্ধের দাম খোলার দামের চেয়ে বেশি—এই মিনিটে ক্রেতাদের চাপ দেখা গেছে। এটি শুধু সম্ভাব্য সেটআপ।"
    elif fast[idx] < slow[idx] and 32 <= rsis[idx] <= 48 and latest["close"] < previous["close"] and latest["close"] < latest["open"]:
        direction = "PUT"
        reason = f"দাম EMA9 ও EMA21-এর নিচে আছে; RSI {rsis[idx]:.1f}; শেষ ১ মিনিটের candle-এ বন্ধের দাম খোলার দামের চেয়ে কম—এই মিনিটে বিক্রেতাদের চাপ দেখা গেছে। এটি শুধু সম্ভাব্য সেটআপ।"
    return {
        "symbol": symbol,
        "direction": direction,
        "reason": reason,
        "price": latest["close"],
        "candle": latest,
        "candle_count": len(candles),
        "first_candle": candles[0]["time"],
        "last_candle": latest["time"],
        "rsi": rsis[idx],
    }


def backtest_ema(candles: list[dict[str, Any]]) -> dict[str, Any]:
    """Small historical sanity check; not a prediction or guaranteed profitability."""
    if len(candles) < 60:
        return {"trades": 0, "wins": 0, "losses": 0, "note": "Not enough candles for backtest."}
    closes = [c["close"] for c in candles]
    fast = ema(closes, 9)
    slow = ema(closes, 21)
    wins = losses = trades = 0
    # Evaluate the next candle after a trend-aligned EMA crossover. One-candle horizon.
    for i in range(22, len(candles) - 1):
        if fast[i - 1] is None or slow[i - 1] is None or fast[i] is None or slow[i] is None:
            continue
        call = fast[i - 1] <= slow[i - 1] and fast[i] > slow[i]
        put = fast[i - 1] >= slow[i - 1] and fast[i] < slow[i]
        if not (call or put):
            continue
        trades += 1
        next_move = candles[i + 1]["close"] - candles[i + 1]["open"]
        won = next_move > 0 if call else next_move < 0
        if won:
            wins += 1
        else:
            losses += 1
    return {"trades": trades, "wins": wins, "losses": losses,
            "win_rate_pct": round(100 * wins / trades, 1) if trades else None,
            "note": "Historical candle simulation only; not Quotex-verified and not a guarantee."}

# --------------------------- Scanner logic ---------------------------
def bd_time(value: Any) -> str:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(BANGLADESH_TZ).strftime("%Y-%m-%d %H:%M:%S")
    return str(value or "সময় পাওয়া যায়নি")


def scan_one_symbol(symbol: str) -> dict[str, Any]:
    payload = twelve_get("time_series", {
        "symbol": symbol, "interval": "1min", "outputsize": HISTORY_OUTPUTSIZE,
        "order": "desc", "format": "JSON", "timezone": "UTC",
    })
    candles = parse_candles(payload)
    result = analyze_candles(symbol, candles)
    result["backtest"] = backtest_ema(candles)
    result["source"] = "Twelve Data external feed"
    result["verified_quotex"] = False
    if result.get("direction") in ("CALL", "PUT") and result.get("candle"):
        result["signal_id"] = save_signal(symbol, result["direction"], result["candle"]["time"], result["price"], result["reason"])
    return result


def format_signal(result: dict[str, Any]) -> str:
    candle = result.get("candle") or {}
    candle_time = bd_time(candle.get("time"))
    generated = bd_time(datetime.now(timezone.utc))
    direction = result.get("direction", "NO TRADE")
    icon = "🟢" if direction == "CALL" else "🔴"
    meaning = "CALL মানে সম্ভাব্য ঊর্ধ্বমুখী দিক" if direction == "CALL" else "PUT মানে সম্ভাব্য নিম্নমুখী দিক"
    return (
        "👻 GHOST MATRIX V2.0\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"{icon} সম্ভাব্য সিগনাল: {direction}\n"
        f"💱 মার্কেট: {result.get('symbol', 'Unknown')}\n"
        f"💵 বাইরের ফিডের দাম: {result.get('price', 'N/A')}\n"
        f"🕯️ Candle time (বাংলাদেশ): {candle_time} UTC+6\n"
        f"📨 সিগনাল তৈরি: {generated} UTC+6\n"
        f"🧾 Signal ID: {result.get('signal_id', 'N/A')}\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"📘 সহজ ব্যাখ্যা: {result.get('reason', '')}\n"
        f"➡️ এর মানে: {meaning}; নিশ্চিত ফল নয়।\n\n"
        "⚠️ ডেটা: Twelve Data (external), Quotex-verified নয়।\n"
        f"ফল জানাতে: /win {result.get('signal_id', '')} অথবা /loss {result.get('signal_id', '')}"
    )


def format_future_batch(results: list[dict[str, Any]], scanned: int) -> str:
    signals = [r for r in results if r.get("direction") in ("CALL", "PUT")]
    now_text = bd_time(datetime.now(timezone.utc))
    lines = [
        "👻 GHOST MATRIX V2.0 — MANUAL SCAN",
        f"🕒 বাংলাদেশ সময়: {now_text} UTC+6",
        f"🔎 স্ক্যান: {scanned}টি নির্বাচিত pair",
        "━━━━━━━━━━━━━━━━━━",
    ]
    if not signals:
        lines.append("📭 এই স্ক্যানে কোনো সেটআপের শর্ত মেলেনি — NO TRADE।")
        lines.append("জোর করে CALL/PUT তৈরি করা হয়নি। পরে আবার /future দিতে পারো।")
    else:
        lines.append(f"📋 সম্ভাব্য সেটআপ: {len(signals)}টি\n")
        for n, r in enumerate(signals, 1):
            direction = r.get("direction")
            icon = "🟢 CALL" if direction == "CALL" else "🔴 PUT"
            candle = (r.get("candle") or {}).get("time")
            lines.append(
                f"{n}. {r.get('symbol')} — {icon}\n"
                f"   💵 বাইরের দাম: {r.get('price', 'N/A')}\n"
                f"   🕯️ Candle (BD): {bd_time(candle)} UTC+6\n"
                f"   🧾 ID: {r.get('signal_id', 'N/A')}\n"
                f"   📘 কেন: {r.get('reason', '')}\n"
                f"   ফল দিতে: /win {r.get('signal_id', '')} বা /loss {r.get('signal_id', '')}"
            )
    lines += [
        "━━━━━━━━━━━━━━━━━━",
        "⚠️ Twelve Data external feed; Quotex-verified নয়। Quotex-এর একই pair ও candle মিলিয়ে দেখো।",
        "📌 /signals = আগের সেটআপ | /stats = তোমার দেওয়া WIN/LOSS হিসাব | /learn = শেখার গাইড",
    ]
    return "\n".join(lines)


def scanner_loop() -> None:
    """Disabled by design: signals are generated only when the user sends /future."""
    while True:
        time.sleep(3600)

# ----------------------------- AI helpers ----------------------------
def ai_answer(prompt: str) -> str:
    if not AI_API_KEY:
        return "AI_API_KEY সেট করা নেই। সাধারণ bot command ব্যবহার করতে পারো; AI উত্তর বন্ধ আছে।"
    try:
        response = http_session.post(GROQ_BASE,
            headers={"Authorization": f"Bearer {AI_API_KEY}", "Content-Type": "application/json"},
            json={"model": GROQ_MODEL, "messages": [
                {"role": "system", "content": "Answer clearly in Bengali when the user writes Bengali. Never invent current news, live prices, trade outcomes, or guaranteed trading advice."},
                {"role": "user", "content": prompt}], "temperature": 0.2, "max_tokens": 700}, timeout=30)
        response.raise_for_status()
        data = response.json()
        return data["choices"][0]["message"]["content"].strip()
    except Exception as exc:
        log.warning("AI request failed: %s", exc)
        return "AI service এখন কাজ করছে না। API key/model/limit Render Logs-এ যাচাই করো।"


def analyze_chart_image(file_id: str) -> str:
    """Describe visible chart context; never claim certainty or verified broker data."""
    if not AI_API_KEY:
        return "AI_API_KEY সেট করা নেই, তাই chart image analysis চালু নেই।"
    try:
        file_url = get_photo_file_url(file_id)
        if not file_url:
            return "Telegram থেকে ছবিটি আনতে পারিনি। আবার পাঠাও।"
        photo = http_session.get(file_url, timeout=25)
        photo.raise_for_status()
        if len(photo.content) > 15 * 1024 * 1024:
            return "ছবিটি অনেক বড়। ছোট/কম রেজোলিউশনের screenshot পাঠাও।"
        mime = photo.headers.get("Content-Type", "image/jpeg").split(";")[0]
        if mime not in ("image/jpeg", "image/png", "image/webp"):
            mime = "image/jpeg"
        encoded = base64.b64encode(photo.content).decode("ascii")
        payload = {
            "model": GROQ_VISION_MODEL,
            "messages": [
                {"role": "system", "content": (
                    "Analyze only what is visible in this trading chart screenshot. "
                    "Reply in Bengali. Mention uncertainty and missing timeframe/asset labels. "
                    "Do not invent price values, predict with certainty, claim Quotex verification, "
                    "or promise profit. If unclear, say NO TRADE / insufficient evidence."
                )},
                {"role": "user", "content": [
                    {"type": "text", "text": "এই chart screenshot-এ দৃশ্যমান trend, candle structure এবং অনিশ্চয়তা ব্যাখ্যা করো। নিশ্চিত CALL/PUT দাবি করবে না।"},
                    {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{encoded}"}}
                ]}
            ],
            "temperature": 0.1,
            "max_completion_tokens": 700,
        }
        response = http_session.post(GROQ_BASE,
            headers={"Authorization": f"Bearer {AI_API_KEY}", "Content-Type": "application/json"},
            json=payload, timeout=45)
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"].strip() + "\n\n⚠️ Screenshot analysis only; not a verified Quotex signal."
    except Exception as exc:
        log.warning("Chart image analysis failed: %s", exc)
        return "Chart analysis ব্যর্থ হয়েছে। AI key, vision model এবং Groq limit Render Logs-এ যাচাই করো।"


def get_photo_file_url(file_id: str) -> Optional[str]:
    try:
        result = telegram_call("getFile", {"file_id": file_id})
        path = result.get("file_path")
        return f"https://api.telegram.org/file/bot{BOT_TOKEN}/{path}" if path else None
    except Exception as exc:
        log.warning("Could not retrieve Telegram photo: %s", exc)
        return None


# ---------------------- Price Action Learning Charts -------------------
# These are schematic educational examples generated locally with Pillow.
# They are NOT live Quotex charts and do not generate trade signals.
PA_LESSONS = [
    {
        "key": "support_resistance",
        "title": "1/7 — Support & Resistance",
        "caption": (
            "📘 SUPPORT ও RESISTANCE\n\n"
            "Support = এমন price area যেখানে আগে কয়েকবার দাম নেমে এসে থেমেছে/উঠেছে; "
            "এখানে buyers আগ্রহী হতে পারে। Resistance = এমন area যেখানে দাম উঠে এসে "
            "বারবার থেমেছে/নেমেছে; sellers সক্রিয় হতে পারে। এগুলো একদম সরু line নয়—zone হিসেবে দেখো।\n\n"
            "উদাহরণ: দাম 1.1000 এলাকার কাছে 3 বার নেমে bounce করলে ওই অঞ্চল support হতে পারে। "
            "কিন্তু 4র্থ বারেও bounce হবেই—এমন নিশ্চয়তা নেই।\n\n"
            "কী দেখবে: আগের swing highs/lows, একাধিক reaction, candle close, rejection এবং market trend। "
            "শুধু level touch হলেই entry নয়; rejection/structure confirmation অপেক্ষা করো। "
            "Level ভেঙে গেলে সেটি পরে role reversal করতে পারে।"
        ),
        "prices": [45,35,43,52,48,37,44,57,51,39,46,58,53,47,55,63],
        "levels": [(39,"SUPPORT"),(57,"RESISTANCE")],
        "notes": ["Repeated reaction zones"]
    },
    {
        "key": "trend_structure",
        "title": "2/7 — Trend & Market Structure",
        "caption": (
            "📘 TREND ও MARKET STRUCTURE\n\n"
            "Uptrend: Higher High (HH) + Higher Low (HL) ধারাবাহিকভাবে তৈরি হয়। "
            "Downtrend: Lower Low (LL) + Lower High (LH)। Sideways market-এ দাম একটি range-এর মধ্যে ঘোরে।\n\n"
            "উদাহরণ: আগের high 1.1050, পরের high 1.1080 এবং pullback low আগের low-এর উপরে থাকলে "
            "buyers শক্তিশালী হতে পারে। কিন্তু একটি wick level ভাঙলেই structure break নিশ্চিত নয়—candle close ও context দেখো।\n\n"
            "কী দেখবে: অন্তত 2টি swing high ও 2টি swing low, candle close, pullback এবং বড় timeframe-এর দিক। "
            "Trend-এর বিপরীতে শুধু একটি বড় candle দেখে entry নিও না। Structure অস্পষ্ট হলে NO TRADE।"
        ),
        "prices": [24,30,27,35,31,40,35,44,39,49,43,54,48,59,53,65],
        "levels": [],
        "notes": ["HH", "HL", "HH", "HL"]
    },
    {
        "key": "breakout_retest",
        "title": "3/7 — Breakout & Retest",
        "caption": (
            "📘 BREAKOUT ও RETEST\n\n"
            "Breakout = price কোনো range/support/resistance zone-এর বাইরে শক্তভাবে close করে। "
            "Retest = ভাঙা level-এ দাম ফিরে এসে সেটিকে নতুন support/resistance হিসেবে পরীক্ষা করে।\n\n"
            "উদাহরণ: Resistance zone-এর উপরে candle close → দাম পুরোনো resistance-এ ফিরে আসে → "
            "সেখানে bullish rejection/confirmation দেখা যায়। তখন সম্ভাব্য bullish setup তৈরি হতে পারে; নিশ্চিত নয়।\n\n"
            "ভুল breakout এড়াতে: শুধু wick-এর বাইরে যাওয়া যথেষ্ট নয়। candle close, momentum, retest reaction "
            "এবং বাজারের structure মিলিয়ে দেখো। Retest না হলে জোর করে entry নয়। M1-এ noise বেশি; "
            "news spike ও spread-ও ফল বদলাতে পারে।"
        ),
        "prices": [35,39,34,38,36,40,37,39,42,48,55,62,57,51,54,59],
        "levels": [(41,"OLD RESISTANCE")],
        "notes": ["Breakout", "Retest", "Reaction"]
    },
    {
        "key": "pin_bar",
        "title": "4/7 — Pin Bar",
        "caption": (
            "📘 PIN BAR / REJECTION CANDLE\n\n"
            "Pin bar-এ body তুলনামূলক ছোট এবং একটি wick লম্বা হয়। নিচের লম্বা wick support-এর কাছে হলে "
            "দাম নিচে reject হয়েছে বোঝাতে পারে; উপরের লম্বা wick resistance-এর কাছে হলে উপরে reject হয়েছে বোঝাতে পারে।\n\n"
            "উদাহরণ: support zone-এ long lower wick + candle close উপরের দিকে = সম্ভাব্য bullish rejection। "
            "কিন্তু মাঝামাঝি random জায়গায় একই candle দুর্বল signal হতে পারে।\n\n"
            "Confirmation: level/structure-এর সঙ্গে মিল, candle close, পরের candle-এর follow-through। "
            "শুধু pin bar দেখেই CALL/PUT নয়। wick-এর দিক, body ও location—তিনটিই দেখো।"
        ),
        "prices": [53,48,44,41,39,46,49,47,52,55,51,57,60,58,62,65],
        "levels": [(41,"SUPPORT ZONE")],
        "notes": ["Long lower wick", "Bullish rejection"]
    },
    {
        "key": "engulfing",
        "title": "5/7 — Engulfing",
        "caption": (
            "📘 BULLISH ও BEARISH ENGULFING\n\n"
            "Bullish engulfing: একটি bearish candle-এর পরে বড় bullish body আগের body-কে ঢেকে দেয়। "
            "Bearish engulfing: bullish candle-এর পরে বড় bearish body আগের body-কে ঢেকে দেয়। "
            "এটি momentum shift-এর ইঙ্গিত হতে পারে, নিশ্চয়তা নয়।\n\n"
            "উদাহরণ: support/পরিষ্কার pullback-এর শেষে bearish candle, তারপর বড় bullish body—এটি bullish engulfing "
            "হতে পারে। Resistance-এ উল্টো pattern bearish হতে পারে।\n\n"
            "Confirmation: গুরুত্বপূর্ণ zone, trend/structure এবং candle close মিলিয়ে দেখো। "
            "Body engulf করেছে কি না দেখো; শুধু wick বড় হলেই engulfing নয়। Range-এর মাঝখানে pattern-এর মান কম হতে পারে।"
        ),
        "prices": [60,56,51,47,50,46,43,52,57,54,59,63,60,66,70,68],
        "levels": [(44,"SUPPORT ZONE")],
        "notes": ["Bearish body", "Bullish engulfing"]
    },
    {
        "key": "liquidity_sweep",
        "title": "6/7 — Liquidity Sweep",
        "caption": (
            "📘 LIQUIDITY SWEEP\n\n"
            "দাম আগের swing high/low-এর একটু বাইরে wick তৈরি করে আবার level-এর ভেতরে close করলে "
            "সেটি সম্ভাব্য liquidity sweep হতে পারে। অনেক trader stop-loss ওই high/low-এর আশেপাশে রাখে—"
            "তাই level-এর বাইরে দ্রুত move-এর পর reversal দেখা যেতে পারে।\n\n"
            "উদাহরণ: আগের low ভেঙে wick নিচে যায়, কিন্তু candle আবার low-এর উপরে close করে এবং bullish structure তৈরি করে। "
            "এটি possible bullish sweep; প্রতিবার reversal হবে না।\n\n"
            "Confirmation: sweep-এর পর candle close back inside, rejection এবং structure shift দেখো। "
            "শুধু wick দেখে entry নয়। Strong breakout যদি level-এর বাইরে close ও hold করে, সেটি sweep নাও হতে পারে।"
        ),
        "prices": [52,47,44,48,43,39,32,42,47,45,52,57,54,61,65,63],
        "levels": [(39,"PRIOR SWING LOW")],
        "notes": ["Sweep below low", "Close back above"]
    },
    {
        "key": "entry_confirmation",
        "title": "7/7 — Entry Confirmation Checklist",
        "caption": (
            "📘 ENTRY-এর আগে CONFIRMATION\n\n"
            "১) Market trend/structure পরিষ্কার? ২) দাম গুরুত্বপূর্ণ support/resistance zone-এ? "
            "৩) Pin bar/engulfing বা breakout-retest-এর মতো price-action evidence আছে? "
            "৪) Candle close হয়েছে, নাকি শুধু চলমান candle? ৫) News/অস্বাভাবিক volatility আছে? "
            "৬) Quotex-এ একই REAL pair, timeframe ও candle মিলছে?\n\n"
            "উদাহরণ: uptrend + support retest + bullish rejection close = সম্ভাব্য bullish context। "
            "কোনো একটি শর্ত অনুপস্থিত বা চার্ট অস্পষ্ট হলে trade বাদ দেওয়াও একটি সিদ্ধান্ত।\n\n"
            "M1-এ false signal বেশি। Fixed expiry-তে candle close, payout, spread/price difference ও timing গুরুত্বপূর্ণ। "
            "Martingale করে loss recover করার চেষ্টা কোরো না। Demo-তে নোট রেখে বহু উদাহরণ পরীক্ষা করো। "
            "এটি শিক্ষা, লাভের নিশ্চয়তা নয়।"
        ),
        "prices": [28,35,31,40,36,45,40,49,44,53,47,56,52,61,57,66],
        "levels": [(44,"KEY ZONE")],
        "notes": ["Trend", "Zone", "Rejection close"]
    },
]


def _chart_font(size: int):
    try:
        return ImageFont.truetype("DejaVuSans.ttf", size)
    except Exception:
        return ImageFont.load_default()


def make_price_action_chart(lesson: dict) -> bytes:
    """Render an illustrative candlestick chart in memory; no external image assets."""
    width, height = 1000, 560
    img = Image.new("RGB", (width, height), "#101827")
    draw = ImageDraw.Draw(img)
    title_font = _chart_font(28)
    label_font = _chart_font(18)
    small_font = _chart_font(15)
    draw.text((34, 22), lesson["title"], font=title_font, fill="#F4F7FB")
    draw.text((36, 62), "ILLUSTRATIVE PRICE ACTION EXAMPLE — NOT LIVE MARKET DATA",
              font=small_font, fill="#9CAEC4")

    left, top, right, bottom = 70, 115, 950, 475
    # Grid
    for i in range(7):
        y = top + i * (bottom-top) // 6
        draw.line((left, y, right, y), fill="#26354A", width=1)
    for i in range(13):
        x = left + i * (right-left) // 12
        draw.line((x, top, x, bottom), fill="#1C2A3D", width=1)

    vals = lesson["prices"]
    lo, hi = min(vals)-9, max(vals)+9
    def py(v):
        return int(bottom - (v-lo)/(hi-lo)*(bottom-top))
    candle_w = 23
    step = (right-left)//len(vals)
    prev = vals[0]
    ohlc = []
    for idx, close in enumerate(vals):
        open_v = prev + ((-1)**idx) * (2 + (idx % 3))
        high_v = max(open_v, close) + (2 + idx % 4)
        low_v = min(open_v, close) - (2 + (idx+1) % 4)
        ohlc.append((open_v, high_v, low_v, close))
        prev = close

    # Special recognizable candles for Pin Bar, Engulfing and sweep examples.
    if lesson["key"] == "pin_bar":
        o,h,l,c = ohlc[5]
        ohlc[5] = (o, max(o,c)+2, lo+2, c)
    elif lesson["key"] == "engulfing":
        ohlc[7] = (vals[7]+5, vals[7]+7, vals[7]-2, vals[7]-1)
        ohlc[8] = (vals[8]-7, vals[8]+2, vals[8]-9, vals[8]+8)
    elif lesson["key"] == "liquidity_sweep":
        o,h,l,c = ohlc[6]
        ohlc[6] = (o, h, lo+1, max(o,c)+1)

    centers = []
    for idx, (o,h,l,c) in enumerate(ohlc):
        x = left + idx*step + step//2
        centers.append((x, py(c)))
        color = "#2ED6A1" if c >= o else "#FF647C"
        draw.line((x, py(h), x, py(l)), fill=color, width=3)
        y1, y2 = py(o), py(c)
        if abs(y2-y1) < 5:
            y2 = y1 + (5 if y2 >= y1 else -5)
        draw.rectangle((x-candle_w//2, min(y1,y2), x+candle_w//2, max(y1,y2)),
                       fill=color, outline=color)

    # Key horizontal zones.
    for level, label in lesson.get("levels", []):
        y = py(level)
        draw.line((left, y, right, y), fill="#F7C85B", width=3)
        draw.rectangle((left+8, y-24, left+8+len(label)*10+18, y-2), fill="#F7C85B")
        draw.text((left+16, y-22), label, font=small_font, fill="#172033")

    key = lesson["key"]
    if key == "trend_structure":
        pts = [centers[i] for i in [1,3,5,7,9,11,13,15]]
        draw.line(pts, fill="#66B7FF", width=3)
        draw.text((760, 130), "HH / HL", font=label_font, fill="#66B7FF")
    elif key == "breakout_retest":
        draw.text((610, 125), "BREAKOUT", font=label_font, fill="#2ED6A1")
        draw.text((720, 300), "RETEST", font=label_font, fill="#F7C85B")
    elif key == "pin_bar":
        x, y = centers[5]
        draw.ellipse((x-11, y-11, x+11, y+11), outline="#F7C85B", width=3)
        draw.text((x+18, py(lo+4)), "LONG LOWER WICK", font=small_font, fill="#F7C85B")
    elif key == "engulfing":
        x, y = centers[8]
        draw.ellipse((x-18, y-26, x+18, y+26), outline="#F7C85B", width=3)
        draw.text((x+22, y-32), "ENGULF", font=small_font, fill="#F7C85B")
    elif key == "liquidity_sweep":
        x, y = centers[6]
        draw.ellipse((x-12, py(lo+1)-8, x+12, py(lo+1)+8), outline="#F7C85B", width=3)
        draw.text((x+18, py(lo+7)), "SWEEP", font=small_font, fill="#F7C85B")
    elif key == "entry_confirmation":
        checklist = ["1. Trend / structure", "2. Key zone", "3. Candle close", "4. Confirmation"]
        for j, item in enumerate(checklist):
            draw.rounded_rectangle((730, 132+j*44, 930, 166+j*44), radius=7,
                                   fill="#20334A", outline="#66B7FF", width=1)
            draw.text((742, 140+j*44), item, font=small_font, fill="#F4F7FB")

    draw.text((35, 510), "Green = bullish candle   |   Red = bearish candle   |   Yellow = key level",
              font=small_font, fill="#B9C7D8")
    output = io.BytesIO()
    img.save(output, format="PNG", optimize=True)
    return output.getvalue()


def send_photo_bytes(photo_bytes: bytes, caption: str, chat_id: Optional[str] = None) -> bool:
    """Upload a generated educational PNG to Telegram without storing it on disk."""
    target = str(chat_id or CHAT_ID).strip()
    if not target or not BOT_TOKEN:
        log.info("Telegram photo skipped: BOT_TOKEN/CHAT_ID missing")
        return False
    try:
        response = http_session.post(
            f"{TELEGRAM_BASE}/sendPhoto",
            data={"chat_id": target, "caption": caption[:1024]},
            files={"photo": ("price_action_example.png", photo_bytes, "image/png")},
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()
        if not data.get("ok"):
            raise RuntimeError(str(data))
        return True
    except Exception as exc:
        log.error("Telegram educational photo send failed: %s", exc)
        return False


def get_learning_progress(chat_id: str) -> int:
    """Return the zero-based index of the next lesson for this Telegram chat."""
    chat_key = str(chat_id).strip()
    try:
        with db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT next_lesson FROM gm_learning_progress WHERE chat_id=%s",
                    (chat_key,),
                )
                row = cur.fetchone()
                if row:
                    return max(0, min(int(row[0]), len(PA_LESSONS)))
                return 0
    except Exception:
        with state_lock:
            return max(0, min(int(state["learn_progress"].get(chat_key, 0)), len(PA_LESSONS)))


def save_learning_progress(chat_id: str, next_lesson: int) -> bool:
    """Persist learning progress; use in-memory fallback if DB is unavailable."""
    chat_key = str(chat_id).strip()
    progress = max(0, min(int(next_lesson), len(PA_LESSONS)))
    try:
        with db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO gm_learning_progress (chat_id, next_lesson, updated_at)
                       VALUES (%s, %s, NOW())
                       ON CONFLICT (chat_id) DO UPDATE
                       SET next_lesson=EXCLUDED.next_lesson, updated_at=NOW()""",
                    (chat_key, progress),
                )
        with state_lock:
            state["learn_progress"].pop(chat_key, None)
        return True
    except Exception as exc:
        log.info("Learning progress using memory fallback: %s", exc)
        with state_lock:
            state["learn_progress"][chat_key] = progress
        return False


def send_price_action_course(chat_id: str, reset: bool = False) -> None:
    """Send exactly one illustrated lesson per /learn command."""
    chat_key = str(chat_id).strip()
    if reset:
        save_learning_progress(chat_key, 0)
        lesson_index = 0
    else:
        lesson_index = get_learning_progress(chat_key)

    if lesson_index >= len(PA_LESSONS):
        send_message(
            "✅ তুমি Price Action-এর ৭টি Lesson শেষ করেছ!\n\n"
            "আবার Lesson 1 থেকে শুরু করতে: /learn reset\n"
            "নিরাপদ অনুশীলন: Demo account-এ chart দেখে নিজের নোট রাখো।\n\n"
            "⚠️ Lesson শেষ করা লাভের নিশ্চয়তা নয়। Twelve Data-এর price Quotex-verified নয়।",
            chat_key,
        )
        return

    lesson = PA_LESSONS[lesson_index]
    number = lesson_index + 1
    send_message(
        "👻 GHOST MATRIX — PRICE ACTION LEARNING\n"
        f"📘 Lesson {number}/{len(PA_LESSONS)}\n\n"
        "এই /learn কমান্ডে শুধু একটি Lesson পাঠানো হবে।\n"
        "পরের Lesson পেতে আবার /learn লিখো।\n"
        "সব ছবি শিক্ষামূলক schematic chart—live Quotex chart নয়।",
        chat_key,
    )

    try:
        photo = make_price_action_chart(lesson)
    except Exception as exc:
        log.exception("Could not generate learning chart for lesson %s", number)
        send_message(
            f"❌ Lesson {number}-এর chart তৈরি হয়নি। Logs পরীক্ষা করে একই /learn আবার দাও।",
            chat_key,
        )
        return

    # Advance only when Telegram confirms the image was sent, so a failed upload
    # does not silently skip the lesson.
    if send_photo_bytes(photo, lesson["caption"], chat_key):
        save_learning_progress(chat_key, number)
        if number == len(PA_LESSONS):
            send_message(
                "🎉 ৭/৭ Lesson পাঠানো হয়েছে!\n"
                "পরের /learn কমান্ডে course completion দেখাবে।\n"
                "আবার শুরু করতে /learn reset লিখো।\n\n"
                "⚠️ কোনো pattern নিশ্চিত ফল দেয় না; আগে Demo-তে পরীক্ষা করো।",
                chat_key,
            )
    else:
        send_message(
            f"⚠️ Lesson {number} skip করা হয়নি। ছবি পাঠানো যায়নি; সমস্যা ঠিক করে আবার /learn দাও।",
            chat_key,
        )


# ------------------------ Economic News Analyzer ---------------------
FINANCE_CALENDAR_BASE = "https://www.financecalendar.com/wp-json/fc/v1"
NEWS_OWNER = "@ghostsupportbd"
NEWS_SOURCE_URL = "https://www.financecalendar.com/api/"


def _extract_number(value: Any) -> Optional[float]:
    """Parse common economic values such as 4.7%, 220K, or -0.2."""
    if value is None:
        return None
    raw = str(value).strip().replace("−", "-")
    if not raw or raw.lower() in {"n/a", "na", "none", "null", "—", "-"}:
        return None
    import re
    match = re.search(r"[-+]?\d+(?:[.,]\d+)?\s*[KMBkmb]?", raw)
    if not match:
        return None
    token = match.group(0).replace(" ", "")
    # A single comma with three trailing digits is treated as a thousands separator.
    if "," in token and "." not in token:
        left, right = token.split(",", 1)
        token = left + right if len(right.rstrip("KMBkmb")) == 3 else left + "." + right
    suffix = token[-1:].upper() if token[-1:].upper() in {"K", "M", "B"} else ""
    number_text = token[:-1] if suffix else token
    try:
        number = float(number_text)
    except ValueError:
        return None
    return number * {"": 1.0, "K": 1_000.0, "M": 1_000_000.0, "B": 1_000_000_000.0}[suffix]


def parse_manual_news_input(text: str) -> Optional[dict]:
    """Accept a currency/event line followed by Actual, Forecast and Previous."""
    import re
    clean = (text or "").strip()
    if not clean or clean.startswith("/"):
        return None
    patterns = {
        "actual": r"(?im)^\s*(?:act|actual)\s*[-:=]\s*([^\n]+)",
        "forecast": r"(?im)^\s*(?:for|forecast|fcst)\s*[-:=]\s*([^\n]+)",
        "previous": r"(?im)^\s*(?:pre|previous|prev)\s*[-:=]\s*([^\n]+)",
    }
    values = {}
    for key, pattern in patterns.items():
        match = re.search(pattern, clean)
        if not match:
            return None
        values[key] = match.group(1).strip()
    first_line = clean.splitlines()[0].strip()
    currency_match = re.match(r"^([A-Za-z]{3})\b(?:\s*[-:|—]?\s*(.*))?$", first_line)
    if not currency_match:
        return None
    currency = currency_match.group(1).upper()
    event = (currency_match.group(2) or "").strip(" -:|—")
    return {
        "currency": currency,
        "event": event or "Economic event not specified",
        "actual_text": values["actual"],
        "forecast_text": values["forecast"],
        "previous_text": values["previous"],
        "actual": _extract_number(values["actual"]),
        "forecast": _extract_number(values["forecast"]),
        "previous": _extract_number(values["previous"]),
    }


def analyze_manual_news(data: dict) -> tuple[str, str, str]:
    """Return direction, explanation and confidence note; heuristic, not a trade guarantee."""
    actual, forecast, previous = data.get("actual"), data.get("forecast"), data.get("previous")
    if actual is None or forecast is None:
        return "UNCLEAR", "Actual বা Forecast-এর মান বুঝতে পারিনি—সংখ্যা ও unit আবার দাও।", "ডেটা অসম্পূর্ণ"
    event = str(data.get("event", "")).lower()
    # For some indicators, a lower number is usually better for the currency.
    lower_is_better_terms = (
        "unemployment rate", "jobless claims", "initial claims", "continuing claims",
        "claims", "unemployment", "job losses", "layoffs", "bad loans",
    )
    lower_is_better = any(term in event for term in lower_is_better_terms)
    if actual == forecast:
        if previous is not None and actual != previous:
            explanation = "Actual Forecast-এর সমান; তাই surprise নেই। Previous-এর তুলনায় পরিবর্তন একা নির্ভরযোগ্য দিক নির্ধারণ করে না।"
        else:
            explanation = "Actual ও Forecast সমান—স্পষ্ট surprise নেই।"
        return "UNCLEAR", explanation, "দিক অনিশ্চিত"
    if lower_is_better:
        up = actual < forecast
        reason = "এই indicator-এ সাধারণত কম মান মুদ্রার জন্য ভালো।" if up else "এই indicator-এ সাধারণত বেশি মান মুদ্রার জন্য নেতিবাচক।"
    else:
        up = actual > forecast
        reason = "Actual, Forecast-এর চেয়ে বেশি।" if up else "Actual, Forecast-এর চেয়ে কম।"
        if data.get("event") == "Economic event not specified":
            reason += " নিউজের নাম দেওয়া হয়নি, তাই সাধারণ surprise rule ব্যবহার করা হয়েছে।"
    direction = "UP ⬆️" if up else "DOWN ⬇️"
    delta = actual - forecast
    diff = f"Forecast থেকে পার্থক্য: {delta:+g} (একই unit ধরে)।"
    if previous is not None:
        prev_note = "Actual Previous-এর চেয়ে বেশি।" if actual > previous else "Actual Previous-এর চেয়ে কম।" if actual < previous else "Actual Previous-এর সমান।"
    else:
        prev_note = "Previous-এর মান তুলনা করা যায়নি।"
    return direction, f"{reason}\n{diff}\n{prev_note}", "সম্ভাব্য প্রভাব; নিশ্চিত Quotex M1 signal নয়"


def format_news_analysis(data: dict) -> str:
    """Format news analysis as copyable Telegram HTML text, never as a photo."""
    import html

    direction, explanation, note = analyze_manual_news(data)
    result_icon = (
        "🟢" if direction.startswith("UP")
        else "🔴" if direction.startswith("DOWN")
        else "🟡"
    )
    currency = html.escape(str(data.get("currency", "—")))
    event = html.escape(str(data.get("event", "Economic event")))
    actual = html.escape(str(data.get("actual_text", "N/A")))
    forecast = html.escape(str(data.get("forecast_text", "N/A")))
    previous = html.escape(str(data.get("previous_text", "N/A")))
    reason = html.escape(explanation)
    safe_note = html.escape(note)

    return (
        "👻 <b>GHOST MATRIX</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "📰 <b>NEWS ANALYZER</b>      🔵 <b>PAID</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"🌐 <b>{currency} • {event}</b>\n\n"
        "┌─────────────────────┐\n"
        f"│ <b>ACTUAL:</b> {actual}\n"
        f"│ <b>FORECAST:</b> {forecast}\n"
        f"│ <b>PREVIOUS:</b> {previous}\n"
        "└─────────────────────┘\n\n"
        f"{result_icon} <b>RESULT: {direction}</b>\n"
        f"🧠 <b>ANALYSIS:</b>\n{reason}\n\n"
        f"⚠️ <i>{safe_note}</i>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 <b>OWNER: {html.escape(NEWS_OWNER)}</b>"
    )


def render_news_analysis_card(data: dict) -> bytes:
    """Render the approved GHOST MATRIX news card as a Telegram-ready PNG."""
    direction, explanation, note = analyze_manual_news(data)
    width, height = 1080, 760
    bg = "#080D1A"
    img = Image.new("RGB", (width, height), bg)
    draw = ImageDraw.Draw(img)
    font = _chart_font
    # Header panel
    draw.rounded_rectangle((28, 24, width - 28, 142), radius=28, fill="#101D35", outline="#254B83", width=2)
    draw.ellipse((52, 50, 116, 114), fill="#173B78", outline="#62B8FF", width=2)
    draw.text((67, 62), "G", font=font(38), fill="#8AD0FF")
    draw.text((140, 43), "GHOST MATRIX", font=font(38), fill="#F1F6FF")
    draw.text((142, 91), "NEWS ANALYZER", font=font(21), fill="#8CA6C8")
    draw.rounded_rectangle((855, 53, 1020, 108), radius=16, fill="#173B78", outline="#2876C9", width=2)
    draw.text((890, 67), "PAID", font=font(28), fill="#69BDFF")

    event_label = f"{data['currency']}  •  {data['event']}"
    draw.text((48, 174), event_label[:52], font=font(28), fill="#B9D7FF")
    labels = [("ACTUAL", data["actual_text"]), ("FORECAST", data["forecast_text"]), ("PREVIOUS", data["previous_text"])]
    x_positions = [38, 373, 708]
    for x, (label, value) in zip(x_positions, labels):
        draw.rounded_rectangle((x, 226, x + 320, 344), radius=20, fill="#111D30", outline="#263B56", width=2)
        draw.text((x + 22, 246), label, font=font(20), fill="#90A7C5")
        draw.text((x + 22, 282), str(value)[:18], font=font(34), fill="#FFFFFF")

    if direction.startswith("UP"):
        panel, accent, arrow = "#103D30", "#5AE2AF", "UP  ↑"
    elif direction.startswith("DOWN"):
        panel, accent, arrow = "#421D2A", "#FF7D8B", "DOWN  ↓"
    else:
        panel, accent, arrow = "#3B3217", "#F3D477", "UNCLEAR"
    draw.rounded_rectangle((38, 372, width - 38, 512), radius=22, fill=panel, outline=accent, width=2)
    draw.text((72, 392), arrow, font=font(46), fill=accent)
    # Use English on the PNG because the runtime may not have a Bengali-capable font.
    event_lower = str(data.get("event", "")).lower()
    lower_is_better = any(term in event_lower for term in (
        "unemployment rate", "jobless claims", "initial claims", "continuing claims",
        "claims", "unemployment", "job losses", "layoffs", "bad loans",
    ))
    if direction.startswith("UP"):
        short_reason = "Lower than forecast may support currency." if lower_is_better else "Actual is above forecast."
    elif direction.startswith("DOWN"):
        short_reason = "Actual is below forecast." if not lower_is_better else "Higher than forecast may weigh on currency."
    else:
        short_reason = "No clear surprise versus forecast."
    draw.text((74, 458), short_reason, font=font(20), fill="#E6F0FA")

    draw.line((44, 550, width - 44, 550), fill="#20324C", width=2)
    draw.text((48, 574), "NEWS-BASED ANALYSIS", font=font(19), fill="#91A5C0")
    draw.text((48, 606), "Not a confirmed Quotex M1 signal", font=font(22), fill="#D5DFEC")
    draw.text((48, 674), f"OWNER: {NEWS_OWNER}", font=font(23), fill="#B9D7FF")
    draw.text((width - 350, 681), "GHOST MATRIX  •  NEWS", font=font(15), fill="#7186A5")

    output = io.BytesIO()
    img.save(output, format="PNG", optimize=True)
    return output.getvalue()


def _event_datetime(event: dict) -> Optional[datetime]:
    for key in ("time_utc", "datetime", "date_time", "release_time"):
        raw = event.get(key)
        if not raw:
            continue
        try:
            dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(BANGLADESH_TZ)
        except (TypeError, ValueError):
            continue
    raw_date = event.get("date")
    if raw_date:
        try:
            dt = datetime.fromisoformat(str(raw_date))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(BANGLADESH_TZ)
        except (TypeError, ValueError):
            return None
    return None


def get_economic_calendar(days: int = 7) -> list[dict]:
    """Fetch high-impact calendar events from the no-key Finance Calendar API."""
    today = datetime.now(BANGLADESH_TZ).date()
    end = today + timedelta(days=max(1, min(int(days), 14)))
    response = http_session.get(
        f"{FINANCE_CALENDAR_BASE}/calendar",
        params={"from": today.isoformat(), "to": end.isoformat(), "impact": "high", "limit": 100},
        timeout=20,
    )
    response.raise_for_status()
    payload = response.json()
    events = payload.get("events", []) if isinstance(payload, dict) else payload
    if not isinstance(events, list):
        raise RuntimeError("Calendar API returned an unexpected response format")
    result = []
    for event in events:
        if not isinstance(event, dict):
            continue
        impact = str(event.get("impact", "")).lower()
        if impact and impact not in {"high", "3", "high impact"}:
            continue
        dt = _event_datetime(event)
        name = str(event.get("title") or event.get("name") or event.get("event") or "Economic event")
        currency = str(event.get("currency") or event.get("country_code") or event.get("country") or "—")
        # Prefer explicitly supplied currency; otherwise use country label as available.
        currency_aliases = {
            "us": "USD", "usa": "USD", "united states": "USD",
            "gb": "GBP", "uk": "GBP", "united kingdom": "GBP",
            "eu": "EUR", "euro area": "EUR", "eurozone": "EUR",
            "au": "AUD", "australia": "AUD", "ca": "CAD", "canada": "CAD",
            "jp": "JPY", "japan": "JPY", "ch": "CHF", "switzerland": "CHF",
            "nz": "NZD", "new zealand": "NZD", "cn": "CNY", "china": "CNY",
        }
        if currency.lower() in currency_aliases:
            currency = currency_aliases[currency.lower()]
        result.append({
            "datetime": dt,
            "name": name,
            "currency": currency,
            "impact": impact or "high",
            "forecast": event.get("consensus", event.get("forecast")),
            "previous": event.get("prior", event.get("previous")),
            "actual": event.get("actual"),
            "url": event.get("url"),
        })
    result.sort(key=lambda item: item["datetime"] or datetime.max.replace(tzinfo=BANGLADESH_TZ))
    return result


def format_economic_calendar(events: list[dict], days: int = 7) -> str:
    lines = [
        "👻 GHOST MATRIX",
        "━━━━━━━━━━━━━━━━━━━━",
        "📰 ECONOMIC NEWS CALENDAR        🔵 PAID",
        f"📅 High-impact events • Next {days} days • Bangladesh time",
        "━━━━━━━━━━━━━━━━━━━━",
    ]
    if not events:
        lines.append("এই সময়সীমায় কোনো high-impact event পাওয়া যায়নি।")
    else:
        for event in events[:12]:
            dt = event.get("datetime")
            date_time = dt.strftime("%d %b • %I:%M %p") if dt else "সময় যাচাই করা যায়নি"
            lines.append(
                f"\n🔴 {event['currency']} • {event['name']}\n"
                f"🕒 {date_time} (UTC+6)\n"
                f"⚠️ Impact: HIGH"
            )
            if event.get("url"):
                lines.append(f"🔗 {event['url']}")
    lines.extend([
        "\nℹ️ Actual release-এর পরে পাওয়া গেলে আবার Actual/Forecast/Previous পাঠিয়ে Analyzer চালাও।",
        f"Source: {NEWS_SOURCE_URL}",
        f"👤 OWNER: {NEWS_OWNER}",
    ])
    return "\n".join(lines)


# ------------------------------ Commands -----------------------------
def handle_command(text: str, chat_id: str, photo_file_id: Optional[str] = None) -> None:
    clean = (text or "").strip()
    command = clean.split()[0].lower() if clean else ""
    args = clean.split(maxsplit=1)[1] if len(clean.split(maxsplit=1)) > 1 else ""

    if photo_file_id:
        send_message("📷 Chart screenshot analysis\n\n" + analyze_chart_image(photo_file_id), chat_id)
        return

    manual_news = parse_manual_news_input(clean)
    if manual_news:
        full_analysis = format_news_analysis(manual_news)
        send_message(full_analysis, chat_id, parse_mode="HTML")
        return

    if command in ("/start", "/help"):
        send_message(
            "👻 GHOST MATRIX V2.0\n\n"
            "Commands:\n"
            "/status — bot/API/database status\n"
            "/markets — cached market catalog count\n"
            "/signals — recent logged setups\n"
            "/future — manually scan all 18 selected pairs; sends one combined list\n"
            "/stats — manually logged WIN/LOSS only\n"
            "/win [ID] — mark a signal WIN\n"
            "/loss [ID] — mark a signal LOSS\n"
            "/risk — risk reminder\n"
            "/learn — পরের Price Action Lesson ও ছবি (একবারে ১টি)\n"
            "/learn reset — Lesson 1 থেকে আবার শুরু\n"
            "/news — আগামী ৭ দিনের high-impact economic news\n"
            "News input: AUD CPI\nAct-4.7%\nFor-4.6%\nPre-4.7% — auto analyzer\n"
            "/ask your question — optional AI answer\n\n"
            "Automatic signal messages are OFF. Use /future to scan manually. External data is not Quotex-verified.", chat_id)
    elif command == "/status":
        db_ok, db_msg = init_database()
        msg = (
            "🛠 GHOST MATRIX STATUS\n"
            f"Bot token: {'configured' if BOT_TOKEN else 'MISSING'}\n"
            f"Chat ID: {'configured' if CHAT_ID else 'MISSING'}\n"
            f"Twelve Data key: {'configured' if TWELVE_DATA_API_KEY else 'MISSING'}\n"
            f"Database: {'OK' if db_ok else 'NOT READY'} — {db_msg}\n"
            f"API credits used today (tracked): {db_api_calls_today()}/{DAILY_CREDIT_BUDGET}\n"
            f"Catalog symbols: {state['catalog_count']}\n"
            f"Last scan UTC: {state['last_scan_utc'] or 'not yet'}\n"
            f"Last scan Bangladesh time: {bd_time(datetime.fromisoformat(state['last_scan_utc'])) + ' UTC+6' if state['last_scan_utc'] else 'not yet'}\n"
            "Automatic signals: OFF (manual /future only)\n"
            f"Last scanner error: {state['last_error'] or 'none'}\n"
            "Data source is external; Quotex verification: NO"
        )
        send_message(msg, chat_id)
    elif command == "/markets":
        catalog = get_catalog()
        send_message(
            f"🎯 GHOST MATRIX — SELECTED MARKETS ({len(catalog)})\n" +
            "\n".join(f"{i}. {x['symbol']}" for i, x in enumerate(catalog, 1)) +
            "\n\nএই তালিকা তোমার নির্বাচিত pair; Quotex-এ REAL tab-এ availability নিজে মিলিয়ে দেখো.", chat_id)
    elif command == "/signals":
        signals = fetch_recent_signals(10)
        if not signals:
            send_message("📋 এখনো কোনো সিগনাল লগ নেই। /future দিয়ে ম্যানুয়াল স্ক্যান করো।", chat_id)
        else:
            lines = ["📋 GHOST MATRIX — RECENT SIGNALS", "ফল জানাতে /win ID বা /loss ID ব্যবহার করো।", ""]
            for sig in signals:
                sid = sig.get("id", "?")
                sym = sig.get("symbol", "?")
                direc = sig.get("direction", "?")
                price = sig.get("entry_price", "N/A")
                ctime = bd_time(sig.get("candle_time"))
                status = sig.get("status", "PENDING")
                lines.append(f"ID {sid} | {sym} | {direc} | {status}\nদাম: {price} | Candle BD: {ctime} UTC+6\n/win {sid} অথবা /loss {sid}")
            send_message("\n\n".join(lines), chat_id)
    elif command == "/future":
        try:
            catalog = get_catalog()
            if not catalog:
                raise RuntimeError("REAL_MARKET_SYMBOLS list is empty")
            send_message(
                f"🔎 Manual scan শুরু হলো: {len(catalog)}টি pair.\n"
                "API limit বজায় রাখতে অনুরোধগুলো ধীরে করা হবে; শেষ হলে সব সম্ভাব্য setup একসাথে পাঠাব।\n"
                "Automatic signals OFF — এই স্ক্যানটি শুধু তোমার /future command-এর জন্য।", chat_id)
            results = []
            last_request_at = 0.0
            for index, item in enumerate(catalog):
                # 8 requests/minute maximum: keep at least 8 seconds between requests.
                if last_request_at:
                    wait = 8.0 - (time.monotonic() - last_request_at)
                    if wait > 0:
                        time.sleep(wait)
                if db_api_calls_today() >= DAILY_CREDIT_BUDGET:
                    results.append({"symbol": item["symbol"], "direction": "NO TRADE", "reason": "দৈনিক API safety budget শেষ; এই pair স্ক্যান হয়নি।"})
                    continue
                try:
                    last_request_at = time.monotonic()
                    results.append(scan_one_symbol(item["symbol"]))
                except Exception as scan_exc:
                    log.warning("Manual scan failed for %s: %s", item["symbol"], scan_exc)
                    results.append({"symbol": item["symbol"], "direction": "NO TRADE", "reason": f"ডেটা পাওয়া যায়নি: {str(scan_exc)[:120]}"})
            state["last_scan_utc"] = datetime.now(timezone.utc).isoformat()
            state["last_error"] = None
            send_message(format_future_batch(results, len(catalog)), chat_id)
        except Exception as exc:
            send_message(f"❌ Manual scan failed: {exc}", chat_id)
    elif command == "/stats":
        wins, losses, total = get_stats()
        rate = round(wins * 100 / total, 1) if total else None
        send_message(
            f"📊 MANUALLY LOGGED RESULTS\nWIN: {wins}\nLOSS: {losses}\nTotal: {total}\n"
            f"Win rate: {str(rate) + '%' if rate is not None else 'N/A (no results)'}\n"
            "This is only based on /win and /loss entries, not an automatic broker result.", chat_id)
    elif command in ("/win", "/loss"):
        signal_id = None
        if args:
            try:
                signal_id = int(args.strip().split()[0])
            except ValueError:
                send_message(f"ব্যবহার: {command} অথবা {command} SIGNAL_ID (যেমন {command} 12)", chat_id)
                return
        outcome = "WIN" if command == "/win" else "LOSS"
        updated, signal = update_signal_result(outcome, signal_id)
        if not updated or not signal:
            send_message("কোনো PENDING signal পাওয়া যায়নি। /signals দেখে সঠিক ID ব্যবহার করো।", chat_id)
            return
        wins, losses, total = get_stats()
        rate = round(wins * 100 / total, 1) if total else 0.0
        sw, sl, st = get_symbol_stats(str(signal.get("symbol", "")))
        sr = round(sw * 100 / st, 1) if st else 0.0
        send_message(
            f"{'🟢' if outcome == 'WIN' else '🔴'} RESULT SAVED\n"
            f"Signal ID: {signal.get('id')}\nAsset: {signal.get('symbol')} | Signal: {signal.get('direction')}\n"
            f"Result: {outcome}\n\n"
            f"📊 এই pair-এর হিসাব ({signal.get('symbol')}): {st}টি ফল\nWIN: {sw} | LOSS: {sl}\n"
            f"Pair win rate: {sr}% | pair loss rate: {round(sl * 100 / st, 1) if st else 0.0}%\n\n"
            f"🌐 সব pair মিলিয়ে: {total}টি ফল\nWIN: {wins} | LOSS: {losses}\n"
            f"Overall win rate: {rate}% | overall loss rate: {round(losses * 100 / total, 1) if total else 0.0}%\n"
            "এটি তোমার হাতে দেওয়া ফলাফলের হিসাব; Quotex থেকে auto-result যাচাই নয়।", chat_id)
    elif command == "/risk":
        send_message(
            "⚠️ Risk reminder\n"
            "• কোনো সিগন্যাল নিশ্চিত নয়।\n• হারানো টাকা ফেরত পেতে stake বাড়িও না।\n"
            "• Demo/backtest আগে ব্যবহার করো।\n• এক ট্রেডে সামর্থ্যের বাইরে ঝুঁকি নিও না।\n"
            "• Quotex-এর candle/asset নিজে মিলিয়ে দেখো; বাইরের feed আলাদা হতে পারে.", chat_id)
    elif command == "/learn":
        if args.strip().lower() == "reset":
            send_price_action_course(chat_id, reset=True)
        elif not args.strip():
            send_price_action_course(chat_id)
        else:
            send_message(
                "ব্যবহার:\n"
                "/learn — পরের Lesson ও ছবি\n"
                "/learn reset — Lesson 1 থেকে আবার শুরু",
                chat_id,
            )
    elif command == "/news":
        days = 7
        if args.strip().lower() == "today":
            days = 1
        elif args.strip().lower() == "week":
            days = 7
        try:
            events = get_economic_calendar(days)
            send_message(format_economic_calendar(events, days), chat_id)
        except Exception as exc:
            log.warning("Economic calendar request failed: %s", exc)
            send_message(
                "👻 GHOST MATRIX — NEWS CALENDAR\n"
                "🔵 PAID\n\n"
                "লাইভ economic calendar এখন পাওয়া যায়নি। কিছুক্ষণ পরে /news আবার চেষ্টা করো।\n"
                f"Source: {NEWS_SOURCE_URL}\n"
                f"👤 OWNER: {NEWS_OWNER}", chat_id)
    elif command.startswith("/ask"):
        if not args:
            send_message("ব্যবহার: /ask তোমার প্রশ্ন লিখো", chat_id)
        else:
            send_message(ai_answer(args), chat_id)
    elif clean.startswith("/"):
        send_message("Command বুঝতে পারিনি। /help লিখে available commands দেখো.", chat_id)
    elif clean:
        send_message(
            "সাধারণ প্রশ্নের জন্য /ask তোমার প্রশ্ন লিখো ব্যবহার করো।\n\n"
            "📰 News Analyzer format:\n"
            "AUD CPI\nAct-4.7%\nFor-4.6%\nPre-4.7%\n\n"
            "Actual, Forecast, Previous তিনটি মান দিলেই auto-analysis হবে।", chat_id)


def telegram_poll_loop() -> None:
    offset = None
    while True:
        if not BOT_TOKEN:
            log.error("BOT_TOKEN missing; Telegram polling paused")
            time.sleep(30)
            continue
        try:
            params = {"timeout": 25}
            if offset is not None:
                params["offset"] = offset
            response = http_session.get(f"{TELEGRAM_BASE}/getUpdates", params=params, timeout=35)
            response.raise_for_status()
            payload = response.json()
            if not payload.get("ok"):
                raise RuntimeError(str(payload))
            for update in payload.get("result", []):
                offset = update["update_id"] + 1
                message = update.get("message") or update.get("edited_message")
                if not message:
                    continue
                chat = message.get("chat", {})
                chat_id = str(chat.get("id", ""))
                if not is_allowed_chat(chat_id):
                    continue
                text_in = message.get("text") or message.get("caption") or ""
                # Accept JSON commands from Telegram Web App keyboard buttons.
                # Telegram Main Mini App launch mode may not support sendData().
                web_app_data = message.get("web_app_data")
                if web_app_data:
                    try:
                        web_payload = json.loads(web_app_data.get("data", "{}"))
                        if isinstance(web_payload, dict):
                            web_command = web_payload.get("command", "")
                            if isinstance(web_command, str) and web_command.startswith("/"):
                                text_in = web_command
                    except (json.JSONDecodeError, TypeError, ValueError):
                        log.warning("Ignoring invalid Telegram Mini App data")
                photos = message.get("photo") or []
                photo_id = photos[-1].get("file_id") if photos else None
                handle_command(text_in, chat_id, photo_id)
        except Exception as exc:
            log.warning("Telegram polling error: %s", exc)
            time.sleep(5)

# ------------------------ Authentication -----------------------------
def _auth_db_ready() -> bool:
    return bool(DATABASE_URL and psycopg2 is not None)


def _user_by_email(email: str):
    if not _auth_db_ready():
        raise RuntimeError("Authentication requires a working DATABASE_URL/PostgreSQL database.")
    with db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT id, email, password_hash, is_admin, email_verified FROM gm_users WHERE LOWER(email)=LOWER(%s)", (email,))
            row = cur.fetchone()
            return dict(row) if row else None


def _send_verification_email(email: str, token: str) -> None:
    if not all([SMTP_HOST, SMTP_USER, SMTP_PASSWORD, SMTP_FROM, APP_BASE_URL]):
        raise RuntimeError("Email verification is not configured. Check SMTP_* and APP_BASE_URL Render variables.")
    link = f"{APP_BASE_URL}/api/auth/verify?token={token}"
    msg = EmailMessage()
    msg["Subject"] = "Verify your GHOST MATRIX account"
    msg["From"] = SMTP_FROM
    msg["To"] = email
    msg.set_content(f"Welcome to GHOST MATRIX. Verify your email within 24 hours by opening this link:\n\n{link}\n\nIf you did not expect this account, ignore this email.")
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as server:
        server.ehlo()
        server.starttls()
        server.ehlo()
        server.login(SMTP_USER, SMTP_PASSWORD)
        server.send_message(msg)



def _bootstrap_admin() -> None:
    if not (ADMIN_EMAIL and ADMIN_PASSWORD):
        log.warning(
            "ADMIN_EMAIL/ADMIN_PASSWORD not configured; "
            "no initial admin account created"
        )
        return

    if len(ADMIN_PASSWORD) < 12:
        log.error(
            "ADMIN_PASSWORD must be at least 12 characters; "
            "initial admin not bootstrapped"
        )
        return

    try:
        with db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO gm_users (
                        email, password_hash, is_admin,
                        email_verified, verified_at
                    )
                    VALUES (%s, %s, TRUE, TRUE, NOW())
                    ON CONFLICT (email) DO UPDATE SET
                        password_hash = EXCLUDED.password_hash,
                        is_admin = TRUE,
                        email_verified = TRUE,
                        verified_at = COALESCE(
                            gm_users.verified_at, NOW()
                        )
                    """,
                    (
                        ADMIN_EMAIL,
                        generate_password_hash(ADMIN_PASSWORD),
                    ),
                )

        log.info(
            "Admin account bootstrap completed for configured ADMIN_EMAIL"
        )

    except Exception:
        log.exception("Admin bootstrap failed")



def login_required(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        uid = session.get("user_id")
        if not uid:
            return jsonify({"error": "Login required", "auth_required": True}), 401
        try:
            with db_connection() as conn:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    cur.execute("SELECT id,email,is_admin,email_verified FROM gm_users WHERE id=%s", (uid,))
                    user = cur.fetchone()
            if not user or not user["email_verified"]:
                session.clear()
                return jsonify({"error": "Please verify your email and log in again.", "auth_required": True}), 401
            request.auth_user = dict(user)
        except Exception as exc:
            log.error("Auth session check failed: %s", exc)
            return jsonify({"error": "Authentication database unavailable"}), 503
        return fn(*args, **kwargs)
    return wrapped


def admin_required(fn):
    @wraps(fn)
    @login_required
    def wrapped(*args, **kwargs):
        if not request.auth_user.get("is_admin"):
            return jsonify({"error": "Admin access required"}), 403
        return fn(*args, **kwargs)
    return wrapped


# -------------------------------- Flask ------------------------------
@app.get("/")
def home():
    return "GHOST MATRIX V2.0 is running. Data source: external Twelve Data; Quotex verification: disabled.", 200

@app.get("/app")
@app.get("/app/")
def mini_app():
    # Keep index.html in the same repository folder as main.py.
    return send_from_directory(os.path.dirname(os.path.abspath(__file__)), "index.html")

@app.get("/health")
def health():
    return jsonify({
        "status": "running",
        "bot_token_configured": bool(BOT_TOKEN),
        "chat_id_configured": bool(CHAT_ID),
        "twelve_data_key_configured": bool(TWELVE_DATA_API_KEY),
        "ai_api_key_configured": bool(AI_API_KEY),
        "database_url_configured": bool(DATABASE_URL),
        "catalog_count": state["catalog_count"],
        "last_scan_utc": state["last_scan_utc"],
        "last_error": state["last_error"],
        "quotex_verified": False,
    }), 200

@app.get("/status")
def status_route():
    db_ok, db_msg = init_database()
    return jsonify({
        "running": True,
        "database_ok": db_ok,
        "database_message": db_msg,
        "api_calls_today": db_api_calls_today(),
        "daily_budget": DAILY_CREDIT_BUDGET,
        "catalog_count": state["catalog_count"],
        "last_scan_utc": state["last_scan_utc"],
        "last_error": state["last_error"],
        "quotex_verified": False,
    }), 200



@app.post("/api/auth/login")
def auth_login():
    if not FLASK_SECRET_KEY:
        return jsonify({"error": "FLASK_SECRET_KEY is not configured in Render."}), 503
    body = request.get_json(silent=True) or {}
    email = str(body.get("email", "")).strip().lower()
    password = str(body.get("password", ""))
    if not email or not password or len(email) > 254 or len(password) > 1024:
        return jsonify({"error": "Email and password are required."}), 400
    try:
        user = _user_by_email(email)
    except Exception as exc:
        log.error("Login database error: %s", exc)
        return jsonify({"error": "Authentication database unavailable. Check DATABASE_URL and database connection."}), 503
    
    
    if not user:
        log.warning("Login debug: no account found for submitted email")
        return jsonify({"error": "Email or password is incorrect."}), 401

    if not check_password_hash(user["password_hash"], password):
        log.warning("Login debug: account found, password mismatch")
        return jsonify({"error": "Email or password is incorrect."}), 401


    if not user["email_verified"]:
        return jsonify({"error": "Email is not verified yet. Open the verification link sent to your email."}), 403
    session.clear()
    session.permanent = True
    session["user_id"] = int(user["id"])
    return jsonify({"ok": True, "user": {"email": user["email"], "is_admin": bool(user["is_admin"])}})


@app.get("/api/auth/me")
def auth_me():
    uid = session.get("user_id")
    if not uid:
        return jsonify({"authenticated": False}), 200
    try:
        with db_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SELECT id,email,is_admin,email_verified FROM gm_users WHERE id=%s", (uid,))
                user = cur.fetchone()
        if not user or not user["email_verified"]:
            session.clear()
            return jsonify({"authenticated": False}), 200
        return jsonify({"authenticated": True, "user": {"email": user["email"], "is_admin": bool(user["is_admin"])}})
    except Exception as exc:
        log.error("Auth status error: %s", exc)
        return jsonify({"authenticated": False, "error": "Authentication database unavailable"}), 503


@app.post("/api/auth/logout")
def auth_logout():
    session.clear()
    return jsonify({"ok": True})


@app.get("/api/auth/verify")
def auth_verify():
    token = request.args.get("token", "")
    if not token:
        return "Verification token missing. Please use the full link from your email.", 400
    try:
        payload = _auth_serializer.loads(token, max_age=86400)
        email = str(payload.get("email", "")).lower()
    except SignatureExpired:
        return "Verification link expired. Ask your admin to create a new account and verification email.", 400
    except (BadSignature, AttributeError, TypeError):
        return "Invalid verification link.", 400
    try:
        with db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE gm_users SET email_verified=TRUE, verified_at=NOW() WHERE LOWER(email)=LOWER(%s)", (email,))
                if cur.rowcount != 1:
                    return "Account not found. Contact your GHOST MATRIX admin.", 404
        return "Email verified successfully. Return to GHOST MATRIX and log in.", 200
    except Exception as exc:
        log.error("Email verification database error: %s", exc)
        return "Verification could not be completed because the database is unavailable.", 503


@app.post("/api/auth/admin/users")
@admin_required
def auth_admin_create_user():
    body = request.get_json(silent=True) or {}
    email = str(body.get("email", "")).strip().lower()
    password = str(body.get("password", ""))
    if not email or "@" not in email or len(email) > 254:
        return jsonify({"error": "Enter a valid email address."}), 400
    if len(password) < 12 or len(password) > 1024:
        return jsonify({"error": "Temporary password must be at least 12 characters."}), 400
    try:
        with db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("INSERT INTO gm_users (email,password_hash,is_admin,email_verified) VALUES (%s,%s,FALSE,FALSE)",
                            (email, generate_password_hash(password)))
    except Exception as exc:
        if "unique" in str(exc).lower() or "duplicate" in str(exc).lower():
            return jsonify({"error": "An account with this email already exists."}), 409
        log.error("Create user database error: %s", exc)
        return jsonify({"error": "Could not create account. Check database connection."}), 503
    token = _auth_serializer.dumps({"email": email})
    try:
        _send_verification_email(email, token)
    except Exception as exc:
        log.error("Verification email send failed: %s", exc)
        # Keep the account unverified; never claim email was sent.
        return jsonify({"ok": False, "account_created": True, "error": "Account was created but verification email could not be sent. Check SMTP settings; ask admin to delete/recreate this account after fixing them."}), 502
    return jsonify({"ok": True, "message": "Account created. Verification email sent; user must verify before login."}), 201


@app.get("/api/dashboard")
@login_required
def api_dashboard():
    """Read-only dashboard summary; never returns secrets."""
    try:
        db_ok, db_msg = init_database()
    except Exception as exc:
        db_ok, db_msg = False, str(exc)
    wins, losses, total = get_stats()
    return jsonify({
        "app": "GHOST MATRIX", "status": "running",
        "data_source": "Twelve Data external feed", "quotex_verified": False,
        "twelve_data_key_configured": bool(TWELVE_DATA_API_KEY),
        "bot_token_configured": bool(BOT_TOKEN), "chat_id_configured": bool(CHAT_ID),
        "database_ok": bool(db_ok), "database_message": str(db_msg),
        "catalog_count": len(get_catalog()), "last_scan_utc": state.get("last_scan_utc"),
        "last_error": state.get("last_error"), "api_calls_today": db_api_calls_today(),
        "daily_budget": DAILY_CREDIT_BUDGET, "signals": fetch_recent_signals(50),
        "stats": {"wins": wins, "losses": losses, "total": total}
    }), 200

@app.get("/api/signals")
@login_required
def api_signals():
    try:
        limit = max(1, min(100, int(request.args.get("limit", "20"))))
    except (TypeError, ValueError):
        limit = 20
    return jsonify({"signals": fetch_recent_signals(limit), "source": "Twelve Data external feed", "quotex_verified": False}), 200

@app.get("/api/stats")
@login_required
def api_stats():
    wins, losses, total = get_stats()
    return jsonify({"wins": wins, "losses": losses, "total": total, "quotex_verified": False,
                    "note": "Only manually recorded outcomes are counted; this is not a guaranteed win rate."}), 200

# ----------------------- Mini App API endpoints -----------------------
# Lightweight in-process rate limits protect paid/external API usage. For a
# public deployment, enable Telegram initData validation or an app access token.
_api_buckets: dict[str, list[float]] = {}
_api_buckets_lock = threading.Lock()
_last_app_scan = 0.0

def _api_allowed(bucket: str, limit: int, window: int = 60) -> bool:
    now = time.monotonic()
    key = f"{request.remote_addr or 'unknown'}:{bucket}"
    with _api_buckets_lock:
        recent = [stamp for stamp in _api_buckets.get(key, []) if now - stamp < window]
        if len(recent) >= limit:
            _api_buckets[key] = recent
            return False
        recent.append(now)
        _api_buckets[key] = recent
        return True

@app.get("/api/markets")
@login_required
def api_markets():
    return jsonify({"markets": get_catalog(), "source": "configured allowlist", "quotex_verified": False})

@app.post("/api/scan")
@login_required
def api_scan_one():
    global _last_app_scan
    if not _api_allowed("scan", 4, 60):
        return jsonify({"error": "Scan limit reached. এক মিনিট পরে আবার চেষ্টা করো।"}), 429
    body = request.get_json(silent=True) or {}
    symbol = str(body.get("symbol", "")).strip().upper()
    allowed = {str(item.get("symbol", "")).upper() for item in get_catalog()}
    if not symbol or symbol not in allowed:
        return jsonify({"error": "তালিকা থেকে একটি valid market নির্বাচন করো।"}), 400
    with _api_buckets_lock:
        now = time.monotonic()
        if now - _last_app_scan < 8:
            return jsonify({"error": "API limit বজায় রাখতে ৮ সেকেন্ড অপেক্ষা করো।"}), 429
        _last_app_scan = now
    if not TWELVE_DATA_API_KEY:
        return jsonify({"error": "TWELVE_DATA_API_KEY Render Environment-এ সেট নেই।"}), 503
    if db_api_calls_today() >= DAILY_CREDIT_BUDGET:
        return jsonify({"error": "দৈনিক API safety budget শেষ হয়েছে।"}), 429
    try:
        result = scan_one_symbol(symbol)
        candle = result.get("candle") or {}
        return jsonify({
            "result": {k: v for k, v in result.items() if k not in ("candle", "backtest")},
            "candle_time": bd_time(candle.get("time")),
            "backtest": result.get("backtest", {}),
            "source": "Twelve Data external feed", "quotex_verified": False
        })
    except Exception as exc:
        log.warning("Mini App scan failed for %s: %s", symbol, exc)
        state["last_error"] = str(exc)[:240]
        return jsonify({"error": f"Market data পাওয়া যায়নি: {str(exc)[:180]}"}), 502

@app.post("/api/ask")
@login_required
def api_ask():
    if not _api_allowed("ask", 8, 60):
        return jsonify({"error": "অনেক request হয়েছে। এক মিনিট পরে চেষ্টা করো।"}), 429
    body = request.get_json(silent=True) or {}
    question = str(body.get("question", "")).strip()
    if not question:
        return jsonify({"error": "প্রশ্ন লিখো।"}), 400
    if len(question) > 3000:
        return jsonify({"error": "প্রশ্ন ৩০০০ অক্ষরের মধ্যে রাখো।"}), 400
    return jsonify({"answer": ai_answer(question), "provider": "configured AI service" if AI_API_KEY else "AI not configured"})

@app.post("/api/analyze-image")
@login_required
def api_analyze_image():
    if not _api_allowed("vision", 4, 60):
        return jsonify({"error": "এক মিনিটে সর্বোচ্চ ৪টি screenshot analysis। পরে আবার চেষ্টা করো।"}), 429
    upload = request.files.get("image")
    if upload is None or not upload.filename:
        return jsonify({"error": "একটি chart screenshot upload করো।"}), 400
    if not AI_API_KEY:
        return jsonify({"error": "AI_API_KEY Render Environment-এ সেট নেই; screenshot analysis চালু নয়।"}), 503
    raw = upload.read(12 * 1024 * 1024 + 1)
    if not raw or len(raw) > 12 * 1024 * 1024:
        return jsonify({"error": "ছবি খালি অথবা ১২ MB-এর বেশি। ছোট screenshot upload করো।"}), 413
    try:
        im = Image.open(io.BytesIO(raw))
        im.verify()
        mime = upload.mimetype if upload.mimetype in ("image/jpeg", "image/png", "image/webp") else "image/jpeg"
        encoded = base64.b64encode(raw).decode("ascii")
        payload = {
            "model": GROQ_VISION_MODEL,
            "messages": [
                {"role": "system", "content": "You are a cautious chart-reading tutor. Answer in Bengali. Analyze only visible screenshot evidence: asset/timeframe if visible, trend, candle structure, support/resistance, uncertainty and missing context. Do not invent prices, claim broker verification, give guaranteed CALL/PUT or promise profit. If unclear say NO TRADE / insufficient evidence."},
                {"role": "user", "content": [
                    {"type": "text", "text": "এই screenshot-এর বিস্তারিত বাংলা analysis দাও: দৃশ্যমান trend, swing structure, support/resistance, candle patterns, কী confirm করতে হবে, এবং কোন তথ্য দেখা যাচ্ছে না। নিশ্চিত prediction দেবে না।"},
                    {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{encoded}"}}
                ]}
            ], "temperature": 0.1, "max_completion_tokens": 900
        }
        response = http_session.post(GROQ_BASE, headers={"Authorization": f"Bearer {AI_API_KEY}", "Content-Type": "application/json"}, json=payload, timeout=50)
        response.raise_for_status()
        answer = response.json()["choices"][0]["message"]["content"].strip()
        return jsonify({"analysis": answer, "warning": "Screenshot-based educational analysis only; not a Quotex-verified signal."})
    except Exception as exc:
        log.warning("Mini App screenshot analysis failed: %s", exc)
        return jsonify({"error": "ছবি বিশ্লেষণ করা যায়নি। image format, AI_API_KEY, vision model ও provider limit পরীক্ষা করো।"}), 502

@app.get("/api/news")
@login_required
def api_news():
    if not _api_allowed("news", 10, 60):
        return jsonify({"error": "একটু পরে আবার চেষ্টা করো।"}), 429
    try:
        days = max(1, min(14, int(request.args.get("days", "7"))))
        events = get_economic_calendar(days)
        clean = []
        for e in events[:50]:
            dt = e.get("datetime")
            clean.append({"datetime": dt.isoformat() if isinstance(dt, datetime) else None,
                          "datetime_bd": dt.strftime("%d %b %Y, %I:%M %p") if isinstance(dt, datetime) else "সময় যাচাই করা যায়নি",
                          "name": e.get("name"), "currency": e.get("currency"), "impact": e.get("impact", "high"),
                          "forecast": e.get("forecast"), "previous": e.get("previous"), "actual": e.get("actual"), "url": e.get("url")})
        return jsonify({"events": clean, "days": days, "source": NEWS_SOURCE_URL, "timezone": "Asia/Dhaka"})
    except Exception as exc:
        log.warning("Mini App news request failed: %s", exc)
        return jsonify({"error": "লাইভ economic calendar এখন পাওয়া যায়নি। পরে আবার চেষ্টা করো।", "source": NEWS_SOURCE_URL}), 502

@app.get("/api/lessons")
@login_required
def api_lessons():
    lessons = [{"key": x["key"], "title": x["title"], "caption": x["caption"], "image": f"/api/lessons/{x['key']}/image"} for x in PA_LESSONS]
    return jsonify({"lessons": lessons, "total": len(lessons), "note": "Illustrative examples, not live market data."})

@app.get("/api/lessons/<lesson_key>/image")
@login_required
def api_lesson_image(lesson_key: str):
    lesson = next((x for x in PA_LESSONS if x["key"] == lesson_key), None)
    if lesson is None:
        return jsonify({"error": "Lesson not found"}), 404
    return app.response_class(make_price_action_chart(lesson), mimetype="image/png", headers={"Cache-Control": "public, max-age=86400"})

@app.post("/api/result")
@login_required
def api_result():
    if not _api_allowed("result", 12, 60):
        return jsonify({"error": "একটু পরে আবার চেষ্টা করো।"}), 429
    body = request.get_json(silent=True) or {}
    try:
        signal_id = int(body.get("id"))
    except (TypeError, ValueError):
        return jsonify({"error": "Signal ID সঠিক নয়।"}), 400
    outcome = str(body.get("result", "")).upper()
    if outcome not in ("WIN", "LOSS"):
        return jsonify({"error": "Result WIN বা LOSS হতে হবে।"}), 400
    updated, signal = update_signal_result(outcome, signal_id)
    if not updated:
        return jsonify({"error": "এই ID-এর pending signal পাওয়া যায়নি।"}), 404
    return jsonify({"ok": True, "signal": signal, "note": "ম্যানুয়ালি দেওয়া ফল; Quotex থেকে স্বয়ংক্রিয়ভাবে যাচাই করা হয়নি।"})

@app.post("/api/news/analyze")
@login_required
def api_news_analyze():
    if not _api_allowed("news_analyze", 8, 60):
        return jsonify({"error": "এক মিনিটে সর্বোচ্চ ৮টি news analysis; পরে আবার চেষ্টা করো।"}), 429
    body = request.get_json(silent=True) or {}
    currency = str(body.get("currency", "")).strip().upper()
    event = str(body.get("event", "")).strip()
    actual = str(body.get("actual", "")).strip()
    forecast = str(body.get("forecast", "")).strip()
    previous = str(body.get("previous", "")).strip()
    if not currency or not event or not actual or not forecast:
        return jsonify({"error": "Currency, event, actual ও forecast পূরণ করো।"}), 400
    prompt = (f"Economic news explanation in Bengali. Currency: {currency}; event: {event}; actual: {actual}; forecast: {forecast}; previous: {previous or 'not supplied'}. Explain actual-vs-forecast carefully, likely first-order currency interpretation, what could reverse the reaction, and uncertainty. Do not claim current market reaction without live price data.")
    return jsonify({"analysis": ai_answer(prompt), "note": "Economic interpretation only; current price reaction is not verified."})

def start_background_threads() -> None:
    # Avoid duplicate start in environments that import the module more than once.
    if not state.get("threads_started"):
        state["threads_started"] = True
        state["catalog_count"] = len(get_catalog())
        threading.Thread(target=telegram_poll_loop, daemon=True, name="telegram-poll").start()
        # Deliberately do not start scanner_loop: automatic signals are disabled.

# Initialize DB and background workers when the Render process starts.
_db_ok, _db_msg = init_database()
if _db_ok:
    _bootstrap_admin()
if not _db_ok:
    log.warning("Database not ready at startup: %s", _db_msg)
start_background_threads()

if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
