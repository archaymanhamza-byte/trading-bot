import os
import time
import threading
import requests
import ccxt
import yfinance as yf
import pandas as pd
from flask import Flask

# ==================== 1. قراءة مفاتيح تيليجرام تلقائياً ====================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

HTF = "4h"                # الفريم الكبير لتحديد الاتجاه العام
LTF = "15m"               # الفريم الصغير لاقتناص الدخول
CHECK_INTERVAL = 180      # فحص السوق كل 3 دقائق

last_signal_candle = {"BTC": None, "GOLD": None}

# ==================== 2. خادم السحابة وفحص الاتصال ====================
app = Flask(__name__)

def send_telegram_msg(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return "خطأ: لم يتم إدخال TELEGRAM_BOT_TOKEN أو TELEGRAM_CHAT_ID في إعدادات Render"
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        res = requests.post(
            url,
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"},
            timeout=10
        )
        return res.json()
    except Exception as e:
        return f"خطأ في الاتصال: {e}"

@app.route("/")
def home():
    token_ok = "✅ موجود" if TELEGRAM_BOT_TOKEN else "❌ مفقود"
    chat_ok = "✅ موجود" if TELEGRAM_CHAT_ID else "❌ مفقود"
    return (
        f"<h2>البوت يعمل بنجاح 24/7 (Live)</h2>"
        f"<p>حالة التوكن (TOKEN): {token_ok}</p>"
        f"<p>حالة معرف الحساب (CHAT_ID): {chat_ok}</p>"
        f"<p><a href='/test'>اضغط هنا لإرسال رسالة تجريبية إلى تيليجرام الآن</a></p>"
    )

@app.route("/test")
def test_msg():
    result = send_telegram_msg("🔔 *رسالة تجريبية:* البوت متصل بحسابك في تيليجرام ويعمل بنجاح!")
    return f"<h3>نتيجة الإرسال إلى تيليجرام:</h3><pre>{result}</pre>"

def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

# ==================== 3. جلب بيانات البيتكوين والذهب ====================
def get_btc_ohlcv(timeframe, limit=250):
    for ex_name in ["kucoin", "binance", "kraken"]:
        try:
            exchange = getattr(ccxt, ex_name)({"enableRateLimit": True})
            symbol = "BTC/USDT" if ex_name != "kraken" else "BTC/USD"
            bars = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
            df = pd.DataFrame(bars, columns=["timestamp", "open", "high", "low", "close", "volume"])
            df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
            return df
        except Exception:
            continue
    return pd.DataFrame()

def get_gold_ohlcv(timeframe):
    ticker = yf.Ticker("GC=F")
    if timeframe == "4h":
        raw = ticker.history(period="60d", interval="1h").reset_index()
        raw = raw.rename(columns={"Datetime": "timestamp", "Date": "timestamp", "Open": "open", "High": "high", "Low": "low", "Close": "close", "Volume": "volume"})
        raw.set_index("timestamp", inplace=True)
        df = raw.resample("4h").agg({
            "open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"
        }).dropna().reset_index()
    else:
        raw = ticker.history(period="15d", interval="15m").reset_index()
        df = raw.rename(columns={"Datetime": "timestamp", "Date": "timestamp", "Open": "open", "High": "high", "Low": "low", "Close": "close", "Volume": "volume"})
    return df[["timestamp", "open", "high", "low", "close", "volume"]]

# ==================== 4. محرك التحليل العميق المدمج ====================
def enrich_indicators(df):
    df = df.copy()
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df["EMA_50"] = df["close"].ewm(span=50, adjust=False).mean()
    df["EMA_200"] = df["close"].ewm(span=200, adjust=False).mean()

    delta = df["close"].diff()
    gain = delta.clip(lower=0).ewm(alpha=1/14, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1/14, adjust=False).mean()
    rs = gain / loss.replace(0, 1e-9)
    df["RSI"] = 100 - (100 / (1 + rs))

    high_low = df["high"] - df["low"]
    high_close = (df["high"] - df["close"].shift(1)).abs()
    low_close = (df["low"] - df["close"].shift(1)).abs()
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    df["ATR"] = tr.ewm(alpha=1/14, adjust=False).mean()

    up_move = df["high"].diff()
    down_move = -df["low"].diff()
    plus_dm = up_move.where((up_move > down_move) & (up_move > 0), 0.0)
    minus_dm = down_move.where((down_move > up_move) & (down_move > 0), 0.0)
    atr_safe = df["ATR"].replace(0, 1e-9)
    plus_di = 100 * (plus_dm.ewm(alpha=1/14, adjust=False).mean() / atr_safe)
    minus_di = 100 * (minus_dm.ewm(alpha=1/14, adjust=False).mean() / atr_safe)
    dx = 100 * ((plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, 1e-9))
    df["ADX"] = dx.ewm(alpha=1/14, adjust=False).mean()

    df["VOL_SMA"] = df["volume"].rolling(window=20).mean()
    df["swing_low"] = df["low"].rolling(window=10).min().shift(1)
    df["swing_high"] = df["high"].rolling(window=10).max().shift(1)
    return df

def detect_smc_patterns(df):
    c1, c2, c3 = df.iloc[-4], df.iloc[-3], df.iloc[-2]
    bullish_fvg = c3["low"] > c1["high"] and c2["close"] > c2["open"]
    bearish_fvg = c3["high"] < c1["low"] and c2["close"] < c2["open"]

    candle_range = max(c3["high"] - c3["low"], 1e-6)
    lower_wick = (min(c3["open"], c3["close"]) - c3["low"]) / candle_range
    upper_wick = (c3["high"] - max(c3["open"], c3["close"])) / candle_range

    bullish_sweep = (c3["low"] <= c3["swing_low"]) and (c3["close"] > c3["swing_low"]) and (lower_wick >= 0.4)
    bearish_sweep = (c3["high"] >= c3["swing_high"]) and (c3["close"] < c3["swing_high"]) and (upper_wick >= 0.4)

    return {
        "bullish_smc": bullish_fvg or bullish_sweep,
        "bearish_smc": bearish_fvg or bearish_sweep,
        "pattern_bull": "سحب سيولة شرائي (Liquidity Sweep)" if bullish_sweep else "فجوة سعرية صاعدة (FVG)",
        "pattern_bear": "سحب سيولة بيعي (Liquidity Sweep)" if bearish_sweep else "فجوة سعرية هابطة (FVG)"
    }

def evaluate_pro_setup(htf_df, ltf_df, asset_name):
    if htf_df.empty or ltf_df.empty or len(htf_df) < 205 or len(ltf_df) < 205:
        return None

    htf = enrich_indicators(htf_df)
    ltf = enrich_indicators(ltf_df)
    h_curr, l_curr = htf.iloc[-2], ltf.iloc[-2]

    price = float(l_curr["close"])
    atr = float(l_curr["ATR"])
    adx = float(l_curr["ADX"])
    rsi = float(l_curr["RSI"])

    if float(l_curr["high"] - l_curr["low"]) > (2.8 * atr) or adx < 22:
        return None

    htf_bullish = h_curr["close"] > h_curr["EMA_200"] and h_curr["EMA_50"] > h_curr["EMA_200"]
    htf_bearish = h_curr["close"] < h_curr["EMA_200"] and h_curr["EMA_50"] < h_curr["EMA_200"]
    smc = detect_smc_patterns(ltf)

    if htf_bullish and (price > l_curr["EMA_200"]) and smc["bullish_smc"] and (45 <= rsi <= 67):
        sl = min(float(l_curr["low"]), float(l_curr["swing_low"])) - (0.4 * atr)
        risk = min(max(price - sl, 0.5 * atr), 1.8 * atr)
        sl = price - risk
        return {
            "asset": asset_name, "direction": "شراء (LONG) 🟢", "pattern": smc["pattern_bull"],
            "entry": round(price, 2), "sl": round(sl, 2),
            "tp1": round(price + 1.5 * risk, 2), "tp2": round(price + 3.0 * risk, 2),
            "adx": round(adx, 1), "candle_time": str(l_curr["timestamp"])
        }

    if htf_bearish and (price < l_curr["EMA_200"]) and smc["bearish_smc"] and (33 <= rsi <= 55):
        sl = max(float(l_curr["high"]), float(l_curr["swing_high"])) + (0.4 * atr)
        risk = min(max(sl - price, 0.5 * atr), 1.8 * atr)
        sl = price + risk
        return {
            "asset": asset_name, "direction": "بيع (SHORT) 🔴", "pattern": smc["pattern_bear"],
            "entry": round(price, 2), "sl": round(sl, 2),
            "tp1": round(price - 1.5 * risk, 2), "tp2": round(price - 3.0 * risk, 2),
            "adx": round(adx, 1), "candle_time": str(l_curr["timestamp"])
        }
    return None

def format_signal_msg(sig):
    return (
        f"💎 *إشارة تداول جديدة (MTF + SMC)*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📊 *الأصل:* `{sig['asset']}`\n"
        f"🧭 *الاتجاه:* {sig['direction']}\n"
        f"🧠 *السبب الفني:* {sig['pattern']}\n"
        f"🔥 *قوة الاتجاه (ADX):* `{sig['adx']}`\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🎯 *سعر الدخول:* `{sig['entry']}`\n"
        f"🛑 *وقف الخسارة (SL):* `{sig['sl']}`\n"
        f"✅ *الهدف الأول (TP1):* `{sig['tp1']}`\n"
        f"🚀 *الهدف الثاني (TP2):* `{sig['tp2']}`"
    )

# ==================== 5. حلقة التشغيل ====================
def run_bot():
    send_telegram_msg("✅ *تم تشغيل بوت الذهب والبيتكوين الاحترافي بنجاح!*\nجاري مراقبة السوق على مدار 24 ساعة وإرسال الصفقات فور اكتمال الشروط.")
    while True:
        try:
            btc_sig = evaluate_pro_setup(get_btc_ohlcv(HTF), get_btc_ohlcv(LTF), "BTC/USDT (البيتكوين)")
            if btc_sig and last_signal_candle["BTC"] != btc_sig["candle_time"]:
                send_telegram_msg(format_signal_msg(btc_sig))
                last_signal_candle["BTC"] = btc_sig["candle_time"]

            gold_sig = evaluate_pro_setup(get_gold_ohlcv(HTF), get_gold_ohlcv(LTF), "XAU/USD (الذهب)")
            if gold_sig and last_signal_candle["GOLD"] != gold_sig["candle_time"]:
                send_telegram_msg(format_signal_msg(gold_sig))
                last_signal_candle["GOLD"] = gold_sig["candle_time"]
        except Exception as e:
            print(f"تنبيه أثناء الفحص: {e}")
        time.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    threading.Thread(target=run_web_server, daemon=True).start()
    run_bot()
