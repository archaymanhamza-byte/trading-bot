import os
import io
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
last_signal_candle = {"BTC": None, "GOLD": None}

app = Flask(__name__)

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
        print(f"Telegram text error: {e}")

def send_photo_msg(photo_buf, caption, chat_id=None):
    target = chat_id or TELEGRAM_CHAT_ID
    if not TELEGRAM_BOT_TOKEN or not target:
        return
    if photo_buf is None:
        send_msg(caption, target)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
    import json
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

# ==================== 1. رسم الشارت الاحترافي (TradingView Dark Style) ====================
def generate_chart_image(df, asset_title, entry, sl, tp1, tp2, is_long=True):
    try:
        plot_df = df.tail(60).reset_index(drop=True)
        fig, ax = plt.subplots(figsize=(10, 5.5), dpi=120)
        fig.patch.set_facecolor("#131722")
        ax.set_facecolor("#131722")

        # رسم الشموع اليابانية
        for idx, row in plot_df.iterrows():
            o, h, l, c = float(row["open"]), float(row["high"]), float(row["low"]), float(row["close"])
            color = "#26a69a" if c >= o else "#ef5350"
            ax.plot([idx, idx], [l, h], color=color, linewidth=1.1, zorder=2)
            body_bottom = min(o, c)
            body_height = max(abs(c - o), (h - l) * 0.02, 1e-4)
            ax.bar(idx, body_height, bottom=body_bottom, width=0.62, color=color, edgecolor=color, zorder=3)

        # رسم المتوسطات المتحركة
        if "EMA_50" in plot_df.columns:
            ax.plot(plot_df.index, plot_df["EMA_50"], color="#2962ff", linewidth=1.3, label="EMA 50", zorder=4)
        if "EMA_200" in plot_df.columns:
            ax.plot(plot_df.index, plot_df["EMA_200"], color="#ff9800", linewidth=1.5, label="EMA 200", zorder=4)

        # رسم خطوط الدخول والستوب والأهداف مع التظليل
        x_start = len(plot_df) - 18
        x_end = len(plot_df) + 3
        ax.set_xlim(-1, x_end + 4)

        ax.axhline(entry, color="#ffffff", linestyle="--", linewidth=1.1, alpha=0.85)
        ax.axhline(sl, color="#ef5350", linestyle="--", linewidth=1.2, alpha=0.9)
        ax.axhline(tp1, color="#26a69a", linestyle=":", linewidth=1.2, alpha=0.9)
        ax.axhline(tp2, color="#00e676", linestyle="--", linewidth=1.3, alpha=0.95)

        ax.fill_between([x_start, x_end], entry, sl, color="#ef5350", alpha=0.14)
        ax.fill_between([x_start, x_end], entry, tp2, color="#26a69a", alpha=0.14)

        # كتابة الأسعار على يمين الشارت
        label_x = x_end + 0.3
        ax.text(label_x, entry, f" ENTRY: {entry}", color="#ffffff", fontsize=8, va="center", fontweight="bold")
        ax.text(label_x, sl, f" SL: {sl}", color="#ef5350", fontsize=8, va="center", fontweight="bold")
        ax.text(label_x, tp1, f" TP1: {tp1}", color="#26a69a", fontsize=8, va="center", fontweight="bold")
        ax.text(label_x, tp2, f" TP2: {tp2}", color="#00e676", fontsize=8, va="center", fontweight="bold")

        direction_txt = "LONG (BUY)" if is_long else "SHORT (SELL)"
        ax.set_title(f"{asset_title}  |  {direction_txt}  |  Entry: {entry}", color="#ffffff", fontsize=11, fontweight="bold", pad=12)

        ax.grid(color="#2a2e39", linestyle="--", linewidth=0.5, alpha=0.6)
        ax.tick_params(colors="#b2b5be", labelsize=8)
        for spine in ax.spines.values():
            spine.set_color("#2a2e39")

        ax.legend(loc="upper left", facecolor="#1e222d", edgecolor="#2a2e39", labelcolor="#ffffff", fontsize=8)
        plt.tight_layout()

        buf = io.BytesIO()
        plt.savefig(buf, format="png", facecolor=fig.get_facecolor(), edgecolor="none")
        plt.close(fig)
        buf.seek(0)
        return buf
    except Exception as e:
        print(f"Chart error: {e}")
        plt.close("all")
        return None

# ==================== 2. جلب بيانات السوق ====================
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

# ==================== 3. حساب المؤشرات الفنية والـ SMC ====================
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

