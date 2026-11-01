import os
import io
import json
import time
import threading
import wave
import struct
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

TRACKER_INTERVAL = 30
active_trades = {}
trades_lock = threading.Lock()

user_settings = {
    "mode_name": "مضاربة متوسطة (15m / 4h)",
    "ltf": "15m",
    "htf": "4h"
}

TIMEFRAME_MODES = {
    "scalp": {"name": "سكالبينج سريع (5m / 15m) ⚡", "ltf": "5m", "htf": "15m"},
    "intraday": {"name": "مضاربة يومية (15m / 4h) ⏱", "ltf": "15m", "htf": "4h"},
    "swing": {"name": "طويل المدى Swing (4h / 1d) 🌊", "ltf": "4h", "htf": "1d"}
}

HTF_MAP = {
    "1m": "15m", "5m": "15m", "15m": "4h", "1h": "4h", "4h": "1d", "1d": "1d"
}

app = Flask(__name__)

REPLY_KEYBOARD = {
    "keyboard": [
        [{"text": "🪙 مناطق دخول الذهب"}, {"text": "₿ مناطق دخول البيتكوين"}],
        [{"text": "⚡ فريم سكالب (5m)"}, {"text": "⏱ فريم متوسط (15m)"}, {"text": "🌊 فريم طويل (4h/1d)"}],
        [{"text": "📊 فحص شامل بالسوق"}, {"text": "📂 الأوامر المعلقة والنشطة"}, {"text": "🗑️ مسح الصفقات"}]
    ],
    "resize_keyboard": True,
    "is_persistent": True
}

def get_inline_tf_buttons(sym_type):
    return {
        "inline_keyboard": [
            [
                {"text": "⚡ 5m", "callback_data": f"TF|{sym_type}|5m"},
                {"text": "⏱ 15m", "callback_data": f"TF|{sym_type}|15m"},
                {"text": "🕐 1h", "callback_data": f"TF|{sym_type}|1h"},
                {"text": "🕓 4h", "callback_data": f"TF|{sym_type}|4h"},
                {"text": "📅 1d يومي", "callback_data": f"TF|{sym_type}|1d"}
            ]
        ]
    }

def send_msg(text, chat_id=None, inline_markup=None):
    target = chat_id or TELEGRAM_CHAT_ID
    if not TELEGRAM_BOT_TOKEN or not target: return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    markup = inline_markup if inline_markup else REPLY_KEYBOARD
    try:
        requests.post(url, json={"chat_id": target, "text": text, "parse_mode": "Markdown", "reply_markup": markup}, timeout=12)
    except Exception as e: print(f"Telegram text error: {e}")

def generate_beep_wav():
    """توليد ملف صوتي تنبيهي (Beep) بصيغة WAV في الذاكرة دون الحاجة لملفات خارجية"""
    sample_rate = 8000
    duration = 0.6  # مدة الصوت ثانية تقريباً
    frequency = 880 # نغمة حادة وواضحة
    num_samples = int(sample_rate * duration)
    
    buf = io.BytesIO()
    with wave.open(buf, 'w') as wav_file:
        wav_file.setnchannels(1) # مونو
        wav_file.setsampwidth(2) # 16-bit
        wav_file.setframerate(sample_rate)
        
        import math
        for i in range(num_samples):
            value = int(32767.0 * 0.5 * math.sin(2.0 * math.pi * frequency * (i / sample_rate)))
            data = struct.pack('<h', value)
            wav_file.writeframes(data)
    buf.seek(0)
    return buf

def send_voice_alert(caption, chat_id=None):
    target = chat_id or TELEGRAM_CHAT_ID
    if not TELEGRAM_BOT_TOKEN or not target: return
    
    audio_buf = generate_beep_wav()
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendVoice"
    data = {"chat_id": target, "caption": caption, "parse_mode": "Markdown"}
    files = {"voice": ("alert.wav", audio_buf, "audio/wav")}
    try:
        requests.post(url, data=data, files=files, timeout=20)
    except Exception as e:
        print(f"Voice alert error: {e}")
        send_msg(caption, target)

