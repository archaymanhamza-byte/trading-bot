# Hardened derivative of the user-provided bot.
# Important: price levels are informational estimates; this bot does not execute trades.
# Persistent user records, per-user settings, and monitored trades are stored in SQLite.
# Email approval tokens are random, hashed at rest, expire, and are consumed once on POST.
import os
import io
import json
import time
import threading
import wave
import struct
import math
import hashlib
import html
import secrets
import tempfile
import sqlite3
from contextlib import contextmanager
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
from werkzeug.security import generate_password_hash, check_password_hash

# ==================== 1. الإعدادات الأساسية وإعدادات الحماية ====================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
ADMIN_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL", "https://trading-bot-1-88ha.onrender.com").strip()

BOT_PASSWORD = os.getenv("BOT_PASSWORD", "").strip()
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "").strip()
SMTP_USER = os.getenv("SMTP_USER", "").strip()
SMTP_APP_PASS = os.getenv("SMTP_APP_PASS", "").strip()

TRACKER_INTERVAL = 15
AUTO_SCAN_INTERVAL = 900
active_trades = {}
trades_lock = threading.Lock()

# ==================== SQLite persistence ====================
DB_PATH = os.path.abspath(os.getenv("BOT_DB_PATH", "trading_bot.sqlite3"))
users_lock = threading.RLock()
trades_lock = threading.RLock()
settings_context = threading.local()

@contextmanager
def db_connect():
    conn = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
    finally:
        conn.close()