# ==================== 4. التحليل الفوري والمضاربة مع توليد الشارت ====================
def send_instant_with_chart(htf_df, ltf_df, asset_name, chart_code, chat_id=None):
    if htf_df.empty or ltf_df.empty or len(htf_df) < 205 or len(ltf_df) < 205:
        send_msg(f"⚠️ جاري تحديث بيانات {asset_name}، حاول بعد لحظات.", chat_id)
        return

    htf = enrich_indicators(htf_df)
    ltf = enrich_indicators(ltf_df)
    h_curr, l_curr = htf.iloc[-1], ltf.iloc[-1]

    price = round(float(l_curr["close"]), 2)
    atr = float(l_curr["ATR"])
    rsi = round(float(l_curr["RSI"]), 1)
    adx = round(float(l_curr["ADX"]), 1)
    s_low = round(float(l_curr["swing_low"]), 2)
    s_high = round(float(l_curr["swing_high"]), 2)

    htf_trend = "صاعد 🟢" if h_curr["close"] > h_curr["EMA_200"] else "هابط 🔴"
    ltf_trend = "صاعد 🟢" if price > l_curr["EMA_200"] else "هابط 🔴"
    is_long = price > l_curr["EMA_50"]

    if is_long:
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

    caption = (
        f"📊 *التحليل العميق الفوري: {asset_name}*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"💵 *السعر الحالي:* `{price}`\n"
        f"🧭 *الاتجاه العام (4H):* {htf_trend}\n"
        f"⏱ *الاتجاه اللحظي (15M):* {ltf_trend}\n"
        f"🔥 *قوة الاتجاه (ADX):* `{adx}` | *RSI:* `{rsi}`\n"
        f"🧱 *أقرب دعم ومقاومة:* `{s_low}` | `{s_high}`\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🎯 *الصفقة المقترحة حالياً:* {side}\n"
        f"📍 *سعر الدخول:* `{price}`\n"
        f"🛑 *وقف الخسارة (SL):* `{sl}`\n"
        f"✅ *الهدف الأول (TP1):* `{tp1}`\n"
        f"🚀 *الهدف الثاني (TP2):* `{tp2}`"
    )
    chart_buf = generate_chart_image(ltf, f"{chart_code} (15m)", price, sl, tp1, tp2, is_long)
    send_photo_msg(chart_buf, caption, chat_id)

def send_scalp_with_chart(m5_df, m1_df, asset_name, chart_code, chat_id=None):
    if m5_df.empty or m1_df.empty or len(m5_df) < 100 or len(m1_df) < 100:
        send_msg(f"⚠️ تعذر جلب بيانات السكالبينج لـ {asset_name} حالياً.", chat_id)
        return

    m5 = enrich_indicators(m5_df)
    m1 = enrich_indicators(m1_df)
    c5, c1 = m5.iloc[-1], m1.iloc[-1]

    price = round(float(c1["close"]), 2)
    atr_1m = max(float(c1["ATR"]), 0.01)
    rsi_1m = round(float(c1["RSI"]), 1)
    adx_5m = round(float(c5["ADX"]), 1)
    m_low = float(c1["micro_low"])
    m_high = float(c1["micro_high"])

    m5_bull = c5["close"] > c5["EMA_50"]
    m1_bull = price > c1["EMA_20"]
    smc_1m = detect_smc(m1)
    is_long = (m5_bull and m1_bull) or (m5_bull and rsi_1m < 45)

    if is_long:
        scalp_dir = "شراء سريع (SCALP LONG) 🟢"
        reason = smc_1m["p_bull"] if smc_1m["bull"] else "اندفاع شرائي أعلى EMA 20/50"
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

    caption = (
        f"⚡ *صفقة مضاربة سريعة (5M + 1M): {asset_name}*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"💵 *السعر اللحظي:* `{price}`\n"
        f"🧭 *الزخم السريع:* {scalp_dir}\n"
        f"🧠 *إشارة الدقيقة (1M):* {reason}\n"
        f"📈 *مؤشر RSI (1M):* `{rsi_1m}` | *ADX (5M):* `{adx_5m}`\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🎯 *سعر الدخول الفوري:* `{price}`\n"
        f"🛑 *وقف الخسارة الضيق (SL):* `{sl}`\n"
        f"✅ *هدف السكالب الأول (TP1):* `{tp1}`\n"
        f"🚀 *هدف السكالب الثاني (TP2):* `{tp2}`"
    )
    chart_buf = generate_chart_image(m1, f"{chart_code} Scalp (1m)", price, sl, tp1, tp2, is_long)
    send_photo_msg(chart_buf, caption, chat_id)

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
                            send_msg("🤖 *أهلاً بك!* اضغط على أي زر بالأسفل ليقوم البوت برسم الشارت وإرسال الصفقة فوراً:", chat_id)
                        elif text in ["/gold", "🪙 تحليل الذهب فوراً"]:
                            send_msg("⏳ *جاري رسم شارت الذهب وحساب المستويات...*", chat_id)
                            send_instant_with_chart(get_gold_ohlcv(HTF), get_gold_ohlcv(LTF), "XAU/USD (الذهب)", "XAU/USD", chat_id)
                        elif text in ["/btc", "₿ تحليل البيتكوين فوراً"]:
                            send_msg("⏳ *جاري رسم شارت البيتكوين وحساب المستويات...*", chat_id)
                            send_instant_with_chart(get_btc_ohlcv(HTF), get_btc_ohlcv(LTF), "BTC/USDT (البيتكوين)", "BTC/USDT", chat_id)
                        elif text in ["/scalp", "⚡ مضاربة سريعة"]:
                            send_msg("⚡ *جاري رسم شارت السكالبينج (5m + 1m) للذهب والبيتكوين...*", chat_id)
                            send_scalp_with_chart(get_gold_ohlcv("5m"), get_gold_ohlcv("1m"), "XAU/USD (الذهب)", "XAU/USD", chat_id)
                            send_scalp_with_chart(get_btc_ohlcv("5m"), get_btc_ohlcv("1m"), "BTC/USDT (البيتكوين)", "BTC/USDT", chat_id)
                        elif text in ["/status", "/all", "📊 فحص السوق الشامل"]:
                            send_msg("⏳ *جاري الفحص الشامل ورسم الشارتات...*", chat_id)
                            send_instant_with_chart(get_gold_ohlcv(HTF), get_gold_ohlcv(LTF), "XAU/USD (الذهب)", "XAU/USD", chat_id)
                            send_instant_with_chart(get_btc_ohlcv(HTF), get_btc_ohlcv(LTF), "BTC/USDT (البيتكوين)", "BTC/USDT", chat_id)
        except Exception as e:
            print(f"Listener error: {e}")
        time.sleep(2)

