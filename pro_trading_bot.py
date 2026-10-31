import os
import time
import threading
import requests
import ccxt
import yfinance as yf
import pandas as pd
from flask import Flask

# ==================== 1. الإعدادات ومفاتيح تيليجرام ====================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

HTF = "4h"                # الفريم الكبير لتحديد الاتجاه العام
LTF = "15m"               # الفريم الصغير لاقتناص الدخول
CHECK_INTERVAL = 180      # فحص الصفقات التلقائي كل 3 دقائق

last_signal_candle = {"BTC": None, "GOLD": None}

# ==================== 2. خادم السحابة والاتصال بتيليجرام ====================
app = Flask(__name__)

def send_telegram_msg(text, chat_id=None):
    target_chat = chat_id or TELEGRAM_CHAT_ID
    if not TELEGRAM_BOT_TOKEN or not target_chat:
        return "خطأ: المفاتيح غير موجودة"
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        res = requests.post(
            url,
            json={"chat_id": target_chat, "text": text, "parse_mode": "Markdown"},
            timeout=10
        )
        return res.json()
    except Exception as e:
        return f"خطأ في الاتصال: {e}"

@app.route("/")
def home():
    return (
        "<h2>البوت التفاعلي يعمل بنجاح 24/7 (Live)</h2>"
        "<p><a href='/analyze_now'>اضغط هنا لإرسال تحليل السوق الحالي فوراً إلى تيليجرام</a></p>"
    )

@app.route("/analyze_now")
def analyze_now_web():
    threading.Thread(target=send_full_report, args=(TELEGRAM_CHAT_ID,), daemon=True).start()
    return "<h3>✅ جاري تحليل الذهب والبيتكوين وإرسال التقرير إلى تيليجرام خلال ثوانٍ...</h3>"

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

# ==================== 4. محرك حساب المؤشرات والـ SMC ====================
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

# ==================== 5. التحليل الفوري عند الطلب (On-Demand Analysis) ====================
def generate_instant_analysis(htf_df, ltf_df, asset_name):
    if htf_df.empty or ltf_df.empty or len(htf_df) < 205 or len(ltf_df) < 205:
        return f"⚠️ تعذر جلب بيانات كافية لـ {asset_name} حالياً."

    htf = enrich_indicators(htf_df)
    ltf = enrich_indicators(ltf_df)
    h_curr = htf.iloc[-1]
    l_curr = ltf.iloc[-1]

    price = float(l_curr["close"])
    atr = float(l_curr["ATR"])
    rsi = float(l_curr["RSI"])
    adx = float(l_curr["ADX"])
    swing_low = float(l_curr["swing_low"])
    swing_high = float(l_curr["swing_high"])

    htf_trend = "صاعد 🟢" if h_curr["close"] > h_curr["EMA_200"] else "هابط 🔴"
    ltf_trend = "صاعد 🟢" if price > l_curr["EMA_200"] else "هابط 🔴"
    market_state = "اتجاه قوي 🔥" if adx >= 22 else "تذبذب عرضي (ينصح بالحذر) ⚠️"

    # تحديد خطة التداول المقترحة بناءً على توافق الاتجاه الحالي
    if price > l_curr["EMA_50"]:
        bias = "شراء (LONG) 🟢"
        sl = round(min(price - (1.5 * atr), swing_low - (0.3 * atr)), 2)
        risk = max(price - sl, 0.5 * atr)
        tp1 = round(price + (1.5 * risk), 2)
        tp2 = round(price + (3.0 * risk), 2)
    else:
        bias = "بيع (SHORT) 🔴"
        sl = round(max(price + (1.5 * atr), swing_high + (0.3 * atr)), 2)
        risk = max(sl - price, 0.5 * atr)
        tp1 = round(price - (1.5 * risk), 2)
        tp2 = round(price - (3.0 * risk), 2)

    return (
        f"📊 *التحليل الفني الفوري: {asset_name}*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"💵 *السعر الحالي:* `{round(price, 2)}`\n"
        f"🧭 *الاتجاه العام (4H):* {htf_trend}\n"
        f"⏱ *الاتجاه اللحظي (15M):* {ltf_trend}\n"
        f"🌊 *حالة السوق:* {market_state} (`ADX: {round(adx, 1)}`)\n"
        f"📈 *مؤشر الزخم (RSI):* `{round(rsi, 1)}`\n"
        f"🧱 *أقرب دعم / مقاومة:* `{round(swing_low, 2)}` | `{round(swing_high, 2)}`\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"💡 *السيناريو الأقرب حالياً:* {bias}\n"
        f"🎯 *نقطة الارتكاز / الدخول:* `{round(price, 2)}`\n"
        f"🛑 *الستوب الهيكلي (SL):* `{sl}`\n"
        f"✅ *الهدف الأول (TP1):* `{tp1}`\n"
        f"🚀 *الهدف الثاني (TP2):* `{tp2}`"
    )

