import os
import io
import json
import time
import threading
import wave
import struct
import math
import hashlib
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import requests
import ccxt
import yfinance as yf
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as patches
from flask import Flask, request

# ==================== 1. الإعدادات الأساسية وإعدادات الحماية ====================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
ADMIN_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL", "https://trading-bot-1-88ha.onrender.com").strip()

BOT_PASSWORD = os.getenv("BOT_PASSWORD", "Gold2026").strip()
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "").strip()
SMTP_USER = os.getenv("SMTP_USER", "").strip()
SMTP_APP_PASS = os.getenv("SMTP_APP_PASS", "").strip()

TRACKER_INTERVAL = 15
active_trades = {}
trades_lock = threading.Lock()

dxy_cache = {"timestamp": 0, "bias": "NEUTRAL", "text": "مستقر"}

# ==================== نظام الحفظ الدائم وعزل إعدادات كل مستخدم ====================
USERS_FILE = "users_store.json"
users_lock = threading.RLock()

PROFIT_PROFILES = {
    "scalp": {
        "label": "سكالبينج سريع ($5 / $10 / $20) ⚡",
        "tp1_usd": 5.0,
        "tp2_usd": 10.0,
        "tp3_usd": 20.0,
        "sl_usd": 5.0
    },
    "swing": {
        "label": "سوينق بعيد ($15 / $30 / $60) 🌊",
        "tp1_usd": 15.0,
        "tp2_usd": 30.0,
        "tp3_usd": 60.0,
        "sl_usd": 12.0
    }
}

TIMEFRAME_MODES = {
    "scalp": {"name": "سكالبينج سريع (5m) ⚡", "ltf": "5m", "htf": "15m"},
    "intraday": {"name": "مضاربة يومية (15m) ⏱", "ltf": "15m", "htf": "4h"},
    "swing": {"name": "سوينق بعيد (4h) 🌊", "ltf": "4h", "htf": "1d"}
}

HTF_MAP = {
    "1m": "15m", "5m": "15m", "15m": "4h", "1h": "4h", "4h": "1d", "1d": "1d"
}

def get_default_user_prefs():
    return {
        "mode_name": "مضاربة متوسطة (15m)",
        "ltf": "15m",
        "htf": "4h",
        "profit_style": "scalp",
        "lot_size": 0.01,
        "tp1_usd": 5.0,
        "tp2_usd": 10.0,
        "tp3_usd": 20.0,
        "sl_usd": 5.0
    }

def load_users_from_disk():
    default_db = {
        "approved": {},
        "pending": {},
        "banned": set(),
        "user_prefs": {},
        "global_enabled": True,
        "password": BOT_PASSWORD,
        "admin_email": ADMIN_EMAIL
    }
    if os.path.exists(USERS_FILE):
        try:
            with open(USERS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                data["approved"] = data.get("approved", {})
                data["pending"] = data.get("pending", {})
                data["banned"] = set(data.get("banned", []))
                data["user_prefs"] = data.get("user_prefs", {})
                data["global_enabled"] = data.get("global_enabled", True)
                data["password"] = data.get("password", BOT_PASSWORD)
                data["admin_email"] = data.get("admin_email", ADMIN_EMAIL)
                return data
        except Exception as e:
            print(f"Error loading users store: {e}")
    return default_db

def save_users_to_disk():
    with users_lock:
        try:
            data = {
                "approved": users_db["approved"],
                "pending": users_db["pending"],
                "banned": list(users_db["banned"]),
                "user_prefs": users_db["user_prefs"],
                "global_enabled": users_db["global_enabled"],
                "password": users_db["password"],
                "admin_email": users_db["admin_email"]
            }
            with open(USERS_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=4)
        except Exception as e:
            print(f"Error saving users store: {e}")

users_db = load_users_from_disk()

def get_user_settings(chat_id):
    cid = str(chat_id).strip()
    with users_lock:
        if cid not in users_db["user_prefs"]:
            users_db["user_prefs"][cid] = get_default_user_prefs()
            save_users_to_disk()
        return users_db["user_prefs"][cid]

def apply_profit_profile(chat_id, profile_key):
    s = get_user_settings(chat_id)
    prof = PROFIT_PROFILES.get(profile_key, PROFIT_PROFILES["scalp"])
    with users_lock:
        s["profit_style"] = profile_key
        s["tp1_usd"] = prof["tp1_usd"]
        s["tp2_usd"] = prof["tp2_usd"]
        s["tp3_usd"] = prof["tp3_usd"]
        s["sl_usd"] = prof["sl_usd"]
        save_users_to_disk()

def usd_to_price_distance(sym_type, usd_amount, lot_size):
    safe_lot = max(float(lot_size), 0.001)
    if sym_type == "GOLD":
        return round(float(usd_amount) / (safe_lot * 100.0), 2)
    else:
        return round(float(usd_amount) / safe_lot, 2)

app = Flask(__name__)

# ==================== لوحة الأزرار السفلية المختصرة (صفان فقط) ====================
USER_KEYBOARD = {
    "keyboard": [
        [{"text": "🪙 تحليل الذهب"}, {"text": "₿ تحليل البيتكوين"}],
        [{"text": "📂 الصفقات النشطة"}, {"text": "⚙️ الإعدادات والتحكم"}]
    ],
    "resize_keyboard": True,
    "is_persistent": True
}

ADMIN_KEYBOARD = {
    "keyboard": [
        [{"text": "🪙 تحليل الذهب"}, {"text": "₿ تحليل البيتكوين"}],
        [{"text": "📂 الصفقات النشطة"}, {"text": "⚙️ الإعدادات والتحكم"}, {"text": "🔐 المشرف"}]
    ],
    "resize_keyboard": True,
    "is_persistent": True
}

def is_admin(chat_id):
    return str(chat_id).strip() == str(ADMIN_CHAT_ID).strip()

def is_user_authorized(chat_id):
    cid = str(chat_id).strip()
    if is_admin(cid):
        return True
    with users_lock:
        if not users_db["global_enabled"] or cid in users_db["banned"]:
            return False
        return cid in users_db["approved"]

def get_action_token(chat_id, action):
    secret = f"{TELEGRAM_BOT_TOKEN}_{chat_id}_{action}"
    return hashlib.sha256(secret.encode()).hexdigest()[:16]

def send_approval_email(user_id, full_name, username):
    target_email = users_db["admin_email"] or ADMIN_EMAIL
    if not target_email or not SMTP_USER or not SMTP_APP_PASS:
        return False, "التأكيد مفعل عبر تيليجرام مباشرة"

    approve_tok = get_action_token(user_id, "approve")
    reject_tok = get_action_token(user_id, "reject")
    approve_url = f"{RENDER_EXTERNAL_URL}/admin/action?uid={user_id}&act=approve&token={approve_tok}"
    reject_url = f"{RENDER_EXTERNAL_URL}/admin/action?uid={user_id}&act=reject&token={reject_tok}"

    subject = f"🔐 طلب تفعيل مستخدم جديد في بوت التداول: {full_name}"
    html_body = f"""
    <div dir="rtl" style="font-family: Arial, sans-serif; padding: 20px; background: #f4f6f9; border-radius: 10px;">
        <h2>🚨 طلب دخول جديد إلى بوت الذهب والبيتكوين</h2>
        <p>أدخل المستخدم التالي كلمة السر الصحيحة وينتظر موافقتك:</p>
        <ul>
            <li><b>الاسم:</b> {full_name}</li>
            <li><b>المعرف:</b> @{username}</li>
            <li><b>Chat ID:</b> <code>{user_id}</code></li>
        </ul>
        <p style="margin-top: 25px;">
            <a href="{approve_url}" style="background: #00c853; color: white; padding: 12px 24px; text-decoration: none; border-radius: 6px; font-weight: bold; margin-left: 10px;">✅ موافقة وتفعيل</a>
            <a href="{reject_url}" style="background: #d50000; color: white; padding: 12px 24px; text-decoration: none; border-radius: 6px; font-weight: bold;">🚫 رفض وحظر</a>
        </p>
    </div>
    """
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = SMTP_USER
        msg["To"] = target_email
        msg.attach(MIMEText(html_body, "html", "utf-8"))
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=15) as server:
            server.login(SMTP_USER, SMTP_APP_PASS)
            server.sendmail(SMTP_USER, target_email, msg.as_string())
        return True, "تم إرسال الإيميل بنجاح"
    except Exception as e:
        return False, str(e)

def get_inline_tf_buttons(sym_type):
    """أزرار الشارت الشفافة مع زرين منفصلين بجانب بعضهما (شراء Market وبيع Market)"""
    return {
        "inline_keyboard": [
            [
                {"text": "🟢 شراء فوري (Market Buy)", "callback_data": f"MKT|{sym_type}|BUY"},
                {"text": "🔴 بيع فوري (Market Sell)", "callback_data": f"MKT|{sym_type}|SELL"}
            ],
            [
                {"text": "⚡ 5m", "callback_data": f"TF|{sym_type}|5m"},
                {"text": "⏱ 15m", "callback_data": f"TF|{sym_type}|15m"},
                {"text": "🕐 1h", "callback_data": f"TF|{sym_type}|1h"},
                {"text": "🕓 4h", "callback_data": f"TF|{sym_type}|4h"},
                {"text": "📅 1d", "callback_data": f"TF|{sym_type}|1d"}
            ],
            [
                {"text": "🎯 سكالب ($5/$10/$20)", "callback_data": f"PMODE|{sym_type}|scalp"},
                {"text": "🌊 سوينق ($15/$30/$60)", "callback_data": f"PMODE|{sym_type}|swing"}
            ]
        ]
    }

def send_msg(text, chat_id, inline_markup=None, remove_keyboard=False):
    target = str(chat_id).strip()
    if not TELEGRAM_BOT_TOKEN or not target:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    if inline_markup:
        markup = inline_markup
    elif remove_keyboard:
        markup = {"remove_keyboard": True}
    else:
        markup = ADMIN_KEYBOARD if is_admin(target) else USER_KEYBOARD
    try:
        res = requests.post(
            url,
            json={"chat_id": target, "text": text, "parse_mode": "Markdown", "reply_markup": markup},
            timeout=12
        )
        if not res.ok:
            clean_text = text.replace("*", "").replace("`", "")
            requests.post(
                url,
                json={"chat_id": target, "text": clean_text, "reply_markup": markup},
                timeout=12
            )
    except Exception as e:
        print(f"Telegram text error: {e}")

# ==================== 2. محرك النطق البشري باللغة العربية ====================
def generate_fallback_alarm_wav(duration_sec=5.0):
    sample_rate = 12000
    num_samples = int(sample_rate * duration_sec)
    buf = io.BytesIO()
    with wave.open(buf, "w") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        frames = bytearray()
        for i in range(num_samples):
            t = i / sample_rate
            cycle_pos = (t * 2.0) % 1.0
            freq = 880.0 if cycle_pos < 0.4 else (1175.0 if cycle_pos < 0.8 else 0.0)
            sample = int(32767.0 * 0.75 * math.sin(2.0 * math.pi * freq * t)) if freq > 0 else 0
            frames.extend(struct.pack("<h", max(-32768, min(32767, sample))))
        wav_file.writeframes(frames)
    buf.seek(0)
    return buf

def generate_arabic_tts_audio(spoken_text):
    try:
        tts_url = "https://translate.google.com/translate_tts"
        params = {"ie": "UTF-8", "client": "tw-ob", "tl": "ar", "q": spoken_text}
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        res = requests.get(tts_url, params=params, headers=headers, timeout=15)
        if res.ok and len(res.content) > 500:
            return res.content, "voice_alert.mp3", "audio/mpeg"
    except Exception as e:
        print(f"Arabic TTS error: {e}")
    return generate_fallback_alarm_wav().read(), "alert.wav", "audio/wav"

def send_arabic_voice_alert(spoken_text, caption, chat_id):
    target = str(chat_id).strip()
    if not TELEGRAM_BOT_TOKEN or not target:
        return
    audio_bytes, filename, mime_type = generate_arabic_tts_audio(spoken_text)
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendVoice"
    try:
        buf = io.BytesIO(audio_bytes)
        data = {"chat_id": target, "caption": caption, "parse_mode": "Markdown"}
        files = {"voice": (filename, buf, mime_type)}
        res = requests.post(url, data=data, files=files, timeout=25)
        if not res.ok:
            send_msg(caption, target)
    except Exception:
        send_msg(caption, target)

def send_photo_msg(photo_buf, caption, sym_type, chat_id):
    target = str(chat_id).strip()
    if not TELEGRAM_BOT_TOKEN or not target:
        return
    inline_kb = get_inline_tf_buttons(sym_type)
    if photo_buf is None:
        send_msg(caption, target, inline_kb)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
    try:
        res = requests.post(
            url,
            data={"chat_id": target, "caption": caption, "parse_mode": "Markdown", "reply_markup": json.dumps(inline_kb)},
            files={"photo": ("chart.png", photo_buf, "image/png")},
            timeout=25
        )
        if not res.ok:
            send_msg(caption, target, inline_kb)
    except Exception:
        send_msg(caption, target, inline_kb)

# ==================== 3. جلب البيانات + مؤشر الدولار DXY + المؤشرات العشر ====================
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
        except Exception:
            continue
    return get_yf_asset_ohlcv("BTC-USD", timeframe)

def get_yf_asset_ohlcv(ticker_sym, timeframe):
    try:
        ticker = yf.Ticker(ticker_sym)
        if timeframe == "1d":
            return clean_yf_dataframe(ticker.history(period="1y", interval="1d"))
        elif timeframe == "4h":
            raw = clean_yf_dataframe(ticker.history(period="60d", interval="1h"))
            if not raw.empty:
                raw.set_index("timestamp", inplace=True)
                return raw.resample("4h").agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}).dropna().reset_index()
        elif timeframe == "1h":
            return clean_yf_dataframe(ticker.history(period="30d", interval="1h"))
        elif timeframe == "15m":
            return clean_yf_dataframe(ticker.history(period="15d", interval="15m"))
        elif timeframe == "5m":
            return clean_yf_dataframe(ticker.history(period="5d", interval="5m"))
        elif timeframe == "1m":
            df = clean_yf_dataframe(ticker.history(period="5d", interval="1m"))
            if len(df) > 35:
                return df
            return clean_yf_dataframe(ticker.history(period="5d", interval="5m"))
    except Exception:
        pass
    return pd.DataFrame()

def get_gold_ohlcv(timeframe):
    for sym in ["GC=F", "XAUUSD=X"]:
        df = get_yf_asset_ohlcv(sym, timeframe)
        if not df.empty and len(df) > 35:
            return df
    return pd.DataFrame()

def get_dxy_correlation_bias():
    now = time.time()
    if now - dxy_cache["timestamp"] < 600:
        return dxy_cache["bias"], dxy_cache["text"]

    for sym in ["DX-Y.NYB", "UUP"]:
        try:
            df_dxy = get_yf_asset_ohlcv(sym, "15m")
            if not df_dxy.empty and len(df_dxy) > 25:
                closes = pd.to_numeric(df_dxy["close"], errors="coerce").dropna()
                ema20 = closes.ewm(span=20, adjust=False).mean().iloc[-1]
                ema50 = closes.ewm(span=50, adjust=False).mean().iloc[-1]
                curr_dxy = float(closes.iloc[-1])
                if curr_dxy < ema20 and ema20 <= ema50:
                    dxy_cache.update({"timestamp": now, "bias": "WEAK_USD", "text": "ضعف الدولار DXY (يدعم الشراء)"})
                elif curr_dxy > ema20 and ema20 >= ema50:
                    dxy_cache.update({"timestamp": now, "bias": "STRONG_USD", "text": "قوة الدولار DXY (يدعم البيع)"})
                else:
                    dxy_cache.update({"timestamp": now, "bias": "NEUTRAL", "text": "استقرار مؤشر الدولار DXY"})
                return dxy_cache["bias"], dxy_cache["text"]
        except Exception:
            continue
    return "NEUTRAL", "حيادية مؤشر الدولار"

def calc_parabolic_sar(df, af_start=0.02, af_step=0.02, af_max=0.20):
    high = df["high"].values
    low = df["low"].values
    n = len(df)
    psar = [0.0] * n
    bull = True
    af = af_start
    ep = high[0]
    psar[0] = low[0]

    for i in range(1, n):
        prev_psar = psar[i - 1]
        if bull:
            sar = prev_psar + af * (ep - prev_psar)
            sar = min(sar, low[i - 1], low[max(0, i - 2)])
            if low[i] < sar:
                bull = False
                sar = ep
                ep = low[i]
                af = af_start
            else:
                if high[i] > ep:
                    ep = high[i]
                    af = min(af + af_step, af_max)
        else:
            sar = prev_psar + af * (ep - prev_psar)
            sar = max(sar, high[i - 1], high[max(0, i - 2)])
            if high[i] > sar:
                bull = True
                sar = ep
                ep = high[i]
                af = af_start
            else:
                if low[i] < ep:
                    ep = low[i]
                    af = min(af + af_step, af_max)
        psar[i] = sar
    return pd.Series(psar, index=df.index)

def calc_supertrend(df, multiplier=3.0):
    high = df["high"].values
    low = df["low"].values
    close = df["close"].values
    atr = df["ATR"].values
    n = len(df)

    st = [0.0] * n
    direction = [1] * n
    hl2 = (high + low) / 2.0
    basic_upper = hl2 + (multiplier * atr)
    basic_lower = hl2 - (multiplier * atr)
    final_upper = list(basic_upper)
    final_lower = list(basic_lower)
    st[0] = final_lower[0]

    for i in range(1, n):
        if basic_upper[i] < final_upper[i - 1] or close[i - 1] > final_upper[i - 1]:
            final_upper[i] = basic_upper[i]
        else:
            final_upper[i] = final_upper[i - 1]

        if basic_lower[i] > final_lower[i - 1] or close[i - 1] < final_lower[i - 1]:
            final_lower[i] = basic_lower[i]
        else:
            final_lower[i] = final_lower[i - 1]

        if direction[i - 1] == 1:
            if close[i] < final_lower[i]:
                direction[i] = -1
                st[i] = final_upper[i]
            else:
                direction[i] = 1
                st[i] = final_lower[i]
        else:
            if close[i] > final_upper[i]:
                direction[i] = 1
                st[i] = final_lower[i]
            else:
                direction[i] = -1
                st[i] = final_upper[i]

    return pd.Series(st, index=df.index), pd.Series(direction, index=df.index)

def calculate_volume_profile(df, num_bins=24):
    recent = df.tail(55).copy()
    min_p = float(recent["low"].min())
    max_p = float(recent["high"].max())
    if max_p <= min_p:
        max_p = min_p + 1.0

    bin_size = (max_p - min_p) / num_bins
    bin_centers = [min_p + (i + 0.5) * bin_size for i in range(num_bins)]
    bin_volumes = [0.0] * num_bins

    for _, row in recent.iterrows():
        h = float(row["high"])
        l = float(row["low"])
        v = float(row["volume"])
        weight = v if v > 0 else max(h - l, 1.0)

        for b_idx in range(num_bins):
            b_low = min_p + b_idx * bin_size
            b_high = b_low + bin_size
            overlap = max(0.0, min(h, b_high) - max(l, b_low))
            if overlap > 0:
                candle_span = max(h - l, 1e-6)
                bin_volumes[b_idx] += weight * (overlap / candle_span)
            elif b_low <= float(row["close"]) <= b_high:
                bin_volumes[b_idx] += weight * 0.5

    max_vol_idx = int(max(range(num_bins), key=lambda i: bin_volumes[i]))
    poc_price = round(bin_centers[max_vol_idx], 2)

    total_vol = sum(bin_volumes)
    target_vol = total_vol * 0.70
    accum_vol = bin_volumes[max_vol_idx]
    low_idx = max_vol_idx
    high_idx = max_vol_idx

    while accum_vol < target_vol and (low_idx > 0 or high_idx < num_bins - 1):
        up_vol = bin_volumes[high_idx + 1] if high_idx < num_bins - 1 else -1.0
        dn_vol = bin_volumes[low_idx - 1] if low_idx > 0 else -1.0
        if up_vol >= dn_vol and high_idx < num_bins - 1:
            high_idx += 1
            accum_vol += bin_volumes[high_idx]
        elif low_idx > 0:
            low_idx -= 1
            accum_vol += bin_volumes[low_idx]
        else:
            break

    val_price = round(min_p + low_idx * bin_size, 2)
    vah_price = round(min_p + (high_idx + 1) * bin_size, 2)

    return {
        "poc": poc_price, "vah": vah_price, "val": val_price,
        "poc_idx": max_vol_idx, "bin_centers": bin_centers,
        "bin_volumes": bin_volumes, "bin_height": bin_size * 0.85
    }

def enrich_indicators(df):
    df = df.copy()
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["open", "high", "low", "close"]).reset_index(drop=True)

    df["EMA_20"] = df["close"].ewm(span=20, adjust=False).mean()
    df["EMA_50"] = df["close"].ewm(span=50, adjust=False).mean()
    df["EMA_200"] = df["close"].ewm(span=200, adjust=False).mean()

    ema12 = df["close"].ewm(span=12, adjust=False).mean()
    ema26 = df["close"].ewm(span=26, adjust=False).mean()
    df["MACD"] = ema12 - ema26
    df["MACD_SIGNAL"] = df["MACD"].ewm(span=9, adjust=False).mean()
    df["MACD_HIST"] = df["MACD"] - df["MACD_SIGNAL"]

    delta = df["close"].diff()
    gain = delta.clip(lower=0).ewm(alpha=1/14, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1/14, adjust=False).mean()
    rs = gain / loss.replace(0, 1e-9)
    df["RSI"] = 100 - (100 / (1 + rs))

    rsi_min = df["RSI"].rolling(window=14, min_periods=1).min()
    rsi_max = df["RSI"].rolling(window=14, min_periods=1).max()
    stoch_raw = 100 * ((df["RSI"] - rsi_min) / (rsi_max - rsi_min).replace(0, 1e-9))
    df["STOCH_K"] = stoch_raw.rolling(window=3, min_periods=1).mean()
    df["STOCH_D"] = df["STOCH_K"].rolling(window=3, min_periods=1).mean()

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

    df["PSAR"] = calc_parabolic_sar(df)
    df["SUPERTREND"], df["ST_DIR"] = calc_supertrend(df, multiplier=2.8)

    typical_price = (df["high"] + df["low"] + df["close"]) / 3.0
    vol_safe = df["volume"].replace(0, 1.0)
    df["VWAP"] = (typical_price * vol_safe).rolling(window=30, min_periods=1).sum() / vol_safe.rolling(window=30, min_periods=1).sum()

    df["BB_MID"] = df["close"].rolling(window=20, min_periods=1).mean()
    bb_std = df["close"].rolling(window=20, min_periods=1).std().fillna(df["ATR"])
    df["BB_UPPER"] = df["BB_MID"] + (2.0 * bb_std)
    df["BB_LOWER"] = df["BB_MID"] - (2.0 * bb_std)
    df["BB_WIDTH"] = (df["BB_UPPER"] - df["BB_LOWER"]) / df["BB_MID"].replace(0, 1e-9)

    candle_range = (df["high"] - df["low"]).replace(0, 1e-9)
    delta_ratio = ((df["close"] - df["low"]) - (df["high"] - df["close"])) / candle_range
    eff_vol = df["volume"].where(df["volume"] > 0, high_low * 100.0)
    df["VOL_DELTA"] = delta_ratio * eff_vol
    df["CVD"] = df["VOL_DELTA"].cumsum()

    return df

# ==================== 4. الفلاتر المؤسسية الإضافية ====================
def analyze_session_and_candle_filters(df):
    recent = df.tail(60).reset_index(drop=True)
    curr = recent.iloc[-1]
    prev = recent.iloc[-2]
    price = float(curr["close"])

    session_slice = recent.iloc[-36:-4] if len(recent) > 36 else recent.iloc[:-3]
    sess_high = round(float(session_slice["high"].max()), 2)
    sess_low = round(float(session_slice["low"].min()), 2)

    last_4_low = float(recent["low"].tail(4).min())
    last_4_high = float(recent["high"].tail(4).max())

    bull_session_sweep = (last_4_low <= sess_low) and (price > sess_low)
    bear_session_sweep = (last_4_high >= sess_high) and (price < sess_high)

    cvd_now = float(recent["CVD"].iloc[-1])
    cvd_prev = float(recent["CVD"].iloc[-6]) if len(recent) > 6 else cvd_now
    price_prev = float(recent["close"].iloc[-6]) if len(recent) > 6 else price

    bull_absorption = (price <= price_prev) and (cvd_now > cvd_prev)
    bear_absorption = (price >= price_prev) and (cvd_now < cvd_prev)
    cvd_bull = (cvd_now > cvd_prev) or bull_absorption

    c_open, c_close = float(curr["open"]), float(curr["close"])
    c_high, c_low = float(curr["high"]), float(curr["low"])
    body = max(abs(c_close - c_open), 1e-4)
    lower_wick = min(c_open, c_close) - c_low
    upper_wick = c_high - max(c_open, c_close)

    bull_pin = lower_wick >= (1.6 * body)
    bear_pin = upper_wick >= (1.6 * body)
    bull_engulf = (c_close > c_open) and (float(prev["close"]) < float(prev["open"])) and (c_close >= float(prev["open"]))
    bear_engulf = (c_close < c_open) and (float(prev["close"]) > float(prev["open"])) and (c_close <= float(prev["open"]))

    return {
        "sess_high": sess_high,
        "sess_low": sess_low,
        "bull_session_sweep": bull_session_sweep,
        "bear_session_sweep": bear_session_sweep,
        "cvd_bull": cvd_bull,
        "bull_absorption": bull_absorption,
        "bear_absorption": bear_absorption,
        "bull_candle": (bull_pin or bull_engulf),
        "bear_candle": (bear_pin or bear_engulf)
    }

# ==================== 5. المحرك الشامل لعشر استراتيجيات ====================
def evaluate_10_strategies_clean(ltf, htf, div_info, bos_events, vp_data):
    curr = ltf.iloc[-1]
    h_curr = htf.iloc[-1]
    price = float(curr["close"])

    poc_val = vp_data["poc"]
    vwap_val = round(float(curr["VWAP"]), 2)
    st_dir = int(curr["ST_DIR"])
    stoch_k = float(curr["STOCH_K"])

    extra = analyze_session_and_candle_filters(ltf)
    dxy_bias, dxy_txt = get_dxy_correlation_bias()

    s1_bull = (float(h_curr["close"]) > float(h_curr["EMA_200"])) or (price > float(curr["EMA_50"]))
    c1, c3 = ltf.iloc[-3], ltf.iloc[-1]
    s2_bull = (float(c3["low"]) > float(c1["high"])) or extra["bull_session_sweep"] or (price > float(curr["EMA_20"]))
    s3_bull = price > float(curr["PSAR"])
    s4_bull = (price >= vwap_val) and (float(curr["MACD"]) >= float(curr["MACD_SIGNAL"]))
    s5_bull = price >= poc_val
    s6_bull = (st_dir == 1) or (stoch_k < 30)
    s7_bull = extra["cvd_bull"]
    s8_bull = True if extra["bull_session_sweep"] else (False if extra["bear_session_sweep"] else s1_bull)
    s9_bull = True if dxy_bias == "WEAK_USD" else (False if dxy_bias == "STRONG_USD" else s1_bull)
    if div_info["found"] and div_info["rsi_div"]:
        s10_bull = (div_info["rsi_div"]["bias"] == "BULL")
    else:
        s10_bull = True if extra["bull_candle"] else (False if extra["bear_candle"] else s1_bull)

    flags = [s1_bull, s2_bull, s3_bull, s4_bull, s5_bull, s6_bull, s7_bull, s8_bull, s9_bull, s10_bull]
    bull_score = sum(1 for f in flags if f)
    final_is_long = bull_score >= 5
    aligned_count = bull_score if final_is_long else (10 - bull_score)
    confidence = min(58 + (aligned_count * 4), 98)

    reasons = []
    if final_is_long:
        if s5_bull:
            reasons.append(f"ارتكاز أعلى سيولة POC ({poc_val})")
        if s6_bull:
            reasons.append("اتجاه Supertrend صاعد")
        if extra["bull_absorption"] or s7_bull:
            reasons.append("تجميع شرائي في دلتا السيولة CVD")
        if dxy_bias == "WEAK_USD":
            reasons.append("ضعف مؤشر الدولار DXY")
        if extra["bull_session_sweep"]:
            reasons.append("سحب سيولة قاع الجلسة")
        if extra["bull_candle"]:
            reasons.append("ظهور شمعة رفض شرائية")
    else:
        if not s5_bull:
            reasons.append(f"تداول أسفل سيولة POC ({poc_val})")
        if not s6_bull:
            reasons.append("اتجاه Supertrend هابط")
        if extra["bear_absorption"] or not s7_bull:
            reasons.append("تصريف بيعي في دلتا السيولة CVD")
        if dxy_bias == "STRONG_USD":
            reasons.append("قوة مؤشر الدولار DXY")
        if extra["bear_session_sweep"]:
            reasons.append("سحب سيولة قمة الجلسة")
        if extra["bear_candle"]:
            reasons.append("ظهور شمعة رفض بيعية")

    if not reasons:
        reasons = ["توافق المتوسطات السعرية", "هيكل السوق اللحظي"]

    top_reasons_text = " + ".join(reasons[:3])
    stars = "⭐⭐⭐⭐⭐" if aligned_count >= 8 else ("⭐⭐⭐⭐" if aligned_count >= 6 else "⭐⭐⭐")

    return {
        "is_long": final_is_long,
        "aligned_count": aligned_count,
        "confidence": confidence,
        "stars": stars,
        "summary_reasons": top_reasons_text,
        "dxy_text": dxy_txt,
        "cvd_state": "شرائية 🟢" if s7_bull else "بيعية 🔴",
        "poc": poc_val,
        "sess_high": extra["sess_high"],
        "sess_low": extra["sess_low"]
    }

# ==================== 6. كشف الدايفرجنس ومحرك الاقتناص القريب ====================
def detect_rsi_macd_divergence(plot_df):
    n = len(plot_df)
    if n < 20:
        return {"found": False, "text": "حيادي", "rsi_div": None}
    piv_lows, piv_highs = [], []
    for i in range(2, n - 1):
        l, h = float(plot_df["low"].iloc[i]), float(plot_df["high"].iloc[i])
        if l <= float(plot_df["low"].iloc[i-1]) and l <= float(plot_df["low"].iloc[i-2]) and l <= float(plot_df["low"].iloc[i+1]):
            if not piv_lows or (i - piv_lows[-1]) >= 4:
                piv_lows.append(i)
        if h >= float(plot_df["high"].iloc[i-1]) and h >= float(plot_df["high"].iloc[i-2]) and h >= float(plot_df["high"].iloc[i+1]):
            if not piv_highs or (i - piv_highs[-1]) >= 4:
                piv_highs.append(i)

    rsi_res = None
    if len(piv_lows) >= 2:
        i1, i2 = piv_lows[-2], piv_lows[-1]
        p1, p2 = float(plot_df["low"].iloc[i1]), float(plot_df["low"].iloc[i2])
        r1, r2 = float(plot_df["RSI"].iloc[i1]), float(plot_df["RSI"].iloc[i2])
        if p2 < p1 and r2 > r1:
            rsi_res = {"found": True, "bias": "BULL", "label": "دايفرجنس شرائي"}
    if len(piv_highs) >= 2:
        i1, i2 = piv_highs[-2], piv_highs[-1]
        p1, p2 = float(plot_df["high"].iloc[i1]), float(plot_df["high"].iloc[i2])
        r1, r2 = float(plot_df["RSI"].iloc[i1]), float(plot_df["RSI"].iloc[i2])
        if p2 > p1 and r2 < r1:
            rsi_res = {"found": True, "bias": "BEAR", "label": "دايفرجنس بيعي"}

    return {"found": bool(rsi_res), "text": rsi_res["label"] if rsi_res else "حيادي", "rsi_div": rsi_res}

def detect_bos_choch(plot_df):
    n = len(plot_df)
    if n < 15:
        return []
    piv_highs, piv_lows = [], []
    for i in range(2, n - 1):
        h, l = float(plot_df["high"].iloc[i]), float(plot_df["low"].iloc[i])
        if h >= float(plot_df["high"].iloc[i-1]) and h >= float(plot_df["high"].iloc[i-2]) and h >= float(plot_df["high"].iloc[i+1]):
            piv_highs.append((i, h))
        if l <= float(plot_df["low"].iloc[i-1]) and l <= float(plot_df["low"].iloc[i-2]) and l <= float(plot_df["low"].iloc[i+1]):
            piv_lows.append((i, l))
    events = []
    if len(piv_highs) >= 2 and float(plot_df["close"].iloc[-1]) > piv_highs[-2][1]:
        events.append({"bias": "BULL", "level": piv_highs[-2][1]})
    if len(piv_lows) >= 2 and float(plot_df["close"].iloc[-1]) < piv_lows[-2][1]:
        events.append({"bias": "BEAR", "level": piv_lows[-2][1]})
    return events

def analyze_structure_and_ote(df, is_long_bias, sym_type, vp_data, u_prefs):
    recent_55 = df.tail(55).reset_index(drop=True)
    curr_row = recent_55.iloc[-1]
    curr_price = float(curr_row["close"])
    atr = max(float(curr_row["ATR"]), curr_price * 0.0008)
    rsi = float(curr_row["RSI"])

    is_swing_mode = (u_prefs.get("profit_style") == "swing") or (u_prefs.get("ltf") in ["4h", "1d"])
    micro_window = 22 if is_swing_mode else 12
    micro_df = df.tail(micro_window).reset_index(drop=True)

    micro_high = float(micro_df["high"].max())
    micro_low = float(micro_df["low"].min())
    micro_span = max(micro_high - micro_low, atr)

    if sym_type == "GOLD":
        if is_swing_mode:
            min_dist, ideal_dist, max_dist = 3.8, 5.5, 8.0
            half_band = 0.85
        else:
            min_dist, ideal_dist, max_dist = 0.9, 1.45, 2.15
            half_band = 0.40
    else:
        if is_swing_mode:
            min_dist, ideal_dist, max_dist = 0.45 * atr, 0.70 * atr, 1.05 * atr
            half_band = 0.18 * atr
        else:
            min_dist, ideal_dist, max_dist = 0.18 * atr, 0.32 * atr, 0.50 * atr
            half_band = 0.10 * atr

    ema20_val = float(curr_row["EMA_20"])
    vwap_val = float(curr_row["VWAP"])
    st_val = float(curr_row["SUPERTREND"])
    poc_val = float(vp_data["poc"]) if vp_data and "poc" in vp_data else curr_price

    micro_fvg_anchor = None
    for i in range(len(micro_df) - 1, 1, -1):
        c1, c3 = micro_df.iloc[i - 2], micro_df.iloc[i]
        if is_long_bias and float(c3["low"]) > float(c1["high"]) and float(c3["low"]) <= curr_price:
            micro_fvg_anchor = (float(c1["high"]) + float(c3["low"])) / 2.0
            break
        elif (not is_long_bias) and float(c3["high"]) < float(c1["low"]) and float(c3["high"]) >= curr_price:
            micro_fvg_anchor = (float(c3["high"]) + float(c1["low"])) / 2.0
            break

    candidates = []
    if is_long_bias:
        fib_382 = curr_price - (0.382 * micro_span)
        fib_500 = curr_price - (0.500 * micro_span)
        prev_candle_low = float(micro_df["low"].iloc[-2]) if len(micro_df) >= 2 else (curr_price - ideal_dist)

        raw_anchors = [
            (ema20_val, "ارتكاز المتوسط السريع EMA 20"),
            (vwap_val, "خط السيولة المؤسسي VWAP"),
            (poc_val, "نقطة التحكم بالحجم POC"),
            (st_val, "دعم خط Supertrend"),
            (fib_382, "تصحيح فيبوناتشي القريب 38.2%"),
            (fib_500, "توازن الموجة الفرعية 50%"),
            (prev_candle_low, "سحب سيولة الشمعة السابقة")
        ]
        if micro_fvg_anchor is not None:
            raw_anchors.insert(0, (micro_fvg_anchor, "فجوة سعرية فرعية Micro FVG"))

        for lvl, reason_txt in raw_anchors:
            dist = curr_price - lvl
            if (min_dist * 0.65) <= dist <= (max_dist * 1.15):
                candidates.append((lvl, dist, reason_txt))

        if candidates:
            best_lvl, _, anchor_reason = min(candidates, key=lambda x: abs(x[1] - ideal_dist))
            if rsi > 68:
                best_lvl = min(best_lvl, curr_price - ideal_dist)
            sniper_mid = max(curr_price - max_dist, min(curr_price - min_dist, best_lvl))
        else:
            pullback_d = max_dist if rsi > 68 else ideal_dist
            sniper_mid = curr_price - pullback_d
            anchor_reason = "تراجع تصحيحي آمن (Micro OTE)"
    else:
        fib_382 = curr_price + (0.382 * micro_span)
        fib_500 = curr_price + (0.500 * micro_span)
        prev_candle_high = float(micro_df["high"].iloc[-2]) if len(micro_df) >= 2 else (curr_price + ideal_dist)

        raw_anchors = [
            (ema20_val, "مقاومة المتوسط السريع EMA 20"),
            (vwap_val, "خط السيولة المؤسسي VWAP"),
            (poc_val, "نقطة التحكم بالحجم POC"),
            (st_val, "مقاومة خط Supertrend"),
            (fib_382, "تصحيح فيبوناتشي القريب 38.2%"),
            (fib_500, "توازن الموجة الفرعية 50%"),
            (prev_candle_high, "سحب سيولة قمة الشمعة السابقة")
        ]
        if micro_fvg_anchor is not None:
            raw_anchors.insert(0, (micro_fvg_anchor, "فجوة سعرية فرعية Micro FVG"))

        for lvl, reason_txt in raw_anchors:
            dist = lvl - curr_price
            if (min_dist * 0.65) <= dist <= (max_dist * 1.15):
                candidates.append((lvl, dist, reason_txt))

        if candidates:
            best_lvl, _, anchor_reason = min(candidates, key=lambda x: abs(x[1] - ideal_dist))
            if rsi < 32:
                best_lvl = max(best_lvl, curr_price + ideal_dist)
            sniper_mid = min(curr_price + max_dist, max(curr_price + min_dist, best_lvl))
        else:
            pullback_d = max_dist if rsi < 32 else ideal_dist
            sniper_mid = curr_price + pullback_d
            anchor_reason = "ارتداد تصحيحي آمن (Micro OTE)"

    sniper_mid = round(sniper_mid, 2)
    sniper_zone = (round(sniper_mid - half_band, 2), round(sniper_mid + half_band, 2))
    dist_from_curr = round(abs(curr_price - sniper_mid), 2)

    return {
        "sniper_mid": sniper_mid,
        "sniper_zone": sniper_zone,
        "anchor_reason": anchor_reason,
        "dist_from_curr": dist_from_curr
    }

# ==================== 7. رسم الشارت المبسط والنقي جداً ====================
def generate_clean_chart_image(df, zones, vp_data, strat_res, asset_title, entry_zone, sl, exit_targets, usd_targets, is_market=False):
    try:
        plot_df = df.tail(45).reset_index(drop=True)
        fig, ax = plt.subplots(figsize=(10.2, 5.8), dpi=120)
        fig.patch.set_facecolor("#131722")
        ax.set_facecolor("#131722")

        for idx, row in plot_df.iterrows():
            o, h, l, c = float(row["open"]), float(row["high"]), float(row["low"]), float(row["close"])
            color = "#26a69a" if c >= o else "#ef5350"
            ax.plot([idx, idx], [l, h], color=color, linewidth=1.2, zorder=3)
            ax.bar(idx, max(abs(c - o), (h - l) * 0.02, 1e-4), bottom=min(o, c), width=0.60, color=color, edgecolor=color, zorder=4)

        x_end = len(plot_df) + 2
        ax.set_xlim(-1, x_end + 11)

        if "SUPERTREND" in plot_df.columns:
            for i in range(1, len(plot_df)):
                st_c = "#00e676" if int(plot_df["ST_DIR"].iloc[i]) == 1 else "#ff5252"
                ax.plot([i - 1, i], [plot_df["SUPERTREND"].iloc[i - 1], plot_df["SUPERTREND"].iloc[i]], color=st_c, linewidth=1.6, alpha=0.85, zorder=3)

        poc_p = vp_data["poc"]
        ax.axhline(poc_p, color="#00e5ff", linestyle=":", linewidth=1.2, alpha=0.75, zorder=2)
        ax.text(0.5, poc_p, f" POC: {poc_p}", color="#00e5ff", fontsize=7.8, va="bottom", fontweight="bold")

        z_low, z_high = entry_zone
        e_mid = zones["sniper_mid"]
        if not is_market:
            ax.add_patch(
                patches.Rectangle(
                    (len(plot_df) - 14, z_low),
                    width=16,
                    height=max(z_high - z_low, 1e-3),
                    facecolor="#ffd700",
                    alpha=0.28,
                    edgecolor="#ffd700",
                    linestyle="--",
                    linewidth=1.4,
                    zorder=2
                )
            )

        tp1, tp2, tp3 = exit_targets
        u1, u2, u3, u_sl = usd_targets

        entry_line_col = "#00e5ff" if is_market else "#ffd700"
        entry_tag = f" MARKET NOW: {e_mid}" if is_market else f" ENTRY: {e_mid}"

        ax.axhline(e_mid, color=entry_line_col, linestyle="-" if is_market else "--", linewidth=1.5, alpha=0.98, zorder=5)
        ax.axhline(sl, color="#ff1744", linestyle="-", linewidth=1.4, alpha=0.95, zorder=5)
        ax.axhline(tp1, color="#26a69a", linestyle="--", linewidth=1.2, alpha=0.90, zorder=5)
        ax.axhline(tp2, color="#00e676", linestyle="--", linewidth=1.2, alpha=0.95, zorder=5)
        ax.axhline(tp3, color="#69f0ae", linestyle="-", linewidth=1.5, alpha=0.98, zorder=5)

        label_x = x_end + 0.3
        ax.text(label_x, e_mid, entry_tag, color=entry_line_col, fontsize=8.5, va="center", fontweight="bold")
        ax.text(label_x, sl, f" SL (-${u_sl}): {sl}", color="#ff1744", fontsize=8.5, va="center", fontweight="bold")
        ax.text(label_x, tp1, f" TP1 (+${u1}): {tp1}", color="#26a69a", fontsize=8.5, va="center", fontweight="bold")
        ax.text(label_x, tp2, f" TP2 (+${u2}): {tp2}", color="#00e676", fontsize=8.5, va="center", fontweight="bold")
        ax.text(label_x, tp3, f" TP3 (+${u3}): {tp3}", color="#69f0ae", fontsize=8.5, va="center", fontweight="bold")

        mode_prefix = "INSTANT MARKET EXECUTION" if is_market else ("BUY SETUP" if strat_res["is_long"] else "SELL SETUP")
        ax.set_title(
            f"{asset_title}  |  {mode_prefix}  |  Score: {strat_res['confidence']}% ({strat_res['aligned_count']}/10)",
            color="#ffffff", fontsize=10.2, fontweight="bold", pad=12
        )

        ax.grid(color="#2a2e39", linestyle="--", linewidth=0.5, alpha=0.5)
        ax.tick_params(colors="#b2b5be", labelsize=8)
        for spine in ax.spines.values():
            spine.set_color("#2a2e39")

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

# ==================== 8. تسجيل الصفقة (المعلقة أو المباشرة بالسعر الحالي Market) ====================
def register_private_trade(owner_chat_id, asset_name, sym_type, is_long, entry_zone, sl, exit_targets, usd_targets, tf_label, is_market=False):
    owner_id = str(owner_chat_id).strip()
    trade_key = f"{owner_id}_{sym_type}_{tf_label}"
    with trades_lock:
        old = active_trades.get(trade_key)
        already_sent = True if is_market else (old.get("voice_alert_sent", False) if (old and abs(old["entry_low"] - float(entry_zone[0])) < 0.5) else False)
        active_trades[trade_key] = {
            "owner_id": owner_id,
            "asset": asset_name,
            "symbol_type": sym_type,
            "is_long": is_long,
            "entry_low": float(entry_zone[0]),
            "entry_high": float(entry_zone[1]),
            "entry_mid": round((entry_zone[0] + entry_zone[1]) / 2, 2),
            "sl": float(sl),
            "tp1": float(exit_targets[0]),
            "tp2": float(exit_targets[1]),
            "tp3": float(exit_targets[2]),
            "tp1_usd": usd_targets[0],
            "tp2_usd": usd_targets[1],
            "tp3_usd": usd_targets[2],
            "sl_usd": usd_targets[3],
            "status": "ACTIVE" if is_market else "PENDING",
            "voice_alert_sent": already_sent,
            "tp1_hit": False,
            "tp2_hit": False,
            "tf": tf_label,
            "created_at": time.strftime("%H:%M UTC", time.gmtime())
        }

def analyze_and_send_zones(sym_type, ltf_str, chat_id):
    cid = str(chat_id).strip()
    u_prefs = get_user_settings(cid)
    ltf_use = ltf_str or u_prefs["ltf"]
    htf_use = HTF_MAP.get(ltf_use, "4h")

    if sym_type == "GOLD":
        asset_name, chart_code = "الذهب XAU/USD", "XAU/USD"
        ltf_df, htf_df = get_gold_ohlcv(ltf_use), get_gold_ohlcv(htf_use)
    else:
        asset_name, chart_code = "البيتكوين BTC/USDT", "BTC/USDT"
        ltf_df, htf_df = get_btc_ohlcv(ltf_use), get_btc_ohlcv(htf_use)

    if ltf_df.empty or len(ltf_df) < 35:
        send_msg(f"⚠️ بيانات {asset_name} غير متوفرة حالياً.", cid)
        return
    if htf_df.empty or len(htf_df) < 25:
        htf_df = ltf_df

    ltf = enrich_indicators(ltf_df)
    htf = enrich_indicators(htf_df)
    plot_slice = ltf.tail(55).reset_index(drop=True)
    div_info = detect_rsi_macd_divergence(plot_slice)
    bos_events = detect_bos_choch(plot_slice)
    vp_data = calculate_volume_profile(ltf, num_bins=24)

    strat_res = evaluate_10_strategies_clean(ltf, htf, div_info, bos_events, vp_data)
    is_long = strat_res["is_long"]

    zones = analyze_structure_and_ote(ltf, is_long, sym_type, vp_data, u_prefs)
    entry_zone = zones["sniper_zone"]
    entry_mid = zones["sniper_mid"]

    curr_price = round(float(ltf["close"].iloc[-1]), 2)
    lot = u_prefs["lot_size"]
    u_tp1, u_tp2, u_tp3, u_sl = u_prefs["tp1_usd"], u_prefs["tp2_usd"], u_prefs["tp3_usd"], u_prefs["sl_usd"]

    dist_tp1 = usd_to_price_distance(sym_type, u_tp1, lot)
    dist_tp2 = usd_to_price_distance(sym_type, u_tp2, lot)
    dist_tp3 = usd_to_price_distance(sym_type, u_tp3, lot)
    dist_sl = usd_to_price_distance(sym_type, u_sl, lot)

    if is_long:
        dir_label = "🟢 شراء قريب (BUY LIMIT)"
        sl = round(entry_mid - dist_sl, 2)
        tp1 = round(entry_mid + dist_tp1, 2)
        tp2 = round(entry_mid + dist_tp2, 2)
        tp3 = round(entry_mid + dist_tp3, 2)
    else:
        dir_label = "🔴 بيع قريب (SELL LIMIT)"
        sl = round(entry_mid + dist_sl, 2)
        tp1 = round(entry_mid - dist_tp1, 2)
        tp2 = round(entry_mid - dist_tp2, 2)
        tp3 = round(entry_mid - dist_tp3, 2)

    exit_targets = (tp1, tp2, tp3)
    usd_targets = (u_tp1, u_tp2, u_tp3, u_sl)

    register_private_trade(cid, asset_name, sym_type, is_long, entry_zone, sl, exit_targets, usd_targets, ltf_use, is_market=False)

    in_zone_now = entry_zone[0] <= curr_price <= entry_zone[1]
    prox_txt = "🔥 السعر داخل منطقة الدخول الآن!" if in_zone_now else f"تبعد `{zones['dist_from_curr']}$` فقط"

    caption = (
        f"🎯 *بطاقة الصفقة الذكية — {asset_name} ({ltf_use}):*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📌 *القرار المقترح:* *{dir_label}*\n"
        f"💵 *السعر الحالي:* `{curr_price}` ({prox_txt})\n"
        f"⭐ *قوة الصفقة:* `{strat_res['confidence']}%` ({strat_res['stars']} توافق `{strat_res['aligned_count']}/10`)\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📥 *منطقة الدخول:* `{entry_zone[0]}` — `{entry_zone[1]}` *(المنتصف: `{entry_mid}`)*\n"
        f"🛑 *وقف الخسارة (-${u_sl}):* `{sl}`\n"
        f"✅ *الهدف الأول (+${u_tp1}):* `{tp1}`\n"
        f"🔥 *الهدف الثاني (+${u_tp2}):* `{tp2}`\n"
        f"🚀 *الهدف الثالث (+${u_tp3}):* `{tp3}`\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"💡 *خلاصة التحليل:* {strat_res['summary_reasons']}\n"
        f"🌍 *السيولة والدولار:* دلتا السيولة {strat_res['cvd_state']} | {strat_res['dxy_text']}\n"
        f"👇 *للدخول الفوري بالسعر الحالي اختاري (شراء فوري أو بيع فوري) بالأسفل:*"
    )

    chart_buf = generate_clean_chart_image(
        ltf, zones, vp_data, strat_res, f"{chart_code} ({ltf_use})", entry_zone, sl, exit_targets, usd_targets, is_market=False
    )
    send_photo_msg(chart_buf, caption, sym_type, cid)

def execute_instant_market_trade(sym_type, mkt_dir, chat_id):
    """حساب الأهداف والستوب فوراً من السعر اللحظي الحالي حسب الزر المضغوط (BUY أو SELL)"""
    cid = str(chat_id).strip()
    u_prefs = get_user_settings(cid)
    ltf_use = u_prefs["ltf"]
    htf_use = HTF_MAP.get(ltf_use, "4h")

    if sym_type == "GOLD":
        asset_name, chart_code = "الذهب XAU/USD", "XAU/USD"
        ltf_df, htf_df = get_gold_ohlcv(ltf_use), get_gold_ohlcv(htf_use)
    else:
        asset_name, chart_code = "البيتكوين BTC/USDT", "BTC/USDT"
        ltf_df, htf_df = get_btc_ohlcv(ltf_use), get_btc_ohlcv(htf_use)

    if ltf_df.empty or len(ltf_df) < 35:
        send_msg(f"⚠️ تعذر جلب السعر اللحظي لـ {asset_name}.", cid)
        return
    if htf_df.empty or len(htf_df) < 25:
        htf_df = ltf_df

    ltf = enrich_indicators(ltf_df)
    htf = enrich_indicators(htf_df)
    plot_slice = ltf.tail(55).reset_index(drop=True)
    div_info = detect_rsi_macd_divergence(plot_slice)
    bos_events = detect_bos_choch(plot_slice)
    vp_data = calculate_volume_profile(ltf, num_bins=24)
    strat_res = evaluate_10_strategies_clean(ltf, htf, div_info, bos_events, vp_data)

    is_long = (mkt_dir == "BUY")
    strat_res["is_long"] = is_long
    curr_price = round(float(ltf["close"].iloc[-1]), 2)

    lot = u_prefs["lot_size"]
    u_tp1, u_tp2, u_tp3, u_sl = u_prefs["tp1_usd"], u_prefs["tp2_usd"], u_prefs["tp3_usd"], u_prefs["sl_usd"]

    dist_tp1 = usd_to_price_distance(sym_type, u_tp1, lot)
    dist_tp2 = usd_to_price_distance(sym_type, u_tp2, lot)
    dist_tp3 = usd_to_price_distance(sym_type, u_tp3, lot)
    dist_sl = usd_to_price_distance(sym_type, u_sl, lot)

    if is_long:
        dir_label = "🟢 دخول شراء فوري الآن (MARKET BUY)"
        sl = round(curr_price - dist_sl, 2)
        tp1 = round(curr_price + dist_tp1, 2)
        tp2 = round(curr_price + dist_tp2, 2)
        tp3 = round(curr_price + dist_tp3, 2)
    else:
        dir_label = "🔴 دخول بيع فوري الآن (MARKET SELL)"
        sl = round(curr_price + dist_sl, 2)
        tp1 = round(curr_price - dist_tp1, 2)
        tp2 = round(curr_price - dist_tp2, 2)
        tp3 = round(curr_price - dist_tp3, 2)

    exit_targets = (tp1, tp2, tp3)
    usd_targets = (u_tp1, u_tp2, u_tp3, u_sl)
    market_zone = (curr_price, curr_price)
    zones = {
        "sniper_mid": curr_price,
        "sniper_zone": market_zone,
        "anchor_reason": "تنفيذ مباشر بالسعر الحالي (Market Execution)",
        "dist_from_curr": 0.0
    }

    register_private_trade(cid, asset_name, sym_type, is_long, market_zone, sl, exit_targets, usd_targets, ltf_use, is_market=True)

    caption = (
        f"⚡ *بطاقة تنفيذ فوري بالسعر الحالي — {asset_name}:*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📌 *النوع:* *{dir_label}*\n"
        f"💵 *سعر الدخول الفوري:* `{curr_price}` *(مفعلة الآن 🔥)*\n"
        f"📦 *حجم اللوت المحسوب:* `{lot}`\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🛑 *وقف الخسارة (-${u_sl}):* `{sl}`\n"
        f"✅ *الهدف الأول (+${u_tp1}):* `{tp1}`\n"
        f"🔥 *الهدف الثاني (+${u_tp2}):* `{tp2}`\n"
        f"🚀 *الهدف الثالث (+${u_tp3}):* `{tp3}`\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🔊 *حالة الرادار:* تم تفعيل الصفقة في حسابكِ، وسينطق البوت صوتياً فور تحقيق كل هدف!"
    )

    chart_buf = generate_clean_chart_image(
        ltf, zones, vp_data, strat_res, f"{chart_code} (MARKET {mkt_dir} {ltf_use})", market_zone, sl, exit_targets, usd_targets, is_market=True
    )
    send_photo_msg(chart_buf, caption, sym_type, cid)

def get_latest_price_extremes(symbol_type):
    df = get_btc_ohlcv("1m", limit=3) if symbol_type == "BTC" else get_gold_ohlcv("5m")
    if df.empty:
        return None
    last = df.iloc[-1]
    return {"close": round(float(last["close"]), 2), "high": round(float(last["high"]), 2), "low": round(float(last["low"]), 2)}

def trade_tracker_loop():
    while True:
        try:
            with trades_lock:
                keys = list(active_trades.keys())
            if keys:
                cache = {}
                for k in keys:
                    with trades_lock:
                        t = active_trades.get(k)
                    if not t:
                        continue

                    owner_id = t.get("owner_id")
                    if not owner_id or not is_user_authorized(owner_id):
                        with trades_lock:
                            active_trades.pop(k, None)
                        continue

                    sym = t["symbol_type"]
                    if sym not in cache:
                        cache[sym] = get_latest_price_extremes(sym)
                    p = cache[sym]
                    if not p:
                        continue
                    high_p, low_p, curr_p = p["high"], p["low"], p["close"]
                    arabic_name = "الذهب" if sym == "GOLD" else "البيتكوين"
                    dir_word = "شراء" if t["is_long"] else "بيع"

                    if not t.get("voice_alert_sent", False):
                        if low_p <= t["entry_high"] and high_p >= t["entry_low"]:
                            with trades_lock:
                                if k in active_trades:
                                    active_trades[k]["voice_alert_sent"] = True
                            speech_txt = f"تنبيه هام. وصل سعر {arabic_name} الآن إلى منطقة الدخول القريبة عند مستوى {int(curr_p)} لصفقة {dir_word}."
                            caption_txt = f"🗣️ *تنبيه خاص بصفقتك: وصل سعر {arabic_name} إلى منطقة الدخول!*\n🎯 المنطقة: `{t['entry_low']} - {t['entry_high']}` | السعر: `{curr_p}`"
                            send_arabic_voice_alert(speech_txt, caption_txt, owner_id)

                    if t["status"] == "PENDING":
                        if low_p <= t["entry_high"] and high_p >= t["entry_low"]:
                            with trades_lock:
                                if k in active_trades:
                                    active_trades[k]["status"] = "ACTIVE"
                        continue

                    if t["is_long"]:
                        if high_p >= t["tp3"]:
                            send_arabic_voice_alert(
                                f"مبروك، حقق سعر {arabic_name} الهدف الثالث بالكامل بربح {int(t['tp3_usd'])} دولار.",
                                f"🚀 *مبروك! تحقق الهدف الثالث الكامل (TP3: {t['tp3']} | +${t['tp3_usd']}) لصفقتك في {t['asset']}!*",
                                owner_id
                            )
                            with trades_lock:
                                active_trades.pop(k, None)
                        elif (not t.get("tp2_hit", False)) and high_p >= t["tp2"]:
                            with trades_lock:
                                if k in active_trades:
                                    active_trades[k]["tp2_hit"] = True
                                    active_trades[k]["tp1_hit"] = True
                                    active_trades[k]["sl"] = t["tp1"]
                            send_arabic_voice_alert(
                                f"مبروك، وصل سعر {arabic_name} إلى الهدف الثاني بربح {int(t['tp2_usd'])} دولار وتم رفع الوقف إلى الهدف الأول.",
                                f"🔥 *تحقق الهدف الثاني (TP2: {t['tp2']} | +${t['tp2_usd']}) لصفقتك في {t['asset']} وتم حجز ربح الهدف الأول!*",
                                owner_id
                            )
                        elif (not t.get("tp1_hit", False)) and high_p >= t["tp1"]:
                            with trades_lock:
                                if k in active_trades:
                                    active_trades[k]["tp1_hit"] = True
                                    active_trades[k]["sl"] = t["entry_mid"]
                            send_arabic_voice_alert(
                                f"مبروك، وصل سعر {arabic_name} إلى الهدف الأول بربح {int(t['tp1_usd'])} دولار وتم تأمين الصفقة على الدخول.",
                                f"✅ *تحقق الهدف الأول (TP1: {t['tp1']} | +${t['tp1_usd']}) وتأمين الستوب على الدخول لصفقتك في {t['asset']}!*",
                                owner_id
                            )
                        elif low_p <= t["sl"]:
                            send_msg(f"🛑 *تم ضرب مستوى الوقف ({t['sl']}) لصفقتك في {t['asset']}.*", owner_id)
                            with trades_lock:
                                active_trades.pop(k, None)
                    else:
                        if low_p <= t["tp3"]:
                            send_arabic_voice_alert(
                                f"مبروك، حقق سعر {arabic_name} الهدف الثالث بالكامل بربح {int(t['tp3_usd'])} دولار.",
                                f"🚀 *مبروك! تحقق الهدف الثالث الكامل (TP3: {t['tp3']} | +${t['tp3_usd']}) لصفقتك في {t['asset']}!*",
                                owner_id
                            )
                            with trades_lock:
                                active_trades.pop(k, None)
                        elif (not t.get("tp2_hit", False)) and low_p <= t["tp2"]:
                            with trades_lock:
                                if k in active_trades:
                                    active_trades[k]["tp2_hit"] = True
                                    active_trades[k]["tp1_hit"] = True
                                    active_trades[k]["sl"] = t["tp1"]
                            send_arabic_voice_alert(
                                f"مبروك، وصل سعر {arabic_name} إلى الهدف الثاني بربح {int(t['tp2_usd'])} دولار وتم رفع الوقف إلى الهدف الأول.",
                                f"🔥 *تحقق الهدف الثاني (TP2: {t['tp2']} | +${t['tp2_usd']}) لصفقتك في {t['asset']} وتم حجز ربح الهدف الأول!*",
                                owner_id
                            )
                        elif (not t.get("tp1_hit", False)) and low_p <= t["tp1"]:
                            with trades_lock:
                                if k in active_trades:
                                    active_trades[k]["tp1_hit"] = True
                                    active_trades[k]["sl"] = t["entry_mid"]
                            send_arabic_voice_alert(
                                f"مبروك، وصل سعر {arabic_name} إلى الهدف الأول بربح {int(t['tp1_usd'])} دولار وتم تأمين الصفقة على الدخول.",
                                f"✅ *تحقق الهدف الأول (TP1: {t['tp1']} | +${t['tp1_usd']}) وتأمين الستوب على الدخول لصفقتك في {t['asset']}!*",
                                owner_id
                            )
                        elif high_p >= t["sl"]:
                            send_msg(f"🛑 *تم ضرب مستوى الوقف ({t['sl']}) لصفقتك في {t['asset']}.*", owner_id)
                            with trades_lock:
                                active_trades.pop(k, None)
        except Exception as e:
            print(f"Tracker error: {e}")
        time.sleep(TRACKER_INTERVAL)

# ==================== 9. القوائم الخاصة بكل مستخدم ولوحة المشرف ====================
def send_compact_control_menu(chat_id):
    s = get_user_settings(chat_id)
    txt = (
        f"🎛️ *إعداداتك الخاصة والتحكم السريع:*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"⏱ *الفريم الحالي:* `{s['ltf']}`\n"
        f"💼 *نمط الربح:* `{s['profit_style'].upper()}` | *اللوت:* `{s['lot_size']}`\n"
        f"🎯 *أهدافك:* `+$ {s['tp1_usd']}` | `+$ {s['tp2_usd']}` | `+$ {s['tp3_usd']}` (SL: `-$ {s['sl_usd']}`)\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"💡 *أوامر التعديل اليدوي السريع:*\n"
        f"• لتغيير الأهداف: `/settp 5 10 20`\n"
        f"• لتغيير الستوب: `/setsl 5`\n"
        f"• لتغيير اللوت: `/setlot 0.01`"
    )
    inline_menu = {
        "inline_keyboard": [
            [
                {"text": "⚡ وضع سكالب (5m | $5/$10/$20)", "callback_data": "MENU|MODE_SCALP"},
                {"text": "🌊 وضع سوينق (4h | $15/$30/$60)", "callback_data": "MENU|MODE_SWING"}
            ],
            [
                {"text": "⏱ فريم 15m", "callback_data": "MENU|TF_15m"},
                {"text": "🕐 فريم 1h", "callback_data": "MENU|TF_1h"},
                {"text": "📅 فريم يومي 1d", "callback_data": "MENU|TF_1d"}
            ],
            [
                {"text": "📊 فحص شامل (ذهب + بيتكوين)", "callback_data": "MENU|SCAN_ALL"},
                {"text": "🔊 تجربة الصوت العربي", "callback_data": "MENU|TEST_VOICE"}
            ],
            [
                {"text": "🗑️ مسح صفقاتي النشطة", "callback_data": "MENU|CLEAR_TRADES"}
            ]
        ]
    }
    send_msg(txt, chat_id, inline_menu)

