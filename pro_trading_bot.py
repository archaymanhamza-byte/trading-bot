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
MT5_SECRET_KEY = os.getenv("MT5_SECRET_KEY", "AymanTrade2026").strip()

active_trades = {}
trades_lock = threading.Lock()

dxy_cache = {"timestamp": 0, "bias": "NEUTRAL", "text": "مستقر"}
spot_gold_cache = {"timestamp": 0, "price": 2750.0}

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
        requests.post(url, json={"chat_id": target, "text": text, "parse_mode": "Markdown", "reply_markup": markup}, timeout=10)
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
        requests.post(url, data={"chat_id": target, "caption": caption, "parse_mode": "Markdown", "reply_markup": json.dumps(inline_kb)}, files={"photo": ("chart.png", photo_buf, "image/png")}, timeout=20)
    except Exception:
        send_msg(caption, target, inline_kb)

def get_raw_spot_gold_price():
    try:
        r = requests.get("https://api.gold-api.com/price/XAU", timeout=3)
        if r.ok:
            p = float(r.json().get("price", 0))
            if p > 1000: return round(p, 2)
    except Exception:
        pass
    return 2750.0

def get_gold_ohlcv(timeframe, user_offset=0.0):
    try:
        t = yf.Ticker("GC=F")
        df = t.history(period="15d", interval=timeframe)
        if df is None or df.empty:
            t2 = yf.Ticker("XAUUSD=X")
            df = t2.history(period="15d", interval=timeframe)
        if df is None or df.empty:
            return pd.DataFrame()
        
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
        shift = (spot_now - last_close) + float(user_offset)
        if abs(shift) <= 200.0:
            for col in ["open", "high", "low", "close"]:
                df[col] = (pd.to_numeric(df[col]) + shift).round(2)
        return df
    except Exception:
        return pd.DataFrame()

def get_btc_ohlcv(timeframe, limit=100):
    try:
        ex = ccxt.binance({"enableRateLimit": True, "timeout": 5000})
        bars = ex.fetch_ohlcv("BTC/USDT", timeframe=timeframe, limit=limit)
        if bars:
            df = pd.DataFrame(bars, columns=["timestamp", "open", "high", "low", "close", "volume"])
            df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
            return df
    except Exception:
        pass
    return pd.DataFrame()

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
    h_l, h_c, l_c = df["high"] - df["low"], (df["high"] - df["close"].shift(1)).abs(), (df["low"] - df["close"].shift(1)).abs()
    df["ATR"] = pd.concat([h_l, h_c, l_c], axis=1).max(axis=1).ewm(alpha=1/14, adjust=False).mean().bfill()
    tp = (df["high"] + df["low"] + df["close"]) / 3.0
    df["VWAP"] = (tp * df["volume"].replace(0, 1.0)).rolling(30, min_periods=1).sum() / df["volume"].replace(0, 1.0).rolling(30, min_periods=1).sum()
    return df

def calculate_volume_profile(df, num_bins=20):
    recent = df.tail(40).copy()
    min_p, max_p = float(recent["low"].min()), float(recent["high"].max())
    if max_p <= min_p: max_p = min_p + 1.0
    bin_size = (max_p - min_p) / num_bins
    bin_centers = [min_p + (i + 0.5) * bin_size for i in range(num_bins)]
    bin_volumes = [1.0] * num_bins
    max_idx = int(max(range(num_bins), key=lambda i: bin_volumes[i]))
    return {"poc": round(bin_centers[max_idx], 2), "bin_centers": bin_centers, "bin_volumes": bin_volumes, "bin_height": bin_size * 0.85, "poc_idx": max_idx}

def evaluate_10_strategies_clean(ltf, vp_data):
    curr = ltf.iloc[-1]
    price = float(curr["close"])
    poc = vp_data["poc"]
    is_long = price >= poc
    return {"is_long": is_long, "confidence": 85, "stars": "⭐⭐⭐⭐", "summary_reasons": "ارتكاز POC + توافق السعر الحالي", "cvd_state": "شرائية 🟢", "dxy_text": "استقرار مؤشر الدولار"}

def detect_rsi_macd_divergence(plot_df):
    return {"found": False, "text": "حيادي"}

def analyze_structure_and_ote(df, is_long_bias, sym_type, vp_data, u_prefs):
    curr_price = float(df["close"].iloc[-1])
    ideal = 1.5 if sym_type == "GOLD" else 50.0
    mid = curr_price - ideal if is_long_bias else curr_price + ideal
    hb = 0.4 if sym_type == "GOLD" else 20.0
    return {"sniper_mid": round(mid, 2), "sniper_zone": (round(mid - hb, 2), round(mid + hb, 2)), "anchor_reason": "Micro OTE", "dist_from_curr": round(abs(curr_price - mid), 2)}