def send_photo_msg(photo_buf, caption, sym_type="GOLD", chat_id=None):
    target = chat_id or TELEGRAM_CHAT_ID
    if not TELEGRAM_BOT_TOKEN or not target: return
    inline_kb = get_inline_tf_buttons(sym_type)
    if photo_buf is None:
        send_msg(caption, target, inline_kb)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
    try:
        requests.post(url, data={"chat_id": target, "caption": caption, "parse_mode": "Markdown", "reply_markup": json.dumps(inline_kb)}, files={"photo": ("chart.png", photo_buf, "image/png")}, timeout=25)
    except Exception as e:
        print(f"Photo error: {e}")
        send_msg(caption, target, inline_kb)

# ==================== 1. جلب البيانات والمؤشرات ====================
def clean_yf_dataframe(raw):
    if raw is None or raw.empty: return pd.DataFrame()
    if isinstance(raw.columns, pd.MultiIndex): raw.columns = raw.columns.get_level_values(0)
    raw = raw.reset_index()
    col_map = {}
    for c in raw.columns:
        cl = str(c).lower()
        if cl in ["datetime", "date", "index"]: col_map[c] = "timestamp"
        elif cl == "open": col_map[c] = "open"
        elif cl == "high": col_map[c] = "high"
        elif cl == "low": col_map[c] = "low"
        elif cl == "close": col_map[c] = "close"
        elif cl == "volume": col_map[c] = "volume"
    raw = raw.rename(columns=col_map)
    needed = ["timestamp", "open", "high", "low", "close", "volume"]
    for col in needed:
        if col not in raw.columns: raw[col] = 0.0
    return raw[needed].dropna(subset=["open", "high", "low", "close"])

def get_btc_ohlcv(timeframe, limit=220):
    for ex_name in ["kucoin", "kraken", "bybit", "binance"]:
        try:
            exchange = getattr(ccxt, ex_name)({"enableRateLimit": True, "timeout": 10000})
            symbol = "BTC/USDT" if ex_name != "kraken" else "BTC/USD"
            bars = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
            if bars and len(bars) > 35:
                df = pd.DataFrame(bars, columns=["timestamp", "open", "high", "low", "close", "volume"])
                df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
                return df
        except Exception: continue
    return get_yf_asset_ohlcv("BTC-USD", timeframe)

def get_yf_asset_ohlcv(ticker_sym, timeframe):
    try:
        ticker = yf.Ticker(ticker_sym)
        if timeframe == "1d": return clean_yf_dataframe(ticker.history(period="1y", interval="1d"))
        elif timeframe == "4h":
            raw = clean_yf_dataframe(ticker.history(period="60d", interval="1h"))
            if not raw.empty:
                raw.set_index("timestamp", inplace=True)
                return raw.resample("4h").agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}).dropna().reset_index()
        elif timeframe == "1h": return clean_yf_dataframe(ticker.history(period="30d", interval="1h"))
        elif timeframe == "15m": return clean_yf_dataframe(ticker.history(period="15d", interval="15m"))
        elif timeframe == "5m": return clean_yf_dataframe(ticker.history(period="5d", interval="5m"))
        elif timeframe == "1m":
            df = clean_yf_dataframe(ticker.history(period="5d", interval="1m"))
            if len(df) > 35: return df
            return clean_yf_dataframe(ticker.history(period="5d", interval="5m"))
    except Exception as e: print(f"YF error: {e}")
    return pd.DataFrame()

def get_gold_ohlcv(timeframe):
    for sym in ["GC=F", "XAUUSD=X"]:
        df = get_yf_asset_ohlcv(sym, timeframe)
        if not df.empty and len(df) > 35: return df
    return pd.DataFrame()

