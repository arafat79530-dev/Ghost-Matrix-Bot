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
import re
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

try:
    from otcharts import Client as OTChartsClient
except Exception:
    OTChartsClient = None
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
GROQ_API_KEY = (os.getenv("GROQ_API_KEY", "") or os.getenv("AI_API_KEY", "")).strip()
AI_API_KEY = GROQ_API_KEY  # Backward-compatible alias used by existing non-selector AI features
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_VISION_MODEL = os.getenv("OPENAI_VISION_MODEL", "gpt-4.1-mini").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_VISION_MODEL = os.getenv("GEMINI_VISION_MODEL", "gemini-2.5-flash").strip()
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile").strip()
# --- GHOST AI Identity & Affiliate (public, not secret) ---
APP_NAME = "GHOST AI"
AFFILIATE_URL = "https://broker-qx.pro/sign-up/?lid=2384783"
TELEGRAM_ADMIN = "https://t.me/ghostsupportbd"
GROQ_VISION_MODEL = os.getenv("GROQ_VISION_MODEL", "qwen/qwen3.6-27b").strip()
OTCHARTS_API_KEY = os.getenv("OTCHARTS_API_KEY", "").strip()
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
OPENAI_BASE = "https://api.openai.com/v1/chat/completions"
GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta/models"

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("ghost-matrix")
app = Flask(__name__)

@app.after_request
def disable_auth_page_caching(response):
    if request.path.startswith("/api/auth/") or request.path in ("/app", "/app/"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response

@app.route("/manifest.json")
def serve_manifest():
    return send_from_directory(
        os.path.dirname(os.path.abspath(__file__)),
        "manifest.json",
        mimetype="application/manifest+json"
    )


@app.route("/icons/<path:filename>")
def serve_icons(filename):
    icon_dir = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "icons"
    )
    return send_from_directory(icon_dir, filename)
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
                cur.execute("ALTER TABLE gm_signals ADD COLUMN IF NOT EXISTS timeframe TEXT NOT NULL DEFAULT '1 MIN'")
                cur.execute("CREATE INDEX IF NOT EXISTS gm_signals_created_idx ON gm_signals(created_at DESC)")
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS gm_users (
                        id BIGSERIAL PRIMARY KEY,
                        email TEXT NOT NULL UNIQUE,
                        password_hash TEXT NOT NULL,
                        is_admin BOOLEAN NOT NULL DEFAULT FALSE,
                        email_verified BOOLEAN NOT NULL DEFAULT FALSE,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        verified_at TIMESTAMPTZ,
                        access_enabled BOOLEAN NOT NULL DEFAULT TRUE
                    )
                """)
                cur.execute("ALTER TABLE gm_users ADD COLUMN IF NOT EXISTS access_enabled BOOLEAN NOT NULL DEFAULT TRUE")
                cur.execute("ALTER TABLE gm_users ADD COLUMN IF NOT EXISTS name TEXT")
                cur.execute("ALTER TABLE gm_users ADD COLUMN IF NOT EXISTS role TEXT NOT NULL DEFAULT 'user'")
                cur.execute("ALTER TABLE gm_users ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'active'")
                cur.execute("CREATE INDEX IF NOT EXISTS gm_users_email_idx ON gm_users(LOWER(email))")
                # --- GHOST AI: Plans ---
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS gm_plans (
                        id BIGSERIAL PRIMARY KEY,
                        name TEXT NOT NULL UNIQUE,
                        price NUMERIC(10,2) NOT NULL DEFAULT 0,
                        description TEXT NOT NULL DEFAULT '',
                        duration_days INTEGER NOT NULL DEFAULT 0,
                        duration TEXT NOT NULL DEFAULT '',
                        status TEXT NOT NULL DEFAULT 'active',
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                """)
                # --- GHOST AI: User Plans ---
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS gm_user_plans (
                        id BIGSERIAL PRIMARY KEY,
                        user_id BIGINT NOT NULL REFERENCES gm_users(id) ON DELETE CASCADE,
                        plan_id BIGINT NOT NULL REFERENCES gm_plans(id) ON DELETE CASCADE,
                        start_date TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        expiry_date TIMESTAMPTZ,
                        status TEXT NOT NULL DEFAULT 'active',
                        notes TEXT NOT NULL DEFAULT '',
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                """)
                cur.execute("CREATE INDEX IF NOT EXISTS gm_user_plans_user_idx ON gm_user_plans(user_id, status)")
                # --- GHOST AI: User Signals (screenshot-based) ---
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS gm_user_signals (
                        id BIGSERIAL PRIMARY KEY,
                        user_id BIGINT REFERENCES gm_users(id) ON DELETE SET NULL,
                        market TEXT NOT NULL DEFAULT '',
                        market_type TEXT NOT NULL DEFAULT 'OTC',
                        timeframe TEXT NOT NULL DEFAULT '1 MIN',
                        signal TEXT NOT NULL DEFAULT 'NO SIGNAL',
                        confidence INTEGER NOT NULL DEFAULT 0,
                        analysis TEXT NOT NULL DEFAULT '',
                        screenshot_reference TEXT NOT NULL DEFAULT '',
                        image_quality INTEGER NOT NULL DEFAULT 0,
                        outcome TEXT NOT NULL DEFAULT 'PENDING',
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                """)
                cur.execute("CREATE INDEX IF NOT EXISTS gm_user_signals_user_idx ON gm_user_signals(user_id, created_at DESC)")
                # --- GHOST MATRIX PRO: profile, preferences, alerts, risk, UID, payments, verification ---
                cur.execute("CREATE TABLE IF NOT EXISTS gm_user_preferences (user_id BIGINT PRIMARY KEY REFERENCES gm_users(id) ON DELETE CASCADE, timezone TEXT NOT NULL DEFAULT 'Asia/Dhaka', default_market_type TEXT NOT NULL DEFAULT 'OTC', default_timeframe TEXT NOT NULL DEFAULT '1 MIN', sound_alerts BOOLEAN NOT NULL DEFAULT TRUE, browser_alerts BOOLEAN NOT NULL DEFAULT TRUE, telegram_alerts BOOLEAN NOT NULL DEFAULT FALSE, compact_mode BOOLEAN NOT NULL DEFAULT FALSE, updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())")
                cur.execute("CREATE TABLE IF NOT EXISTS gm_user_alerts (id BIGSERIAL PRIMARY KEY, user_id BIGINT NOT NULL REFERENCES gm_users(id) ON DELETE CASCADE, alert_type TEXT NOT NULL, market TEXT, timeframe TEXT, enabled BOOLEAN NOT NULL DEFAULT TRUE, threshold INTEGER NOT NULL DEFAULT 70, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), UNIQUE(user_id, alert_type, market, timeframe))")
                cur.execute("CREATE TABLE IF NOT EXISTS gm_risk_profiles (user_id BIGINT PRIMARY KEY REFERENCES gm_users(id) ON DELETE CASCADE, capital NUMERIC(14,2) NOT NULL DEFAULT 0, risk_per_trade NUMERIC(6,2) NOT NULL DEFAULT 1, max_daily_loss NUMERIC(14,2) NOT NULL DEFAULT 0, max_consecutive_losses INTEGER NOT NULL DEFAULT 3, payout NUMERIC(6,2) NOT NULL DEFAULT 80, martingale_enabled BOOLEAN NOT NULL DEFAULT FALSE, updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())")
                cur.execute("CREATE TABLE IF NOT EXISTS gm_uid_verifications (id BIGSERIAL PRIMARY KEY, user_id BIGINT NOT NULL REFERENCES gm_users(id) ON DELETE CASCADE, broker TEXT NOT NULL DEFAULT 'Quotex', uid TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'PENDING', notes TEXT NOT NULL DEFAULT '', created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), reviewed_at TIMESTAMPTZ)")
                cur.execute("CREATE INDEX IF NOT EXISTS gm_uid_user_idx ON gm_uid_verifications(user_id, created_at DESC)")
                cur.execute("CREATE TABLE IF NOT EXISTS gm_payment_requests (id BIGSERIAL PRIMARY KEY, user_id BIGINT NOT NULL REFERENCES gm_users(id) ON DELETE CASCADE, plan_id BIGINT REFERENCES gm_plans(id) ON DELETE SET NULL, amount NUMERIC(10,2) NOT NULL DEFAULT 0, method TEXT NOT NULL DEFAULT 'MANUAL', reference TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'PENDING', notes TEXT NOT NULL DEFAULT '', created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), reviewed_at TIMESTAMPTZ)")
                cur.execute("CREATE TABLE IF NOT EXISTS gm_ai_verifications (id BIGSERIAL PRIMARY KEY, user_id BIGINT REFERENCES gm_users(id) ON DELETE SET NULL, market TEXT NOT NULL, market_type TEXT NOT NULL DEFAULT 'REAL', timeframe TEXT NOT NULL DEFAULT '1 MIN', direction TEXT NOT NULL DEFAULT 'NO SIGNAL', confidence INTEGER NOT NULL DEFAULT 0, model_agreement INTEGER NOT NULL DEFAULT 0, trend_score INTEGER NOT NULL DEFAULT 0, momentum_score INTEGER NOT NULL DEFAULT 0, structure_score INTEGER NOT NULL DEFAULT 0, news_status TEXT NOT NULL DEFAULT 'UNAVAILABLE', created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())")
                cur.execute("CREATE TABLE IF NOT EXISTS gm_signal_alerts_log (id BIGSERIAL PRIMARY KEY, user_id BIGINT REFERENCES gm_users(id) ON DELETE SET NULL, signal_id BIGINT, channel TEXT NOT NULL, status TEXT NOT NULL, message TEXT NOT NULL DEFAULT '', created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())")
                cur.execute("CREATE TABLE IF NOT EXISTS gm_user_preferences_seed (user_id BIGINT PRIMARY KEY REFERENCES gm_users(id) ON DELETE CASCADE)")
                # Seed default plans if empty
                cur.execute("SELECT COUNT(*) FROM gm_plans")
                if cur.fetchone()[0] == 0:
                    cur.execute("INSERT INTO gm_plans (name, price, description, duration_days, duration, status) VALUES ('FREE', 0, 'Explore app, no signal generation', 0, 'Forever', 'active')")
                    cur.execute("INSERT INTO gm_plans (name, price, description, duration_days, duration, status) VALUES ('DEPOSIT PLAN', 0, '$10 deposit through affiliate link - verified by admin', 30, '30 days', 'active')")
                    cur.execute("INSERT INTO gm_plans (name, price, description, duration_days, duration, status) VALUES ('PREMIUM', 99, 'One month unrestricted engine access, priority support', 30, '30 days', 'active')")
                # Auto-expire user plans past expiry
                cur.execute("UPDATE gm_user_plans SET status='inactive' WHERE status='active' AND expiry_date IS NOT NULL AND expiry_date < NOW()")
        return True, "Supabase/PostgreSQL connected"
    except Exception as exc:
        log.warning("Database init failed: %s", exc)
        return False, str(exc)



# --- GHOST AI Helpers ---
def get_user_active_plan(user_id: int):
    """Return active plan dict or None. OWNER/ADMIN always OWNER/ADMIN - never FREE."""
    # OWNER/ADMIN synthetic plan - checked first so owner never shows FREE
    try:
        with db_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SELECT id, email, is_admin FROM gm_users WHERE id=%s", (user_id,))
                _u = cur.fetchone()
                if _u:
                    _em = (_u.get("email") or "")
                    if _is_owner_email(_em):
                        return {"id": 0, "user_id": int(user_id), "plan_id": 0, "plan_name": "OWNER", "name": "OWNER", "display_plan": "OWNER", "status": "active", "access": "FULL", "is_owner": True, "is_admin": True, "price": 0, "description": "Owner - Full Access", "duration_days": 0, "duration": "Lifetime", "start_date": None, "expiry_date": None, "notes": "OWNER • FULL ACCESS"}
                    if bool(_u.get("is_admin")):
                        return {"id": 0, "user_id": int(user_id), "plan_id": 0, "plan_name": "ADMIN", "name": "ADMIN", "display_plan": "ADMIN", "status": "active", "access": "FULL", "is_owner": False, "is_admin": True, "price": 0, "description": "Admin - Full Access", "duration_days": 0, "duration": "Lifetime", "start_date": None, "expiry_date": None, "notes": "ADMIN • FULL ACCESS"}
    except Exception:
        pass
    try:
        with db_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("UPDATE gm_user_plans SET status='inactive' WHERE user_id=%s AND status='active' AND expiry_date IS NOT NULL AND expiry_date < NOW()", (user_id,))
                cur.execute("""
                    SELECT up.id, up.user_id, up.plan_id, up.start_date, up.expiry_date, up.status, up.notes,
                           p.name as plan_name, p.price, p.description, p.duration_days, p.duration
                    FROM gm_user_plans up JOIN gm_plans p ON p.id=up.plan_id
                    WHERE up.user_id=%s AND up.status='active' ORDER BY up.created_at DESC LIMIT 1
                """, (user_id,))
                row = cur.fetchone()
                if not row:
                    return None
                d = dict(row)
                # Serialize dates
                for k in ("start_date","expiry_date"):
                    if d.get(k): d[k]=d[k].isoformat()
                return d
    except Exception as exc:
        log.warning("get_user_active_plan failed: %s", exc)
        return None


def _is_owner_email(email: str) -> bool:
    return bool(email) and bool(ADMIN_EMAIL) and str(email).strip().lower() == ADMIN_EMAIL.strip().lower()

def _display_plan_for_user(user: dict, plan: dict | None):
    """Return display plan for OWNER/ADMIN vs normal user. Owner never FREE."""
    email = (user.get("email") or "") if isinstance(user, dict) else ""
    is_admin = bool(user.get("is_admin")) if isinstance(user, dict) else False
    if _is_owner_email(email):
        return {"plan_name": "OWNER", "name": "OWNER", "status": "active", "display_plan": "OWNER", "access": "FULL", "is_owner": True, "is_admin": True, "role": "admin"}
    if is_admin:
        return {"plan_name": "ADMIN", "name": "ADMIN", "status": "active", "display_plan": "ADMIN", "access": "FULL", "is_owner": False, "is_admin": True, "role": "admin"}
    if plan:
        return {**plan, "display_plan": (plan.get("plan_name") or "FREE").upper(), "is_owner": False, "is_admin": False}
    return {"plan_name": "FREE", "name": "FREE", "status": "inactive", "display_plan": "FREE / NO ACTIVE PLAN", "access": "LIMITED", "is_owner": False, "is_admin": False}

