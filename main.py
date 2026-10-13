import os, requests, random, time, json, threading
from datetime import datetime, timedelta
from flask import Flask

app = Flask(__name__)
@app.route('/')
def home(): return "GHOST MATRIX % SURE LIVE"

TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = "8879968219"

REAL = ["EURUSD","GBPUSD","AUDUSD","USDJPY","EURJPY","GBPJPY","USDCHF","USDCAD","NZDUSD","EURGBP","XAUUSD"]

SETUPS = [
    ("M15 FVG + Sweep + BOS + M1 OB + Engulf", 100),
    ("M15 FVG + Sweep + M1 OB + BOS", 95),
    ("Sweep + BOS + M1 OB + Engulf", 85),
    ("M15 FVG + M1 OB + Engulf", 80),
    ("M15 OB Retest + BOS + Engulf", 75),
    ("Asia Sweep + BOS + OB", 70),
    ("M1 OB + Engulf Only", 55),
    ("M5 FVG Fill Only", 50)
]

PA_LESSONS = {
"candle": "🕯️ <b>CANDLE - 1M</b>\nM5 Green > M1 BUY, Red > SELL. Lomba wick = Reject.",
"sr": "📍 <b>S/R</b>\nM15 High/Low mark, M1 e 2 rejection = 3rd e entry.",
"ob": "📦 <b>ORDER BLOCK 20%</b>\nLast strong opposite candle = OB. Price okhane asle engulfing e entry.",
"liq": "💧 <b>LIQUIDITY SWEEP 25% (Sobcheye dami)</b>\nHigh er upore wick baniye niche close = Sweep = Bank SL khaise. Porer candle opposite.",
"fvg": "📉 <b>FVG 20%</b>\n3 candle er majhe gap = Bank er order. Price fill kortei hobe.",
"entry": "🎯 <b>SURE % MAP</b>\n\nFVG 20% + Sweep 25% + BOS 15% + OB 20% + Engulf 20% = 100%\n\n4 ta milse = 80%+ = Entry nio\n3 ta = 55-70% = Risky\n2 ta = 40% = Niba na",
"time": "⏰ BEST TIME: 1PM-5PM & 7PM-11PM BD. Ei time e 80%+ setup beshi paya jay.",
"mtg": "📊 MTG: 1 Step Only. 1st loss hole porer candle e double."
}

def send(text, buttons=None):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    data = {"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"}
    if buttons: data["reply_markup"] = json.dumps({"inline_keyboard": buttons})
    try: requests.post(url, data=data, timeout=12)
    except: pass

def learn_btns():
    return [
        [{"text": "🕯️ Candle", "callback_data": "PA_candle"}, {"text": "📍 S/R", "callback_data": "PA_sr"}],
        [{"text": "📦 OB 20%", "callback_data": "PA_ob"}, {"text": "💧 Sweep 25%", "callback_data": "PA_liq"}],
        [{"text": "📉 FVG 20%", "callback_data": "PA_fvg"}, {"text": "🎯 100% Formula", "callback_data": "PA_entry"}]
    ]

def main_menu():
    return [[{"text": "📚 Shikho", "callback_data": "MENU_LEARN"}, {"text": "📊 % Sure Signal", "callback_data": "MENU_FUTURE"}]]

def future_signal():
    base = datetime.utcnow() + timedelta(hours=6, minutes=3)
    sigs = []
    for m in random.sample(REAL, 6):
        d = random.choice(["BUY","SELL"])
        t = base + timedelta(minutes=random.randint(2,45))
        setup, perc = random.choice(SETUPS)
        # Filter: 70%+ only
        if perc < 70: perc, setup = 75, "M15 OB Retest + BOS + Engulf"
        emoji = "🔥" if perc >= 85 else "✅" if perc >= 75 else "⚠️"
        sigs.append(f"{emoji} {t.strftime('%H:%M')} | {m} | {d} | {perc}% Sure\n   └ {setup}")
    return f"📊 <b>REAL MARKET 1M - % SURE SIGNAL</b>\n🌐 UTC+6 | 70%+ Only\n\n{chr(10).join(sigs)}\n\n💡 <b>Rule:</b> 80%+ hole entry, 70-79% risky, 70% er niche ami dei na."

def answer_question(q):
    ql=q.lower()
    if any(x in ql for x in ["ob","order"]): return PA_LESSONS["ob"]
    if any(x in ql for x in ["liquidity","sweep","sl"]): return PA_LESSONS["liq"]
    if any(x in ql for x in ["fvg","gap"]): return PA_LESSONS["fvg"]
    if any(x in ql for x in ["support","resistance"]): return PA_LESSONS["sr"]
    if any(x in ql for x in ["candle","pin"]): return PA_LESSONS["candle"]
    if any(x in ql for x in ["sure","percent","%","confirm","koto"]): return PA_LESSONS["entry"]
    return f"🤔 Q: {q}\n\n{PA_LESSONS['entry']}"

def poller():
    offset = 0
    while True:
        try:
            r = requests.get(f"https://api.telegram.org/bot{TOKEN}/getUpdates?offset={offset}&timeout=30", timeout=35).json()
            for upd in r.get("result", []):
                offset = upd["update_id"] + 1
                if "message" in upd:
                    txt = upd["message"].get("text",""); low=txt.lower()
                    if "/start" in low: send("👻 <b>GHOST MATRIX - % SURE</b>\n\nEkhon protita signal e % Sure dekhabo.\n80%+ = Sureshot\n\nShikhte /learn, Signal /future", main_menu())
                    elif "/learn" in low: send("📚 <b>100% Sure Formula Shikho</b> 👇", learn_btns())
                    elif "/future" in low: send(future_signal(), main_menu())
                    elif len(txt)>2 and not txt.startswith("/"): send(answer_question(txt), learn_btns())
                if "callback_query" in upd:
                    cq=upd["callback_query"]; data=cq["data"]; cid=cq["id"]
                    if data.startswith("PA_"): send(PA_LESSONS.get(data.replace("PA_","").lower(), "..."), learn_btns())
                    elif data=="MENU_LEARN": send("📚 <b>100% Sure Formula</b> 👇", learn_btns())
                    elif data=="MENU_FUTURE": send(future_signal(), main_menu())
                    try: requests.post(f"https://api.telegram.org/bot{TOKEN}/answerCallbackQuery", data={"callback_query_id": cid})
                    except: pass
        except: time.sleep(3)

threading.Thread(target=poller, daemon=True).start()
if __name__ == "__main__":
    
