import os, requests, time, threading
from flask import Flask
from datetime import datetime, timedelta
import pytz
import base64
import yfinance as yf  # রিয়েল মার্কেট ডেটার জন্য

BD_TZ = pytz.timezone('Asia/Dhaka')
TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
AI_KEY = os.getenv("AI_API_KEY")

# আপনার Quotex Affiliate Link এখানে বসাবেন
QUOTEX_AFFILIATE_LINK = "https://broker-qx.pro/sign-up/?lid=2384783"

app = Flask(__name__)
@app.route('/')
def home(): return "GHOST MATRIX V2.0 (QUOTEX AFFILIATE PRO) LIVE"

threading.Thread(target=lambda: app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 10000))), daemon=True).start()

# Yahoo Finance থেকে রিয়েল ডেটা এনে এআই এর মাধ্যমে সিগন্যাল জেনারেট করার ফাংশন
def get_real_market_signals():
    if not AI_KEY:
        return "⚠️ AI API KEY পাওয়া যায়নি!"
    try:
        tickers = ["EURUSD=X", "GBPUSD=X", "USDJPY=X", "AUDUSD=X", "USDCAD=X"]
        market_summary = ""
        
        for ticker in tickers:
            data = yf.download(ticker, period="5d", interval="1h", progress=False)
            if not data.empty:
                last_row = data.iloc[-1]
                close_price = float(last_row['Close'].iloc[0]) if hasattr(last_row['Close'], 'iloc') else float(last_row['Close'])
                market_summary += f"Pair: {ticker.replace('=X','')}, Latest Close Price: {close_price:.4f}\n"

        url = "https://api.groq.com/openai/v1/chat/completions"
        headers = {"Authorization": f"Bearer {AI_KEY}", "Content-Type": "application/json"}
        prompt = f"""Tumi ekjon expert Binary Options (Quotex) Signal Generator. Niche forex market er recent real data dewa holo:
        {market_summary}
        
        Ei data gulo analyze kore 4-5 ti active currency pair er binary options signal (CALL ba PUT) toiri koro. 
        Format kivabe hobe niche dewa holo:
        🟢 M1;EURUSD•11:30;CALL | 92% 💎 STRONG
        🔴 M1;GBPUSD•11:45;PUT | 95% 🔥 JACKPOT
        
        Sob somoy 100% Bangla Bornomala use korba, English letter diye kono boro description likhba na, sudhu signal list ti sundor kore sajie diba."""

        data_payload = {
            "model": "llama-3.1-8b-instant",
            "messages": [
                {"role": "system", "content": "Tumi ekjon professional trading bot. Sudhu requested format e 100% accurate real signal ba data diba."},
                {"role": "user", "content": prompt}
            ],
            "temperature": 0.4
        }
        
        response = requests.post(url, headers=headers, json=data_payload, timeout=25)
        res_json = response.json()
        
        if "choices" in res_json:
            ai_output = res_json["choices"][0]["message"]["content"]
            now = datetime.now(BD_TZ)
            return f"""⭐ GHOST MATRIX V2.0 (REAL MARKET SIGNALS) 🎖
🗓 DATE - {now.strftime('%d.%m.%Y')}
🚀 M1 • UTC+6 • Quotex MTG ×1
━━━━━━━━━━━━━━━━━━━━━━
{ai_output}
━━━━━━━━━━━━━━━━━━━━━━
🛡 রিয়েল ডেটা ও প্রাইস অ্যাকশন বেসড
👉 একাউন্ট খুলুন ও ট্রেড করুন: {QUOTEX_AFFILIATE_LINK}
💎 BY - GHOST MATRIX V2.0"""
        else:
            return "⚠️ রিয়েল ডেটা প্রসেস করতে সমস্যা হয়েছে। দয়া করে আবার চেষ্টা করুন।"
    except Exception as e:
        return f"সিগন্যাল জেনারেট এরর: {e}"