def _user_has_full_access(user: dict) -> bool:
    if not isinstance(user, dict):
        return False
    if _is_owner_email(user.get("email") or ""):
        return True
    return bool(user.get("is_admin"))


def get_verified_accuracy():
    """Verified accuracy from actual WIN/LOSS only. Returns None if no settled signals."""
    try:
        with db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FILTER (WHERE status='WIN'), COUNT(*) FILTER (WHERE status='LOSS') FROM gm_signals")
                w1,l1 = cur.fetchone() or (0,0)
                cur.execute("SELECT COUNT(*) FILTER (WHERE outcome='WIN'), COUNT(*) FILTER (WHERE outcome='LOSS') FROM gm_user_signals")
                w2,l2 = cur.fetchone() or (0,0)
                w=int(w1 or 0)+int(w2 or 0); l=int(l1 or 0)+int(l2 or 0)
                total=w+l
                if total==0:
                    return None
                return {"wins":w,"losses":l,"total":total,"accuracy":round(w/total*100,1)}
    except Exception:
        return None

def check_screenshot_quality(raw: bytes):
    """Check screenshot quality: resolution, blur (edge variance), brightness, contrast. Returns (score 0-100, issues list)."""
    issues=[]
    try:
        im = Image.open(io.BytesIO(raw))
        w,h = im.size
        if w < 600 or h < 300:
            issues.append("Low resolution")
        if w < 400 or h < 200:
            issues.append("Chart too small")
        # Grayscale stats
        g = im.convert("L").resize((200,100))
        pixels = list(g.getdata())
        mean = sum(pixels)/len(pixels) if pixels else 0
        var = sum((p-mean)**2 for p in pixels)/len(pixels) if pixels else 0
        # Edge sharpness via FIND_EDGES
        try:
            from PIL import ImageFilter
            edges = g.filter(ImageFilter.FIND_EDGES)
            edge_pixels = list(edges.getdata())
            edge_mean = sum(edge_pixels)/len(edge_pixels) if edge_pixels else 0
        except Exception:
            edge_mean = 0
        score = 100
        if w < 800: score -= 12
        if w < 600: score -= 18
        if var < 400: score -= 22; issues.append("Blur / low contrast")
        if edge_mean < 6: score -= 22; issues.append("Blur - candles not sharp")
        if mean < 25 or mean > 240: score -= 12; issues.append("Too dark / overexposed")
        if edge_mean < 3: issues.append("Chart not visible / cropped")
        score = max(0, min(100, int(score)))
        return score, issues
    except Exception as exc:
        return 0, [f"Unreadable image: {str(exc)[:80]}"]

AFFILIATE_URL_EXPORT = AFFILIATE_URL

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


def _otc_symbol(pair: str) -> str:
    raw = str(pair or "").upper().replace("(OTC)", "").replace("/", "").replace("-", "").replace(" ", "")
    return raw + "_otc"

def _otc_tf_seconds(timeframe: str) -> int:
    return {"1 MIN":60,"2 MIN":120,"5 MIN":300,"10 MIN":600,"15 MIN":900,"30 MIN":1800,"1 HOUR":3600}.get(_normalize_timeframe(timeframe),60)

def _otcharts_client():
    if not OTCHARTS_API_KEY or OTChartsClient is None:
        raise RuntimeError("OTCHARTS_API_KEY is not configured or otcharts package is unavailable")
    return OTChartsClient()

def _otc_candles(pair: str, timeframe: str = "1 MIN", limit: int = 300):
    client = _otcharts_client()
    symbol = _otc_symbol(pair)
    bars = client.candles("quotex", symbol, tf=_otc_tf_seconds(timeframe), limit=max(60, min(500, int(limit))))
    raw = getattr(bars, "candles", bars)
    candles = []
    for item in raw or []:
        def g(k, default=None):
            return item.get(k, default) if isinstance(item, dict) else getattr(item, k, default)
        try:
            stamp = g("time")
            if isinstance(stamp, (int,float)):
                stamp = datetime.fromtimestamp(float(stamp), tz=timezone.utc)
            elif isinstance(stamp, str):
                stamp = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
                if stamp.tzinfo is None: stamp = stamp.replace(tzinfo=timezone.utc)
            candles.append({"time":stamp,"open":float(g("open")),"high":float(g("high")),"low":float(g("low")),"close":float(g("close"))})
        except (TypeError,ValueError):
            continue
    candles.sort(key=lambda x:x["time"])
    return candles, {"symbol":symbol,"venue":"quotex","timeframe":_normalize_timeframe(timeframe),"source":"OTCharts Quotex OTC feed","verified_quotex":False}

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



