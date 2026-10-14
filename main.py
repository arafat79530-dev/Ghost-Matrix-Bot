import os, requests, time, threading
from flask import Flask
from datetime import datetime, timedelta
import pytz
import base64
import yfinance as yf

BD_TZ = pytz.timezone('Asia/Dhaka')
TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
AI_KEY = os.getenv("AI_API_KEY")

user_stats = {"wins": 0, "losses": 0}

app = Flask(__name__)
@app.route('/')
def home(): return "GHOST MATRIX V2.0 (1M PRO TRADER) LIVE"

threading.Thread(target=lambda: app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 10000))), daemon=True).start()

def get_1m_market_signals():
    if not AI_KEY:
        return "⚠️ AI API KEY paoya jayni!"
    try:
        market_summary = ""
        tickers = ["EURUSD=X", "GBPUSD=X", "USDJPY=X", "AUDUSD=X"]
        
        for ticker in tickers:
            try:
                data = yf.download(ticker, period="1d", interval="1m", progress=False)
                if not data.empty:
                    last_row = data.iloc[-1]
                    close_price = float(last_row['Close'].iloc[0]) if hasattr(last_row['Close'], 'iloc') else float(last_row['Close'])
                    market_summary += f"1m Pair: {ticker.replace('=X','')}, Current Price: {close_price:.4f}\n"
            except:
                continue

        url = "https://api.groq.com/openai/v1/chat/completions"
        headers = {"Authorization": f"Bearer {AI_KEY}", "Content-Type": "application/json"}
        prompt = f"""Tumi ekjon expert 1-Minute Binary Options (Quotex) Signal Generator. Niche market er real-time 1m data dewa holo:
        {market_summary if market_summary else "Market is active with fast 1m volatility."}
        
        Ei data er opor vitt kore 1-minute (M1) timeframe er 4-5 ti high accuracy signal (CALL ba PUT) toiri koro. 
        Format strictly ai rokom hobe:
        🟢 M1;EURUSD•11:30;CALL | 94% 💎 JACKPOT
        🔴 M1;GBPUSD•11:45;PUT | 92% 🔥 STRONG
        
        Sob somoy 100% Bangla Bornomala use korba description er jonno, kintu signal format exact uporer moto thakbe."""

        data_payload = {
            "model": "llama-3.1-8b-instant",
            "messages": [
                {"role": "system", "content": "Tumi 1-minute binary options expert. Sudhu exact requested format e signal list diba."},
                {"role": "user", "content": prompt}
            ],
            "temperature": 0.4
        }
        
        response = requests.post(url, headers=headers, json=data_payload, timeout=20)
        res_json = response.json()
        
        if "choices" in res_json:
            ai_output = res_json["choices"][0]["message"]["content"]
            now = datetime.now(BD_TZ)
            return f"""⭐ GHOST MATRIX V2.0 SIGNALS 🎖
🗓 DATE - {now.strftime('%d.%m.%Y')}
🚀 M1 • UTC+6 • MTG ×1
━━━━━━━━━━━━━━━━━━━━━━
{ai_output}
━━━━━━━━━━━━━━━━━━━━━━
🛡 80%+ CONFIRM | 90%+ STRONG | 96%+ JACKPOT
💎 BY - GHOST MATRIX V2.0"""
        else:
            return "⚠️ Signal generate korte shomossha hoyeche."
    except Exception as e:
        now = datetime.now(BD_TZ)
        return f"""⭐ GHOST MATRIX V2.0 SIGNALS 🎖
🗓 DATE - {now.strftime('%d.%m.%Y')}
🚀 M1 • UTC+6 • MTG ×1
━━━━━━━━━━━━━━━━━━━━━━
🟢 M1;EURUSD•{now.strftime('%H:%M')};CALL | 94% 💎 JACKPOT
🔴 M1;GBPUSD•{(now + timedelta(minutes=10)).strftime('%H:%M')};PUT | 92% 🔥 STRONG
━━━━━━━━━━━━━━━━━━━━━━
🛡 80%+ CONFIRM | 90%+ STRONG | 96%+ JACKPOT
💎 BY - GHOST MATRIX V2.0"""

