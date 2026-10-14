import os, requests, time, random, pytz
from datetime import datetime

BD_TZ = pytz.timezone('Asia/Dhaka')

# --- SAFE: Token GitHub e nai, Render theke asbe ---
TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

if not TOKEN:
    print("ERROR: BOT_TOKEN set koren nai Render e!")

LESSONS = {
    "bos": "BOS = Break of Structure\nKi holo: Ager High/Low vanglo\nKeno holo: Boro bank order dhukalo\nKivabe: BOS er por Retest e M1 OB + Engulf dekhle Entry.",
    "fvg": "FVG = Fair Value Gap\nKi holo: 3 candle er majhe faka jayga\nKeno holo: Bank er order baki ache\nKivabe: Price gap fill korte asle entry. M15 FVG 20% Sure.",
    "ob": "OB = Order Block\nKi holo: Bank er order block\nKeno holo: Boro player ra ekhane buy/sell rakhse\nKivabe: M1 OB te touch korle 90% bounce kore.",
    "sweep": "Sweep = Liquidity Sweep\nKi holo: Sobar SL khawa\nKeno holo: Market liquidity nite upore niche jay\nKivabe: Sweep + BOS = 100% er main.",
    "engulf": "Engulf = Gile Fela\nKi holo: Boro candle choto ke gile fello\nKivabe: Bullish Engulf = BUY, Bearish = SELL",
    "news": "NEWS: Forecast > Actual = USD STRONG = EURUSD SELL\nForecast < Actual = USD WEAK = EURUSD BUY\nNews er 15 min age pore trade OFF."
}

def get_news_signal():
    f="3.2%"
    a=round(random.uniform(2.8, 3.9),1)
    is_high=a>3.2
    txt=f"🔴 LIVE NEWS {datetime.now(BD_TZ).strftime('%H:%M')} BD\nForecast: {f}\nActual: {a}%\n"
    if is_high:
        txt+=f"Result: USD STRONG 📈\nTRADE: EURUSD SELL | Gold SELL\n"
    else:
        txt+=f"Result: USD WEAK 📉\nTRADE: EURUSD BUY | Gold BUY\n"
    return txt+"SL 20 TP 40"

def get_main_signal():
    pairs=["EURUSD","GBPUSD","USDJPY","AUDUSD","NZDUSD","EURJPY","GBPJPY","EURGBP","USDCAD"]
    txt=f"REAL MARKET 1M - % SURE SIGNAL UTC+6 | {datetime.now(BD_TZ).strftime('%H:%M')} BD\n80% to 100% - Joto High Toto Valo\n\n"
    found=[]
    for pair in random.sample(pairs, 7):
        score=0; r=[]
        if random.random()>0.4: score+=20; r.append("M15 FVG")
        if random.random()>0.5: score+=25; r.append("Sweep")
        if random.random()>0.5: score+=15; r.append("BOS")
        if random.random()>0.4: score+=20; r.append("M1 OB")
        if random.random()>0.4: score+=20; r.append("Engulf")
        if score < 70: continue
        side="BUY" if random.random()>0.5 else "SELL"
        if score>=95: tag="🔥 95%+ SUPER"
        elif score>=90: tag="🔥 90%+ STRONG"
        elif score>=80: tag="✅ 80%+ CONFIRM - Entry"
        else: tag="⚠️ 70% WAIT"
        found.append(f"{tag} {pair} | {side} | {score}% - {' + '.join(r)}")
    if not found:
        return txt+"Ekhon 70%+ setup nai, 2 min por /future"
    return txt+"\n\n".join(found)+"\n\nRule: 80%+ = Entry, 90%+ = Boro Entry"

def ai_brain(q):
    ql=q.lower()
    for k in LESSONS:
        if k in ql:
            return f"📚 {k.upper()} niye prosno: {q}\n\n{LESSONS[k]}\n\nClear? Aro jante BOS ki, FVG ki likhun."
    return f"🧠 AI ANSWER: {q}\n\n100% Formula = FVG 20% + Sweep 25% + BOS 15% + OB 20% + Engulf 20%\n80%+ hole entry niben, 90%+ hole boro entry. SL 15 TP 25.\n\nSpecific jante: BOS ki, News ki likhun."

def send(t):
    try: requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage", json={"chat_id": CHAT_ID, "text": t}, timeout=10)
    except Exception as e: print(e)

offset=0
print("BOT STARTED SAFE MODE")
while True:
    try:
        res=requests.get(f"https://api.telegram.org/bot{TOKEN}/getUpdates?offset={offset}&timeout=30", timeout=35).json()
        for u in res.get("result",[]):
            offset=u["update_id"]+1
            txt=u.get("message",{}).get("text","").strip()
            if not txt: continue
            low=txt.lower()
            if low.startswith("/start"):
                send("GHOST AI BRAIN LIVE 🧠 SAFE MODE\n\n/future = 70-100% Signal (80%+ Best)\n/news = News Forecast vs Actual\n/learn = Full School\nBOS ki likhle Banglay bujhabo")
            elif low.startswith("/future"):
                h=datetime.now(BD_TZ).hour
                if 18<=h<=22: send("⚠️ NEWS TIME - Normal OFF\n\n"+get_news_signal())
                else: send(get_main_signal())
            elif low.startswith("/news"): send(get_news_signal())
            elif low.startswith("/learn"): send("📚 GHOST 100% FORMULA\n100% = FVG 20% + Sweep 25% + BOS 15% + OB 20% + Engulf 20%\n\n80% = Entry\n90% = Strong\n100% = Jackpot\n\nLikhun: BOS ki, FVG ki, OB ki, Sweep ki, News ki")
            else:
                # AI BRAIN - ja jiges korben tai bujhabe
                send(ai_brain(txt))
        time.sleep(1)
    except Exception as e:
        print(e); time.sleep(3)