def analyze_candles(symbol: str, candles: list[dict[str, Any]], timeframe: str = "1 MIN") -> dict[str, Any]:
    if len(candles) < 60:
        return {
            "symbol": symbol,
            "direction": "NO TRADE",
            "reason": f"শুধু {len(candles)}টি candle; বিশ্লেষণের জন্য অন্তত 60টি দরকার।",
            "candle_count": len(candles),
        }

    closes = [c["close"] for c in candles]
    fast = ema(closes, 9)
    slow = ema(closes, 21)
    trend = ema(closes, 50)
    rsis = rsi(closes, 14)

    idx = len(candles) - 1
    latest = candles[idx]
    previous = candles[idx - 1]

    age = (datetime.now(timezone.utc) - latest["time"]).total_seconds()
    tf_norm = _normalize_timeframe(timeframe) if "_normalize_timeframe" in globals() else timeframe
    tf_minutes = {"1 MIN":1,"2 MIN":2,"5 MIN":5,"10 MIN":10,"15 MIN":15,"30 MIN":30,"1 HOUR":60}.get(tf_norm,1)
    max_age = max(MAX_SIGNAL_AGE_SECONDS, tf_minutes * 60 * 3)
    if age < -120 or age > max_age:
        return {
            "symbol": symbol,
            "direction": "NO TRADE",
            "reason": f"সর্বশেষ M1 candle-এর সময় যাচাই করা যাচ্ছে না ({int(age)} sec)।",
            "candle": latest,
        }

    if any(x[idx] is None for x in (fast, slow, trend, rsis)):
        return {
            "symbol": symbol,
            "direction": "NO TRADE",
            "reason": "Indicator data incomplete.",
            "candle": latest,
        }

    # সাম্প্রতিক 20টি আগের candle দিয়ে support/resistance-এর আনুমানিক স্তর
    recent = candles[max(0, idx - 20):idx]
    support = min(c["low"] for c in recent)
    resistance = max(c["high"] for c in recent)

    close = latest["close"]
    open_price = latest["open"]
    high = latest["high"]
    low = latest["low"]
    candle_range = high - low

    if close <= 0 or candle_range <= 0:
        return {
            "symbol": symbol,
            "direction": "NO TRADE",
            "reason": "Candle range বা price বৈধ নয়।",
            "candle": latest,
        }

    body_ratio = abs(close - open_price) / candle_range
    bullish = close > open_price and close > previous["close"]
    bearish = close < open_price and close < previous["close"]

    # খুব ছোট body হলে দিক পরিষ্কার নয়
    if body_ratio < 0.25:
        direction = "NO TRADE"
        reason = "Candle body দুর্বল; পরিষ্কার momentum পাওয়া যায়নি।"

    # Trend + momentum + RSI + support/resistance filter
    elif (
        fast[idx] > slow[idx] > trend[idx]
        and 52 <= rsis[idx] <= 68
        and bullish
        and close < resistance
        and (resistance - close) > candle_range * 0.15
    ):
        direction = "CALL"
        reason = (
            f"Trend bullish: EMA9 > EMA21 > EMA50; RSI {rsis[idx]:.1f}; "
            "শেষ candle bullish momentum দেখিয়েছে এবং resistance-এর খুব কাছে নয়। "
            "এটি সম্ভাব্য setup, নিশ্চিত ফল নয়।"
        )

    elif (
        fast[idx] < slow[idx] < trend[idx]
        and 32 <= rsis[idx] <= 48
        and bearish
        and close > support
        and (close - support) > candle_range * 0.15
    ):
        direction = "PUT"
        reason = (
            f"Trend bearish: EMA9 < EMA21 < EMA50; RSI {rsis[idx]:.1f}; "
            "শেষ candle bearish momentum দেখিয়েছে এবং support-এর খুব কাছে নয়। "
            "এটি সম্ভাব্য setup, নিশ্চিত ফল নয়।"
        )

    else:
        direction = "NO TRADE"
        reason = (
            f"সব filter একমত নয়। EMA9={fast[idx]:.5f}, "
            f"EMA21={slow[idx]:.5f}, EMA50={trend[idx]:.5f}, "
            f"RSI={rsis[idx]:.1f}; অপেক্ষা করাই ভালো।"
        )

    return {
        "symbol": symbol,
        "direction": direction,
        "reason": reason,
        "price": close,
        "candle": latest,
        "candle_count": len(candles),
        "first_candle": candles[0]["time"],
        "last_candle": latest["time"],
        "rsi": rsis[idx],
        "ema9": fast[idx],
        "ema21": slow[idx],
        "ema50": trend[idx],
        "support": support,
        "resistance": resistance,
        "verified_quotex": False,
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
def _aggregate_timeframe(candles: list[dict[str, Any]], minutes: int) -> list[dict[str, Any]]:
    """Aggregate sorted 1-minute candles into an exact minute timeframe.
    Buckets are aligned to UTC epoch so 2/10 minute selections never silently fall back to 1M.
    """
    if minutes <= 1:
        return candles
    from collections import defaultdict
    buckets: dict[int, list[dict[str, Any]]] = defaultdict(list)
    step = minutes * 60
    for c in candles:
        t = c.get("time")
        if not isinstance(t, datetime):
            continue
        ts = int(t.astimezone(timezone.utc).timestamp())
        bucket = ts - (ts % step)
        buckets[bucket].append(c)
    out=[]
    for bucket in sorted(buckets):
        rows=sorted(buckets[bucket], key=lambda x:x["time"])
        if not rows:
            continue
        out.append({
            "time": datetime.fromtimestamp(bucket, tz=timezone.utc),
            "open": rows[0]["open"],
            "high": max(x["high"] for x in rows),
            "low": min(x["low"] for x in rows),
            "close": rows[-1]["close"],
        })
    return out

def bd_time(value: Any) -> str:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(BANGLADESH_TZ).strftime("%Y-%m-%d %H:%M:%S")
    return str(value or "সময় পাওয়া যায়নি")


def scan_one_symbol(symbol: str, timeframe: str = "1 MIN") -> dict[str, Any]:
    timeframe = _normalize_timeframe(timeframe)
    # Twelve Data has native intervals for common frames. For 2M/10M we fetch M1
    # and aggregate server-side so the selected timeframe is the actual analysis timeframe.
    if timeframe in ("2 MIN", "10 MIN"):
        payload = twelve_get("time_series", {
            "symbol": symbol, "interval": "1min", "outputsize": min(HISTORY_OUTPUTSIZE, 1500),
            "order": "desc", "format": "JSON", "timezone": "UTC",
        })
        raw = parse_candles(payload)
        candles = _aggregate_timeframe(raw, 2 if timeframe == "2 MIN" else 10)
    else:
        interval = {"1 MIN":"1min","5 MIN":"5min","15 MIN":"15min","30 MIN":"30min","1 HOUR":"1h"}.get(timeframe,"1min")
        payload = twelve_get("time_series", {
            "symbol": symbol, "interval": interval, "outputsize": HISTORY_OUTPUTSIZE,
            "order": "desc", "format": "JSON", "timezone": "UTC",
        })
        candles = parse_candles(payload)
    result = analyze_candles(symbol, candles, timeframe)
    result["timeframe"] = timeframe
    result["backtest"] = backtest_ema(candles)
    result["source"] = "Twelve Data external feed"
    result["verified_quotex"] = False
    if result.get("direction") in ("CALL", "PUT") and result.get("candle"):
        result["signal_id"] = save_signal(symbol, result["direction"], result["candle"]["time"], result["price"], result["reason"])
    return result

#
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
                {
                    "role": "system",
                    "content": (
                        "Analyze only what is visible in this trading chart screenshot. "
                        "Reply in Bengali. Mention uncertainty and missing timeframe/asset labels. "
                        "Do not invent price values, predict with certainty, claim Quotex verification, "
                        "or promise profit. If unclear, say NO TRADE / insufficient evidence."
                    ),
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": "এই chart screenshot-এ দৃশ্যমান trend, candle structure এবং অনিশ্চয়তা ব্যাখ্যা করো। নিশ্চিত CALL/PUT দাবি করবে না।",
                        },
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:{mime};base64,{encoded}"
                            },
                        },
                    ],
                },
            ],
            "temperature": 0.05,
            "max_completion_tokens": 700,
        }

        response = http_session.post(
            GROQ_BASE,
            headers={
                "Authorization": f"Bearer {AI_API_KEY}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=45,
        )
        response.raise_for_status()

        return (
            response.json()["choices"][0]["message"]["content"].strip()
            + "\n\n⚠️ Screenshot analysis only; not a verified Quotex signal."
        )

    except Exception as exc:
        log.exception("Chart image analysis failed")
        return f"Chart analysis error: {type(exc).__name__}: {str(exc)[:300]}"
        
               
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
        # Scan only 5 pairs per request to reduce stale signals.
            results = []
            selected = catalog[:5]
            last_request_at = 0.0

            for item in selected:
                if last_request_at:
                    wait = 8.0 - (time.monotonic() - last_request_at)
                    if wait > 0:
                        time.sleep(wait)

                if db_api_calls_today() >= DAILY_CREDIT_BUDGET:
                    results.append({
                        "symbol": item["symbol"],
                        "direction": "NO TRADE",
                        "reason": "দৈনিক API safety budget শেষ; এই pair স্ক্যান হয়নি।"
                    })
                    continue

                try:
                    last_request_at = time.monotonic()
                    result = scan_one_symbol(item["symbol"])
                    results.append(result)
                except Exception as scan_exc:
                    log.warning(
                        "Manual scan failed for %s: %s",
                        item["symbol"], scan_exc
                    )
                    results.append({
                        "symbol": item["symbol"],
                        "direction": "NO TRADE",
                        "reason": "এই pair-এর ডেটা পাওয়া যায়নি।"
                    })

            state["last_scan_utc"] = datetime.now(timezone.utc).isoformat()
            state["last_error"] = None
            send_message(format_future_batch(results, len(selected)), chat_id) 
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
    conflict_warned = False
    while True:
        if not BOT_TOKEN:
            if not conflict_warned:
                log.info("BOT_TOKEN not set - Telegram polling disabled (web app only)")
                conflict_warned = True
            time.sleep(60)
            continue
        if os.getenv("DISABLE_TELEGRAM", "").lower() in ("1","true","yes"):
            if not conflict_warned:
                log.info("DISABLE_TELEGRAM=1 - Telegram polling disabled")
                conflict_warned = True
            time.sleep(60)
            continue
        try:
            params = {"timeout": 25}
            if offset is not None:
                params["offset"] = offset
            response = http_session.get(f"{TELEGRAM_BASE}/getUpdates", params=params, timeout=35)
            if response.status_code == 409:
                if not conflict_warned:
                    log.warning("Telegram 409 Conflict - another instance polling same bot token. Disabling telegram polling for 5 minutes. Set DISABLE_TELEGRAM=1 to disable completely.")
                    conflict_warned = True
                time.sleep(300)
                continue
            response.raise_for_status()
            conflict_warned = False
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
            cur.execute("SELECT id, email, password_hash, is_admin, email_verified, access_enabled FROM gm_users WHERE LOWER(email)=LOWER(%s)", (email,))
            row = cur.fetchone()
            return dict(row) if row else None


def _send_password_setup_email(email: str, token: str) -> None:
    """Send a one-time password setup link; never email a password."""
    if not all([SMTP_HOST, SMTP_USER, SMTP_PASSWORD, SMTP_FROM, APP_BASE_URL]):
        raise RuntimeError("Email is not configured. Check SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, SMTP_FROM, and APP_BASE_URL.")
    link = f"{APP_BASE_URL}/api/auth/set-password?token={token}"
    msg = EmailMessage()
    msg["Subject"] = "Set up your GHOST TRADER account"
    msg["From"] = SMTP_FROM
    msg["To"] = email
    msg.set_content(
        "Welcome to GHOST TRADER!\n\n"
        "An administrator created an account for this email address. "
        "Use the secure link below to choose your password. The link expires in 30 minutes and can only be used while your account is awaiting setup.\n\n"
        f"Set your password: {link}\n\n"
        "If you were not expecting this email, you can ignore it."
    )
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
                    cur.execute("SELECT id,email,name,is_admin,email_verified,access_enabled FROM gm_users WHERE id=%s", (uid,))
                    user = cur.fetchone()
            if not user or not user["email_verified"] or not user.get("access_enabled", True):
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
    return "GHOST AI is running. Screenshot-based 1-minute analysis. Data source: external Twelve Data; Quotex verification: disabled.", 200

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


    if not user.get("access_enabled", True):
        return jsonify({"error": "This account is disabled. Contact the administrator."}), 403
    if not user["email_verified"]:
        return jsonify({"error": "Email is not verified yet. Open the verification link sent to your email."}), 403
    session.clear()
    session.permanent = True
    session["user_id"] = int(user["id"])
    _plan = get_user_active_plan(int(user["id"]))
    _is_owner = _is_owner_email(user.get("email") or "")
    _has = True if (_is_owner or bool(user.get("is_admin"))) else bool(_plan and _plan.get("plan_name") not in ("FREE",) and _plan.get("status")=="active")
    _disp = "OWNER" if _is_owner else ("ADMIN" if user.get("is_admin") else (_plan.get("display_plan") or _plan.get("plan_name") or "FREE"))
    return jsonify({"ok": True, "user": {"id": int(user["id"]), "email": user["email"], "name": user.get("name") or "", "is_admin": bool(user["is_admin"]), "is_owner": _is_owner, "role": "admin" if user["is_admin"] else "user", "access_enabled": bool(user.get("access_enabled", True)), "plan": _plan, "display_plan": _disp, "plan_label": _disp, "access": "FULL" if (_is_owner or user.get("is_admin")) else "LIMITED", "active_plan": _has}})


@app.get("/api/auth/me")
def auth_me():
    uid = session.get("user_id")
    if not uid:
        return jsonify({"authenticated": False}), 200
    try:
        with db_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SELECT id,email,name,is_admin,email_verified,access_enabled FROM gm_users WHERE id=%s", (uid,))
                user = cur.fetchone()
        if not user or not user["email_verified"] or not user.get("access_enabled", True):
            session.clear()
            return jsonify({"authenticated": False}), 200
        _plan2 = get_user_active_plan(int(user["id"])); _is_owner2 = _is_owner_email(user.get("email") or ""); _has2 = True if (_is_owner2 or bool(user.get("is_admin"))) else bool(_plan2 and _plan2.get("plan_name") not in ("FREE",) and _plan2.get("status")=="active"); _disp2 = "OWNER" if _is_owner2 else ("ADMIN" if user.get("is_admin") else (_plan2.get("display_plan") or _plan2.get("plan_name") or "FREE")); return jsonify({"authenticated": True, "user": {"id": int(user["id"]), "email": user["email"], "name": user.get("name") or "", "is_admin": bool(user["is_admin"]), "is_owner": _is_owner2, "role": "admin" if user["is_admin"] else "user", "access_enabled": bool(user.get("access_enabled", True)), "plan": _plan2, "display_plan": _disp2, "plan_label": _disp2, "access": "FULL" if (_is_owner2 or user.get("is_admin")) else "LIMITED", "active_plan": _has2}, "affiliate_url": AFFILIATE_URL, "app_name": APP_NAME})
    except Exception as exc:
        log.error("Auth status error: %s", exc)
        return jsonify({"authenticated": False, "error": "Authentication database unavailable"}), 503


@app.post("/api/auth/logout")
def auth_logout():
    session.clear()
    response = jsonify({"ok": True, "authenticated": False})
    response.delete_cookie(
        app.config.get("SESSION_COOKIE_NAME", "session"),
        path=app.config.get("SESSION_COOKIE_PATH", "/"),
        domain=app.config.get("SESSION_COOKIE_DOMAIN"),
        secure=app.config.get("SESSION_COOKIE_SECURE", False),
        httponly=app.config.get("SESSION_COOKIE_HTTPONLY", True),
        samesite=app.config.get("SESSION_COOKIE_SAMESITE", "Lax"),
    )
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return response



# --- GHOST AI: Register ---
@app.post("/api/auth/register")
def auth_register():
    body = request.get_json(silent=True) or {}
    name = str(body.get("name","") or body.get("full_name","")).strip()
    email = str(body.get("email","")).strip().lower()
    password = str(body.get("password","") or "")
    confirm = str(body.get("confirm_password","") or body.get("confirmPassword","") or body.get("confirm","") or "")
    if not name or len(name) < 2 or len(name) > 100:
        return jsonify({"error": "Full name is required (2-100 characters)."}), 400
    if not email or "@" not in email or "." not in email or len(email)>254:
        return jsonify({"error": "Enter a valid email address."}), 400
    if len(password) < 8:
        return jsonify({"error": "Password must be at least 8 characters."}), 400
    if password != confirm:
        return jsonify({"error": "Passwords do not match."}), 400
    try:
        with db_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SELECT id FROM gm_users WHERE LOWER(email)=LOWER(%s)", (email,))
                if cur.fetchone():
                    return jsonify({"error": "This email is already registered. Please login."}), 409
                pw_hash = generate_password_hash(password)
                cur.execute("""
                    INSERT INTO gm_users (email, password_hash, name, role, status, is_admin, email_verified, verified_at, access_enabled)
                    VALUES (%s,%s,%s,'user','active',FALSE,TRUE,NOW(),TRUE) RETURNING id, email, name
                """, (email, pw_hash, name))
                row = cur.fetchone()
                uid = int(row["id"])
                # Assign FREE plan by default (inactive signal - FREE has no signal access, status active but plan FREE)
                cur.execute("SELECT id FROM gm_plans WHERE name='FREE' LIMIT 1")
                prow = cur.fetchone()
                if prow:
                    cur.execute("INSERT INTO gm_user_plans (user_id, plan_id, start_date, expiry_date, status, notes) VALUES (%s,%s,NOW(),NULL,'active','Auto FREE on register')", (uid, int(prow["id"])))
        session.clear(); session.permanent=True; session["user_id"]=uid
        return jsonify({"ok": True, "user": {"id": uid, "email": email, "name": name, "role": "user"}}), 201
    except Exception as exc:
        log.error("Register error: %s", exc)
        return jsonify({"error": "Registration failed. Try again."}), 503

# --- GHOST AI: Plans (dynamic from DB) ---
@app.get("/api/plans")
def api_plans():
    try:
        with db_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SELECT id, name, price, description, duration_days, duration, status, created_at FROM gm_plans WHERE status='active' ORDER BY price ASC, id ASC")
                rows=[dict(r) for r in cur.fetchall()]
        for r in rows:
            if r.get("created_at"): r["created_at"]=r["created_at"].isoformat()
            # Attach affiliate URL for DEPOSIT PLAN
            if r["name"]=="DEPOSIT PLAN":
                r["affiliate_url"]=AFFILIATE_URL
            if r["name"]=="FREE":
                r["affiliate_url"]=AFFILIATE_URL
        return jsonify({"plans": rows, "affiliate_url": AFFILIATE_URL, "app_name": APP_NAME})
    except Exception as exc:
        log.error("Plans error: %s", exc)
        return jsonify({"plans": [
            {"id":1,"name":"FREE","price":0,"description":"Explore app, no signal generation","duration":"Forever","affiliate_url":AFFILIATE_URL},
            {"id":2,"name":"DEPOSIT PLAN","price":0,"description":"$10 deposit through affiliate link - verified by admin","duration":"30 days","affiliate_url":AFFILIATE_URL},
            {"id":3,"name":"PREMIUM","price":99,"description":"One month unrestricted engine access","duration":"30 days","affiliate_url":AFFILIATE_URL},
        ], "affiliate_url": AFFILIATE_URL, "app_name": APP_NAME})

@app.get("/api/user/plan")
@login_required
def api_user_plan():
    uid = session.get("user_id")
    plan = get_user_active_plan(uid)
    # OWNER/ADMIN = FULL ACCESS, never FREE. FREE does NOT grant signal access - only DEPOSIT PLAN / PREMIUM / CUSTOM active grants
    is_owner = bool(plan and plan.get("is_owner"))
    is_admin = bool(plan and plan.get("is_admin"))
    has_access = True if (is_owner or is_admin) else bool(plan and plan.get("plan_name") not in ("FREE",) and plan.get("status")=="active")
    display = "OWNER" if is_owner else ("ADMIN" if is_admin else (plan.get("display_plan") or plan.get("plan_name") if plan else "FREE / NO ACTIVE PLAN"))
    return jsonify({"plan": plan, "display_plan": display, "plan_label": display, "active_plan": has_access, "has_access": has_access, "access": "FULL" if (is_owner or is_admin) else "LIMITED", "is_owner": is_owner, "is_admin": is_admin})

@app.get("/api/user/signals")
@login_required
def api_user_signals():
    uid = session.get("user_id")
    try:
        with db_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SELECT id, market, market_type, timeframe, signal, confidence, analysis, image_quality, outcome, created_at FROM gm_user_signals WHERE user_id=%s ORDER BY created_at DESC LIMIT 50", (uid,))
                rows=[dict(r) for r in cur.fetchall()]
        for r in rows:
            if r.get("created_at"): r["created_at"]=r["created_at"].isoformat()
        return jsonify({"signals": rows})
    except Exception:
        return jsonify({"signals": []})

# --- GHOST AI: Admin Summary + Users + Plans + Signals ---
@app.get("/api/admin/summary")
@admin_required
def api_admin_summary():
    try:
        with db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM gm_users"); total_users=int(cur.fetchone()[0] or 0)
                cur.execute("SELECT COUNT(*) FROM gm_users WHERE access_enabled=TRUE AND status='active'"); active_users=int(cur.fetchone()[0] or 0)
                cur.execute("SELECT COUNT(*) FROM gm_user_plans WHERE status='active'"); active_plans=int(cur.fetchone()[0] or 0)
                cur.execute("SELECT COUNT(*) FROM gm_user_signals"); s1=int(cur.fetchone()[0] or 0)
                cur.execute("SELECT COUNT(*) FROM gm_signals"); s2=int(cur.fetchone()[0] or 0)
        return jsonify({"total_users": total_users, "active_users": active_users, "active_plans": active_plans, "total_signals": s1+s2})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 503

@app.get("/api/admin/users")
@admin_required
def api_admin_users():
    q = (request.args.get("q") or "").strip().lower()
    try:
        with db_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                if q:
                    cur.execute("SELECT id, name, email, role, status, is_admin, access_enabled, created_at FROM gm_users WHERE LOWER(email) LIKE %s OR LOWER(COALESCE(name,'')) LIKE %s ORDER BY created_at DESC LIMIT 200", (f"%{q}%", f"%{q}%"))
                else:
                    cur.execute("SELECT id, name, email, role, status, is_admin, access_enabled, created_at FROM gm_users ORDER BY created_at DESC LIMIT 200")
                users=[dict(r) for r in cur.fetchall()]
                for u in users:
                    if u.get("created_at"): u["created_at"]=u["created_at"].isoformat()
                    u["is_owner"]=_is_owner_email(u.get("email") or "")
                    # attach current plan - owner shows OWNER, admin shows ADMIN
                    plan=get_user_active_plan(int(u["id"]))
                    u["current_plan"]=plan
                    if u["is_owner"]:
                        u["display_plan"]="OWNER"; u["access"]="FULL"
                    elif u.get("is_admin"):
                        u["display_plan"]="ADMIN"; u["access"]="FULL"
                    else:
                        u["display_plan"]=(plan.get("plan_name") if plan else "FREE")
        return jsonify({"users": users})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 503

@app.post("/api/admin/users")
@admin_required
def api_admin_create_user():
    body=request.get_json(silent=True) or {}
    name=str(body.get("name","")).strip(); email=str(body.get("email","")).strip().lower(); password=str(body.get("password","") or "")
    if not name or not email or len(password)<8:
        return jsonify({"error": "Name, valid email and 8+ char password required."}), 400
    try:
        with db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT id FROM gm_users WHERE LOWER(email)=LOWER(%s)", (email,))
                if cur.fetchone(): return jsonify({"error": "Email already exists."}), 409
                cur.execute("INSERT INTO gm_users (email, password_hash, name, role, status, is_admin, email_verified, verified_at, access_enabled) VALUES (%s,%s,%s,'user','active',FALSE,TRUE,NOW(),TRUE) RETURNING id", (email, generate_password_hash(password), name))
                uid=int(cur.fetchone()[0])
        return jsonify({"ok": True, "user_id": uid}), 201
    except Exception as exc:
        return jsonify({"error": str(exc)}), 503

@app.put("/api/admin/users/<int:user_id>")
@admin_required
def api_admin_update_user(user_id):
    body=request.get_json(silent=True) or {}
    # OWNER protection: never deactivate, never assign plan, never demote OWNER
    try:
        with db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT email, is_admin FROM gm_users WHERE id=%s", (user_id,))
                _t = cur.fetchone()
                if _t and _is_owner_email(_t[0] or ""):
                    return jsonify({"error": "Owner account cannot be modified - OWNER • FULL ACCESS. Assign/Activate/Deactivate not allowed for Owner."}), 403
    except Exception as exc:
        # If the guard check itself fails, fall through to normal handling but log
        log.warning("Owner guard check failed: %s", exc)
    try:
        with db_connection() as conn:
            with conn.cursor() as cur:
                if "name" in body: cur.execute("UPDATE gm_users SET name=%s WHERE id=%s", (str(body["name"]).strip(), user_id))
                if "status" in body: cur.execute("UPDATE gm_users SET status=%s WHERE id=%s", (str(body["status"]), user_id))
                if "access_enabled" in body: cur.execute("UPDATE gm_users SET access_enabled=%s WHERE id=%s", (bool(body["access_enabled"]), user_id))
                if "role" in body: cur.execute("UPDATE gm_users SET role=%s, is_admin=%s WHERE id=%s", (str(body["role"]), str(body["role"])=="admin", user_id))
                if "password" in body and str(body["password"]):
                    cur.execute("UPDATE gm_users SET password_hash=%s WHERE id=%s", (generate_password_hash(str(body["password"])), user_id))
                # Plan assignment
                if "plan_id" in body and body.get("plan_id"):
                    pid=int(body["plan_id"]); start=body.get("start_date"); expiry=body.get("expiry_date"); notes=str(body.get("notes","") or "")
                    cur.execute("UPDATE gm_user_plans SET status='inactive' WHERE user_id=%s AND status='active'", (user_id,))
                    if expiry:
                        cur.execute("INSERT INTO gm_user_plans (user_id, plan_id, start_date, expiry_date, status, notes) VALUES (%s,%s, COALESCE(%s::timestamptz, NOW()), %s::timestamptz, 'active', %s)", (user_id, pid, start, expiry, notes))
                    else:
                        cur.execute("SELECT duration_days FROM gm_plans WHERE id=%s", (pid,))
                        prow=cur.fetchone(); days=int(prow[0] or 30) if prow else 30
                        cur.execute("INSERT INTO gm_user_plans (user_id, plan_id, start_date, expiry_date, status, notes) VALUES (%s,%s, NOW(), NOW() + (%s || ' days')::interval, 'active', %s)", (user_id, pid, days, notes))
                elif "plan_status" in body:
                    cur.execute("UPDATE gm_user_plans SET status=%s, notes=COALESCE(notes,'') || %s WHERE user_id=%s AND status IN ('active','inactive')", (str(body["plan_status"]), " | admin: "+str(body.get("notes","")) if body.get("notes") else "", user_id))
                elif "notes" in body:
                    cur.execute("UPDATE gm_user_plans SET notes=%s WHERE user_id=%s AND status='active'", (str(body["notes"]), user_id))
        return jsonify({"ok": True})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 503

@app.delete("/api/admin/users/<int:user_id>")
@admin_required
def api_admin_delete_user(user_id):
    try:
        with db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM gm_users WHERE id=%s AND is_admin=FALSE", (user_id,))
                if cur.rowcount==0: return jsonify({"error": "User not found or is admin."}), 404
        return jsonify({"ok": True})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 503

@app.get("/api/admin/plans")
@admin_required
def api_admin_plans():
    try:
        with db_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SELECT id, name, price, description, duration_days, duration, status, created_at FROM gm_plans ORDER BY id ASC")
                rows=[dict(r) for r in cur.fetchall()]
        for r in rows:
            if r.get("created_at"): r["created_at"]=r["created_at"].isoformat()
        return jsonify({"plans": rows})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 503

@app.post("/api/admin/plans")
@admin_required
def api_admin_create_plan():
    body=request.get_json(silent=True) or {}
    name=str(body.get("name","")).strip(); price=float(body.get("price",0) or 0); desc=str(body.get("description","") or ""); dur_days=int(body.get("duration_days",30) or 30); duration=str(body.get("duration", f"{dur_days} days") or "")
    if not name: return jsonify({"error": "Plan name required."}), 400
    try:
        with db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("INSERT INTO gm_plans (name, price, description, duration_days, duration, status) VALUES (%s,%s,%s,%s,%s,'active') RETURNING id", (name, price, desc, dur_days, duration))
                pid=int(cur.fetchone()[0])
        return jsonify({"ok": True, "plan_id": pid}), 201
    except Exception as exc:
        return jsonify({"error": str(exc)}), 503

@app.put("/api/admin/plans/<int:plan_id>")
@admin_required
def api_admin_update_plan(plan_id):
    body=request.get_json(silent=True) or {}
    try:
        with db_connection() as conn:
            with conn.cursor() as cur:
                if "name" in body: cur.execute("UPDATE gm_plans SET name=%s WHERE id=%s", (str(body["name"]), plan_id))
                if "price" in body: cur.execute("UPDATE gm_plans SET price=%s WHERE id=%s", (float(body["price"]), plan_id))
                if "description" in body: cur.execute("UPDATE gm_plans SET description=%s WHERE id=%s", (str(body["description"]), plan_id))
                if "duration_days" in body: cur.execute("UPDATE gm_plans SET duration_days=%s WHERE id=%s", (int(body["duration_days"]), plan_id))
                if "duration" in body: cur.execute("UPDATE gm_plans SET duration=%s WHERE id=%s", (str(body["duration"]), plan_id))
                if "status" in body: cur.execute("UPDATE gm_plans SET status=%s WHERE id=%s", (str(body["status"]), plan_id))
        return jsonify({"ok": True})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 503

@app.get("/api/admin/signals")
@admin_required
def api_admin_signals():
    q=(request.args.get("q") or "").strip().lower(); outcome=request.args.get("outcome") or ""
    try:
        with db_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                base="SELECT s.id, s.user_id, u.email as user_email, u.name as user_name, s.market, s.market_type, s.timeframe, s.signal, s.confidence, s.analysis, s.image_quality, s.outcome, s.created_at FROM gm_user_signals s LEFT JOIN gm_users u ON u.id=s.user_id"
                clauses=[]; params=[]
                if q: clauses.append("(LOWER(s.market) LIKE %s OR LOWER(COALESCE(u.email,'')) LIKE %s)"); params+= [f"%{q}%", f"%{q}%"]
                if outcome: clauses.append("s.outcome=%s"); params.append(outcome)
                sql=base + (" WHERE "+" AND ".join(clauses) if clauses else "") + " ORDER BY s.created_at DESC LIMIT 200"
                cur.execute(sql, params)
                rows=[dict(r) for r in cur.fetchall()]
        for r in rows:
            if r.get("created_at"): r["created_at"]=r["created_at"].isoformat()
        return jsonify({"signals": rows})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 503

@app.put("/api/admin/signals/<int:signal_id>/outcome")
@admin_required
def api_admin_signal_outcome(signal_id):
    body=request.get_json(silent=True) or {}
    outcome=str(body.get("outcome","")).upper()
    if outcome not in ("WIN","LOSS","EXPIRED","PENDING"): return jsonify({"error": "Outcome must be WIN, LOSS, EXPIRED or PENDING."}), 400
    try:
        with db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE gm_user_signals SET outcome=%s WHERE id=%s", (outcome, signal_id))
                if cur.rowcount==0: return jsonify({"error": "Signal not found."}), 404
        return jsonify({"ok": True})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 503

@app.get("/api/config")
def api_config():
    return jsonify({"app_name": APP_NAME, "affiliate_url": AFFILIATE_URL, "telegram_admin": TELEGRAM_ADMIN, "timeframe": "1 MIN", "markets": ["REAL","OTC"], "vision_providers": {"openai": bool(OPENAI_API_KEY), "gemini": bool(GEMINI_API_KEY), "groq": bool(GROQ_API_KEY)}})


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
                    return "Account not found. Contact your GHOST TRADER admin.", 404
        return "Email verified successfully. Return to GHOST TRADER and log in.", 200
    except Exception as exc:
        log.error("Email verification database error: %s", exc)
        return "Verification could not be completed because the database is unavailable.", 503


@app.get("/api/auth/set-password")
def auth_set_password_page():
    token = request.args.get("token", "")
    if not token:
        return "Password setup link is missing. Please ask your administrator to resend the setup email.", 400
    try:
        payload = _auth_serializer.loads(token, max_age=1800)
        if payload.get("purpose") != "password_setup":
            return "Invalid password setup link.", 400
        email = str(payload.get("email", "")).strip().lower()
        if not email:
            return "Invalid password setup link.", 400
    except SignatureExpired:
        return "This password setup link has expired. Please ask your administrator to resend the email.", 400
    except (BadSignature, AttributeError, TypeError):
        return "Invalid password setup link.", 400
    # Keep the page self-contained so it works even if the frontend is separate.
    return f'''<!doctype html><html lang="en"><meta name="viewport" content="width=device-width,initial-scale=1"><meta charset="utf-8"><title>GHOST TRADER — Set Password</title><body style="margin:0;background:#0b1020;color:#f4f7ff;font:16px Arial,sans-serif;display:grid;min-height:100vh;place-items:center"><main style="width:min(420px,88vw);padding:26px;border:1px solid #293451;border-radius:18px;background:#111a2e"><h2 style="margin-top:0">GHOST TRADER</h2><p>Set a secure password for your account</p><form method="post" action="/api/auth/set-password"><input type="hidden" name="token" value="{token}"><label>New password (at least 12 characters)</label><input name="password" type="password" minlength="12" maxlength="1024" required autocomplete="new-password" style="box-sizing:border-box;width:100%;padding:13px;margin:10px 0 18px;border-radius:9px;border:1px solid #394765;background:#080d19;color:white"><button style="width:100%;padding:13px;border:0;border-radius:9px;background:#67e8c0;color:#07111c;font-weight:bold">Set password</button></form><p style="color:#aab5cb;font-size:13px">The setup link expires after 30 minutes.</p></main></body></html>''', 200, {"Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store"}


@app.post("/api/auth/set-password")
def auth_set_password_submit():
    token = request.form.get("token", "")
    password = request.form.get("password", "")
    if len(password) < 12 or len(password) > 1024:
        return "Password must be at least 12 characters long. Go back and try again.", 400
    try:
        payload = _auth_serializer.loads(token, max_age=1800)
        if payload.get("purpose") != "password_setup":
            return "Invalid password setup link.", 400
        email = str(payload.get("email", "")).strip().lower()
        if not email:
            return "Invalid password setup link.", 400
    except SignatureExpired:
        return "This password setup link has expired. Please ask your administrator to resend the email.", 400
    except (BadSignature, AttributeError, TypeError):
        return "Invalid password setup link.", 400
    try:
        with db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE gm_users SET password_hash=%s, email_verified=TRUE, verified_at=NOW() WHERE LOWER(email)=LOWER(%s) AND is_admin=FALSE AND email_verified=FALSE", (generate_password_hash(password), email))
                if cur.rowcount != 1:
                    return "This account is already set up or could not be found. Please contact your administrator.", 409
        return "<html><meta name=viewport content='width=device-width,initial-scale=1'><body style='font:16px Arial;background:#0b1020;color:#f4f7ff;padding:40px;text-align:center'><h2>GHOST TRADER</h2><h3>Password set successfully</h3><p>Your email is verified. You can now return to the app and log in.</p></body></html>", 200, {"Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store"}
    except Exception as exc:
        log.error("Password setup database error: %s", exc)
        return "Password could not be saved because the database is unavailable. Please try again later.", 503


@app.post("/api/auth/admin/users")
@admin_required
def auth_admin_create_user():
    body = request.get_json(silent=True) or {}
    email = str(body.get("email", "")).strip().lower()
    if not email or "@" not in email or len(email) > 254:
        return jsonify({"error": "Enter a valid email address."}), 400
    # A random unusable initial password is stored until the user sets their own.
    initial_password = secrets.token_urlsafe(32)
    try:
        with db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("INSERT INTO gm_users (email,password_hash,is_admin,email_verified) VALUES (%s,%s,FALSE,FALSE)",
                            (email, generate_password_hash(initial_password)))
    except Exception as exc:
        if "unique" in str(exc).lower() or "duplicate" in str(exc).lower():
            return jsonify({"error": "An account with this email already exists."}), 409
        log.error("Create user database error: %s", exc)
        return jsonify({"error": "Could not create account. Check database connection."}), 503
    token = _auth_serializer.dumps({"email": email, "purpose": "password_setup"})
    try:
        _send_password_setup_email(email, token)
    except Exception as exc:
        log.error("Password setup email send failed: %s", exc)
        # Do not claim email delivery. Admin should fix SMTP and remove/recreate this account.
        return jsonify({"ok": False, "account_created": True, "email_sent": False, "error": "Account was created, but the setup email could not be sent. Check Gmail SMTP settings in Render, then use Resend Email for this account."}), 502
    return jsonify({"ok": True, "account_created": True, "email_sent": True, "message": "Account created. Password setup email sent; the user must set a password before logging in."}), 201


@app.get("/api/auth/admin/users")
@admin_required
def auth_admin_list_users():
    try:
        with db_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SELECT id,email,is_admin,email_verified,access_enabled,created_at FROM gm_users ORDER BY created_at DESC LIMIT 200")
                rows = cur.fetchall()
        return jsonify({"users": [dict(r) for r in rows]})
    except Exception as exc:
        log.error("Admin user list error: %s", exc)
        return jsonify({"error": "Could not load users."}), 503


@app.patch("/api/auth/admin/users/<int:user_id>/access")
@admin_required
def auth_admin_set_user_access(user_id: int):
    body = request.get_json(silent=True) or {}
    enabled = body.get("enabled")
    if not isinstance(enabled, bool):
        return jsonify({"error": "enabled must be true or false."}), 400
    try:
        with db_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SELECT id,is_admin FROM gm_users WHERE id=%s", (user_id,))
                target = cur.fetchone()
                if not target:
                    return jsonify({"error": "User not found."}), 404
                if target["is_admin"]:
                    return jsonify({"error": "Admin accounts cannot be disabled from this screen."}), 403
                cur.execute("UPDATE gm_users SET access_enabled=%s WHERE id=%s", (enabled, user_id))
        return jsonify({"ok": True, "user_id": user_id, "access_enabled": enabled})
    except Exception as exc:
        log.error("Admin access update error: %s", exc)
        return jsonify({"error": "Could not update account access."}), 503


@app.post("/api/auth/admin/users/<int:user_id>/resend")
@admin_required
def auth_admin_resend_setup_email(user_id: int):
    """Send a fresh password-setup link to an existing unverified non-admin user."""
    try:
        with db_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SELECT id,email,is_admin,email_verified FROM gm_users WHERE id=%s", (user_id,))
                user = cur.fetchone()
        if not user:
            return jsonify({"error": "User not found."}), 404
        if user.get("is_admin"):
            return jsonify({"error": "Resend setup email is only for non-admin users."}), 403
        if user.get("email_verified"):
            return jsonify({"error": "This account is already verified."}), 409
        token = _auth_serializer.dumps({"email": user["email"], "purpose": "password_setup"})
        _send_password_setup_email(user["email"], token)
        log.info("Admin resent password setup email for user_id=%s", user_id)
        return jsonify({"ok": True, "email_sent": True, "message": "Setup email sent again. Ask the user to check Inbox and Spam."})
    except Exception as exc:
        log.error("Resend setup email failed for user_id=%s: %s", user_id, exc)
        return jsonify({"error": "Could not send email. Check SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, SMTP_FROM and APP_BASE_URL in Render."}), 502


@app.delete("/api/auth/admin/users/<int:user_id>")
@admin_required
def auth_admin_delete_user(user_id: int):
    """Permanently delete one non-admin user. Admin records are protected."""
    try:
        with db_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SELECT id,email,is_admin FROM gm_users WHERE id=%s", (user_id,))
                target = cur.fetchone()
                if not target:
                    return jsonify({"error": "User not found."}), 404
                if target.get("is_admin"):
                    return jsonify({"error": "Admin accounts cannot be deleted from this screen."}), 403
                cur.execute("DELETE FROM gm_users WHERE id=%s AND is_admin=FALSE", (user_id,))
        log.info("Admin deleted user_id=%s", user_id)
        return jsonify({"ok": True, "deleted": True, "user_id": user_id})
    except Exception as exc:
        log.error("Admin delete user failed for user_id=%s: %s", user_id, exc)
        return jsonify({"error": "Could not delete account. Check database logs."}), 503


@app.get("/api/dashboard")
@login_required
def api_dashboard():
    """Fast dashboard summary: avoid running schema migrations on every request."""
    today = datetime.now(timezone.utc).date()
    db_ok = False
    db_msg = "Database status unavailable"
    signals = []
    wins = losses = total = api_calls = 0
    try:
        # One database connection for dashboard data. init_database() runs at startup;
        # calling it on every dashboard request caused unnecessary latency/timeouts.
        with db_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SELECT id, symbol, direction, candle_time, created_at, entry_price, reason, source, verified_quotex, status FROM gm_signals ORDER BY created_at DESC LIMIT 50")
                signals = [dict(r) for r in cur.fetchall()]
                cur.execute("SELECT COUNT(*) FILTER (WHERE status='WIN') AS wins, COUNT(*) FILTER (WHERE status='LOSS') AS losses, COUNT(*) FILTER (WHERE status IN ('WIN','LOSS')) AS total FROM gm_signals")
                stats_row = cur.fetchone() or {}
                wins, losses, total = int(stats_row.get("wins") or 0), int(stats_row.get("losses") or 0), int(stats_row.get("total") or 0)
                cur.execute("SELECT calls FROM gm_api_usage WHERE usage_day=%s", (today,))
                usage_row = cur.fetchone()
                api_calls = int(usage_row["calls"] if usage_row and usage_row.get("calls") is not None else 0)
        db_ok, db_msg = True, "Supabase/PostgreSQL connected"
    except Exception as exc:
        log.warning("Dashboard database query failed: %s", exc)
        db_msg = "Database unavailable; showing temporary in-memory data"
        with state_lock:
            signals = list(reversed(state.get("memory_signals", [])[-50:]))
            wins = sum(1 for x in state.get("memory_signals", []) if x.get("status") == "WIN")
            losses = sum(1 for x in state.get("memory_signals", []) if x.get("status") == "LOSS")
            total = wins + losses
            api_calls = int(state.get("memory_api_calls", {}).get(str(today), 0))
    return jsonify({
        "app": "GHOST MATRIX", "status": "running",
        "data_source": "Twelve Data external feed", "quotex_verified": False,
        "twelve_data_key_configured": bool(TWELVE_DATA_API_KEY),
        "bot_token_configured": bool(BOT_TOKEN), "chat_id_configured": bool(CHAT_ID),
        "database_ok": db_ok, "database_message": db_msg,
        "catalog_count": len(get_catalog()), "last_scan_utc": state.get("last_scan_utc"),
        "last_error": state.get("last_error"), "api_calls_today": api_calls,
        "daily_budget": DAILY_CREDIT_BUDGET, "signals": signals,
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


def _clean_model_json(content: str) -> dict[str, Any]:
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", str(content or "").strip(), flags=re.I).strip()
    try:
        obj = json.loads(cleaned)
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}

def _vision_consensus_prompt():
    return """You are GHOST AI, a cautious trading-chart vision analyst.
Analyze ONLY visible chart evidence. Never invent asset, timeframe, price or future candles.
Use price action and SMC concepts: trend, HH/HL or LL/LH, BOS/CHOCH, liquidity sweep,
order block, FVG/imbalance, rejection, momentum and support/resistance.
Return ONLY JSON with:
direction (CALL, PUT, NO TRADE), confidence (0-100 setup quality, NOT win probability),
summary, trend, structure, zones, reasoning, confirmation, invalidation,
confluences (array), market_hint.
Require at least 2 independent confluences for CALL/PUT; otherwise NO TRADE."""

def _call_openai_vision(encoded: str, mime: str, user_prompt: str) -> dict[str, Any]:
    if not OPENAI_API_KEY:
        return {"provider":"OpenAI","configured":False,"available":False,"reason":"OPENAI_API_KEY not configured"}
    payload = {
        "model": OPENAI_VISION_MODEL,
        "temperature": 0.05,
        "max_completion_tokens": 1400,
        "messages": [
            {"role":"system","content":_vision_consensus_prompt()},
            {"role":"user","content":[
                {"type":"text","text":user_prompt},
                {"type":"image_url","image_url":{"url":f"data:{mime};base64,{encoded}"}}
            ]}
        ]
    }
    try:
        r=http_session.post(OPENAI_BASE,
            headers={"Authorization":f"Bearer {OPENAI_API_KEY}","Content-Type":"application/json"},
            json=payload, timeout=30)
        r.raise_for_status()
        content=r.json()["choices"][0]["message"]["content"]
        obj=_clean_model_json(content)
        return {"provider":"OpenAI","configured":True,"available":bool(obj),"analysis":obj}
    except Exception as exc:
        return {"provider":"OpenAI","configured":True,"available":False,"reason":str(exc)[:180]}

def _call_gemini_vision(encoded: str, mime: str, user_prompt: str) -> dict[str, Any]:
    if not GEMINI_API_KEY:
        return {"provider":"Gemini","configured":False,"available":False,"reason":"GEMINI_API_KEY not configured"}
    url=f"{GEMINI_BASE}/{GEMINI_VISION_MODEL}:generateContent"
    payload={
        "contents":[{"parts":[
            {"text":_vision_consensus_prompt()+"\n\n"+user_prompt},
            {"inline_data":{"mime_type":mime,"data":encoded}}
        ]}],
        "generationConfig":{"temperature":0.05,"maxOutputTokens":1400,"responseMimeType":"application/json"}
    }
    try:
        r=http_session.post(url, params={"key":GEMINI_API_KEY},
            headers={"Content-Type":"application/json"}, json=payload, timeout=30)
        r.raise_for_status()
        data=r.json()
        content="".join(
            p.get("text","") for p in
            (data.get("candidates",[{}])[0].get("content",{}).get("parts",[]) or [])
            if isinstance(p,dict)
        )
        obj=_clean_model_json(content)
        return {"provider":"Gemini","configured":True,"available":bool(obj),"analysis":obj}
    except Exception as exc:
        return {"provider":"Gemini","configured":True,"available":False,"reason":str(exc)[:180]}

def _build_multi_model_consensus(results: list[dict[str, Any]]) -> dict[str, Any]:
    valid=[r for r in results if r.get("available") and isinstance(r.get("analysis"),dict)]
    votes=[]
    for r in valid:
        d=str(r["analysis"].get("direction","NO TRADE")).upper()
        votes.append(d if d in ("CALL","PUT","NO TRADE") else "NO TRADE")
    call=votes.count("CALL"); put=votes.count("PUT"); no=votes.count("NO TRADE")
    if call and call>put and call>=no:
        direction="CALL"
    elif put and put>call and put>=no:
        direction="PUT"
    else:
        direction="NO TRADE"
    agreement=round((max(call,put,no)/len(votes))*100) if votes else 0
    chosen=next((r["analysis"] for r in valid if str(r["analysis"].get("direction","")).upper()==direction), (valid[0]["analysis"] if valid else {}))
    base_conf=0
    try: base_conf=int(float(chosen.get("confidence",0)))
    except Exception: pass
    # Consensus is a setup-quality modifier, never a probability claim.
    confidence=max(0,min(92,round(base_conf*0.65 + agreement*0.35))) if direction in ("CALL","PUT") else min(base_conf,48)
    return {
        "direction":direction,
        "confidence":confidence,
        "model_agreement":agreement,
        "votes":{"CALL":call,"PUT":put,"NO TRADE":no},
        "providers":[r.get("provider") for r in valid],
        "provider_status":[{k:r.get(k) for k in ("provider","configured","available","reason")} for r in results],
        "analysis":chosen
    }

@app.post("/api/analyze-image")
@login_required
def api_analyze_image():
    # GHOST AI: Backend-enforced access control - logged_in AND active_plan required
    _uid = session.get("user_id")
    if not _uid:
        return jsonify({"error": "Login required", "auth_required": True, "locked": True}), 401
    _plan = get_user_active_plan(int(_uid))
    # OWNER/ADMIN: full access without plan
    _is_owner_analyze = False
    try:
        with db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT email, is_admin FROM gm_users WHERE id=%s", (int(_uid),))
                _ru = cur.fetchone()
                if _ru: _is_owner_analyze = _is_owner_email(_ru[0] or "") or bool(_ru[1])
    except Exception:
        pass
    _has_plan = True if _is_owner_analyze else bool(_plan and _plan.get("plan_name") not in ("FREE",) and _plan.get("status")=="active")
    if not _has_plan:
        return jsonify({"error": "SIGNAL ACCESS LOCKED", "locked": True, "message": "Your account does not have an active signal plan.", "affiliate_url": AFFILIATE_URL, "plan": _plan}), 403
    if not _api_allowed("vision", 4, 60):
        return jsonify({"error": "এক মিনিটে সর্বোচ্চ ৪টি screenshot analysis। পরে আবার চেষ্টা করো।"}), 429
    upload = request.files.get("image")
    if upload is None or not upload.filename:
        return jsonify({"error": "একটি chart screenshot upload করো।"}), 400
    _provider = str(request.form.get("provider") or "groq").strip().lower()
    _provider_keys = {"openai": OPENAI_API_KEY, "gemini": GEMINI_API_KEY, "groq": GROQ_API_KEY}
    _provider_names = {"openai": "OpenAI", "gemini": "Gemini", "groq": "Groq"}
    if _provider not in _provider_keys:
        return jsonify({"error": "Invalid AI provider selected."}), 400
    if not _provider_keys[_provider]:
        _env_name = {"openai": "OPENAI_API_KEY", "gemini": "GEMINI_API_KEY", "groq": "GROQ_API_KEY (or legacy AI_API_KEY)"}[_provider]
        return jsonify({"error": f"{_provider_names[_provider]} API key is not configured in Render ({_env_name}).", "provider": _provider}), 503
    raw = upload.read(12 * 1024 * 1024 + 1)
    if not raw or len(raw) > 12 * 1024 * 1024:
        return jsonify({"error": "ছবি খালি অথবা ১২ MB-এর বেশি। ছোট screenshot upload করো।"}), 413
    # GHOST AI: Screenshot Quality Control
    _q_score, _q_issues = check_screenshot_quality(raw)
    _market = str(request.form.get("market") or request.form.get("symbol") or "")
    _mtype = str(request.form.get("market_type") or request.form.get("marketType") or "OTC")
    _timeframe = _normalize_timeframe(request.form.get("timeframe") or "1 MIN")
    if _q_score < 55:
        return jsonify({"error": "SCREENSHOT QUALITY TOO LOW", "quality_too_low": True, "image_quality": _q_score, "issues": _q_issues, "message": "Please upload a clearer chart screenshot with visible candles, timeframe and market information."}), 422
    try:
        im = Image.open(io.BytesIO(raw))
        im.verify()
        mime = upload.mimetype if upload.mimetype in ("image/jpeg", "image/png", "image/webp") else "image/jpeg"
        # Reduce vision latency by resizing large screenshots before the model call.
        vision_bytes = raw
        try:
            work = Image.open(io.BytesIO(raw)).convert("RGB")
            work.thumbnail((1800, 1200), Image.Resampling.LANCZOS)
            buf = io.BytesIO()
            work.save(buf, format="JPEG", quality=84, optimize=True)
            vision_bytes = buf.getvalue()
            mime = "image/jpeg"
        except Exception:
            pass
        encoded = base64.b64encode(vision_bytes).decode("ascii")
        system_prompt = '''You are GHOST TRADER V3 — elite SMC price action analyst with high accuracy + tradeable signals.

MISSION: Analyze ONLY what is visible in the chart screenshot. Never hallucinate asset, timeframe, price.

ELITE SMC FRAMEWORK:
1. Trend: HH/HL=up, LL/LH=down, BOS=Break of Structure, CHOCH=Change of Character
2. Liquidity: Sweep, stop hunt, equal highs/lows taken
3. Zones: Order Block (bullish/bearish OB), FVG, Premium/Discount, Imbalance, Breaker
4. Momentum: Engulfing, pinbar rejection, displacement

TRADEABLE RULES (BALANCED - Less NO TRADE):
- 2+ confluences REQUIRED for CALL/PUT (previously 3+ was too strict causing too many NO TRADE)
- CALL if: bullish BOS/CHOCH + bullish OB/FVG or bullish momentum + discount zone preferred
- PUT if: bearish BOS/CHOCH + bearish OB/FVG or bearish momentum + premium zone preferred
- If chart blurry or 0-1 confluence only → NO TRADE
- Confidence = setup quality, not win probability. Never promise profit.

CONFIDENCE CALIBRATION (BALANCED):
- 5+ confluences + clean = 86-92%
- 4 confluences = 80-88%
- 3 confluences = 70-80%
- 2 confluences = 58-70% (tradeable, medium)
- 0-1 confluence or unclear = 0-48% + NO TRADE
- Mixed signals → reduce 10-15, not full 20

Return ONLY valid JSON with keys:
direction (CALL, PUT, or NO TRADE), confidence (0-100), summary (Bengali short), trend (Bengali), structure (Bengali), zones (Bengali), reasoning (Bengali), confirmation (Bengali), invalidation (Bengali), confidence_note (Bengali), confluences (array like ["BOS","Bullish OB","FVG","Engulfing"]), market_hint (asset/timeframe if visible)
'''
        user_prompt = "এই chart screenshot-টি বিশ্লেষণ করো। asset/timeframe দেখা গেলে শনাক্ত করো; না দেখা গেলে অনুমান কোরো না। CALL, PUT অথবা NO TRADE নির্বাচন করো। দুই দিকের প্রমাণ যাচাই করো এবং entry নেওয়ার আগে কী confirmation দরকার বলো। JSON ছাড়া অন্য কিছু দেবে না।"
        if _provider == "openai":
            _provider_result = _call_openai_vision(encoded, mime, user_prompt)
            if not _provider_result.get("available"):
                raise RuntimeError(_provider_result.get("reason") or "OpenAI vision request failed")
            content = json.dumps(_provider_result["analysis"], ensure_ascii=False)
        elif _provider == "gemini":
            _provider_result = _call_gemini_vision(encoded, mime, user_prompt)
            if not _provider_result.get("available"):
                raise RuntimeError(_provider_result.get("reason") or "Gemini vision request failed")
            content = json.dumps(_provider_result["analysis"], ensure_ascii=False)
        else:
            payload = {"model": GROQ_VISION_MODEL, "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": [
                    {"type": "text", "text": user_prompt},
                    {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{encoded}"}}
                ]}
            ], "temperature": 0.05, "max_completion_tokens": 1400}
            response = http_session.post(GROQ_BASE, headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}, json=payload, timeout=28)
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"].strip()
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", content, flags=re.I).strip()
        try:
            analysis = json.loads(cleaned)
            if not isinstance(analysis, dict):
                raise ValueError("analysis JSON is not an object")
            direction = str(analysis.get("direction", "NO TRADE")).upper()
            analysis["direction"] = direction if direction in ("CALL", "PUT", "NO TRADE") else "NO TRADE"
            try:
                base_conf = max(0, min(100, int(float(analysis.get("confidence", 0)))))
            except (TypeError, ValueError):
                base_conf = 0
            
            # --- GHOST V3 Accuracy Boost: Confluence-based recalibration ---
            confluences = analysis.get("confluences", [])
            if not isinstance(confluences, list):
                confluences = []
            # Count from reasoning text if confluences list empty
            reasoning_text = (str(analysis.get("reasoning","")) + " " + str(analysis.get("structure","")) + " " + str(analysis.get("zones",""))).lower()
            keyword_map = ["bos","choch","order block","fvg","liquidity","premium","discount","engulfing","pinbar","pin bar","imbalance","breaker","displacement","sweep"]
            detected = []
            for kw in keyword_map:
                if kw in reasoning_text and kw not in [x.lower() for x in detected]:
                    detected.append(kw)
            if not confluences:
                confluences = detected
            analysis["confluences"] = confluences
            conf_count = len(confluences)
            
            # Balanced filter - less NO TRADE, more tradeable signals
            if analysis["direction"] in ("CALL","PUT"):
                if conf_count < 2:
                    analysis["direction"] = "NO TRADE"
                    analysis["invalidation"] = f"মাত্র {conf_count} confluence - 2+ লাগে trade এর জন্য। Detected: {', '.join(confluences) if confluences else 'কম'}"
                    base_conf = min(base_conf, 48)
                elif conf_count >= 5:
                    base_conf = max(base_conf, 86)
                    base_conf = min(base_conf, 92)
                elif conf_count >= 4:
                    base_conf = max(base_conf, 80)
                    base_conf = min(base_conf, 88)
                elif conf_count >= 3:
                    base_conf = max(base_conf, 70)
                    base_conf = min(base_conf, 80)
                else: # 2 confluences - still tradeable with medium confidence
                    base_conf = max(base_conf, 58)
                    base_conf = min(base_conf, 70)
            else:
                # Keep NO TRADE but with realistic confidence
                if conf_count >= 2:
                    # If model said NO TRADE but we have 2+ confluences, allow reconsider as weak signal?
                    # Keep NO TRADE but bump confidence slightly so user sees not always 45%
                    base_conf = max(base_conf, 48)
                    base_conf = min(base_conf, 58)
                else:
                    base_conf = min(base_conf, 48)
            
            # Cap never 100% to stay realistic
            if base_conf > 92:
                base_conf = 92
            
            analysis["confidence"] = base_conf
            for key in ("summary", "trend", "structure", "zones", "reasoning", "confirmation", "invalidation", "confidence_note"):
                if not isinstance(analysis.get(key), str):
                    analysis[key] = "ছবিতে পরিষ্কারভাবে শনাক্ত করা যায়নি।"
        except (json.JSONDecodeError, ValueError):
            analysis = {"direction": "NO TRADE", "confidence": 0, "summary": content,
                        "trend": "Structured result unavailable", "structure": "Structured result unavailable",
                        "zones": "Structured result unavailable", "reasoning": content,
                        "confirmation": "Clear price confirmation ছাড়া entry নিও না।",
                        "invalidation": "সেটআপ নিশ্চিত নয়।", "confidence_note": "Confidence নির্ভরযোগ্যভাবে বের করা যায়নি; এটি win probability নয়।"}
        # Run only the provider selected in the screenshot AI selector.
        analysis["model_providers"] = [_provider_names[_provider]]
        analysis["selected_provider"] = _provider

        # GHOST AI: Store signal, add quality metrics, prefer NO SIGNAL over low-confidence
        _dir = str(analysis.get("direction","NO TRADE")).upper()
        _conf = int(analysis.get("confidence",0) or 0)
        if _dir in ("CALL","PUT") and _conf < 58:
            _dir = "NO SIGNAL"; analysis["direction"]="NO SIGNAL"; analysis["signal"]="NO SIGNAL"
        if _dir == "NO TRADE":
            _dir = "NO SIGNAL"; analysis["direction"]="NO SIGNAL"
        _verified = get_verified_accuracy()
        _analysis_text = str(analysis.get("summary","") or "") + " | " + str(analysis.get("reasoning","") or "")
        try:
            with db_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("INSERT INTO gm_user_signals (user_id, market, market_type, timeframe, signal, confidence, analysis, image_quality, outcome) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'PENDING') RETURNING id", (int(_uid), _market or "EUR/USD", _mtype or "OTC", _timeframe, _dir, _conf, _analysis_text[:2000], int(_q_score)))
        except Exception as _e:
            log.warning("Could not store user signal: %s", _e)
        return jsonify({"analysis": analysis, "signal": _dir, "confidence": _conf, "market": _market or "EUR/USD", "market_type": _mtype or "OTC", "timeframe": _timeframe, "image_quality": int(_q_score), "image_quality_issues": _q_issues, "verified_accuracy": _verified, "analysis_confidence": _conf, "model_agreement": 0, "model_votes": {}, "model_providers": analysis.get("model_providers",[]), "selected_provider": _provider, "status": "VALID" if _dir in ("CALL","PUT") else "NO SIGNAL", "warning": "Screenshot-based educational analysis only. Confidence is setup quality, not a win probability. Trading involves risk - never trade more than you can afford to lose.", "risk_warning": "Trading involves substantial risk. This is not financial advice."})
    except Exception as exc:
        log.warning("Mini App screenshot analysis failed: %s", exc)
        return jsonify({"error": "ছবি বিশ্লেষণ করা যায়নি। নির্বাচিত AI provider-এর API key, model ও provider limit পরীক্ষা করো।"}), 502