def ai_answer(user_text):
    if not AI_KEY:
        return "⚠️ AI API KEY পাওয়া যায়নি!"
    try:
        url = "https://api.groq.com/openai/v1/chat/completions"
        headers = {"Authorization": f"Bearer {AI_KEY}", "Content-Type": "application/json"}
        prompt = f"""Tumi GHOST MATRIX V2.0, ekjon master Price Action & Smart Money Concept (SMC) Trader. Sob answer 100% Bangla Bornomala te diba, English ekdom na.
        User bolse: {user_text}
        Jodi News jante chay, ajker market news, USD, EUR news Banglay bolo.
        Jodi Learn jante chay, Price Action, BOS, FVG, OB, Support-Resistance sob Bangla bornomalay sohoj kore bujhao.
        Short, pro, friendly answer."""
        data = {
            "model": "llama-3.1-8b-instant",
            "messages": [
                {"role": "system", "content": "Tumi sob somoy 100% Bangla Bornomalay answer diba. English letter use korba na, bangla tei likhba."},
                {"role": "user", "content": prompt}
            ],
            "temperature": 0.7
        }
        response = requests.post(url, headers=headers, json=data, timeout=20)
        res_json = response.json()
        
        if "choices" in res_json:
            return res_json["choices"][0]["message"]["content"]
        else:
            return "📰 আজকের নিউজ: মার্কেটে প্রাইস অ্যাকশন রুল মেনে ট্রেড করুন।"
    except Exception as e:
        return f"এআই এররে: {e}"

# চার্ট স্ক্রিনশট প্রাইস অ্যাকশন ও ভিশন এনালাইসিস করার ফাংশন
def analyze_chart_image(image_bytes):
    if not AI_KEY:
        return "⚠️ AI API KEY পাওয়া যায়নি!"
    try:
        base64_image = base64.b64encode(image_bytes).decode('utf-8')
        url = "https://api.groq.com/openai/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {AI_KEY}",
            "Content-Type": "application/json"
        }
        data = {
            "model": "llama-3.2-11b-vision-preview",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text", 
                            "text": "তুমি একজন মাস্টার প্রাইস অ্যাকশন ট্রেডার। এই চার্টের স্ক্রিনশটটি খুব ভালোভাবে এনালাইসিস করে নিচে পয়েন্ট আকারে বাংলায় উত্তর দাও:\n১. মার্কেটের বর্তমান ট্রেন্ড কি?\n২. কোনো সাপোর্ট বা রেজিস্ট্যান্স জোন আছে কি না?\n৩. ব্রেক অফ স্ট্রাকচার (BOS) বা ফেয়ার ভ্যালু গ্যাপ (FVG) আছে কি না?\n৪. পরবর্তী ক্যান্ডেলের জন্য কি CALL (BUY) নাকি PUT (SELL) এন্ট্রি নেওয়া উচিত এবং কেন?"
                        },
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/jpeg;base64,{base64_image}"
                            }
                        }
                    ]
                }
            ],
            "temperature": 0.3,
            "max_tokens": 600
        }
        response = requests.post(url, headers=headers, json=data, timeout=30)
        res_json = response.json()
        if "choices" in res_json:
            return res_json["choices"][0]["message"]["content"]
        else:
            return "⚠️ চার্ট অ্যানালিসিস করতে সমস্যা হয়েছে, অনুগ্রহ করে পরিষ্কার স্ক্রিনশট দিন।"
    except Exception as e:
        return f"ভিশন এআই এরর: {e}"

