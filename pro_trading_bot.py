import os
import time
import threading
import requests
import ccxt
import yfinance as yf
import pandas as pd
from flask import Flask

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

app = Flask(__name__)

def send_msg(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"}, timeout=10)
    except:
        pass

@app.route("/")
def home():
    return "Bot is running 24/7!"

@app.route("/test")
def test():
    send_msg("🔔 *أهلاً بك!* البوت يعمل ومتصل بنجاح.")
    return "تم إرسال رسالة تجريبية إلى تيليجرام."

def run_server():
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))

def get_data():
    try:
        # جلب الذهب
        gold = yf.Ticker("GC=F").history(period="5d", interval="1h")
        g_price = float(gold["Close"].iloc[-1])
        
        # جلب البيتكوين
        ex = ccxt.binance()
        btc = ex.fetch_ohlcv("BTC/USDT", timeframe="1h", limit=5)
        b_price = float(btc[-1][4])
        
        msg = (
            f"📊 *تقرير الأسعار الفوري*\n"
            f"🪙 *الذهب (XAUUSD):* `{round(g_price, 2)}` $\n"
            f"₿ *البيتكوين (BTC):* `{round(b_price, 2)}` $\n"
            f"✅ البوت يعمل ويرصد الأسعار بنجاح."
        )
        send_msg(msg)
    except Exception as e:
        print(f"Error: {e}")

def background_loop():
    while True:
        get_data()
        time.sleep(3600) # يرسل تحديثاً كل ساعة تلقائياً

if __name__ == "__main__":
    threading.Thread(target=run_server, daemon=True).start()
    threading.Thread(target=background_loop, daemon=True).start()
    send_msg("🚀 *تم إقلاع البوت السحابي بنجاح!* البوت يراقب السوق الآن.")
    while True:
        time.sleep(10)