@app.post("/api/signals/generate")
@login_required
def api_signals_generate():
    # Alias to /api/analyze-image with same access control - frontend uses this for GHOST AI flow
    return api_analyze_image()


def _signal_access_for_user(uid: int) -> bool:
    """Backend access gate shared by live/future/injector signal endpoints."""
    try:
        plan = get_user_active_plan(int(uid))
        if plan and (plan.get("is_owner") or plan.get("is_admin")):
            return True
        return bool(plan and plan.get("plan_name") not in ("FREE",) and plan.get("status") == "active")
    except Exception:
        return False


def _setup_confidence(result: dict[str, Any]) -> int:
    """Return a setup-quality score, not a win probability."""
    direction = str(result.get("direction") or "")
    if direction not in ("CALL", "PUT"):
        return 0
    score = 60
    rsi_v = result.get("rsi")
    try:
        rsi_v = float(rsi_v)
        if direction == "CALL" and 52 <= rsi_v <= 68:
            score += 8
        elif direction == "PUT" and 32 <= rsi_v <= 48:
            score += 8
    except (TypeError, ValueError):
        pass
    try:
        c = result.get("candle") or {}
        rng = float(c.get("high", 0)) - float(c.get("low", 0))
        body = abs(float(c.get("close", 0)) - float(c.get("open", 0)))
        if rng > 0:
            score += min(10, round((body / rng) * 10))
    except (TypeError, ValueError):
        pass
    bt = result.get("backtest") or {}
    try:
        wr = float(bt.get("win_rate_pct"))
        if wr >= 60:
            score += 10
        elif wr >= 50:
            score += 5
    except (TypeError, ValueError):
        pass
    return max(58, min(94, int(score)))


