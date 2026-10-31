import os
import io
import json
import time
import threading
import requests
import ccxt
import yfinance as yf
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from flask import Flask

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

HTF = "4h"
LTF = "15m"
CHECK_INTERVAL = 180
TRACKER_INTERVAL = 35  # فحص الصفقات المفتوحة كل 35 ثانية

last_signal_candle = {"BTC": None, "GOLD": None}
active_trades = {}
trades_lock = threading.Lock()

app = Flask(__name__)

# لوحة الأزرار التفاعلية مع زر متابعة الصفقات النشطة
REPLY_KEYBOARD = {
    "keyboard": [
        [{"text": "🪙 تحليل الذهب فوراً"}, {"text": "₿ تحليل البيتكوين فوراً"}],
        [{"text": "⚡ مضاربة سريعة"}, {"text": "📊 فحص السوق الشامل"}],
        [{"text": "📂 الصفقات النشطة"}, {"text": "🗑️ مسح الصفقات النشطة"}]
    ],
    "resize_keyboard": True,
    "is_persistent": True
}

def send_msg(text, chat_id=None):
    target = chat_id or TELEGRAM_CHAT_ID
    if not TELEGRAM_BOT_TOKEN or not target:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": target,
        "text": text,
        "parse_mode": "Markdown",
        "reply_markup": REPLY_KEYBOARD
    }
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"Telegram text error: {e}")

def send_photo_msg(photo_buf, caption, chat_id=None):
    target = chat_id or TELEGRAM_CHAT_ID
    if not TELEGRAM_BOT_TOKEN or not target:
        return
    if photo_buf is None:
        send_msg(caption, target)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
    data = {
        "chat_id": target,
        "caption": caption,
        "parse_mode": "Markdown",
        "reply_markup": json.dumps(REPLY_KEYBOARD)
    }
    files = {"photo": ("chart.png", photo_buf, "image/png")}
    try:
        res = requests.post(url, data=data, files=files, timeout=20)
        if not res.ok:
            send_msg(caption, target)
    except Exception as e:
        print(f"Telegram photo error: {e}")
        send_msg(caption, target)

# ==================== 1. تسجيل ومتابعة الصفقات النشطة ====================
def register_trade(trade_key, asset_name, symbol_type, is_long, entry, sl, tp1, tp2, mode_label):
    with trades_lock:
        active_trades[trade_key] = {
            "asset": asset_name,
            "symbol_type": symbol_type,  # "GOLD" or "BTC"
            "is_long": is_long,
            "entry": float(entry),
            "sl": float(sl),
            "initial_sl": float(sl),
            "tp1": float(tp1),
            "tp2": float(tp2),
            "tp1_hit": False,
            "mode": mode_label,
            "opened_at": time.strftime("%H:%M UTC", time.gmtime())
        }

def get_latest_price_extremes(symbol_type):
    """جلب السعر الحالي وأعلى/أدنى سعر في آخر شمعة دقيقة لفحص ضرب الأهداف بدقة"""
    if symbol_type == "BTC":
        df = get_btc_ohlcv("1m", limit=3)
    else:
        df = get_gold_ohlcv("1m")
    if df.empty:
        return None
    last_row = df.iloc[-1]
    return {
        "close": round(float(last_row["close"]), 2),
        "high": round(float(last_row["high"]), 2),
        "low": round(float(last_row["low"]), 2)
    }

def trade_tracker_loop():
    """رادار يفحص الصفقات النشطة كل 35 ثانية وينبه عند ضرب TP1 أو TP2 أو SL"""
    while True:
        try:
            with trades_lock:
                keys_to_check = list(active_trades.keys())

            if keys_to_check:
                price_cache = {}
                for key in keys_to_check:
                    with trades_lock:
