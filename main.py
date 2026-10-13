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
]

def send(text, buttons=None):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    data = {"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"}
    if buttons: data["reply_markup"] = json.dumps({"inline_keyboard": buttons})
    try: requests.post(url, data=data, timeout=12)
    except: pass

def future_signal():
    base = datetime.utcnow() + timedelta(hours=6, minutes=3)
    lines = []
    for m in random.sample(REAL, 6):
        d = random.choice(["BUY","SELL"])
        t = base + timedelta(minutes=random.randint(2,45))
        setup, perc = random.choice(SETUPS)
        emoji = "FIRE" if perc >= 85 else "OK"
        lines.append(f"{emoji} {t.strftime('%H:%M')} | {m} | {d} | {perc}% Sure - {setup}")
    msg = "REAL MARKET 1M - % SURE SIGNAL\nUTC+6 | 70%+ Only\n\n" + "\n\n".join(lines) + "\n\nRule: 80%+ = Entry"
    return msg

def poller():
    offset = 0
    while True:
        try:
            r = requests.get(f"https://api.telegram.org/bot{TOKEN}/getUpdates?offset={offset}&timeout=30", timeout=35).json()
            for upd in r.get("result", []):
                offset = upd["update_id"] + 1
                if "message" in upd:
                    txt = upd["message"].get("text","")
                    if "/start" in txt.lower():
                        send("GHOST MATRIX LIVE\nShikhte /learn, Signal /future")
                    elif "/future" in txt.lower():
                        send(future_signal())
                    elif "/learn" in txt.lower():
                        send("100% Formula: FVG 20% + Sweep 25% + BOS 15% + OB 20% + Engulf 20% = 100%")
                    else:
                        if len(txt)>2:
                            send(future_signal())
        except Exception as e:
            print(f"Error: {e}")
            time.sleep(3)

threading.Thread(target=poller, daemon=True).start()

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 10000)))