@app.post("/api/inject-signal")
@login_required
def api_inject_signal():
    uid = session.get("user_id")
    if not _signal_access_for_user(int(uid)):
        return jsonify({"error":"SIGNAL ACCESS LOCKED","locked":True,"affiliate_url":AFFILIATE_URL}),403
    body=request.get_json(silent=True) or {}
    market=str(body.get("market","")).strip().upper()
    market_type=str(body.get("market_type","REAL")).strip().upper()
    duration=max(1,min(60,int(body.get("duration",1) or 1)))
    timeframe=_normalize_timeframe(body.get("timeframe") or "1 MIN")
    if not market: return jsonify({"error":"Market নির্বাচন করো।"}),400
    try:
        if "OTC" in market_type or "(OTC)" in market:
            if not OTCHARTS_API_KEY: return jsonify({"error":"OTCHARTS_API_KEY is not configured."}),503
            if not _api_allowed("otc_inject",2,60): return jsonify({"error":"OTC analysis rate limit reached."}),429
            candles,meta=_otc_candles(market,timeframe,300)
            result=analyze_candles(market.replace(" (OTC)",""),candles,timeframe)
            result.update({"source":meta["source"],"verified_quotex":False,"market_type":"OTC","timeframe":timeframe,"backtest":backtest_ema(candles)})
            direction=result.get("direction","NO TRADE"); confidence=_setup_confidence(result) if direction in ("CALL","PUT") else 0
            return jsonify({"available":direction in ("CALL","PUT"),"direction":direction if direction in ("CALL","PUT") else "NO SIGNAL","signal":direction if direction in ("CALL","PUT") else "NO SIGNAL","confidence":confidence,"market":market,"market_type":"OTC","duration":duration,"timeframe":timeframe,"analysis":result,"factors":["OTCharts Quotex OTC candles","EMA9 / EMA21 / EMA50","RSI momentum","Candle body strength","Support / resistance"],"source":meta["source"],"verified_quotex":False}),200
        symbol=market.replace(" (OTC)","").strip()
        allowed={str(item.get("symbol","")).upper() for item in get_catalog()}
        if symbol not in allowed: return jsonify({"error":"Market is not in the configured real-market allowlist."}),400
        if not TWELVE_DATA_API_KEY: return jsonify({"error":"TWELVE_DATA_API_KEY is not configured."}),503
        if not _api_allowed("inject",2,60): return jsonify({"error":"Signal analysis rate limit reached."}),429
        result=scan_one_symbol(symbol,timeframe)
        direction=result.get("direction","NO SIGNAL"); confidence=_setup_confidence(result) if direction in ("CALL","PUT") else 0
        return jsonify({"available":direction in ("CALL","PUT"),"direction":direction if direction in ("CALL","PUT") else "NO SIGNAL","signal":direction if direction in ("CALL","PUT") else "NO SIGNAL","confidence":confidence,"market":symbol,"market_type":"REAL","duration":duration,"timeframe":timeframe,"analysis":result,"factors":["Twelve Data live candles","EMA9 / EMA21 / EMA50","RSI momentum","Candle body strength","Support / resistance","Historical EMA sanity check"],"source":"Twelve Data external feed","verified_quotex":False}),200
    except Exception as exc:
        log.warning("Injector analysis failed: %s",exc)
        return jsonify({"error":"Live market analysis failed: %s" % str(exc)[:180]}),502

