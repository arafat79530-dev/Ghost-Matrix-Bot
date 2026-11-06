"""GHOST AI - minimal complete project. Single file.
Backend + HTML + CSS + JS in one file.
No fake live prices, no fabricated OTC candles, no Quotex connection.
"""
import base64, json, os, re, secrets, threading, time
from datetime import datetime, timezone, timedelta
import requests
from flask import Flask, jsonify, request, session
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps

TWELVE_KEY = os.getenv("TWELVE_DATA_API_KEY", "").strip()
AI_KEY = os.getenv("AI_API_KEY", "").strip()
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile").strip()
GROQ_VISION = os.getenv("GROQ_VISION_MODEL", "qwen/qwen3.6-27b").strip()
DEMO_EMAIL = os.getenv("DEMO_EMAIL", "demo@ghost.ai").strip().lower()
DEMO_PASS = os.getenv("DEMO_PASSWORD", "ghost1234")
DEMO_NAME = os.getenv("DEMO_NAME", "Arafat Hossain")
APP_BASE = os.getenv("APP_BASE_URL", "").strip()

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "").strip() or secrets.token_urlsafe(48)
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                  SESSION_COOKIE_SECURE=APP_BASE.startswith("https://"),
                  PERMANENT_SESSION_LIFETIME=timedelta(hours=12))

http = requests.Session()
http.headers.update({"User-Agent": "GHOST-AI/1.0"})
TWELVE_BASE = "https://api.twelvedata.com"
GROQ_BASE = "https://api.groq.com/openai/v1/chat/completions"

REAL = ["AUD/USD","AUD/CAD","AUD/CHF","AUD/JPY","AUD/NZD","CAD/CHF","CAD/JPY","CHF/JPY",
        "EUR/AUD","EUR/CAD","EUR/CHF","EUR/GBP","EUR/JPY","EUR/NZD","EUR/USD",
        "GBP/AUD","GBP/CAD","GBP/CHF","GBP/JPY","GBP/NZD","GBP/USD",
        "NZD/CAD","NZD/CHF","NZD/JPY","USD/CAD","USD/CHF","USD/JPY"]
OTC = ["AUD/CAD OTC","AUD/CHF OTC","AUD/JPY OTC","AUD/NZD OTC","AUD/USD OTC","CAD/CHF OTC",
       "CAD/JPY OTC","CHF/JPY OTC","EUR/AUD OTC","EUR/CAD OTC","EUR/CHF OTC","EUR/GBP OTC",
       "EUR/JPY OTC","EUR/NZD OTC","EUR/USD OTC","GBP/AUD OTC","GBP/CAD OTC","GBP/CHF OTC",
       "GBP/JPY OTC","GBP/NZD OTC","GBP/USD OTC","NZD/CAD OTC","NZD/CHF OTC","NZD/JPY OTC",
       "USD/ARS OTC","USD/BDT OTC","USD/BRL OTC","USD/CAD OTC","USD/CHF OTC","USD/COP OTC",
       "USD/IDR OTC","USD/INR OTC","USD/JPY OTC","USD/MXN OTC","USD/PHP OTC","USD/PKR OTC","USD/ZAR OTC"]

_users = {}
if DEMO_EMAIL:
    _users[DEMO_EMAIL] = {"id": 1, "email": DEMO_EMAIL, "name": DEMO_NAME,
                          "password_hash": generate_password_hash(DEMO_PASS),
                          "is_admin": False, "plan": "FREE"}
_signals = []
_next_id = [1]
_buckets = {}
_lock = threading.Lock()
_last_scan = [0.0]

def login_required(fn):
    @wraps(fn)
    def w(*a, **k):
        if not session.get("user_id"):
            return jsonify({"error": "Login required."}), 401
        return fn(*a, **k)
    return w

def _allowed(bucket, limit, window=60):
    now = time.monotonic()
    key = f"{request.remote_addr or 'x'}:{bucket}"
    with _lock:
        lst = [s for s in _buckets.get(key, []) if now - s < window]
        if len(lst) >= limit:
            _buckets[key] = lst
            return False
        lst.append(now)
        _buckets[key] = lst
        return True

def get_catalog():
    c = [{"symbol": s, "group": "Real Market", "otc": False, "scannable": True} for s in REAL]
    c += [{"symbol": s, "group": "OTC Market", "otc": True, "scannable": False} for s in OTC]
    return c

def ema(vals, p):
    out = [None] * len(vals)
    if len(vals) < p:
        return out
    m = 2.0 / (p + 1)
    cur = sum(vals[:p]) / p
    out[p - 1] = cur
    for i in range(p, len(vals)):
        cur = (vals[i] - cur) * m + cur
        out[i] = cur
    return out

def rsi(vals, p=14):
    out = [None] * len(vals)
    if len(vals) <= p:
        return out
    gains, losses = [], []
    for i in range(1, p + 1):
        ch = vals[i] - vals[i - 1]
        gains.append(max(ch, 0))
        losses.append(max(-ch, 0))
    ag, al = sum(gains) / p, sum(losses) / p
    out[p] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    for i in range(p + 1, len(vals)):
        ch = vals[i] - vals[i - 1]
        ag = (ag * (p - 1) + max(ch, 0)) / p
        al = (al * (p - 1) + max(-ch, 0)) / p
        out[i] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    return out

def analyze_candles(symbol, candles):
    if len(candles) < 60:
        return {"symbol": symbol, "direction": "NO TRADE",
                "reason": f"Only {len(candles)} candles; at least 60 needed.", "candle_count": len(candles)}
    closes = [c["close"] for c in candles]
    f, s, t, rs = ema(closes, 9), ema(closes, 21), ema(closes, 50), rsi(closes, 14)
    idx = len(candles) - 1
    latest, prev = candles[idx], candles[idx - 1]
    age = (datetime.now(timezone.utc) - latest["time"]).total_seconds()
    if age < -120 or age > 180:
        return {"symbol": symbol, "direction": "NO TRADE",
                "reason": f"Latest M1 candle is not fresh ({int(age)}s).", "candle": latest}
    if any(x[idx] is None for x in (f, s, t, rs)):
        return {"symbol": symbol, "direction": "NO TRADE", "reason": "Indicator data incomplete.", "candle": latest}
    recent = candles[max(0, idx - 20):idx]
    sup = min(c["low"] for c in recent)
    res = max(c["high"] for c in recent)
    close, op, hi, lo = latest["close"], latest["open"], latest["high"], latest["low"]
    rng = hi - lo
    if close <= 0 or rng <= 0:
        return {"symbol": symbol, "direction": "NO TRADE", "reason": "Candle range / price is not valid.", "candle": latest}
    body = abs(close - op) / rng
    bull = close > op and close > prev["close"]
    bear = close < op and close < prev["close"]
    if body < 0.25:
        d, reason = "NO TRADE", "Candle body is weak; no clear momentum."
    elif f[idx] > s[idx] > t[idx] and 52 <= rs[idx] <= 68 and bull and close < res and (res - close) > rng * 0.15:
        d = "CALL"
        reason = f"Trend bullish: EMA9 > EMA21 > EMA50; RSI {rs[idx]:.1f}; bullish momentum, not pressed against resistance. Possible setup - not a guaranteed outcome."
    elif f[idx] < s[idx] < t[idx] and 32 <= rs[idx] <= 48 and bear and close > sup and (close - sup) > rng * 0.15:
        d = "PUT"
        reason = f"Trend bearish: EMA9 < EMA21 < EMA50; RSI {rs[idx]:.1f}; bearish momentum, not pressed against support. Possible setup - not a guaranteed outcome."
    else:
        d = "NO TRADE"
        reason = f"Not all filters agree. EMA9={f[idx]:.5f}, EMA21={s[idx]:.5f}, EMA50={t[idx]:.5f}, RSI={rs[idx]:.1f}; waiting is better."
    return {"symbol": symbol, "direction": d, "reason": reason, "price": close, "candle": latest,
            "candle_count": len(candles), "rsi": rs[idx], "ema9": f[idx], "ema21": s[idx],
            "ema50": t[idx], "support": sup, "resistance": res, "verified_quotex": False}

def backtest(candles):
    if len(candles) < 60:
        return {"trades": 0, "wins": 0, "losses": 0, "note": "Not enough candles for backtest."}
    closes = [c["close"] for c in candles]
    f, s = ema(closes, 9), ema(closes, 21)
    w = l = tr = 0
    for i in range(22, len(candles) - 1):
        if None in (f[i-1], s[i-1], f[i], s[i]):
            continue
        call = f[i-1] <= s[i-1] and f[i] > s[i]
        put = f[i-1] >= s[i-1] and f[i] < s[i]
        if not (call or put):
            continue
        tr += 1
        if (call and closes[i+1] > closes[i]) or (put and closes[i+1] < closes[i]):
            w += 1
        else:
            l += 1
    return {"trades": tr, "wins": w, "losses": l, "note": "One-candle historical check only; not a prediction."}

