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

# ==================== نظام الحفظ الدائم (Persistent Storage) ====================
USERS_FILE = "users_store.json"
users_lock = threading.RLock()

def load_users_from_disk():
    default_db = {
        "approved": {},
        "pending": {},
        "notified_visitors": set(),
        "banned": set(),
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
                data["notified_visitors"] = set(data.get("notified_visitors", []))
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
                "notified_visitors": list(users_db["notified_visitors"]),
                "banned": list(users_db["banned"]),
                "global_enabled": users_db["global_enabled"],
                "password": users_db["password"],
                "admin_email": users_db["admin_email"]
            }
            with open(USERS_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=4)
        except Exception as e:
            print(f"Error saving users store: {e}")

users_db = load_users_from_disk()

# ==================== إعدادات مستقلة لكل مستخدم على حدة (Per-User Settings) ====================
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
    "scalp": {"name": "سكالبينج سريع (5m / 15m) ⚡", "ltf": "5m", "htf": "15m"},
    "intraday": {"name": "مضاربة يومية (15m / 4h) ⏱", "ltf": "15m", "htf": "4h"},
    "swing": {"name": "طويل المدى Swing (4h / 1d) 🌊", "ltf": "4h", "htf": "1d"}
}

HTF_MAP = {
    "1m": "15m", "5m": "15m", "15m": "4h", "1h": "4h", "4h": "1d", "1d": "1d"
}

user_prefs_store = {}
prefs_lock = threading.Lock()

def get_user_prefs(chat_id):
    """إرجاع إعدادات خاصة ومستقلة لكل مستخدم حتى لا تتداخل الإعدادات بين المشتركين"""
    cid = str(chat_id or ADMIN_CHAT_ID).strip()
    with prefs_lock:
        if cid not in user_prefs_store:
            user_prefs_store[cid] = {
                "mode_name": "مضاربة متوسطة (15m / 4h)",
                "ltf": "15m",
                "htf": "4h",
                "profit_style": "scalp",
                "lot_size": 0.01,
                "tp1_usd": 5.0,
                "tp2_usd": 10.0,
                "tp3_usd": 20.0,
                "sl_usd": 5.0
            }
        return user_prefs_store[cid]

def apply_profit_profile(chat_id, profile_key):
    prefs = get_user_prefs(chat_id)
    prof = PROFIT_PROFILES.get(profile_key, PROFIT_PROFILES["scalp"])
    prefs["profit_style"] = profile_key
    prefs["tp1_usd"] = prof["tp1_usd"]
    prefs["tp2_usd"] = prof["tp2_usd"]
    prefs["tp3_usd"] = prof["tp3_usd"]
    prefs["sl_usd"] = prof["sl_usd"]

def usd_to_price_distance(sym_type, usd_amount, lot_size):
    safe_lot = max(float(lot_size), 0.001)
    if sym_type == "GOLD":
        return round(float(usd_amount) / (safe_lot * 100.0), 2)
    else:
        return round(float(usd_amount) / safe_lot, 2)

app = Flask(__name__)