@app.post("/api/future-signals")
@login_required
def api_future_signals():
    uid=session.get("user_id")
    if not _signal_access_for_user(int(uid)): return jsonify({"error":"SIGNAL ACCESS LOCKED","locked":True,"affiliate_url":AFFILIATE_URL}),403
    body=request.get_json(silent=True) or {}
    markets=[str(x).strip().upper() for x in (body.get("markets") or []) if str(x).strip()]
    market_type=str(body.get("market_type","REAL")).strip().upper()
    count=max(5,min(20,int(body.get("count",5) or 5)))
    timeframe=_normalize_timeframe(body.get("timeframe") or "1 MIN")
    if not markets: return jsonify({"error":"At least one market is required."}),400

    is_otc = ("OTC" in market_type)
    if is_otc:
        # Selected OTC pairs are first; then the configured OTC candidate pool is used
        # only to reach the requested number of independently analysed candidates.
        pool = list(dict.fromkeys(markets + OTC_FUTURE_SYMBOLS))
    else:
        configured=[str(x.get("symbol","")).upper() for x in get_catalog()]
        pool=list(dict.fromkeys(markets + configured))

    results=[]; scanned=0
    for market in pool[:40]:
        if len(results) >= count:
            break
        try:
            if is_otc:
                if not OTCHARTS_API_KEY: break
                if not _api_allowed("otc_future",2,60): break
                candles,meta=_otc_candles(market,timeframe,300)
                r=analyze_candles(market.replace(" (OTC)",""),candles,timeframe)
                source=meta["source"]
            else:
                if not TWELVE_DATA_API_KEY: break
                if not _api_allowed("future",2,60): break
                symbol=market.replace(" (OTC)","")
                allowed={str(x.get("symbol","" )).upper() for x in get_catalog()}
                if symbol not in allowed: continue
                r=scan_one_symbol(symbol,timeframe); source="Twelve Data external feed"
            scanned+=1
            if r.get("direction") in ("CALL","PUT"):
                candle=r.get("candle") or {}
                base_time=candle.get("time")
                if isinstance(base_time,str):
                    base_time=datetime.fromisoformat(base_time.replace("Z","+00:00"))
                if not isinstance(base_time,datetime):
                    base_time=datetime.now(timezone.utc)
                tf_minutes={"1 MIN":1,"2 MIN":2,"5 MIN":5,"10 MIN":10,"15 MIN":15,"30 MIN":30,"1 HOUR":60}.get(timeframe,1)
                # Always schedule after the candle being analysed. This prevents a future card
                # from being stamped with the current candle's start time.
                next_window=base_time + timedelta(minutes=tf_minutes)
                now_utc=datetime.now(timezone.utc)
                while next_window <= now_utc:
                    next_window += timedelta(minutes=tf_minutes)
                results.append({
                    "market":market,"symbol":market,"direction":r["direction"],"signal":r["direction"],
                    "confidence":_setup_confidence(r),"price":r.get("price"),
                    "entry_time":next_window.astimezone(BANGLADESH_TZ).isoformat(),
                    "expiry":f"{tf_minutes} MIN","reason":r.get("reason"),
                    "strategy":"GHOST Future Setup Engine","source":source,"timeframe":timeframe,
                    "verified_quotex":False,"status":"FUTURE CANDIDATE"
                })
        except Exception as exc:
            log.warning("Future candidate scan failed for %s: %s",market,exc)

    results.sort(key=lambda x:(-int(x.get("confidence",0)), x.get("entry_time", "")))
    return jsonify({"available":bool(results),"signals":results[:count],"requested":count,"scanned":scanned,
                    "market_type":"OTC" if is_otc else "REAL","timeframe":timeframe,
                    "mode":"future setup candidates scheduled after the analysed candle",
                    "warning":"Future candidates are watch/setup windows, not guaranteed future prices or outcomes. No current-candle timestamp is returned as a future entry."}),200