def enrich_indicators(df):
    df = df.copy()
    for col in ["open", "high", "low", "close", "volume"]: df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["open", "high", "low", "close"]).reset_index(drop=True)

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
    df["ADX"] = 25.0
    return df

# ==================== 2. تحليل OTE والعرض والطلب ====================
def analyze_structure_and_ote(df, is_long_bias=True):
    recent = df.tail(55).reset_index(drop=True)
    curr_price = float(recent["close"].iloc[-1])
    atr = max(float(recent["ATR"].iloc[-1]), curr_price * 0.001)

    swing_high = float(recent["high"].max())
    swing_low = float(recent["low"].min())
    if swing_high - swing_low < (1.5 * atr):
        swing_high = curr_price + (2.5 * atr)
        swing_low = curr_price - (2.5 * atr)

    total_range = swing_high - swing_low
    eq_50 = round(swing_low + (0.50 * total_range), 2)
    bull_ote_618 = round(swing_high - (0.618 * total_range), 2)
    bull_ote_705 = round(swing_high - (0.705 * total_range), 2)
    bull_ote_786 = round(swing_high - (0.786 * total_range), 2)

    bear_ote_618 = round(swing_low + (0.618 * total_range), 2)
    bear_ote_705 = round(swing_low + (0.705 * total_range), 2)
    bear_ote_786 = round(swing_low + (0.786 * total_range), 2)

    demand_bottom, demand_top = max(swing_low, bull_ote_786 - (0.2 * atr)), max(swing_low + (0.6 * atr), bull_ote_618)
    supply_bottom, supply_top = min(swing_high - (0.6 * atr), bear_ote_618), min(swing_high, bear_ote_786 + (0.2 * atr))

    if is_long_bias:
        ote_zone = (bull_ote_786, bull_ote_618)
        ote_sweet = bull_ote_705
        sniper_zone = (round(min(demand_bottom, bull_ote_786), 2), round(max(demand_top, bull_ote_618), 2))
    else:
        ote_zone = (bear_ote_618, bear_ote_786)
        ote_sweet = bear_ote_705
        sniper_zone = (round(min(supply_bottom, bear_ote_618), 2), round(max(supply_top, bear_ote_786), 2))

    return {
        "bsl": round(swing_high, 2), "ssl": round(swing_low, 2), "eq_50": eq_50,
        "ote_zone": ote_zone, "ote_sweet": ote_sweet, "sniper_zone": sniper_zone,
        "demand_zone": (round(demand_bottom, 2), round(demand_top, 2)),
        "supply_zone": (round(supply_bottom, 2), round(supply_top, 2))
    }