def scan_one(symbol):
    if not TWELVE_KEY:
        raise RuntimeError("TWELVE_DATA_API_KEY is not configured")
    r = http.get(f"{TWELVE_BASE}/time_series",
                 params={"symbol": symbol, "interval": "1min", "outputsize": 5000,
                         "order": "desc", "format": "JSON", "timezone": "UTC", "apikey": TWELVE_KEY}, timeout=25)
    r.raise_for_status()
    data = r.json()
    if isinstance(data, dict) and data.get("status") == "error":
        raise RuntimeError(str(data.get("message", "Twelve Data error"))[:240])
    candles = []
    for it in (data.get("values") or []):
        try:
            stamp = datetime.strptime(it["datetime"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            candles.append({"time": stamp, "open": float(it["open"]), "high": float(it["high"]),
                            "low": float(it["low"]), "close": float(it["close"])})
        except Exception:
            continue
    candles.sort(key=lambda c: c["time"])
    res = analyze_candles(symbol, candles)
    res["backtest"] = backtest(candles)
    res["source"] = "Twelve Data external feed"
    res["verified_quotex"] = False
    if res.get("direction") in ("CALL", "PUT") and res.get("candle"):
        with _lock:
            sid = _next_id[0]; _next_id[0] += 1
            _signals.append({"id": sid, "symbol": symbol, "direction": res["direction"],
                             "candle_time": res["candle"]["time"], "entry_price": res["price"],
                             "reason": res["reason"], "source": "Twelve Data external feed",
                             "verified_quotex": False, "status": "PENDING"})
            _signals[:] = _signals[-100:]
        res["signal_id"] = sid
    return res

def ai_answer(prompt):
    if not AI_KEY:
        return "AI_API_KEY is not set. Use the lessons and screenshot checklist instead."
    try:
        r = http.post(GROQ_BASE, headers={"Authorization": f"Bearer {AI_KEY}", "Content-Type": "application/json"},
                      json={"model": GROQ_MODEL,
                            "messages": [{"role": "system", "content": "Answer clearly. Never invent current news, live prices, trade outcomes, or guaranteed trading advice."},
                                         {"role": "user", "content": prompt}],
                            "temperature": 0.2, "max_tokens": 700}, timeout=30)
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"].strip()
    except Exception:
        return "AI service is unavailable right now. Try again later."

def bd_time(dt):
    if dt is None:
        return "-"
    try:
        if isinstance(dt, str):
            dt = datetime.fromisoformat(dt)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        from zoneinfo import ZoneInfo
        return dt.astimezone(ZoneInfo("Asia/Dhaka")).strftime("%d %b %Y, %I:%M %p")
    except Exception:
        return str(dt)

# ---------------- Pages ----------------
@app.get("/")
@app.get("/app")
@app.get("/app/")
def home():
    return INDEX_HTML

@app.get("/health")
def health():
    return jsonify({"status": "running", "twelve_data_key_configured": bool(TWELVE_KEY),
                    "ai_api_key_configured": bool(AI_KEY), "catalog_count": len(get_catalog()),
                    "quotex_verified": False}), 200

# ---------------- Auth ----------------
@app.post("/api/auth/login")
def auth_login():
    b = request.get_json(silent=True) or {}
    email = str(b.get("email", "")).strip().lower()
    pw = str(b.get("password", ""))
    u = _users.get(email)
    if not u or not check_password_hash(u["password_hash"], pw):
        return jsonify({"error": "Email or password is incorrect."}), 401
    session.clear(); session.permanent = True; session["user_id"] = int(u["id"])
    return jsonify({"ok": True, "user": {"email": u["email"], "name": u["name"], "is_admin": False, "plan": u["plan"]}})

@app.get("/api/auth/me")
def auth_me():
    uid = session.get("user_id")
    if not uid:
        return jsonify({"authenticated": False}), 200
    for u in _users.values():
        if int(u["id"]) == int(uid):
            return jsonify({"authenticated": True, "user": {"email": u["email"], "name": u["name"], "is_admin": False, "plan": u["plan"]}})
    session.clear()
    return jsonify({"authenticated": False}), 200

@app.post("/api/auth/logout")
def auth_logout():
    session.clear()
    return jsonify({"ok": True, "authenticated": False})

# ---------------- API ----------------
@app.get("/api/dashboard")
@login_required
def api_dashboard():
    w = sum(1 for x in _signals if x.get("status") == "WIN")
    l = sum(1 for x in _signals if x.get("status") == "LOSS")
    return jsonify({"app": "GHOST AI", "status": "running", "data_source": "Twelve Data external feed",
                    "quotex_verified": False, "twelve_data_key_configured": bool(TWELVE_KEY),
                    "ai_api_key_configured": bool(AI_KEY), "catalog_count": len(get_catalog()),
                    "signals": list(reversed(_signals[-20:])),
                    "stats": {"wins": w, "losses": l, "total": w + l}}), 200

@app.get("/api/markets")
@login_required
def api_markets():
    return jsonify({"markets": get_catalog(), "source": "configured allowlist", "quotex_verified": False})

@app.post("/api/scan")
@login_required
def api_scan():
    if not _allowed("scan", 4, 60):
        return jsonify({"error": "Scan limit reached. Try again in a minute."}), 429
    b = request.get_json(silent=True) or {}
    symbol = str(b.get("symbol", "")).strip().upper()
    by = {x["symbol"]: x for x in get_catalog()}
    if not symbol or symbol not in by:
        return jsonify({"error": "Choose a valid market from the list."}), 400
    if by[symbol].get("otc"):
        return jsonify({"error": "OTC is in the catalog, but no live OTC candle feed is configured. Use Screenshot Analyzer for OTC; no OTC price / signal is fabricated."}), 503
    with _lock:
        now = time.monotonic()
        if now - _last_scan[0] < 8:
            return jsonify({"error": "Wait 8 seconds between scans to respect the API limit."}), 429
        _last_scan[0] = now
    if not TWELVE_KEY:
        return jsonify({"error": "TWELVE_DATA_API_KEY is not set in Render Environment."}), 503
    try:
        res = scan_one(symbol)
        candle = res.get("candle") or {}
        return jsonify({"result": {k: v for k, v in res.items() if k not in ("candle", "backtest")},
                        "candle_time": bd_time(candle.get("time")),
                        "backtest": res.get("backtest", {}),
                        "source": "Twelve Data external feed", "quotex_verified": False})
    except Exception as exc:
        return jsonify({"error": f"Market data unavailable: {str(exc)[:180]}"}), 502

@app.post("/api/ask")
@login_required
def api_ask():
    if not _allowed("ask", 8, 60):
        return jsonify({"error": "Too many requests. Try again in a minute."}), 429
    b = request.get_json(silent=True) or {}
    q = str(b.get("question", "")).strip()
    if not q:
        return jsonify({"error": "Write a question."}), 400
    return jsonify({"answer": ai_answer(q), "provider": "configured AI service" if AI_KEY else "AI not configured"})

@app.post("/api/analyze-image")
@login_required
def api_image():
    if not _allowed("vision", 4, 60):
        return jsonify({"error": "Max 4 screenshot analyses per minute."}), 429
    up = request.files.get("image")
    if up is None or not up.filename:
        return jsonify({"error": "Upload a chart screenshot."}), 400
    if not AI_KEY:
        return jsonify({"error": "AI_API_KEY is not set in Render Environment; screenshot analysis is off."}), 503
    raw = up.read(12 * 1024 * 1024 + 1)
    if not raw or len(raw) > 12 * 1024 * 1024:
        return jsonify({"error": "Image is empty or over 12 MB."}), 413
    try:
        mime = up.mimetype if up.mimetype in ("image/jpeg", "image/png", "image/webp") else "image/jpeg"
        enc = base64.b64encode(raw).decode("ascii")
        sys_p = ("You are a careful technical-analysis assistant reading a trading-chart screenshot. "
                 "Only report evidence actually visible in the image; never invent asset, timeframe, price, indicator values, candles, or levels. "
                 "Do not promise profit, do not say the broker verified the chart, and do not imply confidence is win probability. "
                 "If evidence is mixed, cropped/blurry, or context is missing, choose NO TRADE. "
                 "Return ONLY valid JSON with keys: direction (CALL, PUT, or NO TRADE), confidence (0-100, clarity of evidence, not win probability), "
                 "summary, trend, structure, zones, reasoning, confirmation, invalidation, confidence_note.")
        payload = {"model": GROQ_VISION,
                   "messages": [{"role": "system", "content": sys_p},
                                {"role": "user", "content": [{"type": "text", "text": "Analyse this chart screenshot. If asset/timeframe is visible, name it; if not, do not guess. JSON only."},
                                                             {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{enc}"}}]}],
                   "temperature": 0.1, "max_completion_tokens": 1100}
        r = http.post(GROQ_BASE, headers={"Authorization": f"Bearer {AI_KEY}", "Content-Type": "application/json"}, json=payload, timeout=50)
        r.raise_for_status()
        content = r.json()["choices"][0]["message"]["content"].strip()
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", content, flags=re.I).strip()
        try:
            a = json.loads(cleaned)
            if not isinstance(a, dict):
                raise ValueError("not object")
            d = str(a.get("direction", "NO TRADE")).upper()
            a["direction"] = d if d in ("CALL", "PUT", "NO TRADE") else "NO TRADE"
            try:
                a["confidence"] = max(0, min(100, int(float(a.get("confidence", 0)))))
            except Exception:
                a["confidence"] = 0
            for k in ("summary", "trend", "structure", "zones", "reasoning", "confirmation", "invalidation", "confidence_note"):
                if not isinstance(a.get(k), str):
                    a[k] = "Not clearly visible in the image."
        except Exception:
            a = {"direction": "NO TRADE", "confidence": 0, "summary": content, "trend": "Structured result unavailable",
                 "structure": "Structured result unavailable", "zones": "Structured result unavailable", "reasoning": content,
                 "confirmation": "Do not enter without clear price confirmation.", "invalidation": "Setup is not confirmed.",
                 "confidence_note": "Confidence could not be reliably extracted; it is not a win probability."}
        return jsonify({"analysis": a, "warning": "Screenshot-based educational analysis only; confidence is not a win probability and this is not a Quotex-verified signal."})
    except Exception:
        return jsonify({"error": "Could not analyse the image. Check image format, AI_API_KEY, vision model and provider limits."}), 502

@app.get("/api/news")
@login_required
def api_news():
    if not _allowed("news", 10, 60):
        return jsonify({"error": "Try again shortly."}), 429
    try:
        days = max(1, min(14, int(request.args.get("days", "7"))))
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("Asia/Dhaka")
        today = datetime.now(tz).date()
        end = today + timedelta(days=days)
        r = http.get("https://www.financecalendar.com/api/calendar",
                     params={"from": today.isoformat(), "to": end.isoformat(), "impact": "high", "limit": 100}, timeout=20)
        r.raise_for_status()
        payload = r.json()
        events = payload.get("events", []) if isinstance(payload, dict) else payload
        if not isinstance(events, list):
            raise RuntimeError("Calendar format unexpected")
        clean = []
        alias = {"us": "USD", "usa": "USD", "gb": "GBP", "uk": "GBP", "eu": "EUR", "au": "AUD", "ca": "CAD", "jp": "JPY", "ch": "CHF", "nz": "NZD"}
        for ev in events[:50]:
            if not isinstance(ev, dict):
                continue
            cur = str(ev.get("currency") or ev.get("country_code") or ev.get("country") or "-")
            if cur.lower() in alias:
                cur = alias[cur.lower()]
            dt_raw = ev.get("datetime") or ev.get("date") or ev.get("time")
            dt = None
            try:
                if dt_raw:
                    s = str(dt_raw).replace("Z", "+00:00")
                    dt = datetime.fromisoformat(s)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    dt = dt.astimezone(tz)
            except Exception:
                dt = None
            clean.append({"datetime": dt.isoformat() if dt else None,
                          "datetime_bd": dt.strftime("%d %b %Y, %I:%M %p") if dt else "Time not verified",
                          "name": str(ev.get("title") or ev.get("name") or ev.get("event") or "Economic event"),
                          "currency": cur, "impact": "high",
                          "forecast": ev.get("consensus", ev.get("forecast")),
                          "previous": ev.get("prior", ev.get("previous")), "actual": ev.get("actual"), "url": ev.get("url")})
        return jsonify({"events": clean, "days": days, "source": "https://www.financecalendar.com/api/", "timezone": "Asia/Dhaka"})
    except Exception:
        return jsonify({"error": "Live economic calendar is unavailable right now. Try again later.",
                        "source": "https://www.financecalendar.com/api/"}), 502

@app.get("/api/lessons")
@login_required
def api_lessons():
    lessons = [
        {"key": "support", "title": "1/7 - Support & Resistance", "caption": "Support = price repeatedly paused / bounced. Resistance = price repeatedly stalled / fell. Treat them as zones, not thin lines. Wait for rejection + structure confirmation - a touch alone is not an entry."},
        {"key": "trend", "title": "2/7 - Trend & Market Structure", "caption": "Uptrend: Higher High + Higher Low. Downtrend: Lower Low + Lower High. Sideways: price rotates in a range. If structure is unclear: NO TRADE."},
        {"key": "breakout", "title": "3/7 - Breakout & Retest", "caption": "Breakout = price closes firmly outside a range. Retest = price returns to the broken level. A wick outside is not enough - need candle close + momentum + retest reaction."},
        {"key": "pinbar", "title": "4/7 - Pin Bar", "caption": "Pin bar: small body, one long wick. Long lower wick near support = rejection from below. Long upper wick near resistance = rejection from above."},
        {"key": "engulfing", "title": "5/7 - Engulfing", "caption": "Bullish engulfing: larger bullish body covers prior bearish body. Bearish engulfing: reverse. It hints at momentum shift, not certainty."},
        {"key": "sweep", "title": "6/7 - Liquidity Sweep", "caption": "Price wicks briefly beyond a prior swing high / low, then closes back inside. Confirmation: close back inside + rejection + structure shift."},
        {"key": "checklist", "title": "7/7 - Entry Checklist", "caption": "Checklist: 1) Trend / structure 2) Key zone 3) Candle close 4) Confirmation. If any step is missing, wait. Risk only what you can afford to lose."},
    ]
    return jsonify({"lessons": lessons, "total": len(lessons), "note": "Illustrative examples, not live market data."})

@app.post("/api/result")
@login_required
def api_result():
    b = request.get_json(silent=True) or {}
    try:
        sid = int(b.get("id"))
    except Exception:
        return jsonify({"error": "Signal ID is not valid."}), 400
    out = str(b.get("result", "")).upper()
    if out not in ("WIN", "LOSS"):
        return jsonify({"error": "Result must be WIN or LOSS."}), 400
    for s in _signals:
        if int(s["id"]) == sid and s.get("status") == "PENDING":
            s["status"] = out
            return jsonify({"ok": True, "signal": s, "note": "Manually recorded result; not automatically verified against Quotex."})
    return jsonify({"error": "No pending signal with this ID."}), 404

@app.post("/api/news/analyze")
@login_required
def api_news_analyze():
    if not _allowed("news_analyze", 8, 60):
        return jsonify({"error": "Max 8 news analyses per minute."}), 429
    b = request.get_json(silent=True) or {}
    cur, ev, act, fore, prev = str(b.get("currency", "")).strip().upper(), str(b.get("event", "")).strip(), str(b.get("actual", "")).strip(), str(b.get("forecast", "")).strip(), str(b.get("previous", "")).strip()
    if not cur or not ev or not act or not fore:
        return jsonify({"error": "Fill currency, event, actual and forecast."}), 400
    prompt = f"Economic news explanation. Currency: {cur}; event: {ev}; actual: {act}; forecast: {fore}; previous: {prev or 'not supplied'}. Explain actual-vs-forecast carefully, likely first-order currency interpretation, what could reverse the reaction, and uncertainty. Do not claim current market reaction without live price data."
    return jsonify({"analysis": ai_answer(prompt), "note": "Economic interpretation only; current price reaction is not verified."})

# ---------------- Frontend (all screens, inline) ----------------
INDEX_HTML = r'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"><meta name="theme-color" content="#160b3c"><title>GHOST AI — Market Console</title><style>
:root{--bg:#14083a;--panel:rgba(54,24,118,.72);--line:rgba(200,160,255,.20);--text:#f8f3ff;--muted:#b8a6dd}
*{box-sizing:border-box}html,body{margin:0;min-height:100%;background:var(--bg);color:var(--text);font-family:Inter,system-ui,Segoe UI,Roboto,Arial,sans-serif}
body{background:radial-gradient(ellipse at 50% -10%,#6a36c9 0,transparent 44%),linear-gradient(160deg,#14083a,#1a0b48 58%,#0d0626);overflow-x:hidden}
button,input,select{font:inherit}button{cursor:pointer}button:disabled{opacity:.45}
#stars{position:fixed;inset:0;z-index:0;pointer-events:none;opacity:.32}.app{position:relative;z-index:1;min-height:100vh}
.top{height:64px;position:sticky;top:0;z-index:40;display:flex;align-items:center;justify-content:space-between;padding:0 14px;background:rgba(20,8,58,.88);border-bottom:1px solid var(--line)}
.brand{display:flex;align-items:center;gap:10px;cursor:pointer}.brandMark{width:38px;height:38px;border-radius:13px;display:grid;place-items:center;background:linear-gradient(145deg,#e4a8ff,#7443e8 55%,#2b176a);font-size:22px}.brand b{font-size:13px;letter-spacing:1.6px}.brand small{display:block;color:#bca8ed;font-size:9px;letter-spacing:1.7px}
.topRight{display:flex;gap:8px}.brokerBtn{font-size:10px;padding:8px 11px;border:1px solid var(--line);border-radius:12px;background:#ffffff0b;color:#e8dcff}.iconBtn{width:37px;height:37px;border:1px solid var(--line);border-radius:12px;color:#fff;background:#ffffff0b;font-size:16px}
.page{max-width:980px;margin:auto;padding:16px 14px 118px}.view{display:none}.view.active{display:block}
.eyebrow{font-size:9px;letter-spacing:2.2px;color:#d8b7ff}.titleRow{display:flex;justify-content:space-between;align-items:center;margin:7px 2px 14px;gap:10px}.titleRow h1{font-size:21px;margin:0}.titleRow p{font-size:10px;color:var(--muted);margin:4px 0 0;line-height:1.5}.pill{font-size:9px;padding:7px 10px;border-radius:20px;border:1px solid var(--line);background:#ffffff0a;white-space:nowrap}
.panel{border:1px solid var(--line);border-radius:21px;background:linear-gradient(145deg,rgba(77,43,155,.55),rgba(36,18,87,.68) 60%,rgba(26,16,69,.8));box-shadow:0 18px 50px rgba(4,1,20,.55)}
.btn{border:1px solid #d0a0ff66;border-radius:13px;padding:12px 16px;color:#fff;font-size:10px;font-weight:800;background:linear-gradient(135deg,#a55bff,#6936d3 60%,#4a2aa5)}.btn.secondary{background:#ffffff0b;border-color:var(--line)}.btn.full{width:100%}.btn.small{padding:8px 11px}
.sectionHead{display:flex;justify-content:space-between;align-items:center;margin:20px 3px 10px}.sectionHead h2{font-size:13px;margin:0}.sectionHead span{font-size:9px;color:var(--muted)}
.muted{color:var(--muted);font-size:10px;line-height:1.6}.notice{padding:11px 12px;border:1px solid #ffd98c33;background:#ffd98c0d;border-radius:13px;color:#e9d6ae;font-size:9px;line-height:1.55;margin:10px 0}
.field{width:100%;border:1px solid #c7a7ff35;border-radius:13px;padding:12px;background:#170d3e;color:#fff;outline:none;font-size:11px}select{width:100%;border:1px solid #c7a7ff35;border-radius:13px;padding:12px;background:#170d3e;color:#fff;font-size:11px}
.tabs{display:flex;gap:7px;padding:3px;margin:12px 0}.tab{flex:1;white-space:nowrap;border:1px solid var(--line);border-radius:12px;padding:11px 12px;color:#c8b7e8;background:#ffffff08;font-size:10px;font-weight:700}.tab.active{color:#fff;background:linear-gradient(120deg,#8a4be955,#5729a477)}
.pairGrid{display:grid;grid-template-columns:repeat(2,1fr);gap:9px}.pairCard{display:flex;align-items:center;gap:9px;padding:12px 10px;border:1px solid var(--line);border-radius:15px;background:#ffffff09;text-align:left;color:#fff;min-width:0}.pairCard.selected{border-color:#d5a0ff;background:#7d43c844}.flag{width:32px;height:32px;border-radius:50%;display:grid;place-items:center;background:#ffffff14;font-size:15px;flex:none}.pairName{font-size:10px;font-weight:800;white-space:nowrap}.pairType{font-size:8px;color:var(--muted);margin-top:3px}.pairCheck{margin-left:auto;color:#b99aff}
.listRow{display:flex;justify-content:space-between;align-items:center;padding:13px 10px;border-bottom:1px solid #ffffff13;background:transparent;border-left:0;border-right:0;border-top:0;color:#fff;width:100%;text-align:left}.listRow b{font-size:11px}.listRow small{display:block;font-size:9px;color:var(--muted);margin-top:4px}.badge{font-size:8px;border:1px solid var(--line);border-radius:8px;padding:5px 7px;color:#d8c7f4;white-space:nowrap}
.heroLanding{border-radius:26px;padding:26px 20px;overflow:hidden;background:radial-gradient(ellipse at 50% 20%,#8a4be955,transparent 55%),linear-gradient(145deg,#4a248c,#1a0e48 62%,#140a36)}.heroLanding h1{font-size:32px;line-height:1.05;margin:10px 0}.heroLanding p{color:#d0c1ed;font-size:11px;line-height:1.7}
.orbitWrap{position:relative;height:180px;display:grid;place-items:center;margin:8px 0}.orbit{position:absolute;border:1px solid #d9b5ff26;border-radius:50%;animation:orbit 22s linear infinite}.orbit.o1{width:160px;height:160px}.orbit.o2{width:220px;height:220px;border-style:dashed;animation-duration:32s;animation-direction:reverse}.orbitNode{position:absolute;width:34px;height:34px;border-radius:50%;background:#2a1160;border:1px solid #d7a7ff66;display:grid;place-items:center;font-size:16px}.centerOrb{width:78px;height:78px;border-radius:50%;display:grid;place-items:center;background:radial-gradient(circle,#e9c6ff,#7b3fe8 55%,#2b176a);font-size:34px;z-index:2}
.planGrid{display:grid;grid-template-columns:1fr 1fr;gap:10px}.planCard{padding:16px}.planCard ul{padding-left:16px;color:#d5c8ed;font-size:10px;line-height:1.7}
.hero{min-height:340px;position:relative;overflow:hidden;display:flex;align-items:center;justify-content:center;text-align:center;padding:22px 18px 84px;background:radial-gradient(ellipse at 50% 52%,#9b5cff55,transparent 40%),linear-gradient(145deg,#4a248c,#1a0e48 62%,#140a36);border-radius:25px}.ghostBig{font-size:150px;filter:drop-shadow(0 0 24px #b57cff88);animation:float 4s ease-in-out infinite}.heroCopy{position:absolute;bottom:16px;left:12px;right:12px}.heroCopy h1{font-size:20px;margin:0 0 5px}.heroCopy p{font-size:10px;color:#d0c1ed;margin:0 0 12px}
.userCard{display:flex;align-items:center;gap:12px;padding:14px}.avatar{width:48px;height:48px;border-radius:50%;display:grid;place-items:center;background:linear-gradient(145deg,#e4a8ff,#7443e8);font-size:24px;flex:none}
.toolGrid{display:grid;grid-template-columns:repeat(2,1fr);gap:10px}.tool{padding:16px;border:1px solid var(--line);border-radius:17px;background:#ffffff09;color:#fff;text-align:left}.tool b{display:block;font-size:11px;margin-top:10px}.tool small{display:block;color:var(--muted);font-size:9px;margin-top:4px}.tool .toolIcon{font-size:24px}
.drop{border:1px dashed #caa0ff77;border-radius:18px;padding:22px;text-align:center;background:#ffffff08}.dropIcon{font-size:32px;display:block;margin-bottom:8px}
.result{padding:15px;margin-top:12px}.resultHead{display:flex;justify-content:space-between;gap:10px}.resultDir{font-size:24px;font-weight:900}.resultLine{padding:10px 0;border-bottom:1px solid #ffffff13;color:#d5c8ed;font-size:10px;line-height:1.55}.resultLine b{color:#fff}.bar{height:6px;background:#ffffff14;border-radius:9px;margin:10px 0;overflow:hidden}.bar i{display:block;height:100%;background:linear-gradient(90deg,#d47bff,#78e9ff)}
.chat{height:340px;overflow:auto;padding:8px}.bubble{max-width:88%;padding:11px 13px;border-radius:15px;margin:8px 0;font-size:10px;line-height:1.6;background:#ffffff0b;border:1px solid var(--line);white-space:pre-wrap}.bubble.me{margin-left:auto;background:#6034a977}.composer{display:flex;gap:7px}.composer input{flex:1}
.marketRow{display:flex;justify-content:space-between;align-items:center;padding:12px 3px;border-bottom:1px solid #ffffff13;gap:10px}.marketRow b{font-size:11px}.marketRow small{display:block;font-size:9px;color:var(--muted);margin-top:4px}
.toggle{display:flex;justify-content:space-between;align-items:center;padding:12px 4px;border-bottom:1px solid #ffffff13}.switch{width:52px;height:28px;border-radius:20px;background:#ffffff22;position:relative;flex:none;cursor:pointer;border:1px solid var(--line)}.switch:after{content:"";position:absolute;top:3px;left:3px;width:20px;height:20px;border-radius:50%;background:#fff;transition:.2s}.switch.on{background:#a55bff}.switch.on:after{left:27px}
.sheetTable{width:100%;border-collapse:collapse;font-size:10px}.sheetTable th,.sheetTable td{padding:9px 6px;border-bottom:1px solid #ffffff13;text-align:left}.sheetTable th{color:#d8b7ff;font-size:9px}
.lockCard{text-align:center;padding:26px 16px}.lockIcon{font-size:44px}
.bottom{position:fixed;left:0;right:0;bottom:0;z-index:50;display:grid;grid-template-columns:repeat(5,1fr);gap:3px;padding:8px;background:rgba(22,11,60,.96);border-top:1px solid var(--line)}
.nav{border:0;background:none;color:#a18cce;font-size:8px;padding:6px 2px}.nav i{display:block;font-style:normal;font-size:19px;margin-bottom:3px}.nav.active{color:#fff}.nav.center i{width:52px;height:52px;margin:-26px auto 2px;border-radius:50%;display:grid;place-items:center;background:linear-gradient(145deg,#e4a8ff,#7443e8);color:#fff;font-size:24px;box-shadow:0 0 24px #a55bff88;border:2px solid #fff2}
.sheet{position:fixed;inset:0;z-index:80;background:rgba(8,3,25,.72);display:none;align-items:flex-end;justify-content:center}.sheet.open{display:flex}.sheetBox{width:min(560px,100%);max-height:88vh;overflow:auto;background:linear-gradient(180deg,#3a1a7a,#170c3c);border:1px solid #c58aff55;border-radius:26px 26px 0 0;padding:18px 16px}
.login{position:fixed;inset:0;z-index:100;display:none;place-items:center;padding:16px;background:rgba(13,6,38,.94)}.login.open{display:grid}.loginBox{width:min(400px,100%);padding:24px;border:1px solid #c58aff55;border-radius:24px;background:linear-gradient(145deg,#48218e,#170c3c)}.loginBox h2{font-size:20px;margin:0}.loginBox p{font-size:10px;color:#c9b8e9;line-height:1.6}.loginBox label{display:block;font-size:9px;color:#d6c6f2;margin:13px 0 6px}.error{min-height:18px;color:#ff9eb3;font-size:10px;margin:8px 0}
.toast{position:fixed;z-index:130;left:50%;bottom:96px;transform:translate(-50%,20px);opacity:0;pointer-events:none;background:#291553;border:1px solid #c28aff66;border-radius:12px;padding:10px 14px;font-size:10px;transition:.25s;max-width:92vw;text-align:center}.toast.show{opacity:1;transform:translate(-50%,0)}
.hidden{display:none!important}.spinner{width:34px;height:34px;border-radius:50%;border:3px solid #ffffff22;border-top-color:#d98aff;animation:orbit 1s linear infinite;margin:14px auto}.spark{width:64px;height:22px;flex:none;opacity:.85}
@keyframes orbit{to{transform:rotate(360deg)}}@keyframes float{50%{transform:translateY(-8px)}}
@media(max-width:560px){.planGrid{grid-template-columns:1fr}.heroLanding h1{font-size:28px}}
</style></head><body><canvas id="stars"></canvas><div class="app">
<header class="top"><div class="brand" onclick="showView(state.user?'dashboard':'landing')"><div class="brandMark">👻</div><div><b>GHOST AI</b><small>MARKET CONSOLE</small></div></div><div class="topRight"><button class="brokerBtn" onclick="openBroker()">Broker <b class="brokerLabel">Quotex</b> · Change</button><button class="iconBtn" onclick="openMore()">☰</button></div></header>
<main class="page">
<section id="view-landing" class="view active"><div class="panel heroLanding"><div class="eyebrow">AI POWERED MARKET INTELLIGENCE</div><h1>The market never sleeps.<br>Neither does Ghost AI.</h1><p>One console for market selection, screenshot analysis, external-feed Real checks, future-setup planning, news calendar and session planning. No fabricated live prices. No Quotex connection. No guaranteed outcomes.</p><div style="display:flex;gap:8px;flex-wrap:wrap"><button class="btn" onclick="launchApp()">LAUNCH GHOST AI →</button><button class="btn secondary" onclick="showView('pricing')">SEE ACCESS PLANS</button></div><div class="orbitWrap"><div class="orbit o1"></div><div class="orbit o2"></div><span class="orbitNode" style="left:12%;top:18%">📈</span><span class="orbitNode" style="right:12%;top:22%">📰</span><span class="orbitNode" style="left:8%;bottom:18%">🧠</span><span class="orbitNode" style="right:8%;bottom:22%">📋</span><div class="centerOrb">👻</div></div><div class="muted" style="text-align:center">GHOST AI · ANALYZER · LIVE · FUTURE · NEWS · MANAGEMENT</div></div>
<div class="sectionHead"><h2>Access paths</h2><span>Choose how you enter</span></div><div class="planGrid"><div class="panel planCard"><span class="pill">FREE</span><h3>Free — $0</h3><p class="muted">Browse console, pair catalog, lessons and planning tools.</p><ul><li>Pair catalog (Real + OTC)</li><li>Management planner</li><li>Lessons</li></ul><button class="btn secondary full" onclick="launchApp()">START FREE</button></div><div class="panel planCard"><span class="pill">DIRECT</span><h3>Direct — $99 / month</h3><p class="muted">Full engine when admin configures keys. No payment collected here — informational only.</p><ul><li>All Free tools</li><li>Screenshot Analyzer (AI key set)</li><li>Real Market external-feed checks</li></ul><button class="btn full" onclick="showView('pricing')">SEE ACCESS PLANS</button></div></div>
<div class="sectionHead"><h2>What it does — and what it does not</h2><span>Honest scope</span></div><div class="panel" style="padding:16px"><p class="muted"><b style="color:#fff">Does:</b> list pairs, read a screenshot you upload, run a Real-market external-feed check when keys are set, show high-impact calendar, build session stake sheet from your numbers.<br><br><b style="color:#fff">Does not:</b> show live broker prices, invent OTC candles, claim Quotex verification, auto-send signals, or promise win rate / profit.</p></div>
<div class="notice">Risk warning: binary options can lose the full stake on a trade. This console is an educational analysis aid, not financial advice, and it is not connected to any broker.</div></section>

<section id="view-dashboard" class="view"><div class="panel userCard"><div class="avatar">👻</div><div style="flex:1;min-width:0"><b id="dashName">Arafat Hossain</b><div class="muted" id="dashEmail">demo@ghost.ai</div></div><span class="pill" id="dashPlan">FREE</span><button class="iconBtn" onclick="openMore()">›</button></div>
<div class="panel hero" style="margin-top:12px"><div class="ghostBig">👻</div><div class="heroCopy"><h1>GHOST AI</h1><p>Your AI desk — pick a market, analyze, then plan.</p><button class="btn" onclick="showView('live')">OPEN TOOLS →</button><div class="muted" style="margin-top:10px">6 AI DESKS · ONE CONSOLE</div></div></div>
<div class="sectionHead"><h2>Your plan and daily engine quota</h2><span>Live status from backend</span></div><div id="dashStats" class="toolGrid"></div>
<div class="sectionHead"><h2>Recorded setups</h2><span>Only what the backend actually scanned</span></div><div class="panel" style="padding:14px"><div id="dashFeed"><div class="muted">Loading…</div></div></div>
<div class="sectionHead"><h2>Quick tools</h2></div><div class="toolGrid"><button class="tool" onclick="showView('live')"><span class="toolIcon">📡</span><b>Live</b><small>Browse pairs</small></button><button class="tool" onclick="showView('analyzer')"><span class="toolIcon">🔍</span><b>Analyzer</b><small>Screenshot / Real scan</small></button><button class="tool" onclick="showView('injector')"><span class="toolIcon">💉</span><b>Injector</b><small>Grid picker</small></button><button class="tool" onclick="showView('future')"><span class="toolIcon">⏱️</span><b>Future Signals</b><small>Step-by-step setup</small></button><button class="tool" onclick="showView('news')"><span class="toolIcon">📰</span><b>News Signals</b><small>Calendar + manual check</small></button><button class="tool" onclick="showView('management')"><span class="toolIcon">📋</span><b>Management</b><small>Session sheet</small></button></div></section>

<section id="view-live" class="view"><div class="eyebrow">SIGNAL CENTER · LIST VIEW</div><div class="titleRow"><div><h1>Live</h1><p>Pick a pair. Pairs are catalog entries — no live broker price is shown here.</p></div><button class="brokerBtn" onclick="openBroker()">Broker <b class="brokerLabel">Quotex</b> · Change</button></div><div class="panel" style="padding:14px"><div class="tabs" id="liveTabs"><button class="tab active" data-market="otc" onclick="state.market='otc';saveState();renderLive()">◉ OTC Market</button><button class="tab" data-market="real" onclick="state.market='real';saveState();renderLive()">◉ Real Market</button></div><input id="liveSearch" class="field" placeholder="Search pair (e.g. EUR/USD, GBP, JPY)…" oninput="state.search=this.value;renderLive()"><div class="sectionHead"><h2 id="liveCount">Pairs</h2><span>1m · catalog</span></div><div id="liveList"></div><div class="notice">Tapping a pair opens Analyzer. OTC has no live feed — Analyzer reads your screenshot. Real uses external feed only when its key is set.</div></div></section>

<section id="view-analyzer" class="view"><div class="eyebrow">GHOST AI ENGINE</div><div class="titleRow"><div><h1>Analyzer</h1><p>Choose a market or upload a chart screenshot.</p></div><span class="pill">✦ VISION AI*</span></div><div class="panel" style="padding:15px"><div class="sectionHead" style="margin-top:0"><h2>SELECT MARKET</h2><span id="analyzerLabel">ghostai - otc-chart</span></div><div class="tabs" id="analyzerTabs"><button class="tab active" data-market="otc" onclick="state.market='otc';saveState();renderAnalyzer()">◉ OTC Market</button><button class="tab" data-market="real" onclick="state.market='real';saveState();renderAnalyzer()">◉ Real Market</button></div><label style="font-size:9px;color:#d6c6f2">PAIR</label><select id="pairSelect" onchange="state.pair=this.value;state.market=this.value.includes('OTC')?'otc':'real';saveState();renderAnalyzer()"></select><div class="sectionHead"><h2>CHART SCREENSHOT</h2><span>OTC: screenshot · Real: external feed</span></div><div class="drop" id="dropZone"><span class="dropIcon">🖼️</span><b>Drop your chart to begin</b><p class="muted">Tap to browse, drag &amp; drop, or paste a screenshot.</p><button class="btn small" type="button">SELECT SCREENSHOT</button><input id="fileInput" type="file" accept="image/*" style="display:none"></div><div id="previewWrap" class="hidden" style="margin-top:12px"></div><button id="scanBtn" class="btn full" style="margin-top:12px" onclick="runAnalyzer()">ANALYZE SCREENSHOT</button><div id="analyzeOut" class="hidden" style="margin-top:12px"></div><div class="notice">* Vision AI runs only when AI_API_KEY is set. Confidence = clarity of evidence, not win probability.</div></div>
<div class="panel" style="padding:14px;margin-top:12px"><div class="sectionHead" style="margin-top:0"><h2>GHOST AI — ASK</h2><span>Educational</span></div><div class="chat" id="chat"><div class="bubble">👻 <b>Ghost AI</b><br>Ask about support / resistance, BOS, CHoCH, liquidity, confirmation or risk management. I never invent live prices or guaranteed outcomes.</div></div><div class="composer"><input id="chatInput" class="field" placeholder="Ask about price action…" onkeydown="if(event.key==='Enter')askAI()"><button class="btn" onclick="askAI()">ASK</button></div></div></section>

<section id="view-injector" class="view"><div class="eyebrow">MARKET SELECTOR · GRID VIEW</div><div class="titleRow"><div><h1>Injector</h1><p>Pick a pair in grid view, then open it in Analyzer.</p></div><button class="brokerBtn" onclick="openBroker()">Broker <b class="brokerLabel">Quotex</b> · Change</button></div><div class="panel" style="padding:14px"><div class="tabs" id="injTabs"><button class="tab active" data-market="otc" onclick="state.market='otc';saveState();renderInjector()">◉ OTC Market</button><button class="tab" data-market="real" onclick="state.market='real';saveState();renderInjector()">◉ Real Market</button></div><input id="injSearch" class="field" placeholder="Search pair…" oninput="state.search=this.value;renderInjector()"><div class="sectionHead"><h2 id="injCount">Pairs</h2><span>Grid · 1m</span></div><div class="pairGrid" id="injGrid"></div><div class="notice">Small sparkline is an illustrative pattern, not a live price chart.</div></div></section>

<section id="view-future" class="view"><div class="eyebrow">SIGNAL TOOLS · 01</div><div class="titleRow"><div><h1>Future Signals</h1><p>Pick markets, then check setups step by step.</p></div><button class="brokerBtn" onclick="openBroker()">Broker <b class="brokerLabel">Quotex</b> · Change</button></div><div class="panel" style="padding:14px"><div class="tabs" id="futureTabs"><button class="tab active" data-market="otc" onclick="state.market='otc';saveState();renderFuture()">◉ OTC Market</button><button class="tab" data-market="real" onclick="state.market='real';saveState();renderFuture()">◉ Real Market</button></div><input id="futureSearch" class="field" placeholder="Search pair…" oninput="state.search=this.value;renderFuture()"><div class="sectionHead"><h2>Pick markets</h2><span id="futureStepLabel">STEP 1 OF 2</span></div><div class="pairGrid" id="futureGrid"></div><div id="futureResult" class="panel hidden" style="padding:12px;margin-top:12px"></div></div><div style="position:sticky;bottom:96px;background:#160b3cf2;border:1px solid var(--line);border-radius:16px;padding:12px;margin-top:12px;display:flex;gap:8px;align-items:center"><div style="flex:1"><b id="futureHint" style="font-size:11px">No markets yet.</b><div class="muted">Step 1 of 2 — pick at least one market.</div></div><button id="futureBack" class="btn secondary hidden" onclick="futureBack()">← BACK</button><button id="futureNext" class="btn" onclick="futureNext()" disabled>NEXT STEP →</button></div></section>

<section id="view-news" class="view"><div class="eyebrow">SIGNAL TOOLS · 02</div><div class="titleRow"><div><h1>News Signals</h1><p>Calendar + manual Actual / Forecast / Previous check.</p></div><span class="pill">● CALENDAR</span></div><div class="panel" style="padding:14px"><div class="tabs" id="newsTabs"><button class="tab active" data-tab="events" onclick="state.newsTab='events';renderNews()">Today's Events</button><button class="tab" data-tab="fundamental" onclick="state.newsTab='fundamental';renderNews()">Fundamental</button></div>
<div id="newsEvents"><div class="panel lockCard" style="background:#ffffff08"><div class="lockIcon">🔒</div><h3 style="margin:8px 0 4px">News Signals is locked</h3><p class="muted">Free account can browse, but auto news-signal calls need an active plan. You can still browse calendar below and run a manual check.</p><button class="btn" onclick="showView('pricing')">UNLOCK ENGINE →</button></div><div class="sectionHead"><h2>Today's high-impact events</h2><span>Calendar only</span></div><div id="newsList"></div></div>
<div id="newsFund" class="hidden"><h3 style="margin:4px 0">Manual news check</h3><p class="muted">Enter released numbers. App explains Actual vs Forecast — it does not claim market already moved that way.</p><label style="font-size:9px;color:#d6c6f2">CURRENCY (e.g. USD)</label><input id="newsCur" class="field" placeholder="USD"><label style="font-size:9px;color:#d6c6f2">EVENT</label><input id="newsEvent" class="field" placeholder="e.g. CPI"><label style="font-size:9px;color:#d6c6f2">ACTUAL</label><input id="newsAct" class="field" placeholder="e.g. 3.1%"><label style="font-size:9px;color:#d6c6f2">FORECAST</label><input id="newsFore" class="field" placeholder="e.g. 3.0%"><label style="font-size:9px;color:#d6c6f2">PREVIOUS</label><input id="newsPrev" class="field" placeholder="e.g. 2.9%"><button class="btn full" style="margin-top:10px" onclick="analyzeNews()">ANALYZE NEWS</button><div id="newsAnalyzeOut" class="panel hidden" style="padding:12px;margin-top:12px"></div></div></div></section>

<section id="view-management" class="view"><div class="eyebrow">SIGNAL TOOLS · 03</div><div class="titleRow"><div><h1>Management</h1><p>Plan a session from your own numbers.</p></div><span class="pill">PLANNER</span></div><div class="panel" style="padding:14px"><div class="tabs" id="mgmtTabs"><button class="tab active" data-tab="management" onclick="state.managementTab='management';renderManagement()">Management</button><button class="tab" data-tab="sheet" onclick="state.managementTab='sheet';renderManagement()">Sheet</button></div>
<div id="mgmtForm"><div class="sectionHead" style="margin-top:0"><h2>SESSION PLANNER</h2><span>Plan your session</span></div><label style="font-size:9px;color:#d6c6f2">INITIAL CAPITAL ($)</label><input id="mgmtCap" class="field" type="number" value="100"><label style="font-size:9px;color:#d6c6f2">AVERAGE PAYOUT (%)</label><input id="mgmtPay" class="field" type="number" value="80"><label style="font-size:9px;color:#d6c6f2">PROFIT TARGET (%)</label><input id="mgmtTarget" class="field" type="number" value="20"><label style="font-size:9px;color:#d6c6f2">RISK PER TRADE (%)</label><input id="mgmtRisk" class="field" type="number" value="2"><div class="toggle"><span><b style="font-size:11px">Martingale (MTG)</b><br><span class="muted">Double the stake after a loss.</span></span><div id="mgmtMtg" class="switch" onclick="this.classList.toggle('on')"></div></div><button class="btn full" style="margin-top:12px" onclick="generateSheet()">GENERATE SHEET →</button><div class="notice">Martingale can wipe out capital quickly. This sheet assumes every step wins — real trading does not. Never risk money you cannot afford to lose.</div></div>
<div id="mgmtSheet" class="hidden"><div id="sheetBox"></div><button class="btn secondary" style="margin-top:12px" onclick="goMgmtForm()">EDIT INPUTS</button></div></div></section>

<section id="view-learn" class="view"><div class="eyebrow">LEARN &amp; IMPROVE</div><div class="titleRow"><div><h1>Ghost Academy</h1><p>Price-action learning center.</p></div><span class="pill">7 LESSONS</span></div><div class="panel" style="padding:14px"><button class="btn" onclick="loadLessons()">LOAD LESSONS</button><div id="lessonsBox" style="margin-top:12px"><div class="muted">Tap Load Lessons. Charts are illustrative, not live data.</div></div></div></section>

<section id="view-pricing" class="view"><div class="eyebrow">ACCESS PLANS</div><div class="titleRow"><div><h1>Choose your access</h1><p>No payment is collected in this build.</p></div><span class="pill">INFO ONLY</span></div><div class="planGrid"><div class="panel planCard"><span class="pill">FREE · $0</span><h3>Free</h3><p class="muted">Browse catalog, planner and lessons.</p><ul><li>Pair catalog</li><li>Management planner</li><li>Lessons</li></ul><button class="btn secondary full" onclick="launchApp()">START FREE</button></div><div class="panel planCard"><span class="pill">DIRECT · $99 / MONTH</span><h3>Direct</h3><p class="muted">Full engine when admin configures keys. No checkout wired.</p><ul><li>All Free tools</li><li>Screenshot Analyzer (AI key set)</li><li>Real Market external-feed checks</li><li>News calendar</li></ul><button class="btn full" onclick="launchApp()">REQUEST ACCESS</button></div></div><div class="notice">Admin: set TWELVE_DATA_API_KEY and AI_API_KEY in Render Environment. No checkout is wired in this build.</div></section>
</main>
<nav class="bottom"><button class="nav" data-view="dashboard"><i>🏠</i>Dashboard</button><button class="nav" data-view="live"><i>📡</i>Live</button><button class="nav center" data-view="analyzer"><i>👻</i>Analyzer</button><button class="nav" data-view="injector"><i>💉</i>Injector</button><button class="nav" data-view="more"><i>⋯</i>More</button></nav>
<div class="sheet" id="moreSheet"><div class="sheetBox"><div style="display:flex;justify-content:space-between;align-items:center"><h3 style="margin:0">More</h3><button class="iconBtn" onclick="closeSheet('#moreSheet')">✕</button></div><div class="panel userCard" style="margin-top:12px" id="moreUser"></div><div class="sectionHead"><h2>SIGNAL TOOLS</h2></div><button class="listRow" onclick="closeSheet('#moreSheet');showView('future')"><span><b>Future Signals</b><small>Forward-looking setup workspace</small></span><span><span class="badge">01</span> ›</span></button><button class="listRow" onclick="closeSheet('#moreSheet');showView('news')"><span><b>News Signals</b><small>Calendar + manual check</small></span><span><span class="badge">02</span> ›</span></button><button class="listRow" onclick="closeSheet('#moreSheet');showView('management')"><span><b>Management</b><small>Session planner + sheet</small></span><span><span class="badge">03</span> ›</span></button><button class="listRow" onclick="closeSheet('#moreSheet');showView('learn')"><span><b>Ghost Academy</b><small>7 price-action lessons</small></span><span>›</span></button><button class="listRow" onclick="closeSheet('#moreSheet');showView('pricing')"><span><b>Access Plans</b><small>Free · Direct $99/mo</small></span><span>›</span></button><div class="sectionHead"><h2>ACCOUNT</h2></div><button class="listRow" onclick="doLogout()"><span><b style="color:#ff9eb3">Log out</b><small>End this session</small></span><span>›</span></button><div class="muted" style="margin-top:10px">GHOST AI is not connected to any broker. Broker choice is a label only.</div></div></div>
<div class="sheet" id="brokerSheet"><div class="sheetBox"><div style="display:flex;justify-content:space-between;align-items:center"><h3 style="margin:0">Choose your broker</h3><button class="iconBtn" onclick="closeSheet('#brokerSheet')">✕</button></div><p class="muted">This only changes the label in the header. It does not log in to, or trade on, any broker.</p><div id="brokerList"></div></div></div>
<div class="login" id="loginOverlay"><div class="loginBox"><div style="text-align:center;font-size:40px">👻</div><h2 style="text-align:center">Welcome to GHOST AI</h2><p style="text-align:center">Sign in to open the console.</p><label>EMAIL</label><input id="loginEmail" class="field" type="email" placeholder="demo@ghost.ai"><label>PASSWORD</label><input id="loginPass" class="field" type="password" placeholder="••••••••" onkeydown="if(event.key==='Enter')doLogin()"><div class="error" id="loginErr"></div><button class="btn full" onclick="doLogin()">SIGN IN →</button><button class="btn secondary full" style="margin-top:8px" onclick="demoLogin()">CONTINUE AS DEMO</button><p class="muted" style="text-align:center;margin-top:12px">Demo: demo@ghost.ai / ghost1234 — change DEMO_PASSWORD in Render before sharing.</p></div></div>
<div class="toast" id="toast"></div></div>
<script>
const $=s=>document.querySelector(s);const $$=s=>[...document.querySelectorAll(s)];
const esc=s=>String(s??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const FLAGS={USD:"🇺🇸",EUR:"🇪🇺",GBP:"🇬🇧",JPY:"🇯🇵",CHF:"🇨🇭",AUD:"🇦🇺",CAD:"🇨🇦",NZD:"🇳🇿",BRL:"🇧🇷",INR:"🇮🇳",IDR:"🇮🇩",MXN:"🇲🇽",PKR:"🇵🇰",PHP:"🇵🇭",COP:"🇨🇴",ARS:"🇦🇷",ZAR:"🇿🇦",BDT:"🇧🇩"};
const flag=c=>FLAGS[c]||"🏳️";
function pairObj(b,q,otc){return{symbol:`${b}/${q}${otc?" OTC":""}`,base:b,quote:q,otc};}
const REAL_PAIRS=[["EUR","USD"],["GBP","USD"],["USD","JPY"],["USD","CHF"],["USD","CAD"],["AUD","USD"],["EUR","GBP"],["EUR","JPY"],["EUR","CHF"],["EUR","CAD"],["EUR","AUD"],["EUR","NZD"],["GBP","AUD"],["GBP","CAD"],["GBP","CHF"],["GBP","JPY"],["GBP","NZD"],["AUD","CAD"],["AUD","CHF"],["AUD","JPY"],["AUD","NZD"],["CAD","JPY"],["CAD","CHF"],["NZD","CAD"],["NZD","CHF"],["NZD","JPY"]].map(([b,q])=>pairObj(b,q,false));
const OTC_PAIRS=[["AUD","CAD"],["AUD","CHF"],["AUD","JPY"],["AUD","NZD"],["AUD","USD"],["CAD","CHF"],["CAD","JPY"],["CHF","JPY"],["EUR","AUD"],["EUR","CAD"],["EUR","CHF"],["EUR","GBP"],["EUR","JPY"],["EUR","NZD"],["EUR","USD"],["GBP","AUD"],["GBP","CAD"],["GBP","CHF"],["GBP","JPY"],["GBP","NZD"],["GBP","USD"],["NZD","CAD"],["NZD","CHF"],["NZD","JPY"],["USD","ARS"],["USD","BDT"],["USD","BRL"],["USD","CAD"],["USD","CHF"],["USD","COP"],["USD","IDR"],["USD","INR"],["USD","JPY"],["USD","MXN"],["USD","PHP"],["USD","PKR"],["USD","ZAR"]].map(([b,q])=>pairObj(b,q,true));
const ALL_PAIRS=[...REAL_PAIRS,...OTC_PAIRS];
const BROKERS=[{name:"Quotex",icon:"💜"},{name:"Binolla",icon:"🟣"},{name:"Pocket Option",icon:"🟠"}];
const state={broker:"Quotex",market:"otc",pair:"EUR/USD OTC",search:"",futureSel:new Set(),futureStep:1,managementTab:"management",sheet:null,user:null,plan:"FREE",newsTab:"events",lessons:[]};
function saveState(){try{localStorage.setItem("ghost_state",JSON.stringify({broker:state.broker,market:state.market,pair:state.pair,sheet:state.sheet}))}catch(e){}}
function loadState(){try{const d=JSON.parse(localStorage.getItem("ghost_state")||"{}");if(d.broker)state.broker=d.broker;if(d.market)state.market=d.market;if(d.pair)state.pair=d.pair;if(d.sheet)state.sheet=d.sheet;}catch(e){}}
loadState();
function toast(m){const t=$("#toast");t.textContent=m;t.classList.add("show");clearTimeout(t._h);t._h=setTimeout(()=>t.classList.remove("show"),3200);}
async function api(p,o={}){const r=await fetch(p,{credentials:"same-origin",headers:{"Content-Type":"application/json"},...o});const d=await r.json().catch(()=>({}));if(!r.ok)throw new Error(d.error||`Request failed (${r.status})`);return d;}
function pairParts(s){const c=s.replace(" OTC","");const [b,q]=c.split("/");return{base:b,quote:q,otc:s.includes("OTC")};}
function flagPair(s){const {base,quote}=pairParts(s);return `<span class="flag">${flag(base)}${flag(quote)}</span>`;}
function sparkSVG(){return `<svg class="spark" viewBox="0 0 64 22" fill="none"><path d="M1 16 L9 12 L17 14 L25 8 L33 11 L41 6 L49 9 L57 4 L63 7" stroke="#b47aff" stroke-width="1.6"/><path d="M1 18 L12 18 L20 15 L30 17 L42 13 L55 15 L63 12" stroke="#79e9ff" stroke-width="1.2" opacity=".7"/></svg>`;}
function filteredPairs(){const w=state.market==="otc";const list=ALL_PAIRS.filter(p=>p.otc===w);const q=state.search.trim().toUpperCase();if(!q)return list;return list.filter(p=>p.symbol.includes(q)||p.base.includes(q)||p.quote.includes(q));}
function renderBrokerLabels(){$$(".brokerLabel").forEach(e=>e.textContent=state.broker);}
function showView(n){$$(".view").forEach(v=>v.classList.remove("active"));const el=document.getElementById("view-"+n);if(el)el.classList.add("active");$$(".nav").forEach(x=>x.classList.toggle("active",x.dataset.view===n));$("#moreSheet").classList.remove("open");window.scrollTo({top:0});if(n==="live")renderLive();if(n==="injector")renderInjector();if(n==="future")renderFuture();if(n==="news")renderNews();if(n==="management")renderManagement();if(n==="dashboard")loadDashboard();if(n==="analyzer")renderAnalyzer();history.replaceState(null,"","#/"+n);}
function openMore(){$("#moreSheet").classList.add("open");renderMore();}
function closeSheet(id){$(id).classList.remove("open");}
function openBroker(){$("#brokerSheet").classList.add("open");renderBrokerSheet();}
function renderBrokerSheet(){$("#brokerList").innerHTML=BROKERS.map(b=>`<button class="listRow" onclick="selectBroker('${b.name}')"><span style="display:flex;gap:10px;align-items:center"><span class="flag">${b.icon}</span><span><b>${b.name}</b><small>${b.name===state.broker?"Currently selected":"Broker account"}</small></span></span><span>${b.name===state.broker?"✓":"›"}</span></button>`).join("");}
function selectBroker(n){state.broker=n;saveState();renderBrokerLabels();closeSheet("#brokerSheet");toast(`Broker set to ${n}. Label only - it does not connect to the broker.`);}
function renderMore(){const u=state.user||{name:"Arafat Hossain",email:"demo@ghost.ai",plan:"FREE"};$("#moreUser").innerHTML=`<div class="avatar">👻</div><div style="flex:1;min-width:0"><b>${esc(u.name)}</b><div class="muted">${esc(u.email)}</div><div style="margin-top:6px"><span class="badge">${esc(u.plan||"FREE")}</span></div></div><span>›</span>`;}
function renderLive(){const list=filteredPairs();const s=$("#liveSearch");if(s)s.value=state.search;const c=$("#liveCount");if(c)c.textContent=`${list.length} pairs · ${state.market==="otc"?"OTC Market":"Real Market"}`;$("#liveList").innerHTML=list.length?list.map(p=>`<button class="listRow" onclick="pickPair('${esc(p.symbol)}','analyzer')"><span style="display:flex;gap:10px;align-items:center">${flagPair(p.symbol)}<span><b>${esc(p.symbol)}</b><small>${p.otc?"OTC MARKET":"REAL MARKET"}</small></span></span><span><span class="badge">1m</span> ›</span></button>`).join(""):`<div class="muted" style="padding:14px">No pairs match.</div>`;$$("#liveTabs .tab").forEach(t=>t.classList.toggle("active",t.dataset.market===state.market));}
function renderInjector(){const list=filteredPairs();const s=$("#injSearch");if(s)s.value=state.search;const c=$("#injCount");if(c)c.textContent=`${list.length} pairs · ${state.market==="otc"?"OTC Market":"Real Market"}`;$("#injGrid").innerHTML=list.length?list.map(p=>`<button class="pairCard ${state.pair===p.symbol?"selected":""}" onclick="pickPair('${esc(p.symbol)}','analyzer')">${flagPair(p.symbol)}<span style="min-width:0"><span class="pairName">${esc(p.symbol)}</span><span class="pairType">${p.otc?"OTC MARKET":"REAL MARKET"} · 1m</span></span>${sparkSVG()}<span class="pairCheck">${state.pair===p.symbol?"✓":""}</span></button>`).join(""):`<div class="muted">No pairs match.</div>`;$$("#injTabs .tab").forEach(t=>t.classList.toggle("active",t.dataset.market===state.market));}
function pickPair(sym,go){state.pair=sym;state.market=sym.includes("OTC")?"otc":"real";saveState();renderBrokerLabels();if(go)showView(go);else renderAnalyzer();toast(`Selected ${sym}`);}
function renderAnalyzer(){const sel=$("#pairSelect");if(sel)sel.innerHTML=ALL_PAIRS.map(p=>`<option value="${esc(p.symbol)}" ${p.symbol===state.pair?"selected":""}>${esc(p.symbol)}</option>`).join("");const lbl=$("#analyzerLabel");if(lbl)lbl.textContent=`ghostai - ${state.market==="otc"?"otc-chart":"real-chart"}`;$$("#analyzerTabs .tab").forEach(t=>t.classList.toggle("active",t.dataset.market===state.market));const btn=$("#scanBtn");if(btn)btn.textContent=state.market==="otc"?"ANALYZE SCREENSHOT":"SCAN REAL MARKET";}
function renderFuture(){const list=filteredPairs();const s=$("#futureSearch");if(s)s.value=state.search;if(state.futureStep===1){$("#futureStepLabel").textContent="STEP 1 OF 2 — PICK AT LEAST ONE MARKET";$("#futureGrid").innerHTML=list.map(p=>`<button class="pairCard ${state.futureSel.has(p.symbol)?"selected":""}" onclick="toggleFuture('${esc(p.symbol)}')">${flagPair(p.symbol)}<span style="min-width:0"><span class="pairName">${esc(p.symbol)}</span><span class="pairType">${p.otc?"OTC MARKET":"REAL MARKET"} · 1m</span></span><span class="pairCheck">${state.futureSel.has(p.symbol)?"●":"○"}</span></button>`).join("");$("#futureNext").disabled=state.futureSel.size===0;$("#futureNext").textContent="NEXT STEP →";$("#futureHint").textContent=state.futureSel.size?`${state.futureSel.size} market(s) selected.`:"No markets yet. Pick at least one to continue.";$("#futureBack").classList.add("hidden");}else{$("#futureStepLabel").textContent="STEP 2 OF 2 — CHECK SETUP (REAL ONLY)";const sel=[...state.futureSel];$("#futureGrid").innerHTML=sel.map(sym=>`<div class="pairCard selected">${flagPair(sym)}<span style="min-width:0"><span class="pairName">${esc(sym)}</span><span class="pairType">${sym.includes("OTC")?"OTC MARKET — no live feed":"REAL MARKET — external feed"}</span></span><span class="pairCheck">✓</span></div>`).join("");$("#futureNext").disabled=false;$("#futureNext").textContent="CHECK SETUP";$("#futureHint").textContent="OTC has no live feed — use Analyzer screenshot for OTC.";$("#futureBack").classList.remove("hidden");}$$("#futureTabs .tab").forEach(t=>t.classList.toggle("active",t.dataset.market===state.market));}
function toggleFuture(sym){if(state.futureSel.has(sym))state.futureSel.delete(sym);else state.futureSel.add(sym);renderFuture();}
function futureNext(){if(state.futureStep===1){if(!state.futureSel.size)return;state.futureStep=2;renderFuture();return;}checkFuture();}
function futureBack(){state.futureStep=1;renderFuture();}
async function checkFuture(){const out=$("#futureResult");out.innerHTML=`<div class="spinner"></div><div class="muted" style="text-align:center">Checking selected Real markets one by one…</div>`;out.classList.remove("hidden");let html="";for(const sym of [...state.futureSel]){if(sym.includes("OTC")){html+=`<div class="resultLine"><b>${esc(sym)}</b> — No live OTC feed. Use Analyzer screenshot. No signal fabricated.</div>`;continue;}try{const d=await api("/api/scan",{method:"POST",body:JSON.stringify({symbol:sym.replace(" OTC","")})});const r=d.result||{};html+=`<div class="resultLine"><b>${esc(sym)}</b> — <b>${esc(r.direction||"NO TRADE")}</b><br>${esc(r.reason||"")}<br><span class="muted">Source: Twelve Data · Not Quotex-verified · Candle: ${esc(d.candle_time||"—")}</span></div>`;}catch(e){html+=`<div class="resultLine"><b>${esc(sym)}</b> — ${esc(e.message)}<br><span class="muted">No signal fabricated.</span></div>`;}}out.innerHTML=html||`<div class="muted">Nothing selected.</div>`;}
function renderNews(){$$("#newsTabs .tab").forEach(t=>t.classList.toggle("active",t.dataset.tab===state.newsTab));$("#newsEvents").classList.toggle("hidden",state.newsTab!=="events");$("#newsFund").classList.toggle("hidden",state.newsTab!=="fundamental");if(state.newsTab==="events")loadNews();}
async function loadNews(){const box=$("#newsList");box.innerHTML=`<div class="spinner"></div>`;try{const d=await api("/api/news?days=7");const ev=d.events||[];if(!ev.length)box.innerHTML=`<div class="muted">No high-impact events in the next 7 days.</div>`;else box.innerHTML=ev.slice(0,20).map(e=>`<div class="marketRow"><span><b>${esc(e.currency)} · ${esc(e.name)}</b><small>${esc(e.datetime_bd||"")} · Impact: HIGH · Forecast: ${esc(e.forecast??"—")} · Previous: ${esc(e.previous??"—")}</small></span><span class="badge">EVENT</span></div>`).join("");}catch(e){box.innerHTML=`<div class="muted">${esc(e.message)} — calendar unavailable.</div>`;}}
async function analyzeNews(){const cur=$("#newsCur").value.trim().toUpperCase(),ev=$("#newsEvent").value.trim(),act=$("#newsAct").value.trim(),fore=$("#newsFore").value.trim(),prev=$("#newsPrev").value.trim();const out=$("#newsAnalyzeOut");if(!cur||!ev||!act||!fore){out.innerHTML=`<div class="muted">Fill currency, event, actual and forecast.</div>`;out.classList.remove("hidden");return;}out.innerHTML=`<div class="spinner"></div>`;out.classList.remove("hidden");try{const d=await api("/api/news/analyze",{method:"POST",body:JSON.stringify({currency:cur,event:ev,actual:act,forecast:fore,previous:prev})});out.innerHTML=`<div class="resultLine"><b>${esc(cur)} · ${esc(ev)}</b><br>${esc(d.analysis||"")}</div><div class="muted">${esc(d.note||"")}</div>`;}catch(e){out.innerHTML=`<div class="muted">${esc(e.message)}</div>`;}}
function renderManagement(){$$("#mgmtTabs .tab").forEach(t=>t.classList.toggle("active",t.dataset.tab===state.managementTab));$("#mgmtForm").classList.toggle("hidden",state.managementTab!=="management");$("#mgmtSheet").classList.toggle("hidden",state.managementTab!=="sheet");if(state.managementTab==="sheet")renderSheet();}
function generateSheet(){const cap=parseFloat($("#mgmtCap").value),pay=parseFloat($("#mgmtPay").value),target=parseFloat($("#mgmtTarget").value),risk=parseFloat($("#mgmtRisk").value),mtg=$("#mgmtMtg").classList.contains("on");if(!cap||!pay||!target||!risk){toast("Fill in all fields.");return;}if(cap<=0||pay<=0||pay>=100||target<=0||risk<=0||risk>=100){toast("Check numbers — payout and risk must be 0-100.");return;}const rows=[];let balance=cap,stake=cap*risk/100,profit=0,step=0;while(profit<cap*target/100&&step<30){step++;const useStake=mtg&&step>1?Math.min(stake*2,balance):stake;const w=useStake*pay/100;profit+=w;balance+=w;rows.push({step,stake:useStake,win:w,balance,profit});stake=useStake;if(mtg)stake=useStake*2;else stake=balance*risk/100;}state.sheet={cap,pay,target,risk,mtg,rows,totalProfit:profit,finalBalance:balance};saveState();state.managementTab="sheet";renderManagement();toast("Sheet generated — planning aid only.");}
function renderSheet(){const box=$("#sheetBox");if(!state.sheet){box.innerHTML=`<div style="text-align:center;padding:26px 10px"><div style="font-size:36px">📋</div><h3>No sheet yet</h3><p class="muted">Enter capital, payout, target and risk to build a sheet.</p><button class="btn" onclick="goMgmtForm()">GO TO MANAGEMENT</button></div>`;return;}const s=state.sheet;box.innerHTML=`<div class="resultLine"><b>Capital $${s.cap.toFixed(2)} · Payout ${s.pay}% · Target ${s.target}% · Risk ${s.risk}% · Martingale ${s.mtg?"YES":"NO"}</b><br><span class="muted">Projected profit $${s.totalProfit.toFixed(2)} · Final $${s.finalBalance.toFixed(2)} · ${s.rows.length} step(s) if every step won — planning aid only.</span></div><table class="sheetTable"><tr><th>STEP</th><th>STAKE</th><th>WIN</th><th>BALANCE</th></tr>${s.rows.map(r=>`<tr><td>${r.step}</td><td>$${r.stake.toFixed(2)}</td><td>+$${r.win.toFixed(2)}</td><td>$${r.balance.toFixed(2)}</td></tr>`).join("")}</table>`;}
function goMgmtForm(){state.managementTab="management";renderManagement();}
let pendingImage=null;
function handleFile(f){if(!f)return;if(!f.type.startsWith("image/")){toast("Choose an image file.");return;}if(f.size>12*1024*1024){toast("Image over 12 MB.");return;}pendingImage=f;const url=URL.createObjectURL(f);$("#previewWrap").innerHTML=`<img src="${url}" style="width:100%;border-radius:14px;border:1px solid var(--line)">`;$("#previewWrap").classList.remove("hidden");$("#scanBtn").disabled=false;}
async function runAnalyzer(){const out=$("#analyzeOut");out.classList.remove("hidden");if(state.market==="real"){const clean=state.pair.replace(" OTC","");out.innerHTML=`<div class="spinner"></div><div class="muted" style="text-align:center">Scanning ${esc(state.pair)}…</div>`;try{const d=await api("/api/scan",{method:"POST",body:JSON.stringify({symbol:clean})});const r=d.result||{};out.innerHTML=`<div class="result"><div class="resultHead"><span class="resultDir">${esc(r.direction||"NO TRADE")}</span><span class="badge">${esc(state.pair)}</span></div><div class="resultLine"><b>Why:</b> ${esc(r.reason||"")}</div><div class="resultLine"><b>External price:</b> ${r.price!=null?esc(String(r.price)):"—"} · <b>Candle:</b> ${esc(d.candle_time||"—")}</div><div class="resultLine"><b>Source:</b> Twelve Data · <b>Quotex-verified:</b> No</div><div class="resultLine"><b>Backtest:</b> ${esc(JSON.stringify(d.backtest||{}))}</div><div class="muted">Educational only. Not financial advice.</div></div>`;}catch(e){out.innerHTML=`<div class="notice">${esc(e.message)} — no signal fabricated.</div>`;}return;}if(!pendingImage){out.innerHTML=`<div class="notice">Choose a chart screenshot first. OTC has no live feed, so Analyzer reads your screenshot.</div>`;return;}out.innerHTML=`<div class="spinner"></div><div class="muted" style="text-align:center">Reading screenshot…</div>`;const fd=new FormData();fd.append("image",pendingImage);try{const r=await fetch("/api/analyze-image",{method:"POST",credentials:"same-origin",body:fd});const d=await r.json();if(!r.ok)throw new Error(d.error||"Analysis failed");const a=d.analysis||{};out.innerHTML=`<div class="result"><div class="resultHead"><span class="resultDir">${esc(a.direction||"NO TRADE")}</span><span class="badge">${esc(state.pair)}</span></div><div class="muted">Confidence (clarity of evidence, not win probability):</div><div class="bar"><i style="width:${a.confidence||0}%"></i></div><div class="muted">${a.confidence||0}% · ${esc(a.confidence_note||"")}</div><div class="resultLine"><b>Summary:</b> ${esc(a.summary||"")}</div><div class="resultLine"><b>Trend:</b> ${esc(a.trend||"")}</div><div class="resultLine"><b>Structure:</b> ${esc(a.structure||"")}</div><div class="resultLine"><b>Zones:</b> ${esc(a.zones||"")}</div><div class="resultLine"><b>Reasoning:</b> ${esc(a.reasoning||"")}</div><div class="resultLine"><b>Confirmation:</b> ${esc(a.confirmation||"")}</div><div class="resultLine"><b>Invalidation:</b> ${esc(a.invalidation||"")}</div><div class="muted">${esc(d.warning||"")}</div></div>`;}catch(e){out.innerHTML=`<div class="notice">${esc(e.message)} — no signal fabricated.</div>`;}}
async function loadDashboard(){const u=state.user||{name:"Arafat Hossain",email:"demo@ghost.ai",plan:"FREE"};const n=$("#dashName");if(n)n.textContent=u.name;const m=$("#dashEmail");if(m)m.textContent=u.email;const p=$("#dashPlan");if(p)p.textContent=u.plan||"FREE";try{const d=await api("/api/dashboard");const s=$("#dashStats");if(s)s.innerHTML=`<div class="panel" style="padding:14px"><small>CATALOG</small><br><strong>${d.catalog_count||ALL_PAIRS.length}</strong><br><small>pairs listed</small></div><div class="panel" style="padding:14px"><small>EXTERNAL FEED</small><br><strong>${d.twelve_data_key_configured?"READY":"NOT SET"}</strong><br><small>Real scan</small></div><div class="panel" style="padding:14px"><small>VISION AI</small><br><strong>${d.ai_api_key_configured?"READY":"NOT SET"}</strong><br><small>Screenshot</small></div>`;const feed=$("#dashFeed");if(feed){const sig=d.signals||[];feed.innerHTML=sig.length?sig.slice(0,5).map(x=>`<div class="marketRow"><span><b>${esc(x.symbol)} · ${esc(x.direction)}</b><small>${esc(x.reason||"")}</small></span><span class="badge">${esc(x.status||"")}</span></div>`).join(""):`<div class="muted">No recorded setups yet. Run a Real scan from Analyzer, or upload an OTC screenshot — nothing is auto-generated.</div>`;}}catch(e){const s=$("#dashStats");if(s)s.innerHTML=`<div class="panel" style="padding:14px"><small>CATALOG</small><br><strong>${ALL_PAIRS.length}</strong></div>`;}}
async function checkAuth(){try{const d=await api("/api/auth/me");if(d.authenticated){state.user=d.user;state.plan=d.user.plan||"FREE";renderBrokerLabels();$("#loginOverlay").classList.remove("open");return true;}}catch(e){}return false;}
async function doLogin(){const email=$("#loginEmail").value.trim().toLowerCase(),pw=$("#loginPass").value;const err=$("#loginErr");if(!email||!pw){err.textContent="Enter email and password.";return;}err.textContent="Signing in…";try{const d=await api("/api/auth/login",{method:"POST",body:JSON.stringify({email,password:pw})});state.user=d.user;state.plan=d.user.plan||"FREE";$("#loginOverlay").classList.remove("open");err.textContent="";toast(`Welcome back, ${d.user.name}.`);showView("dashboard");}catch(e){err.textContent=e.message;}}
function demoLogin(){$("#loginEmail").value="demo@ghost.ai";$("#loginPass").value="ghost1234";doLogin();}
async function doLogout(){try{await api("/api/auth/logout",{method:"POST"});}catch(e){}state.user=null;closeSheet("#moreSheet");$("#loginOverlay").classList.add("open");toast("Logged out.");showView("landing");}
function launchApp(){if(state.user)showView("dashboard");else $("#loginOverlay").classList.add("open");}
async function loadLessons(){const box=$("#lessonsBox");if(!box)return;box.innerHTML=`<div class="spinner"></div>`;try{const d=await api("/api/lessons");state.lessons=d.lessons||[];box.innerHTML=state.lessons.map(l=>`<div class="panel" style="padding:14px;margin-bottom:10px"><b>${esc(l.title)}</b><p class="muted">${esc(l.caption)}</p><div class="muted">Illustrative example — not live market data.</div></div>`).join("");}catch(e){box.innerHTML=`<div class="muted">${esc(e.message)} — login to load lessons.</div>`;}}
async function askAI(){const inp=$("#chatInput");const q=inp.value.trim();if(!q)return;const chat=$("#chat");chat.innerHTML+=`<div class="bubble me">${esc(q)}</div>`;inp.value="";chat.scrollTop=chat.scrollHeight;chat.innerHTML+=`<div class="bubble" id="chatWait">Thinking…</div>`;try{const d=await api("/api/ask",{method:"POST",body:JSON.stringify({question:q})});$("#chatWait").remove();chat.innerHTML+=`<div class="bubble">👻 <b>Ghost AI</b><br>${esc(d.answer||"")}</div>`;}catch(e){const w=$("#chatWait");if(w)w.textContent=e.message;}chat.scrollTop=chat.scrollHeight;}
document.addEventListener("DOMContentLoaded",async()=>{
renderBrokerLabels();renderLive();renderInjector();renderAnalyzer();renderFuture();renderManagement();
const drop=$("#dropZone");if(drop){drop.addEventListener("click",()=>$("#fileInput").click());drop.addEventListener("dragover",e=>{e.preventDefault();drop.style.borderColor="#e58bff";});drop.addEventListener("dragleave",()=>drop.style.borderColor="");drop.addEventListener("drop",e=>{e.preventDefault();drop.style.borderColor="";const f=e.dataTransfer.files[0];if(f)handleFile(f);});}
const fi=$("#fileInput");if(fi)fi.addEventListener("change",e=>handleFile(e.target.files[0]));
document.addEventListener("paste",e=>{const items=e.clipboardData?.items||[];for(const it of items){if(it.type.startsWith("image/")){handleFile(it.getAsFile());toast("Screenshot pasted.");showView("analyzer");break;}}});
$$(".nav").forEach(n=>n.addEventListener("click",()=>{if(n.dataset.view==="more"){openMore();return;}if(!state.user&&["dashboard","live","analyzer","injector"].includes(n.dataset.view)){launchApp();return;}showView(n.dataset.view);}));
const authed=await checkAuth();const hash=(location.hash||"").replace("#/","");if(authed){if(["dashboard","live","analyzer","injector","future","news","management","pricing","learn"].includes(hash))showView(hash);else showView("dashboard");}else{if(hash==="pricing")showView("pricing");else showView("landing");}
});
</script><script>const c=document.getElementById("stars");const x=c.getContext("2d");function rs(){c.width=innerWidth;c.height=innerHeight;x.clearRect(0,0,c.width,c.height);for(let i=0;i<90;i++){x.fillStyle=`rgba(255,255,255,${Math.random()*0.7})`;x.beginPath();x.arc(Math.random()*c.width,Math.random()*c.height,Math.random()*1.4,0,7);x.fill();}}rs();addEventListener("resize",rs);</script></body></html>'''

if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