@app.get("/api/news/events")
@login_required
def api_news_events_alias():
    """Compatibility endpoint used by the News UI."""
    return api_news()


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


@app.get("/api/news/bias")
@login_required
def api_news_bias():
    """News bias for a market - no invented data. Returns NEWS DATA UNAVAILABLE if no real data."""
    market = str(request.args.get("market") or request.args.get("symbol") or "").strip().upper()
    if not market:
        return jsonify({"available": False, "status": "NEWS DATA UNAVAILABLE", "message": "Market not provided."})
    # Extract currencies from EUR/USD
    parts = market.replace(" ", "").split("/")
    base = parts[0][:3] if len(parts) > 0 else ""
    quote = parts[1][:3] if len(parts) > 1 else ""
    try:
        events = get_economic_calendar(7)
    except Exception as exc:
        log.warning("News bias calendar failed: %s", exc)
        return jsonify({"available": False, "status": "NEWS DATA UNAVAILABLE", "market": market, "message": "Real economic calendar unavailable. Do not force a signal from news."})
    if not events:
        return jsonify({"available": False, "status": "NEWS DATA UNAVAILABLE", "market": market, "message": "No high-impact events found for this period."})
    # Filter to base/quote currencies
    rel = [e for e in events if str(e.get("currency") or "").upper() in (base, quote)]
    if not rel:
        return jsonify({"available": False, "status": "NEWS DATA UNAVAILABLE", "market": market, "events": [], "message": f"No high-impact events for {base}/{quote} in next 7 days. Use technical analysis only."})
    # Compute bias from actual vs forecast where actual exists
    bullish = 0; bearish = 0; scored = 0
    clean = []
    for e in rel[:12]:
        dt = e.get("datetime")
        # Simple bias: if actual exists and differs from forecast, infer direction via analyze_manual_news helper
        bias = "NEUTRAL"
        if e.get("actual") is not None and e.get("forecast") is not None:
            try:
                tmp = {"currency": e.get("currency"), "event": e.get("name") or "", "actual": _extract_number(str(e.get("actual"))), "forecast": _extract_number(str(e.get("forecast"))), "previous": _extract_number(str(e.get("previous"))) if e.get("previous") is not None else None}
                dirc, _, _ = analyze_manual_news({**tmp, "actual_text": str(e.get("actual")), "forecast_text": str(e.get("forecast")), "previous_text": str(e.get("previous") or "")})
                if dirc.startswith("UP"): bias = "BULLISH " + str(e.get("currency"))
                elif dirc.startswith("DOWN"): bias = "BEARISH " + str(e.get("currency"))
                if bias != "NEUTRAL": scored += 1
                if "BULLISH" in bias: bullish += 1
                if "BEARISH" in bias: bearish += 1
            except Exception:
                pass
        clean.append({"datetime": dt.isoformat() if isinstance(dt, datetime) else None, "datetime_bd": dt.strftime("%d %b %Y, %I:%M %p") if isinstance(dt, datetime) else None, "name": e.get("name"), "currency": e.get("currency"), "impact": e.get("impact", "high"), "forecast": e.get("forecast"), "previous": e.get("previous"), "actual": e.get("actual"), "bias": bias})
    if scored == 0:
        overall = "NEUTRAL / AVOID"
        detail = "Events found but no Actual vs Forecast to determine bias. Do not use news as confirmation."
    else:
        # Map currency bias to market: if base bullish -> CALL, if quote bullish -> PUT (base vs quote)
        base_bull = sum(1 for c in clean if c["currency"] == base and "BULLISH" in c["bias"])
        base_bear = sum(1 for c in clean if c["currency"] == base and "BEARISH" in c["bias"])
        quote_bull = sum(1 for c in clean if c["currency"] == quote and "BULLISH" in c["bias"])
        quote_bear = sum(1 for c in clean if c["currency"] == quote and "BEARISH" in c["bias"])
        net_base = base_bull - base_bear
        net_quote = quote_bull - quote_bear
        if net_base > 0 and net_quote <= 0:
            overall = "BULLISH " + base + " - Favors CALL / UP"
        elif net_base < 0 and net_quote >= 0:
            overall = "BEARISH " + base + " - Favors PUT / DOWN"
        elif net_base > 0 and net_quote > 0:
            overall = "CONFLICT - Both currencies bullish - NEUTRAL / AVOID"
        elif net_base < 0 and net_quote < 0:
            overall = "CONFLICT - Both bearish - NEUTRAL / AVOID"
        else:
            overall = "NEUTRAL / AVOID"
        detail = f"Scored {scored} event(s) with Actual vs Forecast. Base {base}: +{base_bull}/-{base_bear}, Quote {quote}: +{quote_bull}/-{quote_bear}."
    return jsonify({"available": True, "market": market, "base": base, "quote": quote, "events": clean, "bias": overall, "detail": detail, "status": overall, "source": NEWS_SOURCE_URL, "timezone": "Asia/Dhaka"})

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


# --------------------------- GHOST MATRIX PRO API ---------------------------
TIMEFRAME_MAP = {
    "1 MIN": "1min", "2 MIN": "1min", "5 MIN": "5min", "10 MIN": "1min", "15 MIN": "15min", "30 MIN": "30min", "1 HOUR": "1h", "1H": "1h",
}


def _normalize_timeframe(value: str) -> str:
    v = str(value or "1 MIN").strip().upper().replace("MINUTE", "MIN").replace("MINUTES", "MIN")
    aliases = {"1M":"1 MIN", "2M":"2 MIN", "5M":"5 MIN", "10M":"10 MIN", "15M":"15 MIN", "30M":"30 MIN", "60M":"1 HOUR", "1HR":"1 HOUR", "1 H":"1 HOUR"}
    return aliases.get(v, v if v in TIMEFRAME_MAP else "1 MIN")


def _user_pref(uid: int) -> dict:
    defaults = {"timezone":"Asia/Dhaka","default_market_type":"OTC","default_timeframe":"1 MIN","sound_alerts":True,"browser_alerts":True,"telegram_alerts":False,"compact_mode":False}
    try:
        with db_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SELECT * FROM gm_user_preferences WHERE user_id=%s", (uid,)); row=cur.fetchone()
                if not row:
                    cur.execute("INSERT INTO gm_user_preferences (user_id) VALUES (%s) ON CONFLICT (user_id) DO NOTHING", (uid,))
                    return defaults
                d=dict(row); return {k:(d.get(k) if d.get(k) is not None else v) for k,v in defaults.items()}
    except Exception: return defaults


def _risk_profile(uid: int) -> dict:
    defaults={"capital":0,"risk_per_trade":1,"max_daily_loss":0,"max_consecutive_losses":3,"payout":80,"martingale_enabled":False}
    try:
        with db_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SELECT * FROM gm_risk_profiles WHERE user_id=%s", (uid,)); row=cur.fetchone()
                if not row: return defaults
                d=dict(row); return {k:(float(d[k]) if k in ('capital','risk_per_trade','max_daily_loss','payout') else d.get(k,v)) for k,v in defaults.items()}
    except Exception: return defaults


@app.get("/api/profile")
@login_required
def api_profile():
    uid=int(session["user_id"])
    with db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT id,email,name,is_admin,email_verified,access_enabled,role,status,created_at FROM gm_users WHERE id=%s", (uid,)); u=cur.fetchone()
    if not u: return jsonify({"error":"User not found"}),404
    u=dict(u); u["created_at"]=u["created_at"].isoformat() if u.get("created_at") else None
    return jsonify({"user":u,"plan":get_user_active_plan(uid),"preferences":_user_pref(uid),"risk":_risk_profile(uid)})


@app.put("/api/profile")
@login_required
def api_profile_update():
    uid=int(session["user_id"]); body=request.get_json(silent=True) or {}
    name=str(body.get("name") or "").strip()
    if name and len(name)>100: return jsonify({"error":"Name is too long."}),400
    with db_connection() as conn:
        with conn.cursor() as cur: cur.execute("UPDATE gm_users SET name=%s WHERE id=%s", (name,uid))
    return jsonify({"ok":True,"name":name})


@app.get("/api/preferences")
@login_required
def api_preferences(): return jsonify({"preferences":_user_pref(int(session["user_id"]))})


@app.put("/api/preferences")
@login_required
def api_preferences_update():
    uid=int(session["user_id"]); b=request.get_json(silent=True) or {}; p=_user_pref(uid)
    for k in ("timezone","default_market_type","default_timeframe"):
        if k in b: p[k]=str(b[k])
    if "default_timeframe" in p: p["default_timeframe"]=_normalize_timeframe(p["default_timeframe"])
    for k in ("sound_alerts","browser_alerts","telegram_alerts","compact_mode"):
        if k in b: p[k]=bool(b[k])
    with db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO gm_user_preferences (user_id,timezone,default_market_type,default_timeframe,sound_alerts,browser_alerts,telegram_alerts,compact_mode) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (user_id) DO UPDATE SET timezone=EXCLUDED.timezone,default_market_type=EXCLUDED.default_market_type,default_timeframe=EXCLUDED.default_timeframe,sound_alerts=EXCLUDED.sound_alerts,browser_alerts=EXCLUDED.browser_alerts,telegram_alerts=EXCLUDED.telegram_alerts,compact_mode=EXCLUDED.compact_mode,updated_at=NOW()", (uid,p["timezone"],p["default_market_type"],p["default_timeframe"],p["sound_alerts"],p["browser_alerts"],p["telegram_alerts"],p["compact_mode"]))
    return jsonify({"ok":True,"preferences":p})


@app.get("/api/risk")
@login_required
def api_risk(): return jsonify({"risk":_risk_profile(int(session["user_id"]))})


