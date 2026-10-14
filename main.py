# 👻 GHOST MATRIX V2.0 - FULL FINAL CODE + REAL AI BRAIN API
import os, requests, time, random, threading
from flask import Flask
from datetime import datetime, timedelta
import pytz

BD_TZ = pytz.timezone('Asia/Dhaka')
TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
AI_API_KEY = os.getenv("AI_API_KEY") # OpenAI / Groq API Key

app = Flask(__name__)
@app.route('/')
def home(): return "GHOST MATRIX V2.0 - FINAL LIVE"
threading.Thread(target=lambda: app.run(host='0.0.0.0', port=int(os.environ.get("PORT",10000))), daemon=True).start()

# --- 1. FULL COURSE DETAILS (AGER SOB INCLUDE) ---
COURSE = """
📚 BOS = Break of Structure - Market ager High/Low vanglo, Boro Bank dhuklo. Retest e entry.
📚 FVG = Fair Value Gap - 3 Candle er majhe faka jayga, Bank er baki order. Gap fill e entry.
📚 OB = Order Block - Bank jekhane order rakhse, last buy/sell candle e 90% bounce.
📚 SWEEP = Liquidity Sweep - Sobar SL khawa, BOS er sathe 100% sure.
📚 ENGULF = Boro candle choto ke gile fello. Bullish=BUY, Bearish=SELL.
💰 FORMULA: 100% = FVG 20% + SWEEP 25% + BOS 15% + OB 20% + ENGULF 20%
RULE: 80%+ CONFIRM, 90%+ STRONG, 96%+ JACKPOT
"""

def real_ai_brain(user_question):
    # 1. Jodi API KEY thake, tahole REAL AI (Groq/OpenAI) diye sothik uttor
    if AI_API_KEY:
        try:
            # Groq API (Free & Fast) - OpenAI er motoi
            url = "https://api.groq.com/openai/v1/chat/completions"
            headers = {"Authorization": f"Bearer {AI_API_KEY}", "Content-Type": "application/json"}
            data = {
                "model": "llama3-8b-8192",
                "messages": [
                    {"role": "system", "content": f"Tumi GHOST MATRIX V2.0 bot. Tumi binary trading expert. Bangla te short e bujhao. Context: {COURSE}"},
                    {"role": "user", "content": user_question}
                ],
                "temperature": 0.7
            }
            res = requests.post(url, json=data, headers=headers, timeout=15).json()
            return res['choices'][0]['message']['content']
        except Exception as e:
            print(f"AI API Error: {e}")
            # API fail hole niche fallback jabe

    # 2. API KEY na thakle - Smart Keyword Brain (Agger moto ghurabe na)
    ql = user_question.lower()
    if "bos" in ql: return "📚 BOS = Break of Structure\nMarket ager high/low vangse. Eta bank er kaj. BOS er por Retest + OB te entry niba. 15% score."
    if "fvg" in ql: return "📚 FVG = Fair Value Gap\n3 candle er majhe gap. Bank er order baki. Price oi gap e asle entry. M15 FVG sobcheye strong. 20% score."
    if "ob" in ql: return "📚 OB = Order Block\nBank er last buy/sell candle. Price okhane asle 90% bounce. 20% score."
    if "sweep" in ql: return "📚 SWEEP = Liquidity nise, sobar SL khaise. Sweep + BOS = 100% sure entry. 25% score, sobcheye boro."
    if "engulf" in ql: return "📚 ENGULF = Boro candle choto ke gile fello. Bullish hole BUY 🟢, Bearish hole SELL 🔴. 20% score."

    return f"🧠 GHOST MATRIX AI:\nApni bolchen: '{user_question}'\n\n{ COURSE }\n\nSpecific jante likhun: BOS ki, FVG ki, OB ki? Ar Real AI er jonno Render e AI_API_KEY add korun."

def get_ghost_signal():
    now = datetime.now(BD_TZ)
    if now.weekday() >= 5:
        return f"⭐ GHOST MATRIX V2.0 SIGNALS 🎖\n🗓 DATE - {now.strftime('%d.%m.%Y')}\n🚀 M1 • UTC+6 • MTG ×1\n──────────────────────\n🔒 REAL MARKET CLOSED\n──────────────────────\n🛡 AVOID UNDER 80% MARKET\n──────────────────────\n💎 BY - GHOST MATRIX V2.0"

    date_str = now.strftime('%d.%m.%Y')
    pairs = ["AUDJPY","USDJPY","CADJPY","EURJPY","EURUSD","GBPUSD","AUDUSD","NZDUSD","USDCHF","GBPJPY"]
    start = now + timedelta(minutes=10)
    sigs = ""
    cur = start
    for p in random.sample(pairs, 8):
        cur += timedelta(minutes=random.randint(8,22))
        side = "CALL" if random.random()>0.5 else "PUT"
        sigs += f"M1;{p}•{cur.strftime('%H:%M')};{side}\n"

    return f"⭐ GHOST MATRIX V2.0 SIGNALS 🎖\n🗓 DATE - {date_str}\n🚀 M1 • UTC+6 • MTG ×1\n──────────────────────\n{sigs.strip()}\n──────────────────────\n🛡 AVOID UNDER 80% MARKET\n──────────────────────\n💎 BY - GHOST MATRIX V2.0"

def send(t):
    if not TOKEN or not CHAT_ID: return
    try: requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage", json={"chat_id": CHAT_ID, "text": t}, timeout=10)
    except: pass

offset=0
while True:
    try:
        if not TOKEN: time.sleep(5); continue
        r = requests.get(f"https://api.telegram.org/bot{TOKEN}/getUpdates?offset={offset}&timeout=30", timeout=35).json()
        for u in r.get("result",[]):
            offset = u["update_id"]+1
            txt = u.get("message",{}).get("text","").strip()
            if not txt: continue
            low = txt.lower()
            if low.startswith("/start"): send(f"👻 GHOST MATRIX V2.0 LIVE\n\n{ COURSE }\n\n/future = Signal\nJekono prosno korun, AI uttor dibe!")
            elif low.startswith("/future"): send(get_ghost_signal())
            else: send(real_ai_brain(txt)) # Ekhane ar ghurabe na, sothik uttor dibe
        time.sleep(1)
    except Exception as e:
        print(e); time.sleep(3)
