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
import time
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from typing import Any, Optional

import requests
from flask import Flask, jsonify
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

session = requests.Session()
session.headers.update({"User-Agent": "GHOST-MATRIX-V2/2.0"})
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
                cur.execute("ALTER TABLE gm_signals ADD COLUMN IF NOT EXISTS result_updated_at TIMESTAMPTZ")
                cur.execute("CREATE INDEX IF NOT EXISTS gm_signals_created_idx ON gm_signals(created_at DESC)")
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
    response = session.post(f"{TELEGRAM_BASE}/{method}", json=payload or {}, timeout=timeout)
    response.raise_for_status()
    data = response.json()
    if not data.get("ok"):
        raise RuntimeError(str(data))
    return data.get("result")


def send_message(text: str, chat_id: Optional[str] = None) -> None:
    target = str(chat_id or CHAT_ID).strip()
    if not target or not BOT_TOKEN:
        log.info("Telegram message skipped: BOT_TOKEN/CHAT_ID missing")
        return
    # Telegram message limit is 4096 characters; leave margin.
    for start in range(0, len(text), 3900):
        try:
            telegram_call("sendMessage", {
                "chat_id": target,
                "text": text[start:start + 3900],
                "disable_web_page_preview": True,
            }, timeout=20)
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
    response = session.get(f"{TWELVE_BASE}/{endpoint.lstrip('/')}", params=query, timeout=25)
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
        response = session.post(GROQ_BASE,
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
        photo = session.get(file_url, timeout=25)
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
        response = session.post(GROQ_BASE,
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
        response = session.post(
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
        send_message("⚠️ চার্টের ছবি পাঠানো যায়নি। Render Logs দেখো।", target)
        return False


def send_price_action_course(chat_id: str) -> None:
    intro = (
        "📘 GHOST MATRIX — FULL PRICE ACTION LEARNING\n\n"
        "প্রতিটি ছবিই schematic example, live Quotex chart নয়। "
        "ছবি দেখে pattern চিনতে শেখো; শুধু pattern দেখে trade নিও না।\n\n"
        "নিচে ৭টি lesson একে একে পাঠানো হচ্ছে।"
    )
    send_message(intro, chat_id)
    for lesson in PA_LESSONS:
        photo = make_price_action_chart(lesson)
        send_photo_bytes(photo, lesson["caption"], chat_id)
        time.sleep(0.35)
    send_message(
        "✅ সব lesson শেষ। মনে রাখো: কোনো pattern নিশ্চিত ফল দেয় না। "
        "আগে demo-তে practice, screenshot journal, এবং Quotex-এ REAL pair/timeframe নিজে verify করো। "
        "Twelve Data-এর external price-কে Quotex-verified বলা যাবে না।",
        chat_id,
    )


# ------------------------------ Commands -----------------------------
def handle_command(text: str, chat_id: str, photo_file_id: Optional[str] = None) -> None:
    clean = (text or "").strip()
    command = clean.split()[0].lower() if clean else ""
    args = clean.split(maxsplit=1)[1] if len(clean.split(maxsplit=1)) > 1 else ""

    if photo_file_id:
        send_message("📷 Chart screenshot analysis\n\n" + analyze_chart_image(photo_file_id), chat_id)
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
            "/learn — ছবি-সহ বিস্তারিত Price Action course (৭টি lesson)\n"
            "/news — explains news feed status\n"
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
        send_price_action_course(chat_id)
    elif command == "/news":
        send_message(
            "📰 Real news feed এখন সংযুক্ত নেই। তাই bot live news/prediction বানিয়ে বলবে না।\n"
            "News-based signals পেতে আলাদা যাচাইকৃত news/calendar API যুক্ত করতে হবে.", chat_id)
    elif command.startswith("/ask"):
        if not args:
            send_message("ব্যবহার: /ask তোমার প্রশ্ন লিখো", chat_id)
        else:
            send_message(ai_answer(args), chat_id)
    elif clean.startswith("/"):
        send_message("Command বুঝতে পারিনি। /help লিখে available commands দেখো.", chat_id)
    elif clean:
        send_message("সাধারণ প্রশ্নের জন্য /ask তোমার প্রশ্ন লিখো ব্যবহার করো.", chat_id)


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
            response = session.get(f"{TELEGRAM_BASE}/getUpdates", params=params, timeout=35)
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
                photos = message.get("photo") or []
                photo_id = photos[-1].get("file_id") if photos else None
                handle_command(text_in, chat_id, photo_id)
        except Exception as exc:
            log.warning("Telegram polling error: %s", exc)
            time.sleep(5)

# -------------------------------- Flask ------------------------------
@app.get("/")
def home():
    return "GHOST MATRIX V2.0 is running. Data source: external Twelve Data; Quotex verification: disabled.", 200

@app.get("/health")
def health():
    return jsonify({
        "status": "running",
        "bot_token_configured": bool(BOT_TOKEN),
        "chat_id_configured": bool(CHAT_ID),
        "twelve_data_key_configured": bool(TWELVE_DATA_API_KEY),
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


def start_background_threads() -> None:
    # Avoid duplicate start in environments that import the module more than once.
    if not state.get("threads_started"):
        state["threads_started"] = True
        state["catalog_count"] = len(get_catalog())
        threading.Thread(target=telegram_poll_loop, daemon=True, name="telegram-poll").start()
        # Deliberately do not start scanner_loop: automatic signals are disabled.

# Initialize DB and background workers when the Render process starts.
_db_ok, _db_msg = init_database()
if not _db_ok:
    log.warning("Database not ready at startup: %s", _db_msg)
start_background_threads()

if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