# ==================== 3. رسم الشارت مع OTE ====================
def generate_chart_image(df, zones, asset_title, entry_zone, sl, exit_zone, is_long=True):
    try:
        plot_df = df.tail(55).reset_index(drop=True)
        fig, ax = plt.subplots(figsize=(10.5, 6.0), dpi=115)
        fig.patch.set_facecolor("#131722")
        ax.set_facecolor("#131722")

        for idx, row in plot_df.iterrows():
            o, h, l, c = float(row["open"]), float(row["high"]), float(row["low"]), float(row["close"])
            color = "#26a69a" if c >= o else "#ef5350"
            ax.plot([idx, idx], [l, h], color=color, linewidth=1.1, zorder=3)
            ax.bar(idx, max(abs(c - o), (h - l)*0.02, 1e-4), bottom=min(o, c), width=0.62, color=color, edgecolor=color, zorder=4)

        x_end = len(plot_df) + 4
        ax.set_xlim(-1, x_end + 7)

        d_low, d_high = zones["demand_zone"]
        s_low, s_high = zones["supply_zone"]
        ax.add_patch(patches.Rectangle((0, d_low), width=x_end, height=max(d_high - d_low, 1e-3), facecolor="#00e676", alpha=0.13, edgecolor="#00e676", linewidth=1, zorder=1))
        ax.add_patch(patches.Rectangle((0, s_low), width=x_end, height=max(s_high - s_low, 1e-3), facecolor="#ff5252", alpha=0.13, edgecolor="#ff5252", linewidth=1, zorder=1))

        ote_low, ote_high = zones["ote_zone"]
        ax.add_patch(patches.Rectangle((len(plot_df) - 28, ote_low), width=32, height=max(ote_high - ote_low, 1e-3), facecolor="#ffd700", alpha=0.25, edgecolor="#ffd700", linestyle="--", linewidth=1.4, zorder=2))
        ax.axhline(zones["ote_sweet"], color="#ffd700", linestyle=":", linewidth=1.2, alpha=0.95)
        ax.text(len(plot_df) - 27, zones["ote_sweet"], f" GOLDEN OTE | 70.5%: {zones['ote_sweet']}", color="#ffd700", fontsize=8, va="bottom", fontweight="bold")

        e_mid = round((entry_zone[0] + entry_zone[1]) / 2, 2)
        tp1, tp2 = exit_zone
        ax.axhline(e_mid, color="#ffffff", linestyle="--", linewidth=1.2, alpha=0.95)
        ax.axhline(sl, color="#ff1744", linestyle="-", linewidth=1.3, alpha=0.95)
        ax.axhline(tp1, color="#26a69a", linestyle="--", linewidth=1.1, alpha=0.9)
        ax.axhline(tp2, color="#00e676", linestyle="-", linewidth=1.3, alpha=0.95)

        label_x = x_end + 0.2
        ax.text(label_x, e_mid, f" ENTRY: {e_mid}", color="#ffffff", fontsize=8, va="center", fontweight="bold")
        ax.text(label_x, sl, f" SL: {sl}", color="#ff1744", fontsize=8, va="center", fontweight="bold")
        ax.text(label_x, tp1, f" TP1: {tp1}", color="#26a69a", fontsize=8, va="center", fontweight="bold")
        ax.text(label_x, tp2, f" TP2: {tp2}", color="#00e676", fontsize=8, va="center", fontweight="bold")

        ax.set_title(f"{asset_title}  |  Voice Alert & OTE Touch Active", color="#ffffff", fontsize=10.5, fontweight="bold", pad=10)
        ax.grid(color="#2a2e39", linestyle="--", linewidth=0.5, alpha=0.6)
        ax.tick_params(colors="#b2b5be", labelsize=8)
        for spine in ax.spines.values(): spine.set_color("#2a2e39")

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

# ==================== 4. رادار ملامسة المنطقة والتنبيه الصوتي ====================
def register_pending_trade(trade_key, asset_name, sym_type, is_long, entry_zone, sl, exit_zone, tf_label):
    with trades_lock:
        active_trades[trade_key] = {
            "asset": asset_name, "symbol_type": sym_type, "is_long": is_long,
            "entry_low": float(entry_zone[0]), "entry_high": float(entry_zone[1]),
            "entry_mid": round((entry_zone[0] + entry_zone[1]) / 2, 2),
            "sl": float(sl), "tp1": float(exit_zone[0]), "tp2": float(exit_zone[1]),
            "status": "PENDING", "voice_alert_sent": False, "tp1_hit": False, "tf": tf_label,
            "created_at": time.strftime("%H:%M UTC", time.gmtime())
        }

def get_latest_price_extremes(symbol_type):
    df = get_btc_ohlcv("1m", limit=3) if symbol_type == "BTC" else get_gold_ohlcv("5m")
    if df.empty: return None
    last = df.iloc[-1]
    return {"close": round(float(last["close"]), 2), "high": round(float(last["high"]), 2), "low": round(float(last["low"]), 2)}