def get_learn_static():
    return """🧠 প্রাইস অ্যাকশন ও লার্নিং গাইডলাইন:

📚 ১. সাপোর্ট ও রেজিস্ট্যান্স:
যেখান থেকে মার্কেট বারবার ধাক্কা খেয়ে ফিরে আসে। এখানে পিন বার বা রিজেকশন দেখলে এন্ট্রি নিতে হয়।

📚 ২. বোস (BOS - Break of Structure):
মার্কেট যখন তার আগের হাই বা লো ভেঙে সামনের দিকে এগিয়ে যায়। এটি শক্তিশালী ট্রেন্ডের লক্ষণ।

📚 ৩. অর্ডার ব্লক (OB):
বড় বড় ব্যাংক বা ইনস্টিটিউশনগুলোর অর্ডার দেওয়ার জোন। দাম এই জোনে ফিরে আসলে শক্তিশালী বাউন্স করে।

📚 ৪. এফভিজি (FVG - Fair Value Gap):
দ্রুত মুভমেন্টের কারণে ক্যান্ডেলগুলোর মাঝখানে তৈরি হওয়া ফাঁকা জায়গা, যা পূরণ করতে মার্কেট পরে আবার ফিরে আসে।

💡 পরামর্শ: এই বিষয়গুলো আয়ত্ত করতে প্রতিদিন ডেমো চার্টে অন্তত ১ ঘণ্টা প্র্যাকটিস করুন!"""

def send(t):
    if not TOKEN or not CHAT_ID: 
        return
    try: 
        requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage", json={"chat_id": CHAT_ID, "text": t}, timeout=15)
    except: 
        pass

offset = 0
while True:
    try:
        if not TOKEN: 
            time.sleep(5)
            continue
            
        r = requests.get(f"https://api.telegram.org/bot{TOKEN}/getUpdates?offset={offset}&timeout=30", timeout=35).json()
        for u in r.get("result", []):
            offset = u["update_id"] + 1
            msg = u.get("message", {})
            
            if "photo" in msg:
                send("🔍 প্রাইস অ্যাকশন ও চার্ট স্ক্যান করা হচ্ছে... একটু অপেক্ষা করুন!")
                photo_file_id = msg["photo"][-1]["file_id"]
                file_path_res = requests.get(f"https://api.telegram.org/bot{TOKEN}/getFile?file_id={photo_file_id}", timeout=15).json()
                if "result" in file_path_res:
                    file_path = file_path_res["result"]["file_path"]
                    img_url = f"https://api.telegram.org/file/bot{TOKEN}/{file_path}"
                    img_data = requests.get(img_url, timeout=20).content
                    
                    analysis_result = analyze_chart_image(img_data)
                    send(f"📊 প্রাইস অ্যাকশন অ্যানালিসিস রেজাল্ট:\n\n{analysis_result}")
                continue

            txt = msg.get("text", "").strip()
            if not txt: 
                continue
            low = txt.lower()

            if low.startswith("/start"):
                send("""👋 আসসালামু আলাইকুম! GHOST MATRIX V2.0 (QUOTEX AFFILIATE PRO)

COMMANDS:
1. /future - রিয়েল মার্কেট ডেটা বেসড সিগন্যাল
2. /news - মার্কেট নিউজ আপডেট
3. /learn - প্রাইস অ্যাকশন ও এসএমসি শিখুন
4. 📸 **চার্ট স্ক্রিনশট পাঠান** - প্রাইস অ্যাকশন এনালাইসিস করে সিগন্যাল দিবে!

👉 ট্রেড শুরু করতে ভিজিট করুন: """ + QUOTEX_AFFILIATE_LINK)
            elif "/future" in low:
                send("⏳ রিয়েল মার্কেট ডেটা ফেচ ও অ্যানালিসিস করা হচ্ছে, একটু অপেক্ষা করুন...")
                signal_text = get_real_market_signals()
                send(signal_text)
            elif "/news" in low:
                news_ans = ai_answer("আজকের ফরেক্স মার্কেটের গুরুত্বপূর্ণ নিউজ কি কি আছে বাংলায় বলো")
                send(f"📰 আজকের নিউজ ও মার্কেট আপডেট:\n\n{news_ans}")
            elif "/learn" in low:
                send(get_learn_static())
            else:
                ans = ai_answer(txt)
                send(f"🧠 ঘোস্ট ম্যাট্রিক্স এআই:\n\n{ans}")
        time.sleep(1)
    except Exception as e:
        print(e)
        time.sleep(3)