def init_db():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    with db_connect() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS app_state (
                key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS settings (
                user_id TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS active_trades (
                trade_key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS admin_tokens (
                token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL,
                action TEXT NOT NULL CHECK(action IN ('approve','reject')),
                expires_at INTEGER NOT NULL, used_at INTEGER, created_at INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_admin_tokens_expiry ON admin_tokens(expires_at);
        """)

init_db()

def load_users_from_disk():
    default = {"approved": {}, "pending": {}, "notified_visitors": set(),
               "banned": set(), "global_enabled": True,
               "password": generate_password_hash(BOT_PASSWORD) if BOT_PASSWORD else "", "admin_email": ADMIN_EMAIL}
    with db_connect() as conn:
        row = conn.execute("SELECT value FROM app_state WHERE key='users'").fetchone()
    if not row:
        # One-time migration from the prior JSON store, if present.
        old_path = os.getenv("USERS_FILE", "users_store.json")
        if os.path.exists(old_path):
            try:
                with open(old_path, encoding="utf-8") as f:
                    old = json.load(f)
                default.update(old)
                stored_password=str(default.get("password", ""))
                if stored_password and not stored_password.startswith(("pbkdf2:", "scrypt:")):
                    default["password"] = generate_password_hash(stored_password)
                default["banned"] = set(default.get("banned", []))
                default["notified_visitors"] = set(default.get("notified_visitors", []))
                save_users_to_disk(default)
            except Exception as e:
                print(f"Legacy users migration error: {e}")
        return default
    try:
        data=json.loads(row["value"])
        default.update(data)
        stored_password=str(default.get("password", ""))
        if stored_password and not stored_password.startswith(("pbkdf2:", "scrypt:")):
            default["password"] = generate_password_hash(stored_password)
            save_users_to_disk(default)
        default["banned"] = set(default.get("banned", []))
        default["notified_visitors"] = set(default.get("notified_visitors", []))
        return default
    except Exception as e:
        print(f"SQLite users state error: {e}")
        return default

def save_users_to_disk(data=None):
    with users_lock:
        value = data if data is not None else users_db
        payload = {"approved": value["approved"], "pending": value["pending"],
                   "notified_visitors": list(value["notified_visitors"]),
                   "banned": list(value["banned"]), "global_enabled": value["global_enabled"],
                   "password": value["password"], "admin_email": value["admin_email"]}
        with db_connect() as conn:
            conn.execute("INSERT INTO app_state(key,value,updated_at) VALUES('users',?,?) "
                         "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
                         (json.dumps(payload, ensure_ascii=False), int(time.time())))

users_db = load_users_from_disk()

def verify_bot_password(candidate, stored):
    stored=str(stored or "")
    if stored.startswith(("pbkdf2:", "scrypt:")):
        try: return check_password_hash(stored, str(candidate))
        except ValueError: return False
    # One-time migration of a legacy/default plaintext value after successful authentication.
    if stored and secrets.compare_digest(str(candidate).encode("utf-8"), stored.encode("utf-8")):
        with users_lock:
            users_db["password"] = generate_password_hash(str(candidate))
            save_users_to_disk()
        return True
    return False

def load_trades_from_db():
    with db_connect() as conn:
        rows=conn.execute("SELECT trade_key,value FROM active_trades").fetchall()
    result={}
    for row in rows:
        try: result[row["trade_key"]]=json.loads(row["value"])
        except Exception as e: print(f"Ignoring invalid saved trade {row['trade_key']}: {e}")
    return result

active_trades = load_trades_from_db()

def persist_active_trades():
    # Replace the snapshot in one transaction so stale rows cannot survive a deletion.
    with trades_lock:
        snapshot={k: dict(v) for k,v in active_trades.items()}
    with db_connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM active_trades")
        conn.executemany("INSERT INTO active_trades(trade_key,value,updated_at) VALUES(?,?,?)",
                         [(k,json.dumps(v,ensure_ascii=False),int(time.time())) for k,v in snapshot.items()])
        conn.commit()

# ==================== إعدادات الفريمات ونظام الأرباح بالدولار (TP1 / TP2 / TP3) ====================
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

DEFAULT_USER_SETTINGS = {
    "mode_name": "مضاربة متوسطة (15m / 4h)", "ltf": "15m", "htf": "4h",
    "profit_style": "scalp", "lot_size": 0.01,
    "tp1_usd": 5.0, "tp2_usd": 10.0, "tp3_usd": 20.0, "sl_usd": 5.0
}

def current_settings_user():
    return str(getattr(settings_context, "user_id", "default"))

def bind_settings_user(user_id):
    settings_context.user_id = str(user_id or "default")

class UserSettings:
    def _read(self):
        uid=current_settings_user()
        with db_connect() as conn:
            row=conn.execute("SELECT value FROM settings WHERE user_id=?",(uid,)).fetchone()
        data=dict(DEFAULT_USER_SETTINGS)
        if row:
            try: data.update(json.loads(row["value"]))
            except Exception as e: print(f"Invalid settings for {uid}: {e}")
        return uid,data
    def _write(self, uid, data):
        with db_connect() as conn:
            conn.execute("INSERT INTO settings(user_id,value,updated_at) VALUES(?,?,?) "
                         "ON CONFLICT(user_id) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
                         (uid,json.dumps(data,ensure_ascii=False),int(time.time())))
    def __getitem__(self,key): return self._read()[1][key]
    def __setitem__(self,key,value):
        uid,data=self._read(); data[key]=value; self._write(uid,data)
    def __iter__(self): return iter(self._read()[1])
    def __len__(self): return len(self._read()[1])
    def update(self,*args,**kwargs):
        uid,data=self._read(); data.update(*args,**kwargs); self._write(uid,data)

user_settings = UserSettings()

TIMEFRAME_MODES = {
    "scalp": {"name": "سكالبينج سريع (5m / 15m) ⚡", "ltf": "5m", "htf": "15m"},
    "intraday": {"name": "مضاربة يومية (15m / 4h) ⏱", "ltf": "15m", "htf": "4h"},
    "swing": {"name": "طويل المدى Swing (4h / 1d) 🌊", "ltf": "4h", "htf": "1d"}
}

HTF_MAP = {
    "1m": "15m", "5m": "15m", "15m": "4h", "1h": "4h", "4h": "1d", "1d": "1d"
}

def usd_to_price_distance(sym_type, usd_amount, lot_size):
    """
    تحويل الربح المطلوب بالدولار إلى مسافة سعرية على الشارت:
    - في الذهب (XAUUSD): عقد 1.00 لوت = 100 أونصة (أي عند لوت 0.01 كل 1 دولار حركة في السعر = 1 دولار ربح).
    - في البيتكوين (BTCUSD): عقد 0.01 لوت = 0.01 عملة (أي كل 100 دولار حركة في السعر = 1 دولار ربح).
    """
    safe_lot = max(float(lot_size), 0.001)
    if sym_type == "GOLD":
        return round(float(usd_amount) / (safe_lot * 100.0), 2)
    else:
        return round(float(usd_amount) / safe_lot, 2)

app = Flask(__name__)

USER_KEYBOARD = {
    "keyboard": [
        [{"text": "🪙 مناطق دخول الذهب"}, {"text": "₿ مناطق دخول البيتكوين"}],
        [{"text": "🎯 ربح سكالب ($5/$10/$20)"}, {"text": "🌊 ربح سوينق بعيد ($15/$30/$60)"}],
        [{"text": "⚡ فريم سكالب (5m)"}, {"text": "⏱ فريم متوسط (15m)"}, {"text": "🌊 فريم طويل (4h/1d)"}],
        [{"text": "📊 فحص شامل بالسوق"}, {"text": "📂 الأوامر المعلقة والنشطة"}, {"text": "⚙️ إعدادات الربح واللوت"}],
        [{"text": "🔊 تجربة الناطق العربي"}]
    ],
    "resize_keyboard": True,
    "is_persistent": True
}

ADMIN_KEYBOARD = {
    "keyboard": [
        [{"text": "🪙 مناطق دخول الذهب"}, {"text": "₿ مناطق دخول البيتكوين"}],
        [{"text": "🎯 ربح سكالب ($5/$10/$20)"}, {"text": "🌊 ربح سوينق بعيد ($15/$30/$60)"}],
        [{"text": "⚡ فريم سكالب (5m)"}, {"text": "⏱ فريم متوسط (15m)"}, {"text": "🌊 فريم طويل (4h/1d)"}],
        [{"text": "📊 فحص شامل بالسوق"}, {"text": "📂 الأوامر المعلقة والنشطة"}, {"text": "⚙️ إعدادات الربح واللوت"}],
        [{"text": "🔐 لوحة تحكم المشرف"}, {"text": "🔊 تجربة الناطق العربي"}, {"text": "🗑️ مسح الصفقات"}]
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

def get_all_recipient_ids():
    recipients = []
    if ADMIN_CHAT_ID:
        recipients.append(str(ADMIN_CHAT_ID))
    with users_lock:
        if users_db["global_enabled"]:
            for uid in users_db["approved"].keys():
                if uid not in recipients and uid not in users_db["banned"]:
                    recipients.append(uid)
    return recipients

ADMIN_TOKEN_TTL = 30 * 60

def issue_admin_token(user_id, action):
    if action not in ("approve", "reject"):
        raise ValueError("Unsupported admin action")
    raw=secrets.token_urlsafe(32)
    digest=hashlib.sha256(raw.encode("utf-8")).hexdigest()
    now=int(time.time())
    with db_connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM admin_tokens WHERE expires_at < ? OR used_at IS NOT NULL",(now,))
        conn.execute("UPDATE admin_tokens SET used_at=? WHERE user_id=? AND action=? AND used_at IS NULL",
                     (now,str(user_id),action))
        conn.execute("INSERT INTO admin_tokens(token_hash,user_id,action,expires_at,created_at) VALUES(?,?,?,?,?)",
                     (digest,str(user_id),action,now+ADMIN_TOKEN_TTL,now))
        conn.commit()
    return raw

def admin_token_is_valid(user_id, action, raw):
    digest=hashlib.sha256(raw.encode("utf-8")).hexdigest()
    with db_connect() as conn:
        row=conn.execute("SELECT 1 FROM admin_tokens WHERE token_hash=? AND user_id=? AND action=? AND used_at IS NULL AND expires_at>?",
                         (digest,str(user_id),action,int(time.time()))).fetchone()
    return row is not None

def consume_admin_token(user_id, action, raw):
    digest=hashlib.sha256(raw.encode("utf-8")).hexdigest()
    now=int(time.time())
    with db_connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        cur=conn.execute("UPDATE admin_tokens SET used_at=? WHERE token_hash=? AND user_id=? AND action=? AND used_at IS NULL AND expires_at>?",
                         (now,digest,str(user_id),action,now))
        if cur.rowcount == 1:
            # A decision invalidates the other action link for the same pending request.
            conn.execute("UPDATE admin_tokens SET used_at=? WHERE user_id=? AND used_at IS NULL",
                         (now,str(user_id)))
        conn.commit()
        return cur.rowcount == 1

def send_approval_email(user_id, full_name, username):
    target_email = users_db["admin_email"] or ADMIN_EMAIL
    if not target_email or not SMTP_USER or not SMTP_APP_PASS:
        return False, "التأكيد مفعل عبر تيليجرام مباشرة"

    approve_tok = issue_admin_token(user_id, "approve")
    reject_tok = issue_admin_token(user_id, "reject")
    approve_url = f"{RENDER_EXTERNAL_URL}/admin/action?uid={user_id}&act=approve&token={approve_tok}"
    reject_url = f"{RENDER_EXTERNAL_URL}/admin/action?uid={user_id}&act=reject&token={reject_tok}"

    safe_name = html.escape(str(full_name))
    safe_username = html.escape(str(username))
    safe_user_id = html.escape(str(user_id))
    subject = f"طلب تفعيل مستخدم جديد في بوت التداول: {full_name}"
    html_body = f"""
    <div dir="rtl" style="font-family: Arial, sans-serif; padding: 20px; background: #f4f6f9; border-radius: 10px;">
        <h2>🚨 طلب دخول جديد إلى بوت الذهب والبيتكوين</h2>
        <p>يطلب المستخدم التالي الدخول إلى البوت وينتظر موافقتك:</p>
        <ul>
            <li><b>الاسم:</b> {safe_name}</li>
            <li><b>المعرف:</b> @{safe_username}</li>
            <li><b>Chat ID:</b> <code>{safe_user_id}</code></li>
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
                {"text": "📅 1d يومي", "callback_data": f"TF|{sym_type}|1d"}
            ],
            [
                {"text": "🎯 أهداف سكالب ($5/$10/$20)", "callback_data": f"PMODE|{sym_type}|scalp"},
                {"text": "🌊 أهداف سوينق ($15/$30/$60)", "callback_data": f"PMODE|{sym_type}|swing"}
            ]
        ]
    }


def escape_markdown(text):
    """Escape untrusted text before embedding it in Telegram Markdown messages."""
    value = str(text)
    for char in ("\\", "_", "*", "[", "`"):
        value = value.replace(char, "\\" + char)
    return value

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

def broadcast_msg(text):
    for uid in get_all_recipient_ids():
        send_msg(text, uid)

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

def send_arabic_voice_alert(spoken_text, caption, chat_id=None):
    audio_bytes, filename, mime_type = generate_arabic_tts_audio(spoken_text)
    targets = [str(chat_id)] if chat_id else get_all_recipient_ids()
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendVoice"
    for target in targets:
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

# ==================== 5. كشف الدايفرجنس وهيكل السوق و OTE ====================
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
        r1, r2 = float(plot_df["RSI"].iloc[i1]), float(plot_df["RSI"].iloc[i2])
        m1, m2 = float(plot_df["MACD"].iloc[i1]), float(plot_df["MACD"].iloc[i2])
        if (p2 < p1 and r2 > r1) or (p2 > p1 and r2 < r1):
            rsi_res = {"found": True, "bias": "BULL", "label": "RSI Bull Div", "x1": i1, "x2": i2, "p1": p1, "p2": p2, "v1": r1, "v2": r2, "color": "#00e676"}
        if (p2 < p1 and m2 > m1) or (p2 > p1 and m2 < m1):
            macd_res = {"found": True, "bias": "BULL", "label": "MACD Bull Div", "x1": i1, "x2": i2, "p1": p1, "p2": p2, "v1": m1, "v2": m2, "color": "#00e676"}

    if len(piv_highs) >= 2:
        i1, i2 = piv_highs[-2], piv_highs[-1]
        p1, p2 = float(plot_df["high"].iloc[i1]), float(plot_df["high"].iloc[i2])
        r1, r2 = float(plot_df["RSI"].iloc[i1]), float(plot_df["RSI"].iloc[i2])
        m1, m2 = float(plot_df["MACD"].iloc[i1]), float(plot_df["MACD"].iloc[i2])
        if (p2 > p1 and r2 < r1) or (p2 < p1 and r2 > r1):
            rsi_res = {"found": True, "bias": "BEAR", "label": "RSI Bear Div", "x1": i1, "x2": i2, "p1": p1, "p2": p2, "v1": r1, "v2": r2, "color": "#ff5252"}
        if (p2 > p1 and m2 < m1) or (p2 < p1 and m2 > m1):
            macd_res = {"found": True, "bias": "BEAR", "label": "MACD Bear Div", "x1": i1, "x2": i2, "p1": p1, "p2": p2, "v1": m1, "v2": m2, "color": "#ff5252"}

    txt = "حيادي"
    if rsi_res and macd_res:
        txt = "🌟 دايفرجنس مزدوج مؤكد (RSI + MACD)"
    elif rsi_res:
        txt = f"🟢 دايفرجنس RSI ({rsi_res['label']})"
    elif macd_res:
        txt = f"🔵 دايفرجنس MACD ({macd_res['label']})"
    return {"found": bool(rsi_res or macd_res), "text": txt, "rsi_div": rsi_res, "macd_div": macd_res}

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
        events.append({"type": "BOS/CHoCH", "bias": "BULL", "level": piv_highs[-2][1], "text": "BOS/CHoCH BULL", "color": "#00e676"})
    if len(piv_lows) >= 2 and float(plot_df["close"].iloc[-1]) < piv_lows[-2][1]:
        events.append({"type": "BOS/CHoCH", "bias": "BEAR", "level": piv_lows[-2][1], "text": "BOS/CHoCH BEAR", "color": "#ff5252"})
    return events

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

    demand_bottom, demand_top = max(swing_low, bull_ote_786 - (0.2 * atr)), max(swing_low + (0.5 * atr), bull_ote_618)
    supply_bottom, supply_top = min(swing_high - (0.5 * atr), bear_ote_618), min(swing_high, bear_ote_786 + (0.2 * atr))

    half_band = 0.15 * atr
    if is_long_bias:
        ote_zone = (bull_ote_786, bull_ote_618)
        ote_sweet = bull_ote_705
        sniper_zone = (round(ote_sweet - half_band, 2), round(ote_sweet + half_band, 2))
    else:
        ote_zone = (bear_ote_618, bear_ote_786)
        ote_sweet = bear_ote_705
        sniper_zone = (round(ote_sweet - half_band, 2), round(ote_sweet + half_band, 2))

    return {
        "bsl": round(swing_high, 2), "ssl": round(swing_low, 2), "eq_50": eq_50,
        "ote_zone": ote_zone, "ote_sweet": ote_sweet, "sniper_zone": sniper_zone,
        "demand_zone": (round(demand_bottom, 2), round(demand_top, 2)),
        "supply_zone": (round(supply_bottom, 2), round(supply_top, 2))
    }

# ==================== 6. رسم الشارت المتكامل مع TP1 و TP2 و TP3 بالدولار ====================
def generate_chart_image(df, zones, vp_data, div_info, bos_events, asset_title, entry_zone, sl, exit_targets, usd_targets, is_long=True):
    try:
        plot_df = df.tail(55).reset_index(drop=True)
        fig, (ax, ax_rsi, ax_macd) = plt.subplots(
            3, 1, figsize=(10.8, 9.4), dpi=115, sharex=True,
            gridspec_kw={"height_ratios": [3.5, 1.0, 1.0], "hspace": 0.08}
        )
        fig.patch.set_facecolor("#131722")
        for a in [ax, ax_rsi, ax_macd]:
            a.set_facecolor("#131722")

        max_v = max(vp_data["bin_volumes"]) if vp_data["bin_volumes"] else 1.0
        for b_i, (b_center, b_vol) in enumerate(zip(vp_data["bin_centers"], vp_data["bin_volumes"])):
            bar_w = (b_vol / max(max_v, 1e-9)) * 14.0
            is_poc_bar = (b_i == vp_data["poc_idx"])
            bar_col = "#00e5ff" if is_poc_bar else "#5c6bc0"
            bar_alpha = 0.55 if is_poc_bar else 0.25
            ax.barh(b_center, bar_w, height=vp_data["bin_height"], left=-1.0, color=bar_col, alpha=bar_alpha, edgecolor=bar_col, zorder=2)

        poc_p = vp_data["poc"]
        vah_p = vp_data["vah"]
        val_p = vp_data["val"]
        x_end = len(plot_df) + 4
        ax.set_xlim(-1.2, x_end + 9)

        ax.axhline(poc_p, color="#00e5ff", linestyle="-", linewidth=1.7, alpha=0.95, label=f"POC ({poc_p})", zorder=4)
        ax.axhline(vah_p, color="#00b0ff", linestyle=":", linewidth=0.9, alpha=0.75, zorder=2)
        ax.axhline(val_p, color="#00b0ff", linestyle=":", linewidth=0.9, alpha=0.75, zorder=2)
        ax.text(x_end + 0.2, poc_p, f" POC: {poc_p}", color="#00e5ff", fontsize=8, va="center", fontweight="bold")

        for idx, row in plot_df.iterrows():
            o, h, l, c = float(row["open"]), float(row["high"]), float(row["low"]), float(row["close"])
            color = "#26a69a" if c >= o else "#ef5350"
            ax.plot([idx, idx], [l, h], color=color, linewidth=1.1, zorder=3)
            ax.bar(idx, max(abs(c - o), (h - l) * 0.02, 1e-4), bottom=min(o, c), width=0.62, color=color, edgecolor=color, zorder=4)

        if "PSAR" in plot_df.columns:
            psar_colors = ["#00e676" if plot_df["close"].iloc[i] >= plot_df["PSAR"].iloc[i] else "#ff5252" for i in range(len(plot_df))]
            ax.scatter(plot_df.index, plot_df["PSAR"], c=psar_colors, s=11, alpha=0.9, label="Parabolic SAR", zorder=5)
        if "VWAP" in plot_df.columns:
            ax.plot(plot_df.index, plot_df["VWAP"], color="#e040fb", linewidth=1.3, linestyle="-.", label="VWAP", zorder=5)

        d_low, d_high = zones["demand_zone"]
        s_low, s_high = zones["supply_zone"]
        ax.add_patch(patches.Rectangle((0, d_low), width=x_end, height=max(d_high - d_low, 1e-3), facecolor="#00e676", alpha=0.13, edgecolor="#00e676", linewidth=1, zorder=1))
        ax.add_patch(patches.Rectangle((0, s_low), width=x_end, height=max(s_high - s_low, 1e-3), facecolor="#ff5252", alpha=0.13, edgecolor="#ff5252", linewidth=1, zorder=1))

        ote_low, ote_high = zones["ote_zone"]
        ax.add_patch(patches.Rectangle((len(plot_df) - 28, ote_low), width=32, height=max(ote_high - ote_low, 1e-3), facecolor="#ffd700", alpha=0.24, edgecolor="#ffd700", linestyle="--", linewidth=1.3, zorder=2))
        ax.axhline(zones["ote_sweet"], color="#ffd700", linestyle=":", linewidth=1.1, alpha=0.95)
        ax.text(len(plot_df) - 27, zones["ote_sweet"], f" GOLDEN OTE | 70.5%: {zones['ote_sweet']}", color="#ffd700", fontsize=7.5, va="bottom", fontweight="bold")

        for ev in bos_events:
            ax.axhline(ev["level"], color=ev["color"], linestyle="--", linewidth=1.1, alpha=0.9, zorder=4)
            ax.text(len(plot_df) - 12, ev["level"], f" {ev['text']}", color=ev["color"], fontsize=7.5, va="bottom", fontweight="bold")

        e_mid = round((entry_zone[0] + entry_zone[1]) / 2, 2)
        tp1, tp2, tp3 = exit_targets
        u1, u2, u3, u_sl = usd_targets

        ax.axhline(e_mid, color="#ffffff", linestyle="--", linewidth=1.2, alpha=0.95)
        ax.axhline(sl, color="#ff1744", linestyle="-", linewidth=1.3, alpha=0.95)
        ax.axhline(tp1, color="#26a69a", linestyle="--", linewidth=1.1, alpha=0.9)
        ax.axhline(tp2, color="#00e676", linestyle="--", linewidth=1.2, alpha=0.95)
        ax.axhline(tp3, color="#69f0ae", linestyle="-", linewidth=1.4, alpha=0.98)

        label_x = x_end + 0.2
        ax.text(label_x, e_mid, f" ENTRY: {e_mid}", color="#ffffff", fontsize=7.8, va="center", fontweight="bold")
        ax.text(label_x, sl, f" SL (-${u_sl}): {sl}", color="#ff1744", fontsize=7.8, va="center", fontweight="bold")
        ax.text(label_x, tp1, f" TP1 (+${u1}): {tp1}", color="#26a69a", fontsize=7.8, va="center", fontweight="bold")
        ax.text(label_x, tp2, f" TP2 (+${u2}): {tp2}", color="#00e676", fontsize=7.8, va="center", fontweight="bold")
        ax.text(label_x, tp3, f" TP3 (+${u3}): {tp3}", color="#69f0ae", fontsize=7.8, va="center", fontweight="bold")
        ax.legend(loc="upper left", facecolor="#1e222d", edgecolor="#2a2e39", labelcolor="#ffffff", fontsize=7.5)

        ax_rsi.plot(plot_df.index, plot_df["RSI"], color="#ab47bc", linewidth=1.3)
        ax_rsi.axhline(70, color="#ef5350", linestyle=":", linewidth=0.8)
        ax_rsi.axhline(30, color="#26a69a", linestyle=":", linewidth=0.8)
        ax_rsi.set_ylabel("RSI", color="#b2b5be", fontsize=8)
        if div_info and div_info.get("rsi_div"):
            r_d = div_info["rsi_div"]
            ax.plot([r_d["x1"], r_d["x2"]], [r_d["p1"], r_d["p2"]], color=r_d["color"], linewidth=2.0, marker="o", markersize=3, zorder=6)
            ax_rsi.plot([r_d["x1"], r_d["x2"]], [r_d["v1"], r_d["v2"]], color=r_d["color"], linewidth=2.0, marker="o", markersize=3, zorder=6)

        hist_colors = ["#26a69a" if v >= 0 else "#ef5350" for v in plot_df["MACD_HIST"]]
        ax_macd.bar(plot_df.index, plot_df["MACD_HIST"], color=hist_colors, width=0.6, alpha=0.75)
        ax_macd.plot(plot_df.index, plot_df["MACD"], color="#2962ff", linewidth=1.2)
        ax_macd.plot(plot_df.index, plot_df["MACD_SIGNAL"], color="#ff9800", linewidth=1.2)
        ax_macd.axhline(0, color="#787b86", linestyle="--", linewidth=0.8)
        ax_macd.set_ylabel("MACD", color="#b2b5be", fontsize=8)
        if div_info and div_info.get("macd_div"):
            m_d = div_info["macd_div"]
            ax_macd.plot([m_d["x1"], m_d["x2"]], [m_d["v1"], m_d["v2"]], color=m_d["color"], linewidth=2.0, marker="o", markersize=3, zorder=6)

        ax.set_title(f"{asset_title} | Targets: +${u1} / +${u2} / +${u3} (Lot: {user_settings['lot_size']}) | POC: {poc_p}", color="#ffffff", fontsize=9.5, fontweight="bold", pad=10)
        for a in [ax, ax_rsi, ax_macd]:
            a.grid(color="#2a2e39", linestyle="--", linewidth=0.5, alpha=0.6)
            a.tick_params(colors="#b2b5be", labelsize=7.5)
            for spine in a.spines.values():
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

# ==================== 7. بناء خطة التداول بالأهداف الثلاثة بالدولار ($5 / $10 / $20) ====================
def register_pending_trade(trade_key, asset_name, sym_type, is_long, entry_zone, sl, exit_targets, usd_targets, tf_label):
    with trades_lock:
        old = active_trades.get(trade_key)
        already_sent = old.get("voice_alert_sent", False) if (old and abs(old["entry_low"] - float(entry_zone[0])) < 0.5) else False
        active_trades[trade_key] = {
            "asset": asset_name, "symbol_type": sym_type, "is_long": is_long,
            "entry_low": float(entry_zone[0]), "entry_high": float(entry_zone[1]),
            "entry_mid": round((entry_zone[0] + entry_zone[1]) / 2, 2),
            "sl": float(sl),
            "tp1": float(exit_targets[0]),
            "tp2": float(exit_targets[1]),
            "tp3": float(exit_targets[2]),
            "tp1_usd": usd_targets[0],
            "tp2_usd": usd_targets[1],
            "tp3_usd": usd_targets[2],
            "sl_usd": usd_targets[3],
            "status": "PENDING",
            "voice_alert_sent": already_sent,
            "tp1_hit": False,
            "tp2_hit": False,
            "tf": tf_label,
            "created_at": time.strftime("%H:%M UTC", time.gmtime()),
            "user_id": current_settings_user()
        }
        persist_active_trades()

def analyze_and_send_zones(sym_type, ltf_str=None, chat_id=None, silent_register_only=False):
    ltf_use = ltf_str or user_settings["ltf"]
    htf_use = HTF_MAP.get(ltf_use, "4h")

    if sym_type == "GOLD":
        asset_name, chart_code = "XAU/USD (الذهب)", "XAU/USD"
        ltf_df, htf_df = get_gold_ohlcv(ltf_use), get_gold_ohlcv(htf_use)
    else:
        asset_name, chart_code = "BTC/USDT (البيتكوين)", "BTC/USDT"
        ltf_df, htf_df = get_btc_ohlcv(ltf_use), get_btc_ohlcv(htf_use)

    if ltf_df.empty or len(ltf_df) < 35:
        if not silent_register_only:
            send_msg(f"⚠️ بيانات {asset_name} غير متوفرة حالياً.", chat_id)
        return
    if htf_df.empty or len(htf_df) < 25:
        htf_df = ltf_df

    ltf = enrich_indicators(ltf_df)
    htf = enrich_indicators(htf_df)
    plot_slice = ltf.tail(55).reset_index(drop=True)
    div_info = detect_rsi_macd_divergence(plot_slice)
    bos_events = detect_bos_choch(plot_slice)
    vp_data = calculate_volume_profile(ltf, num_bins=24)

    strat_matrix = evaluate_multi_strategies(ltf, htf, div_info, bos_events, vp_data)
    is_long = strat_matrix["is_long"]

    zones = analyze_structure_and_ote(ltf, is_long)
    entry_zone = zones["sniper_zone"]
    poc_p = vp_data["poc"]

    l_curr = ltf.iloc[-1]
    curr_price = round(float(l_curr["close"]), 2)

    lot = user_settings["lot_size"]
    u_tp1 = user_settings["tp1_usd"]
    u_tp2 = user_settings["tp2_usd"]
    u_tp3 = user_settings["tp3_usd"]
    u_sl = user_settings["sl_usd"]

    dist_tp1 = usd_to_price_distance(sym_type, u_tp1, lot)
    dist_tp2 = usd_to_price_distance(sym_type, u_tp2, lot)
    dist_tp3 = usd_to_price_distance(sym_type, u_tp3, lot)
    dist_sl = usd_to_price_distance(sym_type, u_sl, lot)

    entry_mid = round((entry_zone[0] + entry_zone[1]) / 2, 2)

    if is_long:
        dir_label = "شراء معلق (BUY LIMIT) 🟢"
        sl = round(entry_mid - dist_sl, 2)
        tp1 = round(entry_mid + dist_tp1, 2)
        tp2 = round(entry_mid + dist_tp2, 2)
        tp3 = round(entry_mid + dist_tp3, 2)
    else:
        dir_label = "بيع معلق (SELL LIMIT) 🔴"
        sl = round(entry_mid + dist_sl, 2)
        tp1 = round(entry_mid - dist_tp1, 2)
        tp2 = round(entry_mid - dist_tp2, 2)
        tp3 = round(entry_mid - dist_tp3, 2)

    exit_targets = (tp1, tp2, tp3)
    usd_targets = (u_tp1, u_tp2, u_tp3, u_sl)

    register_pending_trade(f"{current_settings_user()}_{sym_type}_{ltf_use}", asset_name, sym_type, is_long, entry_zone, sl, exit_targets, usd_targets, ltf_use)
    if silent_register_only:
        return

    style_name = "سكالبينج سريع ⚡" if user_settings["profit_style"] == "scalp" else ("سوينق بعيد 🌊" if user_settings["profit_style"] == "swing" else "مخصص ⚙️")

    caption = (
        f"⚡ *تقرير القناص الاحترافي (أهداف بالدولار TP1 / TP2 / TP3):*\n"
        f"📊 *الأصل:* `{asset_name}` | *الفريم:* `{ltf_use}` (الأكبر `{htf_use}`)\n"
        f"💵 *السعر الحالي:* `{curr_price}` | *التوافق:* `{strat_matrix['aligned_count']}/5` (`{strat_matrix['confidence']}%`)\n"
        f"💼 *نمط الربح:* `{style_name}` | *حجم اللوت:* `{lot}`\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📋 *مصفوفة الاستراتيجيات الخمس:*\n"
        f"1️⃣ *SMC + OTE:* {strat_matrix['s1_status']}\n"
        f"2️⃣ *ICT Silver Bullet:* {strat_matrix['s2_status']}\n"
        f"3️⃣ *Parabolic SAR + ADX:* {strat_matrix['s3_status']}\n"
        f"4️⃣ *VWAP + BB Squeeze:* {strat_matrix['s4_status']}\n"
        f"5️⃣ *Volume Profile POC:* {strat_matrix['s5_status']}\n"
        f"🧬 *الدايفرجنس (RSI/MACD):* {div_info['text']}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📌 *القرار الموحد:* {dir_label}\n"
        f"🏛 *ارتكاز السيولة (POC):* `{poc_p}`\n"
        f"🎯 *منطقة الدخول القناصة:* `{entry_zone[0]}` إلى `{entry_zone[1]}` (المنتصف: `{entry_mid}`)\n"
        f"🛑 *وقف الخسارة (SL = -${u_sl}):* `{sl}`\n"
        f"✅ *الهدف الأول (TP1 = +${u_tp1}):* `{tp1}`\n"
        f"🔥 *الهدف الثاني (TP2 = +${u_tp2}):* `{tp2}`\n"
        f"🚀 *الهدف الثالث (TP3 = +${u_tp3}):* `{tp3}`"
    )
    chart_buf = generate_chart_image(ltf, zones, vp_data, div_info, bos_events, f"{chart_code} ({ltf_use})", entry_zone, sl, exit_targets, usd_targets, is_long)
    send_photo_msg(chart_buf, caption, sym_type, chat_id)

def auto_zone_scanner_loop():
    time.sleep(5)
    while True:
        try:
            analyze_and_send_zones("GOLD", user_settings["ltf"], silent_register_only=True)
            analyze_and_send_zones("BTC", user_settings["ltf"], silent_register_only=True)
        except Exception as e:
            print(f"Auto scanner error: {type(e).__name__}: {e}")
        time.sleep(AUTO_SCAN_INTERVAL)

def get_latest_price_extremes(symbol_type):
    df = get_btc_ohlcv("1m", limit=3) if symbol_type == "BTC" else get_gold_ohlcv("5m")
    if df.empty:
        return None
    last = df.iloc[-1]
    return {"close": round(float(last["close"]), 2), "high": round(float(last["high"]), 2), "low": round(float(last["low"]), 2)}

def notify_trade_voice(trade, spoken_text, caption):
    owner = str(trade.get("user_id", "default"))
    send_arabic_voice_alert(spoken_text, caption, None if owner == "default" else owner)

def notify_trade_text(trade, text):
    owner = str(trade.get("user_id", "default"))
    if owner == "default":
        broadcast_msg(text)
    else:
        send_msg(text, owner)

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
                                    persist_active_trades()
                            speech_txt = f"تنبيه هام من بوت التداول. وصل سعر {arabic_name} الآن إلى منطقة الدخول الذهبية عند مستوى {int(curr_p)} لصفقة {dir_word}."
                            caption_txt = f"🗣️ *تنبيه صوتي ناطق: وصل سعر {arabic_name} إلى منطقة الدخول!*\n🎯 المنطقة: `{t['entry_low']} - {t['entry_high']}` | السعر: `{curr_p}`"
                            notify_trade_voice(t, speech_txt, caption_txt)

                    if t["status"] == "PENDING":
                        if low_p <= t["entry_high"] and high_p >= t["entry_low"]:
                            with trades_lock:
                                if k in active_trades:
                                    active_trades[k]["status"] = "ACTIVE"
                                    persist_active_trades()
                        continue

                    if t["is_long"]:
                        if high_p >= t["tp3"]:
                            notify_trade_voice(t, 
                                f"مبروك، حقق سعر {arabic_name} الهدف الثالث بالكامل بربح {int(t['tp3_usd'])} دولار.",
                                f"🚀 *مبروك! تحقق الهدف الثالث الكامل (TP3: {t['tp3']} | +${t['tp3_usd']}) للأصل {t['asset']}!*"
                            )
                            with trades_lock:
                                active_trades.pop(k, None)
                                persist_active_trades()
                        elif (not t.get("tp2_hit", False)) and high_p >= t["tp2"]:
                            with trades_lock:
                                if k in active_trades:
                                    active_trades[k]["tp2_hit"] = True
                                    active_trades[k]["tp1_hit"] = True
                                    active_trades[k]["sl"] = t["tp1"]
                                    persist_active_trades()
                            notify_trade_voice(t, 
                                f"مبروك، وصل سعر {arabic_name} إلى الهدف الثاني بربح {int(t['tp2_usd'])} دولار وتم رفع الوقف إلى الهدف الأول.",
                                f"🔥 *تحقق الهدف الثاني (TP2: {t['tp2']} | +${t['tp2_usd']}) لـ {t['asset']} وتم حجز ربح الهدف الأول!*"
                            )
                        elif (not t.get("tp1_hit", False)) and high_p >= t["tp1"]:
                            with trades_lock:
                                if k in active_trades:
                                    active_trades[k]["tp1_hit"] = True
                                    active_trades[k]["sl"] = t["entry_mid"]
                                    persist_active_trades()
                            notify_trade_voice(t, 
                                f"مبروك، وصل سعر {arabic_name} إلى الهدف الأول بربح {int(t['tp1_usd'])} دولار وتم تأمين الصفقة على الدخول.",
                                f"✅ *تحقق الهدف الأول (TP1: {t['tp1']} | +${t['tp1_usd']}) وتأمين الستوب على الدخول لـ {t['asset']}!*"
                            )
                        elif low_p <= t["sl"]:
                            notify_trade_text(t, f"🛑 *تم ضرب مستوى الوقف ({t['sl']}) لـ {t['asset']}.*")
                            with trades_lock:
                                active_trades.pop(k, None)
                                persist_active_trades()
                    else:
                        if low_p <= t["tp3"]:
                            notify_trade_voice(t, 
                                f"مبروك، حقق سعر {arabic_name} الهدف الثالث بالكامل بربح {int(t['tp3_usd'])} دولار.",
                                f"🚀 *مبروك! تحقق الهدف الثالث الكامل (TP3: {t['tp3']} | +${t['tp3_usd']}) للأصل {t['asset']}!*"
                            )
                            with trades_lock:
                                active_trades.pop(k, None)
                                persist_active_trades()
                        elif (not t.get("tp2_hit", False)) and low_p <= t["tp2"]:
                            with trades_lock:
                                if k in active_trades:
                                    active_trades[k]["tp2_hit"] = True
                                    active_trades[k]["tp1_hit"] = True
                                    active_trades[k]["sl"] = t["tp1"]
                                    persist_active_trades()
                            notify_trade_voice(t, 
                                f"مبروك، وصل سعر {arabic_name} إلى الهدف الثاني بربح {int(t['tp2_usd'])} دولار وتم رفع الوقف إلى الهدف الأول.",
                                f"🔥 *تحقق الهدف الثاني (TP2: {t['tp2']} | +${t['tp2_usd']}) لـ {t['asset']} وتم حجز ربح الهدف الأول!*"
                            )
                        elif (not t.get("tp1_hit", False)) and low_p <= t["tp1"]:
                            with trades_lock:
                                if k in active_trades:
                                    active_trades[k]["tp1_hit"] = True
                                    active_trades[k]["sl"] = t["entry_mid"]
                                    persist_active_trades()
                            notify_trade_voice(t, 
                                f"مبروك، وصل سعر {arabic_name} إلى الهدف الأول بربح {int(t['tp1_usd'])} دولار وتم تأمين الصفقة على الدخول.",
                                f"✅ *تحقق الهدف الأول (TP1: {t['tp1']} | +${t['tp1_usd']}) وتأمين الستوب على الدخول لـ {t['asset']}!*"
                            )
                        elif high_p >= t["sl"]:
                            notify_trade_text(t, f"🛑 *تم ضرب مستوى الوقف ({t['sl']}) لـ {t['asset']}.*")
                            with trades_lock:
                                active_trades.pop(k, None)
                                persist_active_trades()
            persist_active_trades()
        except Exception as e:
            print(f"Tracker error: {e}")
        time.sleep(TRACKER_INTERVAL)

# ==================== 8. لوحة التحكم وإعدادات الربح واللوت ====================
def send_profit_settings_guide(chat_id):
    s = user_settings
    txt = (
        f"⚙️ *إعدادات الأهداف بالدولار وحجم العقد (Lot):*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"💼 *النمط الحالي:* `{s['profit_style'].upper()}`\n"
        f"📦 *حجم اللوت المحسوب:* `{s['lot_size']}`\n"
        f"✅ *الهدف الأول (TP1):* `+${s['tp1_usd']}`\n"
        f"🔥 *الهدف الثاني (TP2):* `+${s['tp2_usd']}`\n"
        f"🚀 *الهدف الثالث (TP3):* `+${s['tp3_usd']}`\n"
        f"🛑 *وقف الخسارة (SL):* `-${s['sl_usd']}`\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"💡 *كيف تتحكم في الأرباح يدوياً؟*\n"
        f"• لتخصيص الأهداف بالدولار أرسل:\n"
        f"`/settp 5 10 20` (أو أي أرقام تريدها لـ TP1 و TP2 و TP3)\n"
        f"• لتخصيص وقف الخسارة بالدولار أرسل:\n"
        f"`/setsl 5`\n"
        f"• لتغيير حجم اللوت أرسل:\n"
        f"`/setlot 0.01` أو `/setlot 0.05`"
    )
    send_msg(txt, chat_id)

def send_admin_panel(chat_id):
    with users_lock:
        app_count = len(users_db["approved"])
        pend_count = len(users_db["pending"])
        ban_count = len(users_db["banned"])
        g_state = "🟢 مفعل للجميع" if users_db["global_enabled"] else "🔴 متوقف عن المستخدمين"
        curr_pass = "مضبوطة ومخزنة كتجزئة" if users_db["password"] else "غير مضبوطة"
        curr_email = users_db["admin_email"] or "غير مضبوط (يتم التأكيد عبر تيليجرام)"

    text = (
        f"🔐 *لوحة تحكم المشرف وإدارة المستخدمين*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🔑 *حالة كلمة سر البوت:* `{curr_pass}`\n"
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
    send_msg("🎉 *تمت موافقة المشرف على طلبك بنجاح!*\nيمكنك الآن استخدام جميع استراتيجيات البوت الخمس من الأزرار بالأسفل:", uid)
    send_msg(f"✅ *تم تفعيل المستخدم:* `{info.get('name')}` (`{uid}`) بنجاح.", ADMIN_CHAT_ID)

def reject_or_delete_user_action(uid):
    uid = str(uid).strip()
    with users_lock:
        info = users_db["approved"].pop(uid, None) or users_db["pending"].pop(uid, {"name": "مستخدم"})
        users_db["banned"].add(uid)
        save_users_to_disk()
    send_msg("🚫 *عذراً، تم إيقاف صلاحية وصولك للبوت من قِبل الإدارة.*", uid, remove_keyboard=True)
    send_msg(f"🗑️ *تم حذف وحظر المستخدم:* `{info.get('name', uid)}` (`{uid}`).", ADMIN_CHAT_ID)

def format_active_trades_report(chat_id=None):
    with trades_lock:
        owner = str(chat_id) if chat_id is not None else None
        snap = {k: dict(v) for k,v in active_trades.items()
                if owner is None or str(v.get("user_id", "default")) == owner}
        if not snap:
            return "📂 *لا توجد صفقات نشطة حالياً.*"
    lines = ["📂 *المناطق المراقبة تلقائياً (أهداف بالدولار TP1 / TP2 / TP3):*\n━━━━━━━━━━━━━━━━━━"]
    for k, t in snap.items():
        st = "⏳ بانتظار الملامسة" if t["status"] == "PENDING" else "🔥 مفعل بالسوق"
        lines.append(
            f"🔹 *{t['asset']}* ({t['tf']}) | {st}\n"
            f"   الدخول: `{t['entry_low']} - {t['entry_high']}` | SL: `{t['sl']}`\n"
            f"   TP1 (+${t['tp1_usd']}): `{t['tp1']}` | TP2 (+${t['tp2_usd']}): `{t['tp2']}` | TP3 (+${t['tp3_usd']}): `{t['tp3']}`"
        )
    return "\n".join(lines)

def apply_profit_profile(profile_key):
    prof = PROFIT_PROFILES.get(profile_key, PROFIT_PROFILES["scalp"])
    user_settings["profit_style"] = profile_key
    user_settings["tp1_usd"] = prof["tp1_usd"]
    user_settings["tp2_usd"] = prof["tp2_usd"]
    user_settings["tp3_usd"] = prof["tp3_usd"]
    user_settings["sl_usd"] = prof["sl_usd"]

def telegram_listener():
    global ADMIN_CHAT_ID
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
                            bind_settings_user(cb_chat)
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
                                        users_db["notified_visitors"].clear()
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

                            if cb_data.startswith("TF|"):
                                if not is_user_authorized(cb_chat):
                                    send_msg("🔒 غير مصرح لك باستخدام البوت.", cb_chat, remove_keyboard=True)
                                    continue
                                _, sym_t, chosen_tf = cb_data.split("|")
                                user_settings["ltf"] = chosen_tf
                                user_settings["htf"] = HTF_MAP.get(chosen_tf, "4h")
                                analyze_and_send_zones(sym_t, chosen_tf, cb_chat)
                                continue

                            if cb_data.startswith("PMODE|"):
                                if not is_user_authorized(cb_chat):
                                    send_msg("🔒 غير مصرح لك باستخدام البوت.", cb_chat, remove_keyboard=True)
                                    continue
                                _, sym_t, p_mode = cb_data.split("|")
                                apply_profit_profile(p_mode)
                                analyze_and_send_zones(sym_t, user_settings["ltf"], cb_chat)
                                continue

                        msg = item.get("message", {})
                        text = msg.get("text", "").strip()
                        if not text:
                            continue
                        chat_obj = msg.get("chat", {})
                        chat_id = str(chat_obj.get("id", "")).strip()
                        bind_settings_user(chat_id)
                        full_name = f"{chat_obj.get('first_name', '')} {chat_obj.get('last_name', '')}".strip() or "مستخدم"
                        username = chat_obj.get("username", "بدون_معرف")

                        if is_admin(chat_id):
                            if text in ["🔐 لوحة تحكم المشرف", "/admin"]:
                                send_admin_panel(chat_id)
                                continue
                            elif text.startswith("/setpass "):
                                new_p = text.replace("/setpass ", "").strip()
                                if new_p:
                                    with users_lock:
                                        users_db["password"] = generate_password_hash(new_p)
                                        save_users_to_disk()
                                    send_msg("🔑 *تم تغيير كلمة سر البوت بنجاح.*", chat_id)
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
                                correct_pass = users_db["password"]

                            if is_banned:
                                send_msg("🚫 *تم إيقاف صلاحية وصولك للبوت من قِبل الإدارة.*", chat_id, remove_keyboard=True)
                                continue
                            if not is_global_on:
                                send_msg("⏸️ *البوت متوقف مؤقتاً للصيانة بأمر من الإدارة.*", chat_id, remove_keyboard=True)
                                continue
                            if not is_approved:
                                if verify_bot_password(text, correct_pass):
                                    with users_lock:
                                        users_db["pending"][chat_id] = {
                                            "name": full_name,
                                            "username": username,
                                            "time": time.strftime("%Y-%m-%d %H:%M", time.gmtime())
                                        }
                                        save_users_to_disk()
                                    send_msg("✅ *كلمة السر صحيحة!* تم إرسال طلب تفعيل حسابك إلى المشرف للموافقة النهائية.", chat_id, remove_keyboard=True)
                                    send_approval_email(chat_id, full_name, username)
                                    admin_kb = {
                                        "inline_keyboard": [[
                                            {"text": "✅ موافقة وتفعيل الحساب", "callback_data": f"ADM|APPROVE|{chat_id}"},
                                            {"text": "🚫 رفض وحظر", "callback_data": f"ADM|DEL|{chat_id}"}
                                        ]]
                                    }
                                    send_msg(
                                        f"🚨 *طلب دخول مؤكد:*\n👤 *الاسم:* `{full_name}` (`@{username}`)\n🆔 *المعرف:* `{chat_id}`",
                                        ADMIN_CHAT_ID,
                                        admin_kb
                                    )
                                else:
                                    with users_lock:
                                        users_db["pending"][chat_id] = {
                                            "name": full_name,
                                            "username": username,
                                            "time": time.strftime("%Y-%m-%d %H:%M", time.gmtime())
                                        }
                                        first_visit = chat_id not in users_db["notified_visitors"]
                                        users_db["notified_visitors"].add(chat_id)
                                        save_users_to_disk()

                                    if first_visit:
                                        admin_kb = {
                                            "inline_keyboard": [[
                                                {"text": "✅ تفعيل فوري", "callback_data": f"ADM|APPROVE|{chat_id}"},
                                                {"text": "🚫 رفض وحظر", "callback_data": f"ADM|DEL|{chat_id}"}
                                            ]]
                                        }
                                        send_msg(
                                            f"👀 *شخص جديد فتح البوت:*\n👤 *الاسم:* `{full_name}` (`@{username}`)\n🆔 *المعرف:* `{chat_id}`",
                                            ADMIN_CHAT_ID,
                                            admin_kb
                                        )
                                    send_msg("🔒 *هذا البوت خاص ومحمي بكلمة سر.*\n🔑 يرجى إرسال **كلمة السر الصحيحة**:", chat_id, remove_keyboard=True)
                                continue

                        # أوامر التحكم في الأرباح واللوت للمستخدمين المصرح لهم
                        if text.startswith("/settp "):
                            try:
                                nums = [float(x) for x in text.replace("/settp ", "").split() if x.strip()]
                                if len(nums) == 3 and all(math.isfinite(x) and 0 < x <= 100000 for x in nums) and nums[0] < nums[1] < nums[2]:
                                    user_settings["tp1_usd"] = round(nums[0], 2)
                                    user_settings["tp2_usd"] = round(nums[1], 2)
                                    user_settings["tp3_usd"] = round(nums[2], 2)
                                    user_settings["profit_style"] = "custom"
                                    send_msg(
                                        f"✅ *تم تحديث الأهداف بالدولار بنجاح:*\n"
                                        f"• TP1: `+${user_settings['tp1_usd']}`\n"
                                        f"• TP2: `+${user_settings['tp2_usd']}`\n"
                                        f"• TP3: `+${user_settings['tp3_usd']}`",
                                        chat_id
                                    )
                                else:
                                    send_msg("⚠️ يرجى إرسال 3 أرقام للأهداف، مثال:\n`/settp 5 10 20`", chat_id)
                            except Exception:
                                send_msg("⚠️ صيغة غير صحيحة، أرسل مثل: `/settp 5 10 20`", chat_id)
                            continue

                        elif text.startswith("/setsl "):
                            try:
                                sl_val = float(text.replace("/setsl ", "").strip())
                                if math.isfinite(sl_val) and 0 < sl_val <= 100000:
                                    user_settings["sl_usd"] = round(sl_val, 2)
                                    send_msg(f"🛑 *تم ضبط وقف الخسارة على:* `-${user_settings['sl_usd']}`", chat_id)
                            except Exception:
                                send_msg("⚠️ أرسل رقماً صحيحاً، مثال: `/setsl 5`", chat_id)
                            continue

                        elif text.startswith("/setlot "):
                            try:
                                lot_val = float(text.replace("/setlot ", "").strip())
                                if math.isfinite(lot_val) and 0.001 <= lot_val <= 1000:
                                    user_settings["lot_size"] = round(lot_val, 3)
                                    send_msg(f"📦 *تم تغيير حجم اللوت إلى:* `{user_settings['lot_size']}`\nسيتم حساب المسافة السعرية للأهداف بناءً على هذا اللوت.", chat_id)
                            except Exception:
                                send_msg("⚠️ أرسل رقماً صحيحاً، مثال: `/setlot 0.01`", chat_id)
                            continue

                        if text in ["/start", "/help"]:
                            send_msg(
                                "🤖 *أهلاً بك في نظام القناص (TP1 = $5 | TP2 = $10 | TP3 = $20)!*\n"
                                "يمكنك التبديل في أي وقت بين ربح السكالب السريع وربح السوينق البعيد من الأزرار بالأسفل.",
                                chat_id
                            )
                        elif text in ["⚙️ إعدادات الربح واللوت", "/settings"]:
                            send_profit_settings_guide(chat_id)
                        elif text in ["🎯 ربح سكالب ($5/$10/$20)", "/scalp_profit"]:
                            apply_profit_profile("scalp")
                            send_msg(
                                "🎯 *تم تفعيل نمط ربح السكالبينج السريع:*\n"
                                "• TP1 = `+$5` | TP2 = `+$10` | TP3 = `+$20` | SL = `-$5`",
                                chat_id
                            )
                        elif text in ["🌊 ربح سوينق بعيد ($15/$30/$60)", "/swing_profit"]:
                            apply_profit_profile("swing")
                            send_msg(
                                "🌊 *تم تفعيل نمط ربح السوينق البعيد:*\n"
                                "• TP1 = `+$15` | TP2 = `+$30` | TP3 = `+$60` | SL = `-$12`",
                                chat_id
                            )
                        elif text in ["🔊 تجربة الناطق العربي", "/sound"]:
                            test_speech = (
                                f"أهلاً بك، تم ضبط الأهداف حالياً على {int(user_settings['tp1_usd'])} دولار للهدف الأول، "
                                f"و {int(user_settings['tp2_usd'])} دولار للهدف الثاني، و {int(user_settings['tp3_usd'])} دولار للهدف الثالث."
                            )
                            send_arabic_voice_alert(test_speech, "🗣️ *تجربة الناطق الصوتي باللغة العربية*", chat_id)
                        elif text in ["⚡ فريم سكالب (5m)", "/scalp"]:
                            user_settings.update({"mode_name": TIMEFRAME_MODES["scalp"]["name"], "ltf": "5m", "htf": "15m"})
                            apply_profit_profile("scalp")
                            send_msg("⚡ تم تفعيل فريم 5 دقائق مع أهداف سكالب ($5 / $10 / $20).", chat_id)
                        elif text in ["⏱ فريم متوسط (15m)", "/intraday"]:
                            user_settings.update({"mode_name": TIMEFRAME_MODES["intraday"]["name"], "ltf": "15m", "htf": "4h"})
                            send_msg("⏱ تم تفعيل الفريم المتوسط 15 دقيقة.", chat_id)
                        elif text in ["🌊 فريم طويل (4h/1d)", "/swing"]:
                            user_settings.update({"mode_name": TIMEFRAME_MODES["swing"]["name"], "ltf": "4h", "htf": "1d"})
                            apply_profit_profile("swing")
                            send_msg("🌊 تم تفعيل الفريم الطويل 4 ساعات مع أهداف سوينق بعيدة ($15 / $30 / $60).", chat_id)
                        elif text in ["/gold", "🪙 مناطق دخول الذهب"]:
                            analyze_and_send_zones("GOLD", user_settings["ltf"], chat_id)
                        elif text in ["/btc", "₿ مناطق دخول البيتكوين"]:
                            analyze_and_send_zones("BTC", user_settings["ltf"], chat_id)
                        elif text in ["/status", "/all", "📊 فحص شامل بالسوق"]:
                            analyze_and_send_zones("GOLD", user_settings["ltf"], chat_id)
                            analyze_and_send_zones("BTC", user_settings["ltf"], chat_id)
                        elif text in ["/trades", "📂 الأوامر المعلقة والنشطة"]:
                            send_msg(format_active_trades_report(chat_id), chat_id)
                        elif text in ["/clear", "🗑️ مسح الصفقات"] and is_admin(chat_id):
                            with trades_lock:
                                active_trades.clear()
                                persist_active_trades()
                            send_msg("🗑️ تم مسح الصفقات.", chat_id)
        except Exception as e:
            print(f"Listener error: {e}")
        time.sleep(2)

@app.route("/")
def home():
    return "Persistent Users + Dynamic USD Targets ($5/$10/$20) Bot is Live 24/7!"

@app.route("/health")
def health():
    return {"status": "ok"}, 200

@app.route("/admin/action")
def admin_email_action():
    uid = request.args.get("uid", "").strip()
    act = request.args.get("act", "").strip()
    tok = request.args.get("token", "").strip()
    if not uid or act not in ["approve", "reject"] or not admin_token_is_valid(uid, act, tok):
        return "<h3>❌ الرابط غير صالح أو منتهي الصلاحية أو سبق استخدامه</h3>", 403
    label = "الموافقة وتفعيل المستخدم" if act == "approve" else "رفض المستخدم وحظره"
    return f"""<!doctype html><html lang="ar" dir="rtl"><meta charset="utf-8">
    <title>تأكيد إجراء الإدارة</title><body style="font-family:Arial,sans-serif;margin:3rem">
    <h2>تأكيد الإجراء</h2><p>هل تريد تنفيذ: {label}؟</p>
    <form method="post" action="/admin/action">
      <input type="hidden" name="uid" value="{html.escape(uid, quote=True)}">
      <input type="hidden" name="act" value="{html.escape(act, quote=True)}">
      <input type="hidden" name="token" value="{html.escape(tok, quote=True)}">
      <button type="submit">تأكيد</button>
    </form></body></html>"""

@app.route("/admin/action", methods=["POST"])
def admin_email_action_confirm():
    uid = request.form.get("uid", "").strip()
    act = request.form.get("act", "").strip()
    tok = request.form.get("token", "").strip()
    if not uid or act not in ["approve", "reject"] or not consume_admin_token(uid, act, tok):
        return "<h3>❌ الرابط غير صالح أو منتهي الصلاحية أو سبق استخدامه</h3>", 403
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
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is required; refusing to start without Telegram credentials")
    if not ADMIN_CHAT_ID:
        raise RuntimeError("TELEGRAM_CHAT_ID is required; refusing to start without an administrator")
    if not BOT_PASSWORD:
        raise RuntimeError("BOT_PASSWORD must be configured explicitly; insecure default passwords are disabled")
    threading.Thread(target=telegram_listener, daemon=True).start()
    threading.Thread(target=auto_zone_scanner_loop, daemon=True).start()
    threading.Thread(target=trade_tracker_loop, daemon=True).start()
    threading.Thread(target=keep_alive_pinger, daemon=True).start()
    send_msg(
        "🚀 *تم تشغيل البوت بنظام الأهداف الثلاثة بالدولار ($5 / $10 / $20) والتحكم بين السكالب والسوينق!*\n"
        "• يمكنك التبديل الفوري من الأزرار أو إرسال `/settp 5 10 20` و `/setlot 0.01`."
    )
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
