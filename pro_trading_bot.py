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
from flask import Flask, request, jsonify

# ==================== 1. الإعدادات الأساسية وإعدادات الحماية ====================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
ADMIN_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL", "").strip()

BOT_PASSWORD = os.getenv("BOT_PASSWORD", "Gold2026").strip()
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "").strip()
SMTP_USER = os.getenv("SMTP_USER", "").strip()
SMTP_APP_PASS = os.getenv("SMTP_APP_PASS", "").strip()

# مفتاح سري لحماية الربط مع ميتاتريدر 5
MT5_SECRET_KEY = os.getenv("MT5_SECRET_KEY", "AymanTrade2026").strip()

TRACKER_INTERVAL = 15
active_trades = {}
trades_lock = threading.Lock()

dxy_cache = {"timestamp": 0, "bias": "NEUTRAL", "text": "مستقر"}
spot_gold_cache = {"timestamp": 0, "price": None}

# طابور الأوامر المؤقتة المرسلة من تيليجرام لتنفيذها في MT5
pending_mt5_orders = []
mt5_lock = threading.Lock()

# تخزين السعر الحي القادم من منصة MT5 مباشرة لمطابقة السعر 100%
mt5_live_prices = {}

# ==================== نظام الحفظ الدائم وعزل إعدادات كل مستخدم ====================
USERS_FILE = "users_store.json"
users_lock = threading.RLock()

PROFIT_PROFILES = {
    "scalp": {"label": "سكالبينج سريع ($5 / $10 / $20) ⚡", "tp1_usd": 5.0, "tp2_usd": 10.0, "tp3_usd": 20.0, "sl_usd": 5.0},
    "swing": {"label": "سوينق بعيد ($15 / $30 / $60) 🌊", "tp1_usd": 15.0, "tp2_usd": 30.0, "tp3_usd": 60.0, "sl_usd": 12.0}
}

TIMEFRAME_MODES = {
    "scalp": {"name": "سكالبينج سريع (5m) ⚡", "ltf": "5m", "htf": "15m"},
    "intraday": {"name": "مضاربة يومية (15m) ⏱", "ltf": "15m", "htf": "4h"},
    "swing": {"name": "سوينق بعيد (4h) 🌊", "ltf": "4h", "htf": "1d"}
}

HTF_MAP = {"1m": "15m", "5m": "15m", "15m": "4h", "1h": "4h", "4h": "1d", "1d": "1d"}

def get_default_user_prefs():
    return {
        "mode_name": "مضاربة متوسطة (15m)", "ltf": "15m", "htf": "4h",
        "profit_style": "scalp", "lot_size": 0.01,
        "tp1_usd": 5.0, "tp2_usd": 10.0, "tp3_usd": 20.0, "sl_usd": 5.0, "gold_offset": 0.0
    }