def send_full_report(chat_id):
    send_telegram_msg("⏳ *جاري سحب بيانات السوق الحية وتحليل الذهب والبيتكوين...*", chat_id)
    btc_report = generate_instant_analysis(get_btc_ohlcv(HTF), get_btc_ohlcv(LTF), "BTC/USDT (البيتكوين)")
    send_telegram_msg(btc_report, chat_id)
    gold_report = generate_instant_analysis(get_gold_ohlcv(HTF), get_gold_ohlcv(LTF), "XAU/USD (الذهب)")
    send_telegram_msg(gold_report, chat_id)

# ==================== 6. الاستماع لأوامرك داخل تيليجرام ====================
def telegram_command_listener():
    offset = None
    while True:
        try:
            if TELEGRAM_BOT_TOKEN:
                url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
                params = {"timeout": 20, "offset": offset}
                res = requests.get(url, params=params, timeout=25).json()
                if res.get("ok"):
                    for item in res.get("result", []):
                        offset = item["update_id"] + 1
                        msg = item.get("message", {})
                        text = msg.get("text", "").strip().lower()
                        chat_id = str(msg.get("chat", {}).get("id", ""))

                        if text in ["/start", "/help"]:
                            help_msg = (
                                "🤖 *أهلاً بك في بوت التحليل المؤسسي!*\n\n"
                                "يمكنك طلب تحليل السوق في أي لحظة عبر الضغط على أحد الأوامر التالية:\n"
                                "🔹 /status — تحليل شامل للذهب والبيتكوين معاً\n"
                                "🔸 /gold — تحليل فوري لـ الذهب XAU/USD\n"
                                "🔹 /btc — تحليل فوري لـ البيتكوين BTC/USDT\n\n"
                                "⚡ *ملاحظة:* البوت يراقب السوق تلقائياً في الخلفية ويرسل الصفقات القوية فور اكتمال شروطها."
                            )
                            send_telegram_msg(help_msg, chat_id)
                        elif text in ["/status", "/all"]:
                            send_full_report(chat_id)
                        elif text == "/gold":
                            send_telegram_msg("⏳ *جاري تحليل الذهب XAU/USD...*", chat_id)
                            rep = generate_instant_analysis(get_gold_ohlcv(HTF), get_gold_ohlcv(LTF), "XAU/USD (الذهب)")
                            send_telegram_msg(rep, chat_id)
                        elif text == "/btc":
                            send_telegram_msg("⏳ *جاري تحليل البيتكوين BTC/USDT...*", chat_id)
                            rep = generate_instant_analysis(get_btc_ohlcv(HTF), get_btc_ohlcv(LTF), "BTC/USDT (البيتكوين)")
                            send_telegram_msg(rep, chat_id)
        except Exception as e:
            print(f"خطأ في قراءة الأوامر: {e}")
        time.sleep(2)

# ==================== 7. الفحص التلقائي للصفقات الذهبية ====================
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
            "asset": asset_name, "direction": "شراء مؤكد (LONG) 🟢", "pattern": smc["pattern_bull"],
            "entry": round(price, 2), "sl": round(sl, 2),
            "tp1": round(price + 1.5 * risk, 2), "tp2": round(price + 3.0 * risk, 2),
            "adx": round(adx, 1), "candle_time": str(l_curr["timestamp"])
        }

    if htf_bearish and (price < l_curr["EMA_200"]) and smc["bearish_smc"] and (33 <= rsi <= 55):
        sl = max(float(l_curr["high"]), float(l_curr["swing_high"])) + (0.4 * atr)
        risk = min(max(sl - price, 0.5 * atr), 1.8 * atr)
        sl = price + risk
        return {
            "asset": asset_name, "direction": "بيع مؤكد (SHORT) 🔴", "pattern": smc["pattern_bear"],
            "entry": round(price, 2), "sl": round(sl, 2),
            "tp1": round(price - 1.5 * risk, 2), "tp2": round(price - 3.0 * risk, 2),
            "adx": round(adx, 1), "candle_time": str(l_curr["timestamp"])
        }
    return None

def format_signal_msg(sig):
    return (
        f"💎 *إشارة تداول مكتملة الشروط (MTF + SMC)*\n"
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

def run_bot():
    send_telegram_msg(
        "✅ *تم تفعيل الأوامر التفاعلية بنجاح!*\n"
        "أرسل الآن:\n"
        "👈 /status لتحليل السوق بالكامل فوراً\n"
        "👈 /gold لتحليل الذهب فوراً\n"
        "👈 /btc لتحليل البيتكوين فو