# ==================== لوحة الأزرار السفلية المختصرة ====================
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
        <p>يطلب المستخدم التالي الدخول إلى البوت وينتظر موافقتك:</p>
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
    return {
        "inline_keyboard": [
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

def send_msg(text, chat_id=None, inline_markup=None, remove_keyboard=False):
    target = str(chat_id or ADMIN_CHAT_ID).strip()
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

# ==================== 2. محرك النطق البشري باللغة العربية (خاص بصاحب الصفقة فقط) ====================
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
    """إرسال التنبيه الصوتي حصرياً للمستخدم صاحب الصفقة (chat_id) دون إزعاج بقية المشتركين"""
    target = str(chat_id).strip()
    if not TELEGRAM_BOT_TOKEN or not target or not is_user_authorized(target):
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

def send_photo_msg(photo_buf, caption, sym_type="GOLD", chat_id=None):
    target = str(chat_id or ADMIN_CHAT_ID).strip()
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

# ==================== 3. جلب البيانات وحساب المؤشرات + Volume Profile POC ====================
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

    typical_price = (df["high"] + df["low"] + df["close"]) / 3.0
    vol_safe = df["volume"].replace(0, 1.0)
    df["VWAP"] = (typical_price * vol_safe).rolling(window=30, min_periods=1).sum() / vol_safe.rolling(window=30, min_periods=1).sum()

    df["BB_MID"] = df["close"].rolling(window=20, min_periods=1).mean()
    bb_std = df["close"].rolling(window=20, min_periods=1).std().fillna(df["ATR"])
    df["BB_UPPER"] = df["BB_MID"] + (2.0 * bb_std)
    df["BB_LOWER"] = df["BB_MID"] - (2.0 * bb_std)
    df["BB_WIDTH"] = (df["BB_UPPER"] - df["BB_LOWER"]) / df["BB_MID"].replace(0, 1e-9)

    return df

# ==================== 4. محرك الاستراتيجيات الخمس ====================
def evaluate_multi_strategies(ltf, htf, div_info, bos_events, vp_data):
    curr = ltf.iloc[-1]
    h_curr = htf.iloc[-1]

    price = float(curr["close"])
    adx = round(float(curr["ADX"]), 1)
    psar_val = round(float(curr["PSAR"]), 2)
    vwap_val = round(float(curr["VWAP"]), 2)
    poc_val = vp_data["poc"]
    vah_val = vp_data["vah"]
    val_val = vp_data["val"]

    htf_bull = float(h_curr["close"]) > float(h_curr["EMA_200"])
    ema_bull = price > float(curr["EMA_50"])
    psar_bull = price > psar_val
    vwap_bull = price > vwap_val
    macd_bull = float(curr["MACD"]) > float(curr["MACD_SIGNAL"])
    poc_bull = price >= poc_val

    s1_bull = htf_bull and (ema_bull or any(e["bias"] == "BULL" for e in bos_events))
    s1_status = "🟢 شراء (توافق هيكل SMC + OTE)" if s1_bull else "🔴 بيع (ضغط عرض SMC + OTE)"

    recent_15 = ltf.tail(15)
    prev_low_15 = float(recent_15["low"].iloc[:-2].min())
    prev_high_15 = float(recent_15["high"].iloc[:-2].max())
    c1, c2, c3 = ltf.iloc[-3], ltf.iloc[-2], ltf.iloc[-1]

    bull_fvg = float(c3["low"]) > float(c1["high"]) and float(c2["close"]) > float(c2["open"])
    bear_fvg = float(c3["high"]) < float(c1["low"]) and float(c2["close"]) < float(c2["open"])
    bull_sweep = float(recent_15["low"].tail(3).min()) <= prev_low_15 and price > prev_low_15
    bear_sweep = float(recent_15["high"].tail(3).max()) >= prev_high_15 and price < prev_high_15

    utc_hour = time.gmtime().tm_hour
    in_killzone = (7 <= utc_hour <= 11) or (12 <= utc_hour <= 16)
    kz_tag = "وقت سيولة Killzone 🔥" if in_killzone else "جلسة عادية"

    if bull_sweep or bull_fvg:
        s2_bull = True
        s2_status = f"🟢 شراء ICT ({'سحب سيولة SSL' if bull_sweep else 'فجوة FVG صاعدة'} | {kz_tag})"
    elif bear_sweep or bear_fvg:
        s2_bull = False
        s2_status = f"🔴 بيع ICT ({'سحب سيولة BSL' if bear_sweep else 'فجوة FVG هابطة'} | {kz_tag})"
    else:
        s2_bull = ema_bull
        s2_status = f"⚖️ انتظار سحب سيولة ({kz_tag})"

    s3_bull = psar_bull
    s3_status = f"🟢 صاعد أعلى SAR (`{psar_val}`) | ADX: `{adx}`" if s3_bull else f"🔴 هابط أسفل SAR (`{psar_val}`) | ADX: `{adx}`"

    avg_bb_width = float(ltf["BB_WIDTH"].tail(40).mean())
    is_squeeze = float(curr["BB_WIDTH"]) < (avg_bb_width * 0.85)
    sq_txt = "⚡ انضغاط انفجاري" if is_squeeze else "تمدد طبيعي"
    s4_bull = vwap_bull and macd_bull
    s4_status = f"🟢 أعلى VWAP (`{vwap_val}`) | {sq_txt}" if vwap_bull else f"🔴 أسفل VWAP (`{vwap_val}`) | {sq_txt}"

    s5_bull = poc_bull
    vp_loc = "اختراق شرائي أعلى VAH" if price > vah_val else ("كسر بيعي أسفل VAL" if price < val_val else "داخل منطقة القيمة")
    s5_status = f"🟢 ارتكاز أعلى POC (`{poc_val}`) — {vp_loc}" if s5_bull else f"🔴 ضغط أسفل POC (`{poc_val}`) — {vp_loc}"

    bull_votes = sum([1 if s1_bull else 0, 1 if s2_bull else 0, 1 if s3_bull else 0, 1 if s4_bull else 0, 1 if s5_bull else 0])
    if div_info["found"] and div_info["rsi_div"]:
        bull_votes += 1 if div_info["rsi_div"]["bias"] == "BULL" else -1

    final_is_long = bull_votes >= 3
    aligned_count = sum([
        1 if s1_bull == final_is_long else 0,
        1 if s2_bull == final_is_long else 0,
        1 if s3_bull == final_is_long else 0,
        1 if s4_bull == final_is_long else 0,
        1 if s5_bull == final_is_long else 0
    ])
    confidence = min(60 + (aligned_count * 7) + (4 if in_killzone else 0), 99)

    return {
        "is_long": final_is_long, "aligned_count": aligned_count, "confidence": confidence,
        "psar": psar_val, "vwap": vwap_val, "poc": poc_val, "vah": vah_val, "val": val_val,
        "s1_status": s1_status, "s2_status": s2_status, "s3_status": s3_status, "s4_status": s4_status, "s5_status": s5_status
    }

# ==================== 5. كشف الدايفرجنس وهيكل السوق ومحرك الاقتناص القريب ====================
def detect_rsi_macd_divergence(plot_df):
    n = len(plot_df)
    if n < 20:
        return {"found": False, "text": "حيادي", "rsi_div": None, "macd_div": None}
    piv_lows, piv_highs = [], []
    for i in range(2, n - 1):
        l, h = float(plot_df["low"].iloc[i]), float(plot_df["high"].iloc[i])
        if l <= float(plot_df["low"].iloc[i-1]) and l <= float(plot_df["low"].iloc[i-2]) and l <= float(plot_df["low"].iloc[i+1]):
            if not piv_lows or (i - piv_lows[-1]) >= 4:
                piv_lows.append(i)
        if h >= float(plot_df["high"].iloc[i-1]) and h >= float(plot_df["high"].iloc[i-2]) and h >= float(plot_df["high"].iloc[i+1]):
            if not piv_highs or (i - piv_highs[-1]) >= 4:
                piv_highs.append(i)

    rsi_res, macd_res = None, None
    if len(piv_lows) >= 2:
        i1, i2 = piv_lows[-2], piv_lows[-1]
        p1, p2 = float(plot_df["low"].iloc[i1]), float(plot_df["low"].iloc[i2])
        r1, r2 = float(plot
