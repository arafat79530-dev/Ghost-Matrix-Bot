import os, requests, random, time, json
from datetime import datetime, timedelta
import threading

TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = "8879968219"

MARKETS = ["USDCAD","USDCHF","NZDUSD","EURJPY","GBPJPY","CADJPY","CADCHF","EURGBP","AUDJPY","EURAUD","GBPAUD","XAUUSD","XAGUSD","WTI","Brent","NATGAS","Copper","Platinum","Palladium"]

def send(text, buttons=None):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    data = {"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"}
    if buttons:
        data["reply_markup"] = json.dumps({"inline_keyboard": buttons})
    try: requests.post(url, data=data, timeout=10)
    except: pass

def future_msg():
    base = datetime.utcnow() + timedelta(hours=6, minutes=3)
    sigs = []
    for m in random.sample(MARKETS, 10):
        d = random.choice(["BUY","PUT"])
        t = base + timedelta(minutes=random.randint(2,70))
        sigs.append(f"M1 {m} {t.strftime('%H:%M')} {d}")
    return f"📊 Matrix Future Signal\n🌐 TIME ZONE - ( UTC +6:00 )\n➡️ 1 STEP MTG IF NEED\n➡️ 1 MINUTES\n\n{chr(10).join(sigs)}\n\n👻 GHOST MATRIX"

def news_msg():
    n = random.choice(["🔴 HIGH IMPACT - USD CPI 19:30 UTC+6","🔴 FOMC 00:00 - Gold Alert","🟡 No High News - Safe Trade","📈 Gold Bullish"])
    return f"📰 Matrix News Alert\n🌐 TIME: {(datetime.utcnow()+timedelta(hours=6)).strftime('%H:%M')} UTC+6\n\n{n}\n\n👻 GHOST MATRIX"

def menu():
    text = "👻 GHOST MATRIX BOT\n\n📊 /Future - Future Signal\n📰 /News - News Alert\n\nNiche button 👇"
    btns = [[{"text": "📊 Future Signal", "callback_data": "FUTURE"}], [{"text": "📰 News Alert", "callback_data": "NEWS"}]]
    send(text, btns)

def poller():
    offset = 0
    while True:
        try:
            url = f"https://api.telegram.org/bot{TOKEN}/getUpdates?offset={offset}&timeout=30"
            res = requests.get(url, timeout=35).json()
            for upd in res.get("result", []):
                offset = upd["update_id"] + 1
                if "message" in upd:
                    txt = upd["message"].get("text","")
                    if "/Future" in txt or "/future" in txt: send(future_msg())
                    elif "/News" in txt or "/news" in txt: send(news_msg())
                    elif "/start" in txt: menu()
                if "callback_query" in upd:
                    d = upd["callback_query"]["data"]
                    if d == "FUTURE": send(future_msg())
                    if d == "NEWS": send(news_msg())
                    requests.post(f"https://api.telegram.org/bot{TOKEN}/answerCallbackQuery", data={"callback_query_id": upd["callback_query"]["id"]})
        except: time.sleep(3)

threading.Thread(target=poller, daemon=True).start()
time.sleep(2)
menu()
while True:
    time.sleep(300)
    send(future_msg())