def send_admin_panel(chat_id):
    with users_lock:
        app_count = len(users_db["approved"])
        pend_count = len(users_db["pending"])
        ban_count = len(users_db["banned"])
        g_state = "🟢 مفعل للجميع" if users_db["global_enabled"] else "🔴 متوقف عن المستخدمين"
        curr_pass = users_db["password"]
        curr_email = users_db["admin_email"] or "غير مضبوط (يتم التأكيد عبر تيليجرام)"

    text = (
        f"🔐 *لوحة تحكم المشرف وإدارة المستخدمين*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🔑 *كلمة سر البوت الحالية:* `{curr_pass}`\n"
        f"📧 *إيميل التأكيد:* `{curr_email}`\n"
        f"🌐 *حالة البوت للمشتركين:* {g_state}\n"
        f"👥 *المفعلون:* `{app_count}` | *بانتظار الموافقة:* `{pend_count}` | *المحظورون:* `{ban_count}`\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"• لتغيير كلمة السر أرسل: `/setpass 12345`\n"
        f"• لتحديد إيميلك أرسل: `/setemail name@gmail.com`"
    )
    inline_admin = {
        "inline_keyboard": [
            [{"text": "👥 عرض وحذف المستخدمين المفعلين", "callback_data": "ADM|LIST_APPROVED"}],
            [{"text": f"⏳ طلبات بانتظار الموافقة ({pend_count})", "callback_data": "ADM|LIST_PENDING"}],
            [{"text": "⏸️ إيقاف / ▶️ تشغيل البوت للمستخدمين", "callback_data": "ADM|TOGGLE_GLOBAL"}],
            [{"text": "🔓 فك الحظر عن الجميع", "callback_data": "ADM|CLEAR_BANNED"}]
        ]
    }
    send_msg(text, chat_id, inline_admin)

def approve_user_action(uid):
    uid = str(uid).strip()
    with users_lock:
        info = users_db["pending"].pop(uid, {"name": "مستخدم", "username": "بدون_معرف"})
        users_db["banned"].discard(uid)
        users_db["approved"][uid] = {
            "name": info.get("name", "مستخدم"),
            "username": info.get("username", ""),
            "joined": time.strftime("%Y-%m-%d %H:%M", time.gmtime())
        }
        save_users_to_disk()
    send_msg("🎉 *تمت موافقة المشرف على طلبك بنجاح!*\nيمكنك الآن استخدام البوت بخصوصية كاملة من الأزرار بالأسفل:", uid)
    if ADMIN_CHAT_ID:
        send_msg(f"✅ *تم تفعيل المستخدم:* `{info.get('name')}` (`{uid}`) بنجاح.", ADMIN_CHAT_ID)

def reject_or_delete_user_action(uid):
    uid = str(uid).strip()
    with users_lock:
        info = users_db["approved"].pop(uid, None) or users_db["pending"].pop(uid, {"name": "مستخدم"})
        users_db["banned"].add(uid)
        save_users_to_disk()
    with trades_lock:
        for k in [k for k, v in active_trades.items() if v.get("owner_id") == uid]:
            active_trades.pop(k, None)
    send_msg("🚫 *عذراً، تم إيقاف صلاحية وصولك للبوت من قِبل الإدارة.*", uid, remove_keyboard=True)
    if ADMIN_CHAT_ID:
        send_msg(f"🗑️ *تم حذف وحظر المستخدم:* `{info.get('name', uid)}` (`{uid}`).", ADMIN_CHAT_ID)

def format_user_trades_report(chat_id):
    cid = str(chat_id).strip()
    with trades_lock:
        user_trades = {k: v for k, v in active_trades.items() if v.get("owner_id") == cid}
    if not user_trades:
        return "📂 *لا توجد لديك صفقات نشطة حالياً.*\nاضغط على تحليل الذهب أو البيتكوين لبدء متابعة صفقتك الخاصة."
    lines = ["📂 *صفقاتك الخاصة المراقبة تلقائياً:*\n━━━━━━━━━━━━━━━━━━"]
    for k, t in user_trades.items():
        st = "⏳ بانتظار الملامسة" if t["status"] == "PENDING" else "🔥 مفعل بالسوق الآن"
        dir_ic = "🟢 شراء" if t["is_long"] else "🔴 بيع"
        lines.append(
            f"🔹 *{t['asset']}* ({t['tf']}) | {dir_ic} | {st}\n"
            f"   الدخول: `{t['entry_mid']}` | SL: `{t['sl']}`\n"
            f"   TP1 (+${t['tp1_usd']}): `{t['tp1']}` | TP2 (+${t['tp2_usd']}): `{t['tp2']}` | TP3 (+${t['tp3_usd']}): `{t['tp3']}`"
        )
    return "\n".join(lines)

def clear_user_trades(chat_id):
    cid = str(chat_id).strip()
    with trades_lock:
        keys_to_del = [k for k, v in active_trades.items() if v.get("owner_id") == cid]
        for k in keys_to_del:
            active_trades.pop(k, None)