def trade_tracker_loop():
    while True:
        try:
            with trades_lock: keys = list(active_trades.keys())
            if keys:
                cache = {}
                for k in keys:
                    with trades_lock: t = active_trades.get(k)
                    if not t: continue
                    sym = t["symbol_type"]
                    if sym not in cache: cache[sym] = get_latest_price_extremes(sym)
                    p = cache[sym]
                    if not p: continue
                    high_p, low_p, curr_p = p["high"], p["low"], p["close"]

                    # إرسال تنبيه صوتي فوري (Voice Note) عند الملامسة
                    if not t.get("voice_alert_sent", False):
                        if low_p <= t["entry_high"] and high_p >= t["entry_low"]:
                            with trades_lock:
                                if k in active_trades: active_trades[k]["voice_alert_sent"] = True
                            send_voice_alert(
                                f"🚨 *تنبيه صوتي: لامس السعر المنطقة الذهبية OTE الآن!*\n"
                                f"📊 *الأصل:* `{t['asset']}` (فريم `{t['tf']}`)\n"
                                f"🎯 *منطقة الدخول:* `{t['entry_low']} - {t['entry_high']}`\n"
                                f"💵 *السعر الحالي:* `{curr_p}`"
                            )

                    if t["status"] == "PENDING":
                        if low_p <= t["entry_high"] and high_p >= t["entry_low"]:
                            with trades_lock:
                                if k in active_trades: active_trades[k]["status"] = "ACTIVE"
                        continue

                    if t["is_long"]:
                        if high_p >= t["tp2"]:
                            send_msg(f"🚀 *تحقق الهدف الثاني TP2 للأصل {t['asset']}!*")
                            with trades_lock: active_trades.pop(k, None)
                        elif (not t["tp1_hit"]) and high_p >= t["tp1"]:
                            with trades_lock:
                                if k in active_trades:
                                    active_trades[k]["tp1_hit"] = True
                                    active_trades[k]["sl"] = t["entry_mid"]
                            send_msg(f"✅ *تحقق TP1 وتأمين الستوب دخول لـ {t['asset']}!*")
                        elif low_p <= t["sl"]:
                            send_msg(f"🛑 *تم ضرب وقف الخسارة لـ {t['asset']}.*")
                            with trades_lock: active_trades.pop(k, None)
                    else:
                        if low_p <= t["tp2"]:
                            send_msg(f"🚀 *تحقق الهدف الثاني TP2 للأصل {t['asset']}!*")
                            with trades_lock: active_trades.pop(k, None)
                        elif (not t["tp1_hit"]) and low_p <= t["tp1"]:
                            with trades_lock:
                                if k in active_trades:
                                    active_trades[k]["tp1_hit"] = True
                                    active_trades[k]["sl"] = t["entry_mid"]
                            send_msg(f"✅ *تحقق TP1 وتأمين الستوب دخول لـ {t['asset']}!*")
                        elif high_p >= t["sl"]:
                            send_msg(f"🛑 *تم ضرب وقف الخسارة لـ {t['asset']}.*")
                            with trades_lock: active_trades.pop(k, None)
        except Exception as e: print(f"Tracker error: {e}")
        time.sleep(15)

def format_active_trades_report():
    with trades_lock:
        if not active_trades: return "📂 *لا توجد صفقات نشطة حالياً.*"
        snap = dict(active_trades)
    lines = ["📂 *الأوامر والصفقات النشطة:*\n━━━━━━━━━━━━━━━━━━"]
    for k, t in snap.items():
        st = "⏳ بانتظار الملامسة" if t["status"] == "PENDING" else "🔥 مفعل بالسوق"
        lines.append(f"🔹 *{t['asset']}* ({t['tf']}) | {st}\n   منطقة OTE: `{t['entry_low']}-{t['entry_high']}`")
    return "\n".join(lines)

