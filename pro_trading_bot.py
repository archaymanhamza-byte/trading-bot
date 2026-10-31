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

HTF = "4h"
LTF = "15m"
CHECK_INTERVAL = 180
last_signal_candle = {"BTC": None, "GOLD": None}

app = Flask(__name__)

# لوحة الأزرار المحدثة وتضم زر المضاربة السريعة الرابع
REPLY_KEYBOARD = {
    "keyboard": [
        [{"text": "🪙 تحليل الذهب فوراً"}, {"text": "₿ تحليل البيتكوين فوراً"}],
        [{"text": "⚡ مضاربة سريعة"}, {"text": "📊 فحص السوق الشامل"}]
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
        print(f"Telegram error: {e}")

# ==================== 1. جلب بيانات السوق (يدعم 4h, 15m, 5m, 1m) ====================
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
    try:
        ticker = yf.Ticker("GC=F")
        if timeframe == "4h":
            raw = ticker.history(period="60d", interval="1h").reset_index()
            raw = raw.rename(columns={"Datetime": "timestamp", "Date": "timestamp", "Open": "open", "High": "high", "Low": "low", "Close": "close", "Volume": "volume"})
            raw.set_index("timestamp", inplace=True)
            df = raw.resample("4h").agg({
                "open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"
            }).dropna().reset_index()
        elif timeframe == "15m":
            raw = ticker.history(period="15d", interval="15m").reset_index()
            df = raw.rename(columns={"Datetime": "timestamp", "Date": "timestamp", "Open": "open", "High": "high", "Low": "low", "Close": "close", "Volume": "volume"})
        elif timeframe == "5m":
            raw = ticker.history(period="5d", interval="5m").reset_index()
            df = raw.rename(columns={"Datetime": "timestamp", "Date": "timestamp", "Open": "open", "High": "high", "Low": "low", "Close": "close", "Volume": "volume"})
        elif timeframe == "1m":
            raw = ticker.history(period="5d", interval="1m").reset_index()
            df = raw.rename(columns={"Datetime": "timestamp", "Date": "timestamp", "Open": "open", "High": "high", "Low": "low", "Close": "close", "Volume": "volume"})
        else:
            return pd.DataFrame()
        return df[["timestamp", "open", "high", "low", "close", "volume"]]
    except Exception as e:
        print(f"Gold fetch error ({timeframe}): {e}")
        return pd.DataFrame()

# ==================== 2. حساب المؤشرات الفنية والـ SMC ====================
def enrich_indicators(df):
    df = df.copy()
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df["EMA_20"] = df["close"].ewm(span=20, adjust=False).mean()
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

    df["swing_low"] = df["low"].rolling(window=10).min().shift(1)
    df["swing_high"] = df["high"].rolling(window=10).max().shift(1)
    df["micro_low"] = df["low"].rolling(window=5).min().shift(1)
    df["micro_high"] = df["high"].rolling(window=5).max().shift(1)
    return df

def detect_smc(df):
    c1, c2, c3 = df.iloc[-4], df.iloc[-3], df.iloc[-2]
    bull_fvg = c3["low"] > c1["high"] and c2["close"] > c2["open"]
    bear_fvg = c3["high"] < c1["low"] and c2["close"] < c2["open"]

    rng = max(c3["high"] - c3["low"], 1e-6)
    l_wick = (min(c3["open"], c3["close"]) - c3["low"]) / rng
    u_wick = (c3["high"] - max(c3["open"], c3["close"])) / rng

    bull_sweep = (c3["low"] <= c3["swing_low"]) and (c3["close"] > c3["swing_low"]) and (l_wick >= 0.4)
    bear_sweep = (c3["high"] >= c3["swing_high"]) and (c3["close"] < c3["swing_high"]) and (u_wick >= 0.4)

    return {
        "bull": bull_fvg or bull_sweep,
        "bear": bear_fvg or bear_sweep,
        "p_bull": "سحب سيولة شرائي (Liquidity Sweep)" if bull_sweep else "فجوة سعرية صاعدة (FVG)",
        "p_bear": "سحب سيولة بيعي (Liquidity Sweep)" if bear_sweep else "فجوة سعرية هابطة (FVG)"
    }

# ==================== 3. التحليل الفوري (4H + 15M) ====================
def instant_analysis(htf_df, ltf_df, asset_name):
    if htf_df.empty or ltf_df.empty or len(htf_df) < 205 or len(ltf_df) < 205:
        return f"⚠️ جاري تحديث بيانات {asset_name}، حاول بعد لحظات."

    htf = enrich_indicators(htf_df)
    ltf = enrich_indicators(ltf_df)
    h_curr, l_curr = htf.iloc[-1], ltf.iloc[-1]

    price = float(l_curr["close"])
    atr = float(l_curr["ATR"])
    rsi = float(l_curr["RSI"])
    adx = float(l_curr["ADX"])
    s_low = float(l_curr["swing_low"])
    s_high = float(l_curr["swing_high"])

    htf_trend = "صاعد 🟢" if h_curr["close"] > h_curr["EMA_200"] else "هابط 🔴"
    ltf_trend = "صاعد 🟢" if price > l_curr["EMA_200"] else "هابط 🔴"

    if price > l_curr["EMA_50"]:
        side = "شراء (LONG) 🟢"
        sl = round(min(price - (1.5 * atr), s_low - (0.3 * atr)), 2)
        risk = max(price - sl, 0.5 * atr)
        tp1 = round(price + (1.5 * risk), 2)
        tp2 = round(price + (3.0 * risk), 2)
    else:
        side = "بيع (SHORT) 🔴"
        sl = round(max(price + (1.5 * atr), s_high + (0.3 * atr)), 2)
        risk = max(sl - price, 0.5 * atr)
        tp1 = round(price - (1.5 * risk), 2)
        tp2 = round(price - (3.0 * risk), 2)

    return (
        f"📊 *التحليل العميق الفوري: {asset_name}*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"💵 *السعر الحالي:* `{round(price, 2)}`\n"
        f"🧭 *الاتجاه العام (4H):* {htf_trend}\n"
        f"⏱ *الاتجاه اللحظي (15M):* {ltf_trend}\n"
        f"🔥 *قوة الاتجاه (ADX):* `{round(adx, 1)}` | *RSI:* `{round(rsi, 1)}`\n"
        f"🧱 *أقرب دعم ومقاومة:* `{round(s_low, 2)}` | `{round(s_high, 2)}`\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🎯 *الصفقة المقترحة حالياً:* {side}\n"
        f"📍 *سعر الدخول:* `{round(price, 2)}`\n"
        f"🛑 *وقف الخسارة (SL):* `{sl}`\n"
        f"✅ *الهدف الأول (TP1):* `{tp1}`\n"
        f"🚀 *الهدف الثاني (TP2):* `{tp2}`"
    )

# ==================== 4. محرك المضاربة السريعة (Scalping 5M + 1M) ====================
def scalp_analysis(m5_df, m1_df, asset_name):
    if m5_df.empty or m1_df.empty or len(m5_df) < 100 or len(m1_df) < 100:
        return f"⚠️ تعذر جلب بيانات السكالبينج لـ {asset_name} حالياً."

    m5 = enrich_indicators(m5_df)
    m1 = enrich_indicators(m1_df)
    c5, c1 = m5.iloc[-1], m1.iloc[-1]

    price = float(c1["close"])
    atr_1m = max(float(c1["ATR"]), 0.01)
    rsi_1m = float(c1["RSI"])
    adx_5m = float(c5["ADX"])
    m_low = float(c1["micro_low"])
    m_high = float(c1["micro_high"])

    m5_bull = c5["close"] > c5["EMA_50"]
    m1_bull = price > c1["EMA_20"]
    smc_1m = detect_smc(m1)

    # تحديد اتجاه السكالبينج والوقف الضيق
    if (m5_bull and m1_bull) or (m5_bull and rsi_1m < 45):
        scalp_dir = "شراء سريع (SCALP LONG) 🟢"
        reason = smc_1m["p_bull"] if smc_1m["bull"] else "اندفاع شرائي مع تقاطع EMA 20/50"
        sl = round(min(price - (1.2 * atr_1m), m_low - (0.2 * atr_1m)), 2)
        risk = min(max(price - sl, 0.8 * atr_1m), 2.0 * atr_1m)
        sl = round(price - risk, 2)
        tp1 = round(price + (1.2 * risk), 2)
        tp2 = round(price + (2.2 * risk), 2)
    else:
        scalp_dir = "بيع سريع (SCALP SHORT) 🔴"
        reason = smc_1m["p_bear"] if smc_1m["bear"] else "ضغط بيعي أسفل EMA 20/50"
        sl = round(max(price + (1.2 * atr_1m), m_high + (0.2 * atr_1m)), 2)
        risk = min(max(sl - price, 0.8 * atr_1m), 2.0 * atr_1m)
        sl = round(price + risk, 2)
        tp1 = round(price - (1.2 * risk), 2)
        tp2 = round(price - (2.2 * risk), 2)

    speed_status = "سيولة عالية ممتازة للسكالبينج ⚡" if adx_5m >= 20 else "سيولة هادئة (التزم بالهدف الأول) ⚠️"

    return (
        f"⚡ *صفقة مضاربة سريعة (5M + 1M): {asset_name}*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"💵 *السعر اللحظي:* `{round(price, 2)}`\n"
        f"🧭 *الزخم السريع:* {scalp_dir}\n"
        f"🧠 *إشارة الدقيقة (1M):* {reason}\n"
        f"🌊 *حالة السيولة:* {speed_status}\n"
        f"📈 *مؤشر RSI (1M):* `{round(rsi_1m, 1)}` | *ADX (5M):* `{round(adx_5m, 1)}`\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🎯 *سعر الدخول الفوري:* `{round(price, 2)}`\n"
        f"🛑 *وقف الخسارة الضيق (SL):* `{sl}`\n"
        f"✅ *هدف السكالب الأول (TP1):* `{tp1}`\n"
        f"🚀 *هدف السكالب الثاني (TP2):* `{tp2}`"
    )

# ==================== 5. الاستماع لأزرار وأوامر تيليجرام ====================
def telegram_listener():
    offset = None
    while True:
        try:
            if TELEGRAM_BOT_TOKEN:
                url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
                res = requests.get(url, params={"timeout": 20, "offset": offset}, timeout=25).json()
                if res.get("ok"):
                    for item in res.get("result", []):
                        offset = item["update_id"] + 1
                        msg = item.get("message", {})
                        text = msg.get("text", "").strip()
                        chat_id = str(msg.get("chat", {}).get("id", ""))

                        if text in ["/start", "/help"]:
                            send_msg("🤖 *أهلاً بك!* اختر نوع التحليل المطلوب من الأزرار بالأسفل:", chat_id)
                        elif text in ["/gold", "🪙 تحليل الذهب فوراً"]:
                            send_msg("⏳ *جاري تحليل الذهب XAU/USD...*", chat_id)
                            send_msg(instant_analysis(get_gold_ohlcv(HTF), get_gold_ohlcv(LTF), "XAU/USD (الذهب)"), chat_id)
                        elif text in ["/btc", "₿ تحليل البيتكوين فوراً"]:
                            send_msg("⏳ *جاري تحليل البيتكوين BTC/USDT...*", chat_id)
                            send_msg(instant_analysis(get_btc_ohlcv(HTF), get_btc_ohlcv(LTF), "BTC/USDT (البيتكوين)"), chat_id)
                        elif text in ["/scalp", "⚡ مضاربة سريعة"]:
                            send_msg("⚡ *جاري فحص فريم 5 دقائق و1 دقيقة لاقتناص صفقات السكالبينج...*", chat_id)
                            send_msg(scalp_analysis(get_gold_ohlcv("5m"), get_gold_ohlcv("1m"), "XAU/USD (الذهب)"), chat_id)
                            send_msg(scalp_analysis(get_btc_ohlcv("5m"), get_btc_ohlcv("1m"), "BTC/USDT (البيتكوين)"), chat_id)
                        elif text in ["/status", "/all", "📊 فحص السوق الشامل"]:
                            send_msg("⏳ *جاري الفحص الشامل للذهب والبيتكوين...*", chat_id)
                            send_msg(instant_analysis(get_gold_ohlcv(HTF), get_gold_ohlcv(LTF), "XAU/USD (الذهب)"), chat_id)
                            send_msg(instant_analysis(get_btc_ohlcv(HTF), get_btc_ohlcv(LTF), "BTC/USDT (البيتكوين)"), chat_id)
        except Exception as e:
            print(f"Listener error: {e}")
        time.sleep(2)

# ==================== 6. الرادار التلقائي للصفقات الذهبية ====================
def check_auto_signal(htf_df, ltf_df, asset_name):
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

    htf_bull = h_curr["close"] > h_curr["EMA_200"] and h_curr["EMA_50"] > h_curr["EMA_200"]
    htf_bear = h_curr["close"] < h_curr["EMA_200"] and h_curr["EMA_50"] < h_curr["EMA_200"]
    smc = detect_smc(ltf)

    if htf_bull and (price > l_curr["EMA_200"]) and smc["bull"] and (45 <= rsi <= 67):
        sl = min(float(l_curr["low"]), float(l_curr["swing_low"])) - (0.4 * atr)
        risk = min(max(price - sl, 0.5 * atr), 1.8 * atr)
        sl = price - risk
        return {
            "asset": asset_name, "dir": "شراء مؤسسي (LONG) 🟢", "pat": smc["p_bull"],
            "entry": round(price, 2), "sl": round(sl, 2),
            "tp1": round(price + 1.5 * risk, 2), "tp2": round(price + 3.0 * risk, 2),
            "adx": round(adx, 1), "time": str(l_curr["timestamp"])
        }

    if htf_bear and (price < l_curr["EMA_200"]) and smc["bear"] and (33 <= rsi <= 55):
        sl = max(float(l_curr["high"]), float(l_curr["swing_high"])) + (0.4 * atr)
        risk = min(max(sl - price, 0.5 * atr), 1.8 * atr)
        sl = price + risk
        return {
            "asset": asset_name, "dir": "بيع مؤسسي (SHORT) 🔴", "pat": smc["p_bear"],
            "entry": round(price, 2), "sl": round(sl, 2),
            "tp1": round(price - 1.5 * risk, 2), "tp2": round(price - 3.0 * risk, 2),
            "adx": round(adx, 1), "time": str(l_curr["timestamp"])
        }
    return None

def scanner_loop():
    send_msg(
        "✅ *تم إضافة زر (⚡ مضاربة سريعة) بنجاح!*\n"
        "اضغط الآن على زر *⚡ مضاربة سريعة* بالأسفل (أو أرسل /scalp) لتحليل فريم 5 دقائق و1 دقيقة للذهب والبيتكوين."
    )
    while True:
        try:
            for key, name, fn in [
                ("BTC", "BTC/USDT (البيتكوين)", get_btc_ohlcv),
                ("GOLD", "XAU/USD (الذهب)", get_gold_ohlcv)
            ]:
                sig = check_auto_signal(fn(HTF), fn(LTF), name)
                if sig and last_signal_candle[key] != sig["time"]:
                    alert = (
                        f"💎 *صفقة مكتملة الشروط (MTF + SMC)*\n"
                        f"━━━━━━━━━━━━━━━━━━\n"
                        f"📊 *الأصل:* `{sig['asset']}`\n"
                        f"🧭 *الاتجاه:* {sig['dir']}\n"
                        f"🧠 *السبب:* {sig['pat']}\n"
                        f"🎯 *سعر الدخول:* `{sig['entry']}`\n"
                        f"🛑 *وقف الخسارة (SL):* `{sig['sl']}`\n"
                        f"✅ *الهدف الأول (TP1):* `{sig['tp1']}`\n"
                        f"🚀 *الهدف الثاني (TP2):* `{sig['tp2']}`"
                    )
                    send_msg(alert)
                    last_signal_candle[key] = sig["time"]
        except Exception as e:
            print(f"Scanner error: {e}")
        time.sleep(CHECK_INTERVAL)

@app.route("/")
def home():
    return "Pro Trading Bot + Scalping is Live 24/7!"

@app.route("/test")
def test():
    threading.Thread(target=lambda: send_msg(scalp_analysis(get_gold_ohlcv("5m"), get_gold_ohlcv("1m"), "XAU/USD (الذهب)")), daemon=True).start()
    return "جاري إرسال صفقة المضاربة السريعة للذهب إلى تيليجرام..."

if __name__ == "__main__":
    threading.Thread(target=telegram_listener, daemon=True).start()
    threading.Thread(target=scanner_loop, daemon=True).start()
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