def load_users_from_disk():
    default_db = {
        "approved": {}, "pending": {}, "banned": set(), "user_prefs": {},
        "global_enabled": True, "password": BOT_PASSWORD, "admin_email": ADMIN_EMAIL
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
        except Exception:
            pass
    return default_db

def save_users_to_disk():
    with users_lock:
        try:
            data = {
                "approved": users_db["approved"], "pending": users_db["pending"],
                "banned": list(users_db["banned"]), "user_prefs": users_db["user_prefs"],
                "global_enabled": users_db["global_enabled"], "password": users_db["password"],
                "admin_email": users_db["admin_email"]
            }
            with open(USERS_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=4)
        except Exception:
            pass

users_db = load_users_from_disk()

def get_user_settings(chat_id):
    cid = str(chat_id).strip()
    with users_lock:
        if cid not in users_db["user_prefs"]:
            users_db["user_prefs"][cid] = get_default_user_prefs()
            save_users_to_disk()
        prefs = users_db["user_prefs"][cid]
        if "gold_offset" not in prefs: prefs["gold_offset"] = 0.0
        return prefs

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

USER_KEYBOARD = {
    "keyboard": [
        [{"text": "🪙 تحليل الذهب"}, {"text": "₿ تحليل البيتكوين"}],
        [{"text": "📂 الصفقات النشطة"}, {"text": "⚙️ الإعدادات والتحكم"}]
    ],
    "resize_keyboard": True, "is_persistent": True
}

ADMIN_KEYBOARD = {
    "keyboard": [
        [{"text": "🪙 تحليل الذهب"}, {"text": "₿ تحليل البيتكوين"}],
        [{"text": "📂 الصفقات النشطة"}, {"text": "⚙️ الإعدادات والتحكم"}, {"text": "🔐 المشرف"}]
    ],
    "resize_keyboard": True, "is_persistent": True
}

def is_admin(chat_id):
    return str(chat_id).strip() == str(ADMIN_CHAT_ID).strip()

def is_user_authorized(chat_id):
    cid = str(chat_id).strip()
    if is_admin(cid): return True
    with users_lock:
        if not users_db["global_enabled"] or cid in users_db["banned"]: return False
        return cid in users_db["approved"]

def get_action_token(chat_id, action):
    secret = f"{TELEGRAM_BOT_TOKEN}_{chat_id}_{action}"
    return hashlib.sha256(secret.encode()).hexdigest()[:16]

def get_inline_tf_buttons(sym_type):
    return {
        "inline_keyboard": [
            [
                {"text": "🟢 شراء فوري MT5", "callback_data": f"MT5|{sym_type}|BUY"},
                {"text": "🔴 بيع فوري MT5", "callback_data": f"MT5|{sym_type}|SELL"}
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
    if not TELEGRAM_BOT_TOKEN or not target: return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    markup = inline_markup if inline_markup else (ADMIN_KEYBOARD if is_admin(target) else USER_KEYBOARD)
    if remove_keyboard: markup = {"remove_keyboard": True}
    try:
        res = requests.post(url, json={"chat_id": target, "text": text, "parse_mode": "Markdown", "reply_markup": markup}, timeout=12)
        if not res.ok:
            requests.post(url, json={"chat_id": target, "text": text.replace("*","").replace("`",""), "reply_markup": markup}, timeout=12)
    except Exception as e:
        print(f"Telegram error: {e}")

def send_photo_msg(photo_buf, caption, sym_type, chat_id):
    target = str(chat_id).strip()
    if not TELEGRAM_BOT_TOKEN or not target: return
    inline_kb = get_inline_tf_buttons(sym_type)
    if photo_buf is None:
        send_msg(caption, target, inline_kb)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
    try:
        requests.post(url, data={"chat_id": target, "caption": caption, "parse_mode": "Markdown", "reply_markup": json.dumps(inline_kb)}, files={"photo": ("chart.png", photo_buf, "image/png")}, timeout=25)
    except Exception:
        send_msg(caption, target, inline_kb)

def get_raw_spot_gold_price():
    # إذا كانت منصة MT5 متصلة وترسل السعر الحي، نأخذ سعر MT5 كمرجع أساسي أول
    if "XAUUSD" in mt5_live_prices:
        return mt5_live_prices["XAUUSD"]
    now = time.time()
    if spot_gold_cache["price"] is not None and (now - spot_gold_cache["timestamp"] < 8):
        return spot_gold_cache["price"]
    try:
        r = requests.get("https://api.gold-api.com/price/XAU", timeout=5)
        if r.ok:
            p = float(r.json().get("price", 0))
            if p > 1000:
                spot_gold_cache.update({"timestamp": now, "price": round(p, 2)})
                return p
    except Exception:
        pass
    return spot_gold_cache["price"]

def get_gold_ohlcv(timeframe, user_offset=0.0):
    df = pd.DataFrame()
    for sym in ["GC=F", "XAUUSD=X"]:
        try:
            ticker = yf.Ticker(sym)
            if timeframe == "1d": df = ticker.history(period="1y", interval="1d")
            elif timeframe == "4h":
                raw = ticker.history(period="60d", interval="1h")
                if not raw.empty:
                    raw.index = pd.to_datetime(raw.index)
                    df = raw.resample("4h").agg({"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}).dropna().reset_index()
                    df.columns = ["timestamp", "open", "high", "low", "close", "volume"]
            elif timeframe == "1h": df = ticker.history(period="30d", interval="1h")
            elif timeframe == "15m": df = ticker.history(period="15d", interval="15m")
            elif timeframe == "5m": df = ticker.history(period="5d", interval="5m")
            if not df.empty and len(df) > 35: break
        except Exception:
            continue
    if df.empty: return df

    # تنظيف الأعمدة
    if isinstance(df.columns, pd.MultiIndex): df.columns = df.columns.get_level_values(0)
    df = df.reset_index()
    col_map = {}
    for c in df.columns:
        cl = str(c).lower()
        if cl in ["datetime", "date", "index"]: col_map[c] = "timestamp"
        elif cl == "open": col_map[c] = "open"
        elif cl == "high": col_map[c] = "high"
        elif cl == "low": col_map[c] = "low"
        elif cl == "close": col_map[c] = "close"
        elif cl == "volume": col_map[c] = "volume"
    df = df.rename(columns=col_map)
    needed = ["timestamp", "open", "high", "low", "close", "volume"]
    for col in needed:
        if col not in df.columns: df[col] = 0.0
    df = df[needed].dropna(subset=["open", "high", "low", "close"]).reset_index(drop=True)

    spot_now = get_raw_spot_gold_price()
    last_close = float(df["close"].iloc[-1])
    shift = 0.0
    if spot_now and spot_now > 1000:
        shift = spot_now - last_close
    shift += float(user_offset)

    if abs(shift) > 0.001:
        for col in ["open", "high", "low", "close"]:
            df[col] = (pd.to_numeric(df[col]) + shift).round(2)
    return df

def get_btc_ohlcv(timeframe, limit=220):
    for ex_name in ["binance", "bybit", "kraken"]:
        try:
            ex = getattr(ccxt, ex_name)({"enableRateLimit": True, "timeout": 10000})
            bars = ex.fetch_ohlcv("BTC/USDT", timeframe=timeframe, limit=limit)
            if bars and len(bars) > 35:
                df = pd.DataFrame(bars, columns=["timestamp", "open", "high", "low", "close", "volume"])
                df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
                return df
        except Exception:
            continue
    return pd.DataFrame()

def get_dxy_correlation_bias():
    now = time.time()
    if now - dxy_cache["timestamp"] < 600: return dxy_cache["bias"], dxy_cache["text"]
    try:
        t = yf.Ticker("DX-Y.NYB")
        hist = t.history(period="10d", interval="15m")
        if not hist.empty:
            closes = hist["Close"].dropna()
            ema20 = closes.ewm(span=20).mean().iloc[-1]
            ema50 = closes.ewm(span=50).mean().iloc[-1]
            curr = closes.iloc[-1]
            if curr < ema20: dxy_cache.update({"timestamp": now, "bias": "WEAK_USD", "text": "ضعف الدولار DXY (يدعم الشراء)"})
            else: dxy_cache.update({"timestamp": now, "bias": "STRONG_USD", "text": "قوة الدولار DXY (يدعم البيع)"})
            return dxy_cache["bias"], dxy_cache["text"]
    except Exception:
        pass
    return "NEUTRAL", "استقرار مؤشر الدولار"

def calc_parabolic_sar(df, af_start=0.02, af_step=0.02, af_max=0.20):
    high, low = df["high"].values, df["low"].values
    n = len(df)
    psar = [0.0] * n
    bull = True
    af = af_start
    ep = high[0]
    psar[0] = low[0]
    for i in range(1, n):
        prev = psar[i - 1]
        if bull:
            sar = prev + af * (ep - prev)
            sar = min(sar, low[i - 1], low[max(0, i - 2)])
            if low[i] < sar: bull, sar, ep, af = False, ep, low[i], af_start
            else:
                if high[i] > ep: ep, af = high[i], min(af + af_step, af_max)
        else:
            sar = prev + af * (ep - prev)
            sar = max(sar, high[i - 1], high[max(0, i - 2)])
            if high[i] > sar: bull, sar, ep, af = True, ep, high[i], af_start
            else:
                if low[i] < ep: ep, af = low[i], min(af + af_step, af_max)
        psar[i] = sar
    return pd.Series(psar, index=df.index)

def calc_supertrend(df, multiplier=3.0):
    high, low, close, atr = df["high"].values, df["low"].values, df["close"].values, df["ATR"].values
    n = len(df)
    st, direction = [0.0] * n, [1] * n
    hl2 = (high + low) / 2.0
    u_b, l_b = hl2 + multiplier * atr, hl2 - multiplier * atr
    fu, fl = list(u_b), list(l_b)
    st[0] = fl[0]
    for i in range(1, n):
        fu[i] = u_b[i] if u_b[i] < fu[i - 1] or close[i - 1] > fu[i - 1] else fu[i - 1]
        fl[i] = l_b[i] if l_b[i] > fl[i - 1] or close[i - 1] < fl[i - 1] else fl[i - 1]
        if direction[i - 1] == 1:
            if close[i] < fl[i]: direction[i], st[i] = -1, fu[i]
            else: direction[i], st[i] = 1, fl[i]
        else:
            if close[i] > fu[i]: direction[i], st[i] = 1, fl[i]
            else: direction[i], st[i] = -1, fu[i]
    return pd.Series(st, index=df.index), pd.Series(direction, index=df.index)

def calculate_volume_profile(df, num_bins=24):
    recent = df.tail(55).copy()
    min_p, max_p = float(recent["low"].min()), float(recent["high"].max())
    if max_p <= min_p: max_p = min_p + 1.0
    bin_size = (max_p - min_p) / num_bins
    bin_centers = [min_p + (i + 0.5) * bin_size for i in range(num_bins)]
    bin_volumes = [0.0] * num_bins
    for _, row in recent.iterrows():
        h, l, v = float(row["high"]), float(row["low"]), float(row["volume"])
        weight = v if v > 0 else max(h - l, 1.0)
        for b in range(num_bins):
            b_low, b_high = min_p + b * bin_size, min_p + (b + 1) * bin_size
            overlap = max(0.0, min(h, b_high) - max(l, b_low))
            if overlap > 0: bin_volumes[b] += weight * (overlap / max(h - l, 1e-6))
    max_idx = int(max(range(num_bins), key=lambda i: bin_volumes[i]))
    return {"poc": round(bin_centers[max_idx], 2), "bin_centers": bin_centers, "bin_volumes": bin_volumes, "bin_height": bin_size * 0.85, "poc_idx": max_idx}

def enrich_indicators(df):
    df = df.copy()
    for col in ["open", "high", "low", "close", "volume"]: df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["open", "high", "low", "close"]).reset_index(drop=True)
    df["EMA_20"] = df["close"].ewm(span=20, adjust=False).mean()
    df["EMA_50"] = df["close"].ewm(span=50, adjust=False).mean()
    df["EMA_200"] = df["close"].ewm(span=200, adjust=False).mean()
    ema12, ema26 = df["close"].ewm(span=12, adjust=False).mean(), df["close"].ewm(span=26, adjust=False).mean()
    df["MACD"], df["MACD_SIGNAL"] = ema12 - ema26, (ema12 - ema26).ewm(span=9, adjust=False).mean()
    delta = df["close"].diff()
    gain, loss = delta.clip(lower=0).ewm(alpha=1/14, adjust=False).mean(), (-delta.clip(upper=0)).ewm(alpha=1/14, adjust=False).mean()
    df["RSI"] = 100 - (100 / (1 + (gain / loss.replace(0, 1e-9))))
    r_min, r_max = df["RSI"].rolling(14, min_periods=1).min(), df["RSI"].rolling(14, min_periods=1).max()
    df["STOCH_K"] = (100 * ((df["RSI"] - r_min) / (r_max - r_min).replace(0, 1e-9))).rolling(3, min_periods=1).mean()
    h_l, h_c, l_c = df["high"] - df["low"], (df["high"] - df["close"].shift(1)).abs(), (df["low"] - df["close"].shift(1)).abs()
    df["ATR"] = pd.concat([h_l, h_c, l_c], axis=1).max(axis=1).ewm(alpha=1/14, adjust=False).mean().bfill()
    df["PSAR"] = calc_parabolic_sar(df)
    df["SUPERTREND"], df["ST_DIR"] = calc_supertrend(df, multiplier=2.8)
    tp = (df["high"] + df["low"] + df["close"]) / 3.0
    vol_safe = df["volume"].replace(0, 1.0)
    df["VWAP"] = (tp * vol_safe).rolling(30, min_periods=1).sum() / vol_safe.rolling(30, min_periods=1).sum()
    df["CVD"] = (((df["close"] - df["low"]) - (df["high"] - df["close"])) / (df["high"] - df["low"]).replace(0, 1e-9) * df["volume"].replace(0, 1.0)).cumsum()
    return df

def evaluate_10_strategies_clean(ltf, htf, div_info, vp_data):
    curr, h_curr = ltf.iloc[-1], htf.iloc[-1]
    price = float(curr["close"])
    poc = vp_data["poc"]
    s1 = float(h_curr["close"]) > float(h_curr["EMA_200"]) or price > float(curr["EMA_50"])
    s5 = price >= poc
    s6 = int(curr["ST_DIR"]) == 1 or float(curr["STOCH_K"]) < 30
    s9, _ = get_dxy_correlation_bias()
    bull_score = (1 if s1 else 0) + (1 if s5 else 0) + (1 if s6 else 0) + (1 if s9 == "WEAK_USD" else 0) + (1 if div_info["found"] else 0)
    is_long = bull_score >= 3
    conf = min(60 + (bull_score * 7), 98)
    return {"is_long": is_long, "confidence": conf, "stars": "⭐⭐⭐⭐" if conf >= 80 else "⭐⭐⭐", "summary_reasons": "ارتكاز POC + مؤشر Supertrend"}

def detect_rsi_macd_divergence(plot_df):
    if len(plot_df) < 15: return {"found": False}
    p_low = float(plot_df["low"].iloc[-1]) < float(plot_df["low"].iloc[-5])
    r_low = float(plot_df["RSI"].iloc[-1]) > float(plot_df["RSI"].iloc[-5])
    return {"found": (p_low and r_low), "text": "دايفرجنس شرائي"}

def analyze_structure_and_ote(df, is_long_bias, sym_type, vp_data, u_prefs):
    curr_price = float(df["close"].iloc[-1])
    atr = max(float(df["ATR"].iloc[-1]), curr_price * 0.0008)
    ideal = 1.45 if sym_type == "GOLD" else 0.32 * atr
    mid = curr_price - ideal if is_long_bias else curr_price + ideal
    hb = 0.40 if sym_type == "GOLD" else 0.10 * atr
    return {"sniper_mid": round(mid, 2), "sniper_zone": (round(mid - hb, 2), round(mid + hb, 2)), "anchor_reason": "Micro OTE", "dist_from_curr": round(abs(curr_price - mid), 2)}

def generate_clean_chart_image(df, zones, vp_data, strat_res, asset_title, entry_zone, sl, exit_targets, usd_targets, is_market=False):
    try:
        plot_df = df.tail(45).reset_index(drop=True)
        fig, ax = plt.subplots(figsize=(10.2, 5.8), dpi=120)
        fig.patch.set_facecolor("#131722")
        ax.set_facecolor("#131722")
        for idx, row in plot_df.iterrows():
            c_col = "#26a69a" if float(row["close"]) >= float(row["open"]) else "#ef5350"
            ax.plot([idx, idx], [float(row["low"]), float(row["high"])], color=c_col, linewidth=1.2)
            ax.bar(idx, max(abs(float(row["close"]) - float(row["open"])), 0.01), bottom=min(float(row["open"]), float(row["close"])), width=0.6, color=c_col)
        ax.axhline(vp_data["poc"], color="#00e5ff", linestyle=":", linewidth=1.2, alpha=0.75)
        e_mid = zones["sniper_mid"]
        tp1, tp2, tp3 = exit_targets
        u1, u2, u3, u_sl = usd_targets
        ax.axhline(e_mid, color="#ffd700", linestyle="--", linewidth=1.5)
        ax.axhline(sl, color="#ff1744", linewidth=1.4)
        ax.axhline(tp1, color="#26a69a", linestyle="--", linewidth=1.2)
        ax.axhline(tp2, color="#00e676", linestyle="--", linewidth=1.2)
        ax.axhline(tp3, color="#69f0ae", linewidth=1.5)
        ax.set_title(f"{asset_title} | Score: {strat_res['confidence']}%", color="#ffffff", fontsize=10)
        plt.tight_layout()
        buf = io.BytesIO()
        plt.savefig(buf, format="png", facecolor=fig.get_facecolor(), edgecolor="none")
        plt.close(fig)
        buf.seek(0)
        return buf
    except Exception:
        plt.close("all")
        return None

def analyze_and_send_zones(sym_type, ltf_str, chat_id):
    cid = str(chat_id).strip()
    u_prefs = get_user_settings(cid)
    ltf_use = ltf_str or u_prefs["ltf"]
    user_offset = float(u_prefs.get("gold_offset", 0.0))

    if sym_type == "GOLD":
        asset_name, chart_code = "الذهب الفوري XAU/USD", "XAU/USD"
        ltf_df = get_gold_ohlcv(ltf_use, user_offset)
    else:
        asset_name, chart_code = "البيتكوين BTC/USDT", "BTC/USDT"
        ltf_df = get_btc_ohlcv(ltf_use)

    if ltf_df.empty or len(ltf_df) < 35:
        send_msg(f"⚠️ بيانات {asset_name} غير متوفرة حالياً.", cid)
        return

    ltf = enrich_indicators(ltf_df)
    div_info = detect_rsi_macd_divergence(ltf.tail(55))
    vp_data = calculate_volume_profile(ltf)
    strat_res = evaluate_10_strategies_clean(ltf, ltf, div_info, [], vp_data)
    is_long = strat_res["is_long"]

    zones = analyze_structure_and_ote(ltf, is_long, sym_type, vp_data, u_prefs)
    entry_zone = zones["sniper_zone"]
    entry_mid = zones["sniper_mid"]
    curr_price = round(float(ltf["close"].iloc[-1]), 2)

    lot = u_prefs["lot_size"]
    u_tp1, u_tp2, u_tp3, u_sl = u_prefs["tp1_usd"], u_prefs["tp2_usd"], u_prefs["tp3_usd"], u_prefs["sl_usd"]
    dist_tp1, dist_tp2, dist_tp3, dist_sl = usd_to_price_distance(sym_type, u_tp1, lot), usd_to_price_distance(sym_type, u_tp2, lot), usd_to_price_distance(sym_type, u_tp3, lot), usd_to_price_distance(sym_type, u_sl, lot)

    if is_long:
        dir_label = "🟢 شراء قريب (BUY LIMIT)"
        sl, tp1, tp2, tp3 = round(entry_mid - dist_sl, 2), round(entry_mid + dist_tp1, 2), round(entry_mid + dist_tp2, 2), round(entry_mid + dist_tp3, 2)
    else:
        dir_label = "🔴 بيع قريب (SELL LIMIT)"
        sl, round(entry_mid + dist_sl, 2), round(entry_mid - dist_tp1, 2), round(entry_mid - dist_tp2, 2), round(entry_mid - dist_tp3, 2)
        # للتأكد من صياغة البيع بدقة
        tp1, tp2, tp3 = round(entry_mid - dist_tp1, 2), round(entry_mid - dist_tp2, 2), round(entry_mid - dist_tp3, 2)

    caption = (
        f"🎯 *بطاقة الصفقة الذكية — {asset_name}:*\n"
        f"📌 *القرار:* *{dir_label}*\n"
        f"💵 *السعر الفوري الحقيقي:* `{curr_price}`\n"
        f"📥 *الدخول المقترح:* `{entry_mid}` | 🛑 *SL:* `{sl}`\n"
        f"✅ *TP1:* `{tp1}` | 🔥 *TP2:* `{tp2}` | 🚀 *TP3:* `{tp3}`\n"
        f"👇 *اضغط تحت الشارت لتنفيذ الصفقة فوراً في منصة MT5 عندك:*"
    )
    chart_buf = generate_clean_chart_image(ltf, zones, vp_data, strat_res, chart_code, entry_zone, sl, (tp1, tp2, tp3), (u_tp1, u_tp2, u_tp3, u_sl))
    send_photo_msg(chart_buf, caption, sym_type, cid)

def execute_instant_market_trade(sym_type, mkt_dir, chat_id):
    cid = str(chat_id).strip()
    u_prefs = get_user_settings(cid)
    user_offset = float(u_prefs.get("gold_offset", 0.0))

    if sym_type == "GOLD":
        ltf_df = get_gold_ohlcv("15m", user_offset)
        asset_full = "XAUUSD"
    else:
        ltf_df = get_btc_ohlcv("15m")
        asset_full = "BTCUSDT"

    if ltf_df.empty:
        send_msg("⚠️ تعذر جلب السعر اللحظي.", cid)
        return

    curr_price = round(float(ltf_df["close"].iloc[-1]), 2)
    lot = u_prefs["lot_size"]
    u_tp1, u_tp2, u_tp3, u_sl = u_prefs["tp1_usd"], u_prefs["tp2_usd"], u_prefs["tp3_usd"], u_prefs["sl_usd"]
    is_long = (mkt_dir == "BUY")

    dist_tp1 = usd_to_price_distance(sym_type, u_tp1, lot)
    dist_tp3 = usd_to_price_distance(sym_type, u_tp3, lot)
    dist_sl = usd_to_price_distance(sym_type, u_sl, lot)

    if is_long:
        sl = round(curr_price - dist_sl, 2)
        tp3 = round(curr_price + dist_tp3, 2)
    else:
        sl = round(curr_price + dist_sl, 2)
        tp3 = round(curr_price - dist_tp3, 2)

    # وضع الأمر في طابور الإرسال لمنصة MT5 الخاصة بالمهندس أيمن
    with mt5_lock:
        pending_mt5_orders.append({
            "chat_id": cid,
            "symbol": asset_full,
            "action": mkt_dir,
            "lot": lot,
            "price": curr_price,
            "sl": sl,
            "tp": tp3
        })

    send_msg(f"📡 *تم إرسال أمر الـ {mkt_dir} إلى منصة MetaTrader 5 بنجاح!* سيتم التنفيذ فوراً.", cid)

# ==================== مسارات الاتصال مع منصة MT5 (Webhook Bridge) ====================
@app.route("/mt5/bridge", methods=["POST"])
def mt5_bridge():
    """
    نقطة الاتصال التي يستقبل منها سيرفر Render طلبات ميتاتريدر 5 (سحب الأوامر وإرسال السعر الحي)
    """
    data = request.json
    if not data or data.get("secret") != MT5_SECRET_KEY:
        return jsonify({"status": "error", "message": "Unauthorized"}), 401

    # 1. استقبال السعر الحي من MT5 لتحديث الذهب بدقة 100%
    if "live_price" in data:
        sym = data["symbol"]
        price = float(data["live_price"])
        mt5_live_prices[sym] = price

    # 2. إرجاع الأوامر المعلقة للمتداول أيمن لتنفيذها في MT5
    with mt5_lock:
        orders_to_send = list(pending_mt5_orders)
        pending_mt5_orders.clear()

    return jsonify({"status": "ok", "orders": orders_to_send})

@app.route("/")
def home():
    return "MT5 Bridge + Spot Gold Bot is Live!"

@app.route("/health")
def health():
    return {"status": "ok"}, 200

if __name__ == "__main__":
    threading.Thread(target=lambda: app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000))), daemon=True).start()
    send_msg("🚀 *تم تفعيل جسر الاتصال مع MetaTrader 5 بنجاح يا بشمهندس أيمن!*", ADMIN_CHAT_ID)
    while True: time.sleep(60)