def analyze_and_send_zones(sym_type, ltf_str=None, chat_id=None):
    ltf_use = ltf_str or user_settings["ltf"]
    htf_use = HTF_MAP.get(ltf_use, "4h")

    if sym_type == "GOLD":
        asset_name = "XAU/USD (الذهب)"
        chart_code = "XAU/USD"
        ltf_df = get_gold_ohlcv(ltf_use)
        htf_df = get_gold_ohlcv(htf_use)
    else:
        asset_name = "BTC/USDT (البيتكوين)"
        chart_code = "BTC/USDT"
        ltf_df = get_btc_ohlcv(ltf_use)
        htf_df = get_btc_ohlcv(htf_use)

    if ltf_df.empty or len(ltf_df) < 35:
        send_msg(f"⚠️ بيانات {asset_name} غير متوفرة.", chat_id)
        return
    if htf_df.empty: htf_df = ltf_df

    ltf = enrich_indicators(ltf_df)
    htf = enrich_indicators(htf_df)

    l_curr = ltf.iloc[-1]
    curr_price = round(float(l_curr["close"]), 2)
    atr = max(float(l_curr["ATR"]), curr_price * 0.001)

    is_long = curr_price > float(l_curr["EMA_50"])
    zones = analyze_structure_and_ote(ltf, is_long)
    entry_zone = zones["sniper_zone"]
    s_low, s_high = zones["supply_zone"]
    d_low, d_high = zones["demand_zone"]
    bsl, ssl = zones["bsl"], zones["ssl"]
    eq_50 = zones["eq_50"]

    if is_long:
        order_type = "شراء معلق (BUY LIMIT — OTE) 🟢"
        entry_mid = round((entry_zone[0] + entry_zone[1]) / 2, 2)
        sl = round(min(d_low, ssl, entry_zone[0]) - (0.4 * atr), 2)
        risk = max(entry_mid - sl, 0.5 * atr)
        tp1 = round(max(eq_50, entry_mid + (1.6 * risk)), 2)
        tp2 = round(max(bsl, entry_mid + (3.2 * risk)), 2)
        exit_zone = (tp1, tp2)
    else:
        order_type = "بيع معلق (SELL LIMIT — OTE) 🔴"
        entry_mid = round((entry_zone[0] + entry_zone[1]) / 2, 2)
        sl = round(max(s_high, bsl, entry_zone[1]) + (0.4 * atr), 2)
        risk = max(sl - entry_mid, 0.5 * atr)
        tp1 = round(min(eq_50, entry_mid - (1.6 * risk)), 2)
        tp2 = round(min(ssl, entry_mid - (3.2 * risk)), 2)
        exit_zone = (tp1, tp2)

    register_pending_trade(f"{sym_type}_{ltf_use}", asset_name, sym_type, is_long, entry_zone, sl, exit_zone, ltf_use)

    caption = (
        f"🔊 *رادار التنبيه الصوتي النشط (Voice Alert Active):*\n"
        f"📊 *الأصل:* `{asset_name}` | *الفريم:* `{ltf_use}`\n"
        f"💵 *السعر الحالي:* `{curr_price}`\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🎯 *منطقة OTE المحددة:* `{entry_zone[0]}` إلى `{entry_zone[1]}`\n"
        f"🛑 *وقف الخسارة (SL):* `{sl}`\n"
        f"✅ *الهدف الأول (TP1):* `{exit_zone[0]}` | *الهدف الثاني (TP2):* `{exit_zone[1]}`\n"
        f"🔊 *ملاحظة:* سيرسل لك البوت ملفاً صوتياً (Voice Note) فوريًا عند ملامسة السعر للمنطقة."
    )

    chart_buf = generate_chart_image(ltf, zones, f"{chart_code} ({ltf_use})", entry_zone, sl, exit_zone, is_long)
    send_photo_msg(chart_buf, caption, sym_type, chat_id)