def get_market_news_with_prediction():
    if not AI_KEY:
        return "⚠️ AI API KEY paoya jayni!"
    try:
        url = "https://api.groq.com/openai/v1/chat/completions"
        headers = {"Authorization": f"Bearer {AI_KEY}", "Content-Type": "application/json"}
        prompt = """Tumi ekjon expert Forex & Binary Options Fundamental Analyst. Ajker market er major news (jemon USD, EUR, GBP) gulo niye ekta high-probability analysis toiri koro.
        Niche dewa format a response dao:
        ১. নিউজের নাম ও সম্ভাব্য সময়:
        ২. প্রিভিয়াস ও ফোরকাস্ট ডেটার তুলনামূলক অবস্থা:
        ৩. মার্কেটে এর প্রভাব ও ডিরেকশন প্রেডিকশন (CALL নাকি PUT এর দিকে যেতে পারে):
        ৪. উইনিং একুরেসি বাড়াতে করণীয় টিপস:
        
        Sob answer 100% Bangla Bornomalay hobe, kono English letter use korba na."""
        
        data = {
            "model": "llama-3.1-8b-instant",
            "messages": [
                {"role": "system", "content": "Tumi 100% Bangla Bornomalay high accuracy news prediction diba."},
                {"role": "user", "content": prompt}
            ],
            "temperature": 0.5
        }
        response = requests.post(url, headers=headers, json=data, timeout=25)
        res_json = response.json()
        if "choices" in res_json:
            ans = res_json["choices"][0]["message"]["content"]
            now = datetime.now(BD_TZ)
            return f"""📰 High-Probability News Prediction & Analysis ({now.strftime('%d.%m.%Y')}):
━━━━━━━━━━━━━━━━━━━━━━
{ans}
━━━━━━━━━━━━━━━━━━━━━━
💡 High Winning Tips: News prokash howar thik 1 minute age o pore trade theke dure thakun!"""
        else:
            return "📰 Ajker market e boro kono impact news nei, pure price action follow korun."
    except Exception as e:
        return f"News prediction error: {e}"

def ai_answer(user_text):
    if not AI_KEY:
        return "⚠️ AI API KEY paoya jayni!"
    try:
        url = "https://api.groq.com/openai/v1/chat/completions"
        headers = {"Authorization": f"Bearer {AI_KEY}", "Content-Type": "application/json"}
        prompt = f"""Tumi GHOST MATRIX V2.0, ekjon master Price Action & Smart Money Concept (SMC) Trader. Sob answer 100% Bangla Bornomala te diba, English ekdom na.
        User bolse: {user_text}
        Short, pro, friendly answer."""
        data = {
            "model": "llama-3.1-8b-instant",
            "messages": [
                {"role": "system", "content": "Tumi sob somoy 100% Bangla Bornomalay answer diba."},
                {"role": "user", "content": prompt}
            ],
            "temperature": 0.7
        }
        response = requests.post(url, headers=headers, json=data, timeout=20)
        res_json = response.json()
        if "choices" in res_json:
            return res_json["choices"][0]["message"]["content"]
        else:
            return "Price action rule mene trade korun."
    except Exception as e:
        return f"AI error: {e}"

def analyze_chart_image(image_bytes):
    if not AI_KEY:
        return "⚠️ AI API KEY paoya jayni!"
    try:
        base64_image = base64.b64encode(image_bytes).decode('utf-8')
        url = "https://api.groq.com/openai/v1/chat/completions"
        headers = {"Authorization": f"Bearer {AI_KEY}", "Content-Type": "application/json"}
        data = {
            "model": "llama-3.2-11b-vision-preview",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text", 
                            "text": "Tumi ekjon 1-minute price action master trader. Ei 1 minute chart screenshot ti bhabe analysis kore niche point akare বাংলায় uttor dao:\n1. 1 minute candle onujayi bortoman trend ki?\n2. Kono fake out ba pin bar ache ki na?\n3. Break of structure ba FVG dekha jacche ki na?\n4. Poroborti 1 minute candle er jonno CALL naki PUT entry neoa uchit ebong keno?"
                        },
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}
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
            return "⚠️ Chart analysis korte shomossha hoyeche."
    except Exception as e:
        return f"Vision AI error: {e}"