def telegram_listener():
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

                        if "callback_query" in item:
                            cb = item["callback_query"]
                            cb_data = cb.get("data", "")
                            cb_chat = str(cb.get("message", {}).get("chat", {}).get("id", "")).strip()
                            requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery", json={"callback_query_id": cb["id"]}, timeout=5)

                            if cb_data.startswith("ADM|") and is_admin(cb_chat):
                                parts = cb_data.split("|")
                                action = parts[1]
                                if action == "APPROVE":
                                    approve_user_action(parts[2])
                                elif action == "DEL":
                                    reject_or_delete_user_action(parts[2])
                                elif action == "TOGGLE_GLOBAL":
                                    with users_lock:
                                        users_db["global_enabled"] = not users_db["global_enabled"]
                                        save_users_to_disk()
                                        st_now = "🟢 تم تشغيل البوت لجميع المشتركين" if users_db["global_enabled"] else "🔴 تم إيقاف البوت عن جميع المشتركين"
                                    send_msg(f"⚙️ *{st_now}*", cb_chat)
                                    send_admin_panel(cb_chat)
                                elif action == "CLEAR_BANNED":
                                    with users_lock:
                                        users_db["banned"].clear()
                                        save_users_to_disk()
                                    send_msg("🔓 *تم فك الحظر عن جميع الحسابات المحظورة سابقاً.*", cb_chat)
                                elif action == "LIST_APPROVED":
                                    with users_lock:
                                        app_copy = dict(users_db["approved"])
                                    if not app_copy:
                                        send_msg("👥 *لا يوجد مستخدمون مفعلون حالياً.*", cb_chat)
                                    else:
                                        for u_id, u_info in app_copy.items():
                                            kb = {"inline_keyboard": [[{"text": f"❌ حذف وحظر {u_info['name']}", "callback_data": f"ADM|DEL|{u_id}"}]]}
                                            send_msg(f"👤 *المستخدم:* `{u_info['name']}` (`@{u_info['username']}`)\n🆔 المعرف: `{u_id}`", cb_chat, kb)
                                elif action == "LIST_PENDING":
                                    with users_lock:
                                        pend_copy = dict(users_db["pending"])
                                    if not pend_copy:
                                        send_msg("⏳ *لا توجد طلبات معلقة حالياً.*", cb_chat)
                                    else:
                                        for u_id, u_info in pend_copy.items():
                                            kb = {"inline_keyboard": [[
                                                {"text": "✅ موافقة وتفعيل", "callback_data": f"ADM|APPROVE|{u_id}"},
                                                {"text": "🚫 رفض وحظر", "callback_data": f"ADM|DEL|{u_id}"}
                                            ]]}
                                            send_msg(f"⏳ *طلب تفعيل معلق:*\nالاسم: `{u_info['name']}` (`@{u_info['username']}`)\nID: `{u_id}`", cb_chat, kb)
                                continue

                            # معالجة زري الشراء والبيع الفوري بالسعر الحالي (MKT|...|BUY أو MKT|...|SELL)
                            if cb_data.startswith("MKT|"):
                                if not is_user_authorized(cb_chat):
                                    send_msg("🔒 غير مصرح لك باستخدام البوت.", cb_chat, remove_keyboard=True)
                                    continue
                                _, sym_t, mkt_dir = cb_data.split("|")
                                execute_instant_market_trade(sym_t, mkt_dir, cb_chat)
                                continue

                            if cb_data.startswith("MENU|"):
                                if not is_user_authorized(cb_chat):
                                    send_msg("🔒 غير مصرح لك باستخدام البوت.", cb_chat, remove_keyboard=True)
                                    continue
                                u_s = get_user_settings(cb_chat)
                                m_act = cb_data.split("|")[1]
                                if m_act == "MODE_SCALP":
                                    with users_lock:
                                        u_s.update({"mode_name": TIMEFRAME_MODES["scalp"]["name"], "ltf": "5m", "htf": "15m"})
                                    apply_profit_profile(cb_chat, "scalp")
                                    send_msg("⚡ *تم تفعيل وضع السكالبينج لحسابك:*\nفريم `5m` | دخول قريب `1$-2$` | أهداف `+$5 / +$10 / +$20`.", cb_chat)
                                elif m_act == "MODE_SWING":
                                    with users_lock:
                                        u_s.update({"mode_name": TIMEFRAME_MODES["swing"]["name"], "ltf": "4h", "htf": "1d"})
                                    apply_profit_profile(cb_chat, "swing")
                                    send_msg("🌊 *تم تفعيل وضع السوينق لحسابك:*\nفريم `4h` | دخول متوازن `4$-8$` | أهداف `+$15 / +$30 / +$60`.", cb_chat)
                                elif m_act.startswith("TF_"):
                                    chosen_tf = m_act.replace("TF_", "")
                                    with users_lock:
                                        u_s["ltf"] = chosen_tf
                                        u_s["htf"] = HTF_MAP.get(chosen_tf, "4h")
                                        save_users_to_disk()
                                    send_msg(f"⏱ *تم ضبط الفريم الخاص بك على `{chosen_tf}`.*", cb_chat)
                                elif m_act == "SCAN_ALL":
                                    analyze_and_send_zones("GOLD", u_s["ltf"], cb_chat)
                                    analyze_and_send_zones("BTC", u_s["ltf"], cb_chat)
                                elif m_act == "TEST_VOICE":
                                    test_speech = f"أهلاً بك، أزرار الشراء والبيع الفوري بالسعر الحالي تعمل بنجاح، والهدف الأول مضبوط على {int(u_s['tp1_usd'])} دولار."
                                    send_arabic_voice_alert(test_speech, "🗣️ *تجربة الناطق الصوتي الخاص بحسابك*", cb_chat)
                                elif m_act == "CLEAR_TRADES":
                                    clear_user_trades(cb_chat)
                                    send_msg("🗑️ *تم مسح صفقاتك النشطة بنجاح.*", cb_chat)
                                continue

                            if cb_data.startswith("TF|"):
                                if not is_user_authorized(cb_chat):
                                    send_msg("🔒 غير مصرح لك باستخدام البوت.", cb_chat, remove_keyboard=True)
                                    continue
                                _, sym_t, chosen_tf = cb_data.split("|")
                                u_s = get_user_settings(cb_chat)
                                with users_lock:
                                    u_s["ltf"] = chosen_tf
                                    u_s["htf"] = HTF_MAP.get(chosen_tf, "4h")
                                    save_users_to_disk()
                                analyze_and_send_zones(sym_t, chosen_tf, cb_chat)
                                continue

                            if cb_data.startswith("PMODE|"):
                                if not is_user_authorized(cb_chat):
                                    send_msg("🔒 غير مصرح لك باستخدام البوت.", cb_chat, remove_keyboard=True)
                                    continue
                                _, sym_t, p_mode = cb_data.split("|")
                                u_s = get_user_settings(cb_chat)
                                apply_profit_profile(cb_chat, p_mode)
                                analyze_and_send_zones(sym_t, u_s["ltf"], cb_chat)
                                continue

                        msg = item.get("message", {})
                        text = msg.get("text", "").strip()
                        if not text:
                            continue
                        chat_obj = msg.get("chat", {})
                        chat_id = str(chat_obj.get("id", "")).strip()
                        full_name = f"{chat_obj.get('first_name', '')} {chat_obj.get('last_name', '')}".strip() or "مستخدم"
                        username = chat_obj.get("username", "بدون_معرف")

                        if is_admin(chat_id):
                            if text in ["🔐 المشرف", "🔐 لوحة تحكم المشرف", "/admin"]:
                                send_admin_panel(chat_id)
                                continue
                            elif text.startswith("/setpass "):
                                new_p = text.replace("/setpass ", "").strip()
                                if new_p:
                                    with users_lock:
                                        users_db["password"] = new_p
                                        save_users_to_disk()
                                    send_msg(f"🔑 *تم تغيير كلمة سر البوت بنجاح إلى:* `{new_p}`", chat_id)
                                continue
                            elif text.startswith("/setemail "):
                                new_em = text.replace("/setemail ", "").strip()
                                if "@" in new_em:
                                    with users_lock:
                                        users_db["admin_email"] = new_em
                                        save_users_to_disk()
                                    send_msg(f"📧 *تم حفظ إيميل التأكيد بنجاح:* `{new_em}`", chat_id)
                                continue

                        if not is_admin(chat_id):
                            with users_lock:
                                is_banned = chat_id in users_db["banned"]
                                is_global_on = users_db["global_enabled"]
                                is_approved = chat_id in users_db["approved"]
                                is_pending = chat_id in users_db["pending"]
                                correct_pass = users_db["password"]

                            if is_banned:
                                send_msg("🚫 *تم إيقاف صلاحية وصولك للبوت من قِبل الإدارة.*", chat_id, remove_keyboard=True)
                                continue
                            if not is_global_on:
                                send_msg("⏸️ *البوت متوقف مؤقتاً للصيانة بأمر من الإدارة.*", chat_id, remove_keyboard=True)
                                continue
                            if not is_approved:
                                if text.lower() == correct_pass.lower():
                                    with users_lock:
                                        already_pending = chat_id in users_db["pending"]
                                        users_db["pending"][chat_id] = {
                                            "name": full_name,
                                            "username": username,
                                            "time": time.strftime("%Y-%m-%d %H:%M", time.gmtime())
                                        }
                                        save_users_to_disk()
                                    send_msg("✅ *كلمة السر صحيحة!* تم إرسال طلب تفعيل حسابك إلى المشرف للموافقة النهائية.", chat_id, remove_keyboard=True)
                                    if not already_pending and ADMIN_CHAT_ID:
                                        send_approval_email(chat_id, full_name, username)
                                        admin_kb = {
                                            "inline_keyboard": [[
                                                {"text": "✅ موافقة وتفعيل الحساب", "callback_data": f"ADM|APPROVE|{chat_id}"},
                                                {"text": "🚫 رفض وحظر", "callback_data": f"ADM|DEL|{chat_id}"}
                                            ]]
                                        }
                                        send_msg(
                                            f"🚨 *طلب تفعيل جديد (أدخل كلمة السر الصحيحة):*\n👤 *الاسم:* `{full_name}` (`@{username}`)\n🆔 *المعرف:* `{chat_id}`",
                                            ADMIN_CHAT_ID,
                                            admin_kb
                                        )
                                else:
                                    if is_pending:
                                        send_msg("⏳ *طلبك قيد المراجعة لدى المشرف، يرجى الانتظار لحين التفعيل.*", chat_id, remove_keyboard=True)
                                    else:
                                        send_msg("🔒 *هذا البوت خاص ومحمي بكلمة سر.*\n🔑 يرجى إرسال **كلمة السر الصحيحة** لطلب التفعيل:", chat_id, remove_keyboard=True)
                                continue

                        u_s = get_user_settings(chat_id)

                        if text.startswith("/settp "):
                            try:
                                nums = [float(x) for x in text.replace("/settp ", "").split() if x.strip()]
                                if len(nums) >= 3:
                                    with users_lock:
                                        u_s["tp1_usd"] = round(nums[0], 2)
                                        u_s["tp2_usd"] = round(nums[1], 2)
                                        u_s["tp3_usd"] = round(nums[2], 2)
                                        u_s["profit_style"] = "custom"
                                        save_users_to_disk()
                                    send_msg(
                                        f"✅ *تم تحديث أهدافك الخاصة بالدولار:*\n"
                                        f"• TP1: `+${u_s['tp1_usd']}` | TP2: `+${u_s['tp2_usd']}` | TP3: `+${u_s['tp3_usd']}`",
                                        chat_id
                                    )
                            except Exception:
                                send_msg("⚠️ أرسل مثل: `/settp 5 10 20`", chat_id)
                            continue

                        elif text.startswith("/setsl "):
                            try:
                                sl_val = float(text.replace("/setsl ", "").strip())
                                if sl_val > 0:
                                    with users_lock:
                                        u_s["sl_usd"] = round(sl_val, 2)
                                        save_users_to_disk()
                                    send_msg(f"🛑 *تم ضبط وقف الخسارة الخاص بك على:* `-${u_s['sl_usd']}`", chat_id)
                            except Exception:
                                send_msg("⚠️ أرسل مثل: `/setsl 5`", chat_id)
                            continue

                        elif text.startswith("/setlot "):
                            try:
                                lot_val = float(text.replace("/setlot ", "").strip())
                                if lot_val >= 0.001:
                                    with users_lock:
                                        u_s["lot_size"] = round(lot_val, 3)
                                        save_users_to_disk()
                                    send_msg(f"📦 *تم تغيير حجم اللوت الخاص بك إلى:* `{u_s['lot_size']}`", chat_id)
                            except Exception:
                                send_msg("⚠️ أرسل مثل: `/setlot 0.01`", chat_id)
                            continue

                        if text in ["/start", "/help"]:
                            send_msg(
                                "🤖 *أهلاً بك في الإصدار الاحترافي المبسط (10-in-1 + Market Buy/Sell)!*\n"
                                "• ستجدين أسفل كل شارت زرين منفصلين: **`🟢 شراء فوري (Market Buy)`** و **`🔴 بيع فوري (Market Sell)`** لحساب الأهداف فوراً من السعر الحالي.",
                                chat_id
                            )
                        elif text in ["⚙️ الإعدادات والتحكم", "⚙️ إعدادات الربح واللوت", "/settings"]:
                            send_compact_control_menu(chat_id)
                        elif text in ["/gold", "🪙 تحليل الذهب", "🪙 مناطق دخول الذهب"]:
                            analyze_and_send_zones("GOLD", u_s["ltf"], chat_id)
                        elif text in ["/btc", "₿ تحليل البيتكوين", "₿ مناطق دخول البيتكوين"]:
                            analyze_and_send_zones("BTC", u_s["ltf"], chat_id)
                        elif text in ["/trades", "📂 الصفقات النشطة", "📂 الأوامر المعلقة والنشطة"]:
                            send_msg(format_user_trades_report(chat_id), chat_id)
                        elif text in ["/status", "/all", "📊 فحص شامل بالسوق"]:
                            analyze_and_send_zones("GOLD", u_s["ltf"], chat_id)
                            analyze_and_send_zones("BTC", u_s["ltf"], chat_id)
                        elif text in ["/clear", "🗑️ مسح الصفقات"]:
                            clear_user_trades(chat_id)
                            send_msg("🗑️ *تم مسح صفقاتك الخاصة بنجاح.*", chat_id)
        except Exception as e:
            print(f"Listener error: {e}")
        time.sleep(2)

@app.route("/")
def home():
    return "10-Strategy + Dual Market Buy/Sell Bot is Live 24/7!"

@app.route("/health")
def health():
    return {"status": "ok"}, 200

@app.route("/admin/action")
def admin_email_action():
    uid = request.args.get("uid", "").strip()
    act = request.args.get("act", "").strip()
    tok = request.args.get("token", "").strip()
    if not uid or act not in ["approve", "reject"] or tok != get_action_token(uid, act):
        return "<h3>❌ رابط غير صالح</h3>", 403
    if act == "approve":
        approve_user_action(uid)
        return f"<h2 style='color:green;'>✅ تم تفعيل المستخدم ({uid}) بنجاح!</h2>"
    else:
        reject_or_delete_user_action(uid)
        return f"<h2 style='color:red;'>🚫 تم رفض وحظر المستخدم ({uid}) بنجاح!</h2>"

def keep_alive_pinger():
    while True:
        time.sleep(480)
        try:
            requests.get(f"{RENDER_EXTERNAL_URL}/health", timeout=10)
        except Exception:
            pass

if __name__ == "__main__":
    threading.Thread(target=telegram_listener, daemon=True).start()
    threading.Thread(target=trade_tracker_loop, daemon=True).start()
    threading.Thread(target=keep_alive_pinger, daemon=True).start()
    if ADMIN_CHAT_ID:
        send_msg(
            "✅ *تمت إضافة زري الشراء الفوري والبيع الفوري (`🟢 Market Buy` | `🔴 Market Sell`) جنباً إلى جنب أسفل الشارت!*",
            ADMIN_CHAT_ID
        )
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