# ==================== 6. الرادار التلقائي للصفقات الذهبية مع الشارت ====================
def check_auto_signal(htf_df, ltf_df, asset_name, chart_code):
    if htf_df.empty or ltf_df.empty or len(htf_df) < 205 or len(ltf_df) < 205:
        return None
    htf = enrich_indicators(htf_df)
    ltf = enrich_indicators(ltf_df)
    h_curr, l_curr = htf.iloc[-2], ltf.iloc[-2]

    price = round(float(l_curr["close"]), 2)
    atr = float(l_curr["ATR"])
    adx = round(float(l_curr["ADX"]), 1)
    rsi = float(l_curr["RSI"])

    if float(l_curr["high"] - l_curr["low"]) > (2.8 * atr) or adx < 22:
        return None

    htf_bull = h_curr["close"] > h_curr["EMA_200"] and h_curr["EMA_50"] > h_curr["EMA_200"]
    htf_bear = h_curr["close"] < h_curr["EMA_200"] and h_curr["EMA_50"] < h_curr["EMA_200"]
    smc = detect_smc(ltf)

    if htf_bull and (price > l_curr["EMA_200"]) and smc["bull"] and (45 <= rsi <= 67):
        sl = min(float(l_curr["low"]), float(l_curr["swing_low"])) - (0.4 * atr)
        risk = min(max(price - sl, 0.5 * atr), 1.8 * atr)
        sl = round(price - risk, 2)
        tp1 = round(price + 1.5 * risk, 2)
        tp2 = round(price + 3.0 * risk, 2)
        chart_buf = generate_chart_image(ltf, f"{chart_code} (15m)", price, sl, tp1, tp2, True)
        return {
            "asset": asset_name, "dir": "شراء مؤسسي (LONG) 🟢", "pat": smc["p_bull"],
            "entry": price, "sl": sl, "tp1": tp1, "tp2": tp2,
            "time": str(l_curr["timestamp"]), "chart": chart_buf
        }

    if htf_bear and (price < l_curr["EMA_200"]) and smc["bear"] and (33 <= rsi <= 55):
        sl = max(float(l_curr["high"]), float(l_curr["swing_high"])) + (0.4 * atr)
        risk = min(max(sl - price, 0.5 * atr), 1.8 * atr)
        sl = round(price + risk, 2)
        tp1 = round(price - 1.5 * risk, 2)
        tp2 = round(price - 3.0 * risk, 2)
        chart_buf = generate_chart_image(ltf, f"{chart_code} (15m)", price, sl, tp1, tp2, False)
        return {
            "asset": asset_name, "dir": "بيع مؤسسي (SHORT) 🔴", "pat": smc["p_bear"],
            "entry": price, "sl": sl, "tp1": tp1, "tp2": tp2,
            "time": str(l_curr["timestamp"]), "chart": chart_buf
        }
    return None

def scanner_loop():
    send_msg(
        "🎨 *تم تفعيل ميزة الرسم البياني المصور (TradingView Chart) بنجاح!*\n"
        "اضغط على أي زر بالأسفل لتصلك الصفقة مرفقة بصورة الشارت ومناطق الدخول والستوب والأهداف."
    )
    while True:
        try:
            for key, name, code, fn in [
                ("BTC", "BTC/USDT (البيتكوين)", "BTC/USDT", get_btc_ohlcv),
                ("GOLD", "XAU/USD (الذهب)", "XAU/USD", get_gold_ohlcv)
            ]:
                sig = check_auto_signal(fn(HTF), fn(LTF), name, code)
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
                    send_photo_msg(sig["chart"], alert)
                    last_signal_candle[key] = sig["time"]
        except Exception as e:
            print(f"Scanner error: {e}")
        time.sleep(CHECK_INTERVAL)

@app.route("/")
def home():
    return "Pro Trading Bot with Live Charts is Active 24/7!"

if __name__ == "__main__":
    threading.Thread(target=telegram_listener, daemon=True).start()
    threading.Thread(target=scanner_loop, daemon=True).start()
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