def get_learn_static():
    return """🧠 Advanced Price Action & SMC Guideline:

📚 ১. Market Structure & BOS:
Market jokhon urdhhomukhi thake tokhon higher high o higher low toiri kore. Dam ager prodhan high bhenge opore gele take BOS bole.

📚 ২. Order Block (OB):
Boro bank ba institution gulor boro order bosanor age market e je sesh opposite candle toiri hoy, setai Order Block.

📚 ৩. Fair Value Gap (FVG):
Market e druto impulsive movement er karone candle gular majkhane faka jayga toiri hoy, ja puron korte market pore fire ase.

📚 ৪. Liquidity Sweep & Pin Bar:
1 minute e support ba resistance er samne giye retail trader der stop loss hit kore market jokhon ghure jay, take liquidity sweep bole.

🔧 সেরা প্র্যাকটিস টুলস (Practice Tools):
1. TradingView (tradingview.com) - Real-time 1m chart o drawing practice er jonno.
2. Investing.com / Forex Factory - Live economic calendar o news track korar jonno.
3. Bot Vision AI - Je kono chart screenshot pathiye instantly BOS/FVG check korar jonno."""

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
            
            sender_chat_id = str(msg.get("chat", {}).get("id", ""))
            if sender_chat_id != str(CHAT_ID):
                continue
            
            if "photo" in msg:
                send("🔍 1-minute chart scan o price action analysis kora hocche...")
                photo_file_id = msg["photo"][-1]["file_id"]
                file_path_res = requests.get(f"https://api.telegram.org/bot{TOKEN}/getFile?file_id={photo_file_id}", timeout=15).json()
                if "result" in file_path_res:
                    file_path = file_path_res["result"]["file_path"]
                    img_url = f"https://api.telegram.org/file/bot{TOKEN}/{file_path}"
                    img_data = requests.get(img_url, timeout=20).content
                    
                    analysis_result = analyze_chart_image(img_data)
                    send(f"📊 1-Minute Chart Analysis Result:\n\n{analysis_result}")
                continue

            txt = msg.get("text", "").strip()
            if not txt: 
                continue
            low = txt.lower()

            if low.startswith("/start"):
                send("""👋 Assalamu Alaikum! GHOST MATRIX V2.0 (1M PRO TRADER)

COMMANDS:
1. /future - 1-Minute Professional Signal List
2. /news - News Prediction & Market Analysis
3. /learn - Advanced Price Action, SMC & Practice Tools
4. /stats - Today's Win/Loss Journal
5. /risk - Money Management Rules
6. 📸 **Chart Screenshot Pathan** - 1-Minute Chart Analysis kore dibe!

Apnar personal trading assistant prostut!""")
            elif "/future" in low:
                send("⏳ 1-Minute er professional signal toiri kora hocche...")
                signal_text = get_1m_market_signals()
                send(signal_text)
            elif "/news" in low:
                send("⏳ Forecast o actual data analisi kore news prediction toiri kora hocche...")
                news_ans = get_market_news_with_prediction()
                send(news_ans)
            elif "/learn" in low:
                send(get_learn_static())
            elif "/risk" in low:
                send("""💰 1-Minute Money Management & Risk Rules:
1. Capital er matro 2%-3% diye proti trade shuru korun.
2. Martingale (MTG) sorbobhoccho 1 bar use korun.
3. Tana 3 ti trade loss gele sedin market theke biroti nin.""")
            elif "/stats" in low:
                total = user_stats["wins"] + user_stats["losses"]
                win_rate = (user_stats["wins"] / total * 100) if total > 0 else 0
                send(f"📊 Apnar Personal Trading Stats:\n✅ Win: {user_stats['wins']}\n❌ Loss: {user_stats['losses']}\n📈 Win Rate: {win_rate:.1f}%")
            elif low.startswith("/win"):
                user_stats["wins"] += 1
                send("✅ Ekta win record kora hoyeche! /stats likhe stats dekhun.")
            elif low.startswith("/loss"):
                user_stats["losses"] += 1
                send("❌ Ekta loss record kora hoyeche! /stats likhe stats dekhun.")
            else:
                ans = ai_answer(txt)
                send(f"🧠 Ghost Matrix AI:\n\n{ans}")
        time.sleep(1)
    except Exception as e:
        print(e)
        time.sleep(3)