def telegram_listener():
    if TELEGRAM_BOT_TOKEN:
        try: requests.get(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/deleteWebhook", timeout=10)
        except: pass

    offset = None
    while True:
        try:
            if TELEGRAM_BOT_TOKEN:
                url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
                res = requests.get(url, params={"timeout": 20, "offset": offset}, timeout=25).json()
                if res.get("ok"):
                    for item in res.get("result", []):
                        offset = item["update_id"] + 1
                        if "callback_query" in item:
                            cb = item["callback_query"]
                            cb_data = cb.get("data", "")
                            cb_chat = str(cb.get("message", {}).get("chat", {}).get("id", ""))
                            if cb_data.startswith("TF|"):
                                _, sym_t, chosen_tf = cb_data.split("|")
                                user_settings["ltf"] = chosen_tf
                                user_settings["htf"] = HTF_MAP.get(chosen_tf, "4h")
                                requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery", json={"callback_query_id": cb["id"], "text": f"تحديث فريم {chosen_tf}.."}, timeout=5)
                                analyze_and_send_zones(sym_t, chosen_tf, cb_chat)
                            continue

                        msg = item.get("message", {})
                        text = msg.get("text", "").strip()
                        chat_id = str(msg.get("chat", {}).get("id", ""))

                        if text in ["/start", "/help"]:
                            send_msg("🤖 *رادار التنبيه الصوتي لملامسة OTE يعمل بنجاح!*", chat_id)
                        elif text in ["⚡ فريم سكالب (5m)", "/scalp"]:
                            user_settings.update({"mode_name": TIMEFRAME_MODES["scalp"]["name"], "ltf": "5m", "htf": "15m"})
                            send_msg("⚡ تم تفعيل فريم 5 دقائق.", chat_id)
                        elif text in ["⏱ فريم متوسط (15m)", "/intraday"]:
                            user_settings.update({"mode_name": TIMEFRAME_MODES["intraday"]["name"], "ltf": "15m", "htf": "4h"})
                            send_msg("⏱ تم تفعيل الفريم المتوسط 15 دقيقة.", chat_id)
                        elif text in ["🌊 فريم طويل (4h/1d)", "/swing"]:
                            user_settings.update({"mode_name": TIMEFRAME_MODES["swing"]["name"], "ltf": "4h", "htf": "1d"})
                            send_msg("🌊 تم تفعيل الفريم الطويل 4 ساعات.", chat_id)
                        elif text in ["/gold", "🪙 مناطق دخول الذهب"]:
                            analyze_and_send_zones("GOLD", user_settings["ltf"], chat_id)
                        elif text in ["/btc", "₿ مناطق دخول البيتكوين"]:
                            analyze_and_send_zones("BTC", user_settings["ltf"], chat_id)
                        elif text in ["/status", "/all", "📊 فحص شامل بالسوق"]:
                            analyze_and_send_zones("GOLD", user_settings["ltf"], chat_id)
                            analyze_and_send_zones("BTC", user_settings["ltf"], chat_id)
                        elif text in ["/trades", "📂 الأوامر المعلقة والنشطة"]:
                            send_msg(format_active_trades_report(), chat_id)
                        elif text in ["/clear", "🗑️ مسح الصفقات"]:
                            with trades_lock: active_trades.clear()
                            send_msg("🗑️ تم مسح الصفقات.", chat_id)
        except Exception as e: print(f"Listener error: {e}")
        time.sleep(2)

def keep_alive_pinger():
    while True:
        time.sleep(480)
        try: requests.get(f"{RENDER_EXTERNAL_URL}/health", timeout=10)
        except: pass

@app.route("/")
def home():
    return "Voice Alert OTE Bot is Live 24/7!"

@app.route("/health")
def health():
    return {"status": "ok"}, 200

if __name__ == "__main__":
    threading.Thread(target=telegram_listener, daemon=True).start()
    threading.Thread(target=trade_tracker_loop, daemon=True).start()
    threading.Thread(target=keep_alive_pinger, daemon=True).start()
    send_msg("🔊 *تم تفعيل التنبيه الصوتي الفوري (Voice Note Alert) عند ملامسة OTE بنجاح!*")
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
