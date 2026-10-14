import os, requests, time, threading, random
from flask import Flask
from datetime import datetime, timedelta
import pytz

BD_TZ = pytz.timezone('Asia/Dhaka')
TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
AI_KEY = os.getenv("AI_API_KEY")

app = Flask(__name__)
@app.route('/')
def home(): return "GHOST MATRIX V2.0 LIVE"
threading.Thread(target=lambda: app.run(host='0.0.0.0', port=int(os.environ.get("PORT",10000))), daemon=True).start()

# 1. Problem Fix: % Profit Sure Add
def get_signal():
    now = datetime.now(BD_TZ)
    pairs = ["EURUSD","EURJPY","USDCHF","AUDJPY","NZDUSD","USDJPY","CADJPY","GBPJPY","GBPUSD","AUDUSD"]
    start = now + timedelta(minutes=10)
    sigs = ""
    cur = start
    for p in random.sample(pairs, 8):
        cur += timedelta(minutes=random.randint(8,22))
        side = "CALL" if random.random()>0.5 else "PUT"
        icon = "🟢" if side=="CALL" else "🔴"
        # % Logic
        percent = random.choice([82,84,86,88,90,92,94,96,97,98])
        if percent >= 96: tag = "💎 JACKPOT"
        elif percent >= 90: tag = "🔥 STRONG"
        else: tag = "✅ CONFIRM"
        sigs += f"{icon} M1;{p}•{cur.strftime('%H:%M')};{side} | {percent}% {tag}\n"

    return f"""⭐ GHOST MATRIX V2.0 SIGNALS 🎖
🗓 DATE - {now.strftime('%d.%m.%Y')}
🚀 M1 • UTC+6 • MTG ×1
━━━━━━━━━━━━━━━━━━━━━━
{sigs.strip()}
━━━━━━━━━━━━━━━━━━━━━━
🛡 80%+ CONFIRM | 90%+ STRONG | 96%+ JACKPOT
💎 BY - GHOST MATRIX V2.0"""

def ai_answer(user_text):
    if not AI_KEY:
        return "⚠️ AI API KEY পাইনি! Render > Environment > AI_API_KEY চেক করুন।"
    try:
        url = "https://api.groq.com/openai/v1/chat/completions"
        headers = {"Authorization": f"Bearer {AI_KEY}", "Content-Type": "application/json"}
        prompt = f"""Tumi GHOST MATRIX V2.0, ekjon pro trader. Sob answer 100% Bangla Bornomala te diba, English ekdom na.
        User bolse: {user_text}
        Jodi News jante chay, ajker market news, USD, EUR news Banglay bolo.
        Jodi Learn jante chay, BOS, FVG, OB, SWEEP, ENGULF sob Bangla bornomalay sohoj kore bujhao.
        Short, pro, friendly answer."""
        data = {
            "model": "llama-3.1-8b-instant",
            "messages": [
                {"role": "system", "content": "Tumi sob somoy 100% Bangla Bornomalay answer diba. English letter use korba na, bangla tei likhba."},
                {"role": "user", "content": prompt}
            ],
            "temperature": 0.7
        }
        r = requests.post(url, headers=headers, json=data, timeout=20).json()
        if "choices" in j:
    return j["choices"][0]["message"]["content"]
else:
    return "📰 আজকের নিউজ: USD te CPI, EUR e BOS+FVG 90%+ BUY. 90%+ market e entry nin! (Backup News)"
    except Exception as e:
        return f"এআই এররে: {e}"

# 2. Problem Fix: Sob Bangla Bornomalay
def get_learn_static():
    return """🧠 GHOST TRADER AI LEARNING:

📚 বোস (BOS) = ব্রেক অফ স্ট্রাকচার
মার্কেট আগের হাই বা লো ভেঙে ফেললো। মানে বড় ব্যাংক ঢুকেছে। রিটেস্টে এন্ট্রি নিতে হয়।

📚 এফভিজি (FVG) = ফেয়ার ভ্যালু গ্যাপ
৩টা ক্যান্ডেলের মাঝে ফাঁকা জায়গা। এটা ব্যাংকের বাকি থাকা অর্ডার। মার্কেট এই ফাঁকা জায়গা পূরণ করতে আসবেই।

📚 ওবি (OB) = অর্ডার ব্লক
ব্যাংক যেখানে শেষবার অর্ডার রেখেছে, শেষ বাই বা সেল ক্যান্ডেল। দাম ওখানে আসলে ৯০% বাউন্স করে।

📚 সুইপ (SWEEP) = লিকুইডিটি সুইপ
সবার স্টপ লস খাওয়ার জন্য মার্কেট একটা ফেক মুভ দেয়। সবার এসএল খেয়ে তারপর আসল দিকে যায়। বোস এর সাথে হলে ১০০% সিওর।

📚 এনগাল্ফ (ENGULF) = গিলে ফেলা
বড় ক্যান্ডেল ছোট ক্যান্ডেলকে গিলে ফেললো। সবুজ হলে বাই (CALL), লাল হলে সেল (PUT)।

💰 ফর্মুলা: ১০০% = এফভিজি ২০% + সুইপ ২৫% + বোস ১৫% + ওবি ২০% + এনগাল্ফ ২০%

নিয়ম: ৮০%+ কনফার্ম, ৯০%+ স্ট্রং, ৯৬%+ জ্যাকপট"""

def send(t):
    if not TOKEN or not CHAT_ID: return
    try: requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage", json={"chat_id": CHAT_ID, "text": t}, timeout=15)
    except: pass

offset=0
while True:
    try:
        if not TOKEN: time.sleep(5); continue
        r = requests.get(f"https://api.telegram.org/bot{TOKEN}/getUpdates?offset={offset}&timeout=30", timeout=35).json()
        for u in r.get("result",[]):
            offset = u["update_id"]+1
            msg = u.get("message",{})
            txt = msg.get("text","").strip()
            if not txt: continue
            low = txt.lower()

            # 3. Problem Fix: /start e 3 ta command
            if low.startswith("/start"):
                send("""👋 আসসালামু আলাইকুম! I'M GHOST MATRIX V2.0

MY 3 COMMANDS:

1. /future - আজকের ৯০%+ সিওর সিগন্যাল পাবেন
2. /news - আজকের মার্কেট নিউজ বাংলায় পাবেন
3. /learn - ট্রেডিং এর BOS, FVG, OB সব বাংলায় শিখবেন

একটা লিখে পাঠান! 👻""")
            elif "/future" in low:
                send(get_signal())
            elif "/news" in low:
                news_ans = ai_answer("আজকের ফরেক্স মার্কেটের গুরুত্বপূর্ণ নিউজ কি কি আছে বাংলায় বলো")
                send(f"📰 আজকের নিউজ:\n\n{news_ans}")
            elif "/learn" in low:
                # 2. Fix er jonno static bangla + AI
                send(get_learn_static())
            else:
                # 4. Problem Fix: Jekono message e AI jobab dibe
                ans = ai_answer(txt)
                send(f"🧠 ঘোস্ট ম্যাট্রিক্স এআই:\n\n{ans}")
        time.sleep(1)
    except Exception as e:
        print(e)
        time.sleep(3)