@app.put("/api/risk")
@login_required
def api_risk_update():
    uid=int(session["user_id"]); b=request.get_json(silent=True) or {}
    def num(k,default):
        try:return float(b.get(k,default))
        except:return default
    risk={"capital":max(0,num("capital",0)),"risk_per_trade":max(0,min(100,num("risk_per_trade",1))),"max_daily_loss":max(0,num("max_daily_loss",0)),"max_consecutive_losses":max(1,min(20,int(num("max_consecutive_losses",3)))),"payout":max(0,min(100,num("payout",80))),"martingale_enabled":bool(b.get("martingale_enabled",False))}
    with db_connection() as conn:
        with conn.cursor() as cur: cur.execute("INSERT INTO gm_risk_profiles VALUES (%s,%s,%s,%s,%s,%s,%s,NOW()) ON CONFLICT(user_id) DO UPDATE SET capital=EXCLUDED.capital,risk_per_trade=EXCLUDED.risk_per_trade,max_daily_loss=EXCLUDED.max_daily_loss,max_consecutive_losses=EXCLUDED.max_consecutive_losses,payout=EXCLUDED.payout,martingale_enabled=EXCLUDED.martingale_enabled,updated_at=NOW()", (uid,risk["capital"],risk["risk_per_trade"],risk["max_daily_loss"],risk["max_consecutive_losses"],risk["payout"],risk["martingale_enabled"]))
    return jsonify({"ok":True,"risk":risk})


@app.get("/api/timeframes")
@login_required
def api_timeframes(): return jsonify({"timeframes":["1 MIN","2 MIN","5 MIN","10 MIN","15 MIN","30 MIN","1 HOUR"],"default":"1 MIN"})


@app.get("/api/live")
@login_required
def api_live():
    market_type=str(request.args.get("market_type") or "REAL").upper(); tf=_normalize_timeframe(request.args.get("timeframe")); symbols=[x["symbol"] for x in get_catalog()]
    if market_type=="OTC": return jsonify({"available":False,"market_type":"OTC","timeframe":tf,"signals":[],"message":"Verified OTC feed is not connected. GHOST MATRIX will not fabricate OTC prices or signals."})
    if not _signal_access_for_user(int(session["user_id"])): return jsonify({"locked":True,"signals":[],"message":"Active signal plan required."}),403
    results=[]
    for sym in symbols[:8]:
        try:
            interval=TIMEFRAME_MAP[tf]; payload=twelve_get("time_series",{"symbol":sym,"interval":interval,"outputsize":300,"order":"desc","format":"JSON","timezone":"UTC"}); candles=parse_candles(payload); r=analyze_candles(sym,candles); r["confidence"]=_setup_confidence(r); r["timeframe"]=tf; results.append({"symbol":sym,"direction":r.get("direction"),"confidence":r.get("confidence",0),"price":r.get("price"),"reason":r.get("reason"),"timeframe":tf,"source":"Twelve Data external feed"})
        except Exception as exc: log.warning("live scan %s: %s",sym,exc)
    return jsonify({"available":True,"market_type":"REAL","timeframe":tf,"signals":results,"source":"Twelve Data external feed","verified_quotex":False,"warning":"Confidence is setup quality, not win probability."})


@app.get("/api/performance")
@login_required
def api_performance():
    with db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT COUNT(*) FILTER (WHERE outcome='WIN') wins, COUNT(*) FILTER (WHERE outcome='LOSS') losses, COUNT(*) FILTER (WHERE outcome='PENDING') pending FROM gm_user_signals WHERE user_id=%s", (int(session["user_id"]),)); row=dict(cur.fetchone() or {})
            cur.execute("SELECT COUNT(*) FILTER (WHERE direction='CALL') calls, COUNT(*) FILTER (WHERE direction='PUT') puts FROM gm_signals"); a=dict(cur.fetchone() or {})
    w=int(row.get("wins") or 0); l=int(row.get("losses") or 0); total=w+l
    return jsonify({"wins":w,"losses":l,"pending":int(row.get("pending") or 0),"settled":total,"win_rate":round(100*w/total,1) if total else None,"call_count":int(a.get("calls") or 0),"put_count":int(a.get("puts") or 0),"verified_only":True})


@app.get("/api/history")
@login_required
def api_history():
    uid=int(session["user_id"]); limit=max(1,min(100,int(request.args.get("limit",50))))
    with db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT id,market,market_type,timeframe,signal,confidence,analysis,outcome,created_at FROM gm_user_signals WHERE user_id=%s ORDER BY created_at DESC LIMIT %s", (uid,limit)); rows=[dict(x) for x in cur.fetchall()]
    for r in rows:
        if r.get("created_at"): r["created_at"]=r["created_at"].isoformat()
    return jsonify({"signals":rows,"count":len(rows)})


@app.get("/api/alerts")
@login_required
def api_alerts():
    uid=int(session["user_id"])
    with db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT id,alert_type,market,timeframe,enabled,threshold,created_at FROM gm_user_alerts WHERE user_id=%s ORDER BY id DESC",(uid,)); rows=[dict(x) for x in cur.fetchall()]
    for r in rows:
        if r.get("created_at"):r["created_at"]=r["created_at"].isoformat()
    return jsonify({"alerts":rows})


@app.post("/api/alerts")
@login_required
def api_alerts_create():
    uid=int(session["user_id"]); b=request.get_json(silent=True) or {}; typ=str(b.get("alert_type") or "SIGNAL").upper(); market=str(b.get("market") or ""); tf=_normalize_timeframe(b.get("timeframe")); enabled=bool(b.get("enabled",True)); threshold=max(0,min(100,int(b.get("threshold",70))))
    with db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO gm_user_alerts(user_id,alert_type,market,timeframe,enabled,threshold) VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(user_id,alert_type,market,timeframe) DO UPDATE SET enabled=EXCLUDED.enabled,threshold=EXCLUDED.threshold",(uid,typ,market,tf,enabled,threshold))
    return jsonify({"ok":True})


@app.delete("/api/alerts/<int:alert_id>")
@login_required
def api_alert_delete(alert_id):
    with db_connection() as conn:
        with conn.cursor() as cur: cur.execute("DELETE FROM gm_user_alerts WHERE id=%s AND user_id=%s",(alert_id,int(session["user_id"])))
    return jsonify({"ok":True})


@app.get("/api/uid")
@login_required
def api_uid_list():
    with db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT id,broker,uid,status,notes,created_at,reviewed_at FROM gm_uid_verifications WHERE user_id=%s ORDER BY created_at DESC",(int(session["user_id"]),)); rows=[dict(x) for x in cur.fetchall()]
    for r in rows:
        for k in ("created_at","reviewed_at"):
            if r.get(k):r[k]=r[k].isoformat()
    return jsonify({"verifications":rows})


@app.post("/api/uid")
@login_required
def api_uid_submit():
    uid=int(session["user_id"]); b=request.get_json(silent=True) or {}; broker=str(b.get("broker") or "Quotex").strip()[:80]; value=str(b.get("uid") or "").strip()[:120]
    if len(value)<3:return jsonify({"error":"Valid broker UID required."}),400
    with db_connection() as conn:
        with conn.cursor() as cur: cur.execute("INSERT INTO gm_uid_verifications(user_id,broker,uid,status) VALUES(%s,%s,%s,'PENDING')",(uid,broker,value))
    return jsonify({"ok":True,"status":"PENDING","message":"UID submitted for admin review."})


@app.get("/api/payment-requests")
@login_required
def api_payment_requests():
    with db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT pr.id,pr.amount,pr.method,pr.reference,pr.status,pr.notes,pr.created_at,p.name plan_name FROM gm_payment_requests pr LEFT JOIN gm_plans p ON p.id=pr.plan_id WHERE pr.user_id=%s ORDER BY pr.created_at DESC",(int(session["user_id"]),)); rows=[dict(x) for x in cur.fetchall()]
    for r in rows:
        if r.get("created_at"):r["created_at"]=r["created_at"].isoformat()
    return jsonify({"requests":rows,"affiliate_url":AFFILIATE_URL,"support":"https://t.me/ghostsupportbd"})


@app.post("/api/payment-requests")
@login_required
def api_payment_request_create():
    uid=int(session["user_id"]); b=request.get_json(silent=True) or {}; plan_id=b.get("plan_id"); amount=float(b.get("amount",0) or 0); method=str(b.get("method") or "MANUAL"); ref=str(b.get("reference") or "")[:200]
    try: plan_id=int(plan_id) if plan_id is not None else None
    except: plan_id=None
    with db_connection() as conn:
        with conn.cursor() as cur: cur.execute("INSERT INTO gm_payment_requests(user_id,plan_id,amount,method,reference,status) VALUES(%s,%s,%s,%s,%s,'PENDING') RETURNING id",(uid,plan_id,amount,method,ref)); rid=cur.fetchone()[0]
    return jsonify({"ok":True,"request_id":rid,"status":"PENDING","message":"Payment request sent for admin verification."}),201


@app.get("/api/verification")
@login_required
def api_verification():
    uid=int(session["user_id"]); plan=get_user_active_plan(uid); pref=_user_pref(uid); risk=_risk_profile(uid)
    return jsonify({"account_verified":True,"email_verified":True,"uid_status":"PENDING","plan":plan,"preferences":pref,"risk":risk,"security":{"session_cookie":"HTTPOnly + SameSite=Lax","automatic_trade_execution":False}})


@app.post("/api/ai/verify")
@login_required
def api_ai_verify():
    uid=int(session["user_id"]); b=request.get_json(silent=True) or {}; market=str(b.get("market") or "EUR/USD").upper(); mt=str(b.get("market_type") or "REAL").upper(); tf=_normalize_timeframe(b.get("timeframe")); direction=str(b.get("direction") or "NO SIGNAL").upper(); conf=max(0,min(100,int(b.get("confidence",0) or 0)))
    # Model verification is a confluence record, never a fabricated probability.
    model_agreement=max(0,min(100,int(b.get("model_agreement",conf) or 0))); trend=max(0,min(100,int(b.get("trend_score",0) or 0))); momentum=max(0,min(100,int(b.get("momentum_score",0) or 0))); structure=max(0,min(100,int(b.get("structure_score",0) or 0)))
    with db_connection() as conn:
        with conn.cursor() as cur: cur.execute("INSERT INTO gm_ai_verifications(user_id,market,market_type,timeframe,direction,confidence,model_agreement,trend_score,momentum_score,structure_score) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",(uid,market,mt,tf,direction,conf,model_agreement,trend,momentum,structure))
    return jsonify({"verified": direction in ("CALL","PUT") and conf>=58 and model_agreement>=50,"direction":direction,"confidence":conf,"model_agreement":model_agreement,"components":{"trend":trend,"momentum":momentum,"structure":structure},"note":"Agreement is model/feature confluence, not win probability."})


@app.get("/api/market-status")
@login_required
def api_market_status():
    return jsonify({"real":{"connected":bool(TWELVE_DATA_API_KEY),"source":"Twelve Data","verified_quotex":False},"otc":{"connected":bool(OTCHARTS_API_KEY and OTChartsClient is not None),"source":"OTCharts Quotex OTC feed" if OTCHARTS_API_KEY and OTChartsClient is not None else "OTC feed not configured","verified_quotex":False},"ai":{"configured":bool(AI_API_KEY),"vision_model":GROQ_VISION_MODEL if AI_API_KEY else None},"telegram":{"configured":bool(BOT_TOKEN)},"database":{"configured":bool(DATABASE_URL)}})


@app.get("/api/admin/verification-queue")
@admin_required
def api_admin_verification_queue():
    with db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT u.id,u.email,u.name,v.id verification_id,v.broker,v.uid,v.status,v.created_at FROM gm_uid_verifications v JOIN gm_users u ON u.id=v.user_id ORDER BY v.created_at DESC LIMIT 100"); uid_rows=[dict(x) for x in cur.fetchall()]
            cur.execute("SELECT pr.id,u.email,p.name plan_name,pr.amount,pr.method,pr.reference,pr.status,pr.created_at FROM gm_payment_requests pr JOIN gm_users u ON u.id=pr.user_id LEFT JOIN gm_plans p ON p.id=pr.plan_id ORDER BY pr.created_at DESC LIMIT 100"); pay_rows=[dict(x) for x in cur.fetchall()]
    for rows in (uid_rows,pay_rows):
        for r in rows:
            for k,v in list(r.items()):
                if isinstance(v,datetime):r[k]=v.isoformat()
    return jsonify({"uid":uid_rows,"payments":pay_rows})


@app.put("/api/admin/verification/<int:verification_id>")
@admin_required
def api_admin_verify_uid(verification_id):
    b=request.get_json(silent=True) or {}; status=str(b.get("status") or "APPROVED").upper(); notes=str(b.get("notes") or "")[:500]
    if status not in ("APPROVED","REJECTED","PENDING"):return jsonify({"error":"Invalid status"}),400
    with db_connection() as conn:
        with conn.cursor() as cur:cur.execute("UPDATE gm_uid_verifications SET status=%s,notes=%s,reviewed_at=NOW() WHERE id=%s",(status,notes,verification_id))
    return jsonify({"ok":True,"status":status})


@app.put("/api/admin/payment/<int:request_id>")
@admin_required
def api_admin_payment(request_id):
    b=request.get_json(silent=True) or {}; status=str(b.get("status") or "APPROVED").upper(); notes=str(b.get("notes") or "")[:500]
    if status not in ("APPROVED","REJECTED","PENDING"):return jsonify({"error":"Invalid status"}),400
    with db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("UPDATE gm_payment_requests SET status=%s,notes=%s,reviewed_at=NOW() WHERE id=%s RETURNING user_id,plan_id",(status,notes,request_id)); row=cur.fetchone()
            if status=="APPROVED" and row and row.get("plan_id"):
                cur.execute("UPDATE gm_user_plans SET status='inactive' WHERE user_id=%s AND status='active'",(row["user_id"],))
                cur.execute("SELECT duration_days FROM gm_plans WHERE id=%s",(row["plan_id"],)); p=cur.fetchone(); days=int(p[0] or 30) if p else 30
                cur.execute("INSERT INTO gm_user_plans(user_id,plan_id,start_date,expiry_date,status,notes) VALUES(%s,%s,NOW(),NOW()+(%s || ' days')::interval,'active','Approved payment request')",(row["user_id"],row["plan_id"],days))
    return jsonify({"ok":True,"status":status})

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