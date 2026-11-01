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
import matplotlib.patches as patches
from flask import Flask

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL", "https://trading-bot-1-88ha.onrender.com").strip()

HTF = "4h"
LTF = "15m"
CHECK_INTERVAL = 180
TRACKER_INTERVAL = 40

last_signal_candle = {"BTC": None, "GOLD": None}
active_trades = {}
trades_lock = threading.Lock()

app = Flask(__name__)

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
        requests.post(url, json=payload, timeout=12)
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
        res = requests.post(url, data=data, files=files, timeout=25)
        if not res.ok:
            send_msg(caption, target)
    except Exception as e:
        print(f"Telegram photo error: {e}")
        send_msg(caption, target)

# ==================== 1. جلب البيانات بشكل مضاد للأخطاء والعطلات ====================
def clean_yf_dataframe(raw):
    if raw is None or raw.empty:
        return pd.DataFrame()
    if isinstance(raw.columns, pd.MultiIndex):
        raw.columns = raw.columns.get_level_values(0)
    raw = raw.reset_index()
    col_map = {}
    for c in raw.columns:
        cl = str(c).lower()
        if cl in ["datetime", "date", "index"]:
            col_map[c] = "timestamp"
        elif cl == "open":
            col_map[c] = "open"
        elif cl == "high":
            col_map[c] = "high"
        elif cl == "low":
            col_map[c] = "low"
        elif cl == "close":
            col_map[c] = "close"
        elif cl == "volume":
            col_map[c] = "volume"
    raw = raw.rename(columns=col_map)
    needed = ["timestamp", "open", "high", "low", "close", "volume"]
    for col in needed:
        if col not in raw.columns:
            raw[col] = 0.0
    return raw[needed].dropna(subset=["open", "high", "low", "close"])

def get_btc_ohlcv(timeframe, limit=250):
    for ex_name in ["kucoin", "kraken", "bybit", "binance"]:
        try:
            exchange = getattr(ccxt, ex_name)({"enableRateLimit": True, "timeout": 10000})
            symbol = "BTC/USDT" if ex_name != "kraken" else "BTC/USD"
            bars = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
            if bars and len(bars) > 30:
                df = pd.DataFrame(bars, columns=["timestamp", "open", "high", "low", "close", "volume"])
                df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
                return df
        except Exception:
            continue
    # بديل احتياطي للبيتكوين عبر Yahoo Finance إذا حُظرت المنصات
    try:
        ticker = yf.Ticker("BTC-USD")
        period = "60d" if timeframe == "4h" else ("15d" if timeframe == "15m" else "5d")
        interval = "1h" if timeframe == "4h" else timeframe
        raw = clean_yf_dataframe(ticker.history(period=period, interval=interval))
        if timeframe == "4h" and not raw.empty:
            raw.set_index("timestamp", inplace=True)
            raw = raw.resample("4h").agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}).dropna().reset_index()
        return raw
    except Exception:
        return pd.DataFrame()

def get_gold_ohlcv(timeframe):
    for sym in ["GC=F", "XAUUSD=X"]:
        try:
            ticker = yf.Ticker(sym)
            if timeframe == "4h":
                raw = clean_yf_dataframe(ticker.history(period="60d", interval="1h"))
                if not raw.empty:
                    raw.set_index("timestamp", inplace=True)
                    df = raw.resample("4h").agg({
                        "open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"
                    }).dropna().reset_index()
                    if len(df) > 30:
                        return df
            elif timeframe == "15m":
                df = clean_yf_dataframe(ticker.history(period="15d", interval="15m"))
                if len(df) > 30:
                    return df
            elif timeframe == "5m":
                df = clean_yf_dataframe(ticker.history(period="5d", interval="5m"))
                if len(df) > 30:
                    return df
            elif timeframe == "1m":
                df = clean_yf_dataframe(ticker.history(period="5d", interval="1m"))
                if len(df) > 30:
                    return df
                # في حال عطلة نهاية الأسبوع، نستخدم فريم 5 دقائق بدلاً من التوقف
                df_fallback = clean_yf_dataframe(ticker.history(period="5d", interval="5m"))
                if len(df_fallback) > 30:
                    return df_fallback
        except Exception as e:
            print(f"Gold error ({sym} - {timeframe}): {e}")
    return pd.DataFrame()

# ==================== 2. رصد الفجوات السعرية (FVG) ورسم الشارت ====================
def find_fvg_zones(df):
    fvg_list = []
    if len(df) < 5:
        return fvg_list
    for i in range(2, len(df)):
        c1 = df.iloc[i-2]
        c3 = df.iloc[i]
        if c3["low"] > c1["high"]:
            fvg_list.append({"type": "bullish", "bottom": float(c1["high"]), "top": float(c3["low"]), "index": i})
        elif c3["high"] < c1["low"]:
            fvg_list.append({"type": "bearish", "bottom": float(c3["high"]), "top": float(c1["low"]), "index": i})
    return fvg_list[-3:]

def generate_chart_image(df, asset_title, entry, sl, tp1, tp2, is_long=True):
    try:
        plot_df = df.tail(60).reset_index(drop=True)
        fig, ax = plt.subplots(figsize=(10, 5.8), dpi=115)
        fig.patch.set_facecolor("#131722")
        ax.set_facecolor("#131722")

        for idx, row in plot_df.iterrows():
            o, h, l, c = float(row["open"]), float(row["high"]), float(row["low"]), float(row["close"])
            color = "#26a69a" if c >= o else "#ef5350"
            ax.plot([idx, idx], [l, h], color=color, linewidth=1.1, zorder=2)
            body_bottom = min(o, c)
            body_height = max(abs(c - o), (h - l) * 0.02, 1e-4)
            ax.bar(idx, body_height, bottom=body_bottom, width=0.62, color=color, edgecolor=color, zorder=3)

        fvgs = find_fvg_zones(plot_df)
        for fvg in fvgs:
            rect_color = "#26a69a" if fvg["type"] == "bullish" else "#ef5350"
            rect = patches.Rectangle(
                (fvg["index"] - 1, fvg["bottom"]),
                width=len(plot_df) - (fvg["index"] - 1),
                height=fvg["top"] - fvg["bottom"],
                facecolor=rect_color, alpha=0.22, edgecolor=rect_color, linestyle=":", linewidth=1, zorder=1
            )
            ax.add_patch(rect)

        if "EMA_50" in plot_df.columns:
            ax.plot(plot_df.index, plot_df["EMA_50"], color="#2962ff", linewidth=1.3, label="EMA 50", zorder=4)
        if "EMA_200" in plot_df.columns:
            ax.plot(plot_df.index, plot_df["EMA_200"], color="#ff9800", linewidth=1.5, label="EMA 200", zorder=4)

        x_start = max(len(plot_df) - 18, 0)
        x_end = len(plot_df) + 3
        ax.set_xlim(-1, x_end + 4)

        ax.axhline(entry, color="#ffffff", linestyle="--", linewidth=1.1, alpha=0.85)
        ax.axhline(sl, color="#ef5350", linestyle="--", linewidth=1.2, alpha=0.9)
        ax.axhline(tp1, color="#26a69a", linestyle=":", linewidth=1.2, alpha=0.9)
        ax.axhline(tp2, color="#00e676", linestyle="--", linewidth=1.3, alpha=0.95)

        ax.fill_between([x_start, x_end], entry, sl, color="#ef5350", alpha=0.14)
        ax.fill_between([x_start, x_end], entry, tp2, color="#26a69a", alpha=0.14)

        label_x = x_end + 0.3
        ax.text(label_x, entry, f" ENTRY: {entry}", color="#ffffff", fontsize=8, va="center", fontweight="bold")
        ax.text(label_x, sl, f" SL: {sl}", color="#ef5350", fontsize=8, va="center", fontweight="bold")
        ax.text(label_x, tp1, f" TP1: {tp1}", color="#26a69a", fontsize=8, va="center", fontweight="bold")
        ax.text(label_x, tp2, f" TP2: {tp2}", color="#00e676", fontsize=8, va="center", fontweight="bold")

        direction_txt = "LONG (BUY)" if is_long else "SHORT (SELL)"
        ax.set_title(f"{asset_title}  |  {direction_txt}  |  FVG + SMC", color="#ffffff", fontsize=11, fontweight="bold", pad=12)

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

# ==================== 3. حساب المؤشرات الفنية ====================
def enrich_indicators(df):
    df = df.copy()
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["open", "high", "low", "close"])

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
    df["ATR"] = tr.ewm(alpha=1/14, adjust=False).mean().bfill()

    up_move = df["high"].diff()
    down_move = -df["low"].diff()
    plus_dm = up_move.where((up_move > down_move) & (up_move > 0), 0.0)
    minus_dm = down_move.where((down_move > up_move) & (down_move > 0), 0.0)
    atr_safe = df["ATR"].replace(0, 1e-9)
    plus_di = 100 * (plus_dm.ewm(alpha=1/14, adjust=False).mean() / atr_safe)
    minus_di = 100 * (minus_dm.ewm(alpha=1/14, adjust=False).mean() / atr_safe)
    dx = 100 * ((plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, 1e-9))
    df["ADX"] = dx.ewm(alpha=1/14, adjust=False).mean().fillna(20.0)

    df["swing_low"] = df["low"].rolling(window=10, min_periods=1).min().shift(1).bfill()
    df["swing_high"] = df["high"].rolling(window=10, min_periods=1).max().shift(1).bfill()
    df["micro_low"] = df["low"].rolling(window=5, min_periods=1).min().shift(1).bfill()
    df["micro_high"] = df["high"].rolling(window=5, min_periods=1).max().shift(1).bfill()
    return df

# ==================== 4. التحليل الفوري والسكالبينج ====================
def register_trade(trade_key, asset_name, symbol_type, is_long, entry, sl, tp1, tp2, mode_label):
    with trades_lock:
        active_trades[trade_key] = {
            "asset": asset_name, "symbol_type": symbol_type, "is_long": is_long,
            "entry": float(entry), "sl": float(sl), "tp1": float(tp1), "tp2": float(tp2),
            "tp1_hit": False, "mode": mode_label, "opened_at": time.strftime("%H:%M UTC", time.gmtime())
        }

def send_instant_with_chart(htf_df, ltf_df, asset_name, chart_code, sym_type, chat_id=None):
    try:
        if ltf_df.empty or len(ltf_df) < 30:
            send_msg(f"⚠️ سوق {asset_name} في عطلة حالياً أو لا توجد بيانات كافية من المصدر.", chat_id)
            return
        if htf_df.empty or len(htf_df) < 20:
            htf_df = ltf_df

        htf = enrich_indicators(htf_df)
        ltf = enrich_indicators(ltf_df)
        h_curr, l_curr = htf.iloc[-1], ltf.iloc[-1]

        price = round(float(l_curr["close"]), 2)
        atr = max(float(l_curr["ATR"]), price * 0.001)
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

        register_trade(f"{sym_type}_15M", asset_name, sym_type, is_long, price, sl, tp1, tp2, "15M")

        caption = (
            f"📊 *التحليل العميق الفوري: {asset_name}*\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"💵 *السعر الحالي:* `{price}`\n"
            f"🧭 *الاتجاه العام (4H):* {htf_trend}\n"
            f"⏱ *الاتجاه اللحظي (15M):* {ltf_trend}\n"
            f"🔥 *قوة الاتجاه (ADX):* `{adx}` | *RSI:* `{rsi}`\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"🎯 *الصفقة المقترحة:* {side}\n"
            f"📍 *سعر الدخول:* `{price}`\n"
            f"🛑 *وقف الخسارة (SL):* `{sl}`\n"
            f"✅ *الهدف الأول (TP1):* `{tp1}`\n"
            f"🚀 *الهدف الثاني (TP2):* `{tp2}`\n"
            f"📡 *تم تفعيل المتابعة التلقائية للصفقة.*"
        )
        chart_buf = generate_chart_image(ltf, f"{chart_code} (15m)", price, sl, tp1, tp2, is_long)
        send_photo_msg(chart_buf, caption, chat_id)
    except Exception as e:
        send_msg(f"⚠️ حدث خطأ أثناء تحليل {asset_name}: `{e}`", chat_id)

def send_scalp_with_chart(m5_df, m1_df, asset_name, chart_code, sym_type, chat_id=None):
    try:
        if m1_df.empty or len(m1_df) < 30:
            send_msg(f"⚠️ بيانات الدقيقة لـ {asset_name} غير متوفرة حالياً (قد يكون السوق مغلقاً في عطلة نهاية الأسبوع).", chat_id)
            return
        if m5_df.empty or len(m5_df) < 30:
            m5_df = m1_df

        m5 = enrich_indicators(m5_df)
        m1 = enrich_indicators(m1_df)
        c5, c1 = m5.iloc[-1], m1.iloc[-1]

        price = round(float(c1["close"]), 2)
        atr_1m = max(float(c1["ATR"]), price * 0.0005)
        rsi_1m = round(float(c1["RSI"]), 1)
        adx_5m = round(float(c5["ADX"]), 1)
        is_long = (c5["close"] > c5["EMA_50"] and price > c1["EMA_20"])

        if is_long:
            scalp_dir = "شراء سريع (SCALP LONG) 🟢"
            sl = round(price - (1.4 * atr_1m), 2)
            risk = max(price - sl, atr_1m)
            tp1 = round(price + (1.4 * risk), 2)
            tp2 = round(price + (2.5 * risk), 2)
        else:
            scalp_dir = "بيع سريع (SCALP SHORT) 🔴"
            sl = round(price + (1.4 * atr_1m), 2)
            risk = max(sl - price, atr_1m)
            tp1 = round(price - (1.4 * risk), 2)
            tp2 = round(price - (2.5 * risk), 2)

        register_trade(f"{sym_type}_SCALP", asset_name, sym_type, is_long, price, sl, tp1, tp2, "سكالبينج")

        caption = (
            f"⚡ *صفقة مضاربة سريعة (5M + 1M): {asset_name}*\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"💵 *السعر اللحظي:* `{price}`\n"
            f"🧭 *الزخم السريع:* {scalp_dir}\n"
            f"📈 *مؤشر RSI:* `{rsi_1m}` | *ADX:* `{adx_5m}`\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"🎯 *سعر الدخول الفوري:* `{price}`\n"
            f"🛑 *وقف الخسارة الضيق (SL):* `{sl}`\n"
            f"✅ *هدف السكالب الأول (TP1):* `{tp1}`\n"
            f"🚀 *هدف السكالب الثاني (TP2):* `{tp2}`"
        )
        chart_buf = generate_chart_image(m1, f"{chart_code} Scalp", price, sl, tp1, tp2, is_long)
        send_photo_msg(chart_buf, caption, chat_id)
    except Exception as e:
        send_msg(f"⚠️ حدث خطأ أثناء سكالبينج {asset_name}: `{e}`", chat_id)

# ==================== 5. الاستماع لأوامر تيليجرام والنبض الذاتي ====================
def format_active_trades_report():
    with trades_lock:
        if not active_trades:
            return "📂 *لا توجد صفقات نشطة قيد المراقبة حالياً.*"
        snapshot = dict(active_trades)
    lines = ["📂 *قائمة الصفقات النشطة قيد المراقبة:*\n━━━━━━━━━━━━━━━━━━"]
    for key, t in snapshot.items():
        dir_str = "شراء 🟢" if t["is_long"] else "بيع 🔴"
        lines.append(f"🔹 *{t['asset']}* ({t['mode']}) | {dir_str}\n   الدخول: `{t['entry']}` | SL: `{t['sl']}` | TP1: `{t['tp1']}` | TP2: `{t['tp2']}`")
    return "\n".join(lines)

def telegram_listener():
    # إلغاء أي Webhook قديم قد يعطل قراءة الرسائل
    if TELEGRAM_BOT_TOKEN:
        try:
            requests.get(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/deleteWebhook", timeout=10)
        except Exception:
            pass

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
                            send_msg("✅ *البوت مستيقظ ويعمل بنجاح!* اختر التحليل المطلوب من الأزرار بالأسفل:", chat_id)
                        elif text in ["/gold", "🪙 تحليل الذهب فوراً"]:
                            send_msg("⏳ *جاري تحليل الذهب ورسم مستطيلات FVG...*", chat_id)
                            send_instant_with_chart(get_gold_ohlcv(HTF), get_gold_ohlcv(LTF), "XAU/USD (الذهب)", "XAU/USD", "GOLD", chat_id)
                        elif text in ["/btc", "₿ تحليل البيتكوين فوراً"]:
                            send_msg("⏳ *جاري تحليل البيتكوين ورسم مستطيلات FVG...*", chat_id)
                            send_instant_with_chart(get_btc_ohlcv(HTF), get_btc_ohlcv(LTF), "BTC/USDT (البيتكوين)", "BTC/USDT", "BTC", chat_id)
                        elif text in ["/scalp", "⚡ مضاربة سريعة"]:
                            send_msg("⚡ *جاري تجهيز صفقات السكالبينج (5M + 1M)...*", chat_id)
                            send_scalp_with_chart(get_gold_ohlcv("5m"), get_gold_ohlcv("1m"), "XAU/USD (الذهب)", "XAU/USD", "GOLD", chat_id)
                            send_scalp_with_chart(get_btc_ohlcv("5m"), get_btc_ohlcv("1m"), "BTC/USDT (البيتكوين)", "BTC/USDT", "BTC", chat_id)
                        elif text in ["/status", "/all", "📊 فحص السوق الشامل"]:
                            send_msg("⏳ *جاري الفحص الشامل للذهب والبيتكوين...*", chat_id)
                            send_instant_with_chart(get_gold_ohlcv(HTF), get_gold_ohlcv(LTF), "XAU/USD (الذهب)", "XAU/USD", "GOLD", chat_id)
                            send_instant_with_chart(get_btc_ohlcv(HTF), get_btc_ohlcv(LTF), "BTC/USDT (البيتكوين)", "BTC/USDT", "BTC", chat_id)
                        elif text in ["/trades", "📂 الصفقات النشطة"]:
                            send_msg(format_active_trades_report(), chat_id)
                        elif text in ["/clear", "🗑️ مسح الصفقات النشطة"]:
                            with trades_lock:
                                active_trades.clear()
                            send_msg("🗑️ *تم مسح الصفقات النشطة بنجاح.*", chat_id)
        except Exception as e:
            print(f"Listener error: {e}")
        time.sleep(2)

def keep_alive_pinger():
    """نبض داخلي كل 8 دقائق للمساعدة في إبقاء الخادم نشطاً"""
    while True:
        time.sleep(480)
        try:
            requests.get(f"{RENDER_EXTERNAL_URL}/health", timeout=10)
        except Exception:
            pass

@app.route("/")
def home():
    return "Pro Trading Bot is Awake and Running 24/7!"

@app.route("/health")
def health():
    return {"status": "ok"}, 200

if __name__ == "__main__":
    threading.Thread(target=telegram_listener, daemon=True).start()
    threading.Thread(target=keep_alive_pinger, daemon=True).start()
    send_msg("🚀 *تم إعادة تشغيل البوت بنسخته المضادة للتوقف بنجاح!*\nاضغط على أي زر بالأسفل لتجربة التحليل الفوري.")
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