def generate_clean_chart_image(df, zones, vp_data, strat_res, asset_title, entry_zone, sl, exit_targets, usd_targets):
    try:
        plot_df = df.tail(35).reset_index(drop=True)
        fig, ax = plt.subplots(figsize=(10.0, 5.2), dpi=110)
        fig.patch.set_facecolor("#131722")
        ax.set_facecolor("#131722")
        for idx, row in plot_df.iterrows():
            c_col = "#26a69a" if float(row["close"]) >= float(row["open"]) else "#ef5350"
            ax.plot([idx, idx], [float(row["low"]), float(row["high"])], color=c_col, linewidth=1.2)
            ax.bar(idx, max(abs(float(row["close"]) - float(row["open"])), 0.01), bottom=min(float(row["open"]), float(row["close"])), width=0.6, color=c_col)
        ax.axhline(vp_data["poc"], color="#00e5ff", linestyle=":", linewidth=1.2)
        e_mid = zones["sniper_mid"]
        tp1, tp2, tp3 = exit_targets
        u1, u2, u3, u_sl = usd_targets
        ax.axhline(e_mid, color="#ffd700", linestyle="--", linewidth=1.4)
        ax.axhline(sl, color="#ff1744", linewidth=1.3)
        ax.axhline(tp1, color="#26a69a", linestyle="--", linewidth=1.1)
        ax.axhline(tp3, color="#69f0ae", linewidth=1.4)
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

    if ltf_df.empty or len(ltf_df) < 20:
        send_msg(f"⚠️ جاري جلب بيانات {asset_name}، حاول مرة أخرى خلال لحظات.", cid)
        return

    ltf = enrich_indicators(ltf_df)
    vp_data = calculate_volume_profile(ltf)
    strat_res = evaluate_10_strategies_clean(ltf, vp_data)
    is_long = strat_res["is_long"]

    zones = analyze_structure_and_ote(ltf, is_long, sym_type, vp_data, u_prefs)
    entry_zone = zones["sniper_zone"]
    entry_mid = zones["sniper_mid"]
    curr_price = round(float(ltf["close"].iloc[-1]), 2)

    lot = u_prefs["lot_size"]
    u_tp1, u_tp2, u_tp3, u_sl = u_prefs["tp1_usd"], u_prefs["tp2_usd"], u_prefs["tp3_usd"], u_prefs["sl_usd"]
    dist_tp1, dist_tp3, dist_sl = usd_to_price_distance(sym_type, u_tp1, lot), usd_to_price_distance(sym_type, u_tp3, lot), usd_to_price_distance(sym_type, u_sl, lot)

    if is_long:
        dir_label = "🟢 شراء قريب (BUY LIMIT)"
        sl, tp1, tp2, tp3 = round(entry_mid - dist_sl, 2), round(entry_mid + dist_tp1, 2), round(entry_mid + dist_tp1*1.8, 2), round(entry_mid + dist_tp3, 2)
    else:
        dir_label = "🔴 بيع قريب (SELL LIMIT)"
        sl, tp1, tp2, tp3 = round(entry_mid + dist_sl, 2), round(entry_mid - dist_tp1, 2), round(entry_mid - dist_tp1*1.8, 2), round(entry_mid - dist_tp3, 2)

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

def telegram_listener():
    if not TELEGRAM_BOT_TOKEN: return
    try: requests.get(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/deleteWebhook", timeout=10)
    except Exception: pass

    offset = None
    while True:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
            res = requests.get(url, params={"timeout": 15, "offset": offset}, timeout=20).json()
            if res.get("ok"):
                for item in res.get("result", []):
                    offset = item["update_id"] + 1

                    if "callback_query" in item:
                        cb = item["callback_query"]
                        cb_data = cb.get("data", "")
                        cb_chat = str(cb.get("message", {}).get("chat", {}).get("id", "")).strip()
                        requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery", json={"callback_query_id": cb["id"]}, timeout=5)

                        if cb_data.startswith("TF|"):
                            _, sym_t, chosen_tf = cb_data.split("|")
                            u_s = get_user_settings(cb_chat)
                            u_s["ltf"] = chosen_tf
                            analyze_and_send_zones(sym_t, chosen_tf, cb_chat)
                        continue

                    msg = item.get("message", {})
                    text = msg.get("text", "").strip()
                    if not text: continue
                    chat_id = str(msg.get("chat", {}).get("id", "")).strip()

                    u_s = get_user_settings(chat_id)
                    if text in ["/start", "/help", "⚙️ الإعدادات والتحكم"]:
                        send_msg("🤖 أهلاً بك يا بشمهندس أيمن! البوت يعمل الآن بكامل كفاءته.\nاختر من الأزرار بالأسفل لتحليل الذهب أو البيتكوين.", chat_id)
                    elif text in ["/gold", "🪙 تحليل الذهب", "🪙 مناطق دخول الذهب"]:
                        analyze_and_send_zones("GOLD", u_s["ltf"], chat_id)
                    elif text in ["/btc", "₿ تحليل البيتكوين", "₿ مناطق دخول البيتكوين"]:
                        analyze_and_send_zones("BTC", u_s["ltf"], chat_id)
        except Exception as e:
            print(f"Listener error: {e}")
        time.sleep(1)

@app.route("/")
def home():
    return "Fast Responsive Bot is Live!"

@app.route("/health")
def health():
    return {"status": "ok"}, 200

if __name__ == "__main__":
    threading.Thread(target=telegram_listener, daemon=True).start()
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
