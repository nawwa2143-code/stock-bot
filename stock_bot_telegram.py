#!/usr/bin/env python3
"""
بوت الأسهم الذكي - تلغرام
"""

import os
import json
import logging
import time
import threading
from datetime import datetime, timedelta, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler

import holidays as _holidays_lib
import yfinance as yf
import pandas as pd
import numpy as np
import ta
import pytz
import requests
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

# ══════════════════════════════════════════════
# إعداد اللوغ
# ══════════════════════════════════════════════
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════
# الإعدادات
# ══════════════════════════════════════════════
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
RENDER_URL       = os.environ.get("RENDER_URL", "https://stock-bot-ilzq.onrender.com")
MIN_CONFIDENCE   = 45
DATA_FILE        = "bot_data.json"

# ══════════════════════════════════════════════
# العطل الأمريكية — تلقائي كل سنة
# ══════════════════════════════════════════════
def _get_us_holidays():
    yr = datetime.now().year
    h  = _holidays_lib.US(years=[yr, yr + 1])
    return {d.strftime("%Y-%m-%d") for d in h.keys()}

US_HOLIDAYS = _get_us_holidays()

# ══════════════════════════════════════════════
# القوائم الاحتياطية
# ══════════════════════════════════════════════
SP500_BACKUP = [
    "AAPL","MSFT","NVDA","GOOGL","AMZN","META","TSLA","JPM","JNJ",
    "V","PG","UNH","HD","MA","MRK","ABBV","PFE","KO","PEP","BAC",
    "DIS","CSCO","ADBE","CRM","NFLX","AMD","QCOM","TXN","AVGO",
    "COST","NKE","MCD","GE","BA","CAT","IBM","ORCL","PYPL","UBER",
    "XOM","CVX","COP","WFC","GS","MS","LLY","AMGN","GILD",
]

SPECULATIVE_BACKUP = [
    "SOUN","AMC","GME","SPCE","MVIS","SNDL","ACB","CGC","TLRY",
    "NOK","BB","SOFI","OPEN","PLTR","NIO","XPEV","RIVN","HOOD",
    "COIN","BBAI","APLD","CTIC","VXRT","OCGN",
]

# ══════════════════════════════════════════════
# القطاعات — معرّفة مبكراً عشان تُستخدم في analyze_all
# ══════════════════════════════════════════════
SECTORS = {
    "تقنية 💻":   ["AAPL","MSFT","NVDA","AMD","GOOGL","META","TSLA","AVGO","QCOM","ORCL"],
    "طاقة ⛽":    ["XOM","CVX","COP","SLB","EOG","PXD","MPC","VLO","OXY","HAL"],
    "صحة 🏥":     ["JNJ","UNH","PFE","ABBV","MRK","LLY","AMGN","GILD","CVS","MDT"],
    "بنوك 🏦":    ["JPM","BAC","WFC","GS","MS","C","USB","PNC","TFC","COF"],
    "استهلاك 🛒": ["AMZN","WMT","HD","MCD","NKE","SBUX","TGT","COST","LOW","DG"],
}

# متغيرات عامة
TODAY_INVESTMENT  = []
TODAY_SPECULATIVE = []

# ══════════════════════════════════════════════
# البيانات
# ══════════════════════════════════════════════
def load_data():
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except:
            pass
    return {
        "portfolio":        {},
        "signals":          {},
        "signal_counter":   0,
        "history":          [],
        "capital":          10000,
        "risk_pct":         1.0,
        "weekly_signals":   [],
        "sent_today":       {},
        "portfolio_alerts": {},
        "last_update_id":   0,
    }

def save_data(data):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

# ══════════════════════════════════════════════
# تيليجرام
# ══════════════════════════════════════════════
def send_telegram(message, chat_id=None):
    cid = chat_id or TELEGRAM_CHAT_ID
    if not cid or not TELEGRAM_TOKEN:
        return
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": cid, "text": message, "parse_mode": "Markdown"},
            timeout=10,
        )
        if r.status_code != 200:
            logger.error(f"خطأ إرسال: {r.text}")
    except Exception as e:
        logger.error(f"فشل الإرسال: {e}")

# ══════════════════════════════════════════════
# السوق
# ══════════════════════════════════════════════
def is_market_open():
    now   = datetime.now(pytz.timezone("America/New_York"))
    today = now.strftime("%Y-%m-%d")
    if now.weekday() >= 5:
        return False
    if today in US_HOLIDAYS:
        return False
    open_t  = now.replace(hour=9,  minute=30, second=0, microsecond=0)
    close_t = now.replace(hour=16, minute=0,  second=0, microsecond=0)
    return open_t <= now <= close_t

def is_market_bearish():
    """يتحقق إذا SPY نازل 1%+ — نوقف التوصيات"""
    try:
        info       = yf.Ticker("SPY").fast_info
        change_pct = (info.last_price - info.previous_close) / info.previous_close * 100
        if change_pct <= -1.0:
            logger.info(f"السوق هابط: SPY {change_pct:.2f}%")
            return True
        return False
    except:
        return False

# ══════════════════════════════════════════════
# جلب الأسهم التلقائي
# ══════════════════════════════════════════════
def get_smart_investment_list():
    tickers = []
    for scr in ["day_gainers", "most_actives", "growth_technology_stocks"]:
        try:
            result = yf.screen(scr, count=30)
            found  = [
                q["symbol"] for q in result.get("quotes", [])
                if 20 <= q.get("regularMarketPrice", 0) <= 500  # $20-$500 فقط
                and q.get("averageDailyVolume3Month", 0) > 1_000_000
            ]
            tickers.extend(found)
        except Exception as e:
            logger.warning(f"فشل سكرينر {scr}: {e}")
    tickers = list(dict.fromkeys(tickers))
    return tickers[:30] if tickers else SP500_BACKUP[:30]

def get_smart_speculative_list():
    tickers = []
    for scr in ["most_actives", "day_gainers"]:
        try:
            result = yf.screen(scr, count=50)
            found  = [
                q["symbol"] for q in result.get("quotes", [])
                if 5 <= q.get("regularMarketPrice", 0) <= 20  # $5-$20 فقط
            ]
            tickers.extend(found)
        except Exception as e:
            logger.warning(f"فشل سكرينر {scr}: {e}")
    tickers = list(dict.fromkeys(tickers))
    return tickers[:20] if tickers else SPECULATIVE_BACKUP[:20]

# ══════════════════════════════════════════════
# التحليل الفني
# ══════════════════════════════════════════════
def fetch_and_analyze(ticker):
    try:
        stock = yf.Ticker(ticker)
        df    = stock.history(period="3mo", interval="1d")
        if df.empty or len(df) < 30:
            return None, None, None, None

        info       = stock.fast_info
        price      = round(info.last_price, 2)
        prev_close = info.previous_close
        change_pct = round((price - prev_close) / prev_close * 100, 2)

        c = df["Close"]
        h = df["High"]
        l = df["Low"]
        v = df["Volume"]

        df["rsi"]         = ta.momentum.RSIIndicator(c, window=14).rsi()
        macd_obj          = ta.trend.MACD(c)
        df["macd"]        = macd_obj.macd()
        df["macd_signal"] = macd_obj.macd_signal()
        bb                = ta.volatility.BollingerBands(c)
        df["bb_pct"]      = bb.bollinger_pband()
        df["ema9"]        = ta.trend.EMAIndicator(c, window=9).ema_indicator()
        df["ema21"]       = ta.trend.EMAIndicator(c, window=21).ema_indicator()
        df["atr"]         = ta.volatility.AverageTrueRange(h, l, c).average_true_range()
        df["vol_ratio"]   = v / v.rolling(20).mean()
        df["adx"]         = ta.trend.ADXIndicator(h, l, c, window=14).adx()
        df["obv"]         = ta.volume.OnBalanceVolumeIndicator(c, v).on_balance_volume()
        df["obv_ema"]     = df["obv"].ewm(span=20).mean()

        df = df.dropna()
        if len(df) < 2:
            return None, None, None, None

        last = df.iloc[-1]
        prev = df.iloc[-2]
        price_info = {"ticker": ticker, "price": price, "change_pct": change_pct}
        return last, prev, price_info, df

    except Exception as e:
        logger.warning(f"خطأ في {ticker}: {e}")
        return None, None, None, None

# ══════════════════════════════════════════════
# توليد الإشارة
# ══════════════════════════════════════════════
def generate_signal(ticker, last, prev, price_info, mode="investment"):
    buy, sell = 0, 0
    reasons_buy, reasons_sell = [], []

    rsi        = last["rsi"]
    price      = price_info["price"]
    oversold   = 40 if mode == "investment" else 45
    overbought = 60 if mode == "investment" else 55

    # ١. RSI (20 نقطة)
    if rsi < oversold:
        buy += 20
        reasons_buy.append(f"RSI={rsi:.0f} (مبالغ في بيعه)")
    elif rsi < 50:
        buy += 10
        reasons_buy.append(f"RSI={rsi:.0f} (تحت المنتصف)")
    elif rsi > overbought:
        sell += 20
        reasons_sell.append(f"RSI={rsi:.0f} (مبالغ في شرائه)")
    elif rsi > 50:
        sell += 10
        reasons_sell.append(f"RSI={rsi:.0f} (فوق المنتصف)")

    # ٢. MACD (25 نقطة)
    if prev["macd"] < prev["macd_signal"] and last["macd"] > last["macd_signal"]:
        buy += 25
        reasons_buy.append("تقاطع MACD صعودي ✅")
    elif last["macd"] > last["macd_signal"]:
        buy += 12
        reasons_buy.append("MACD فوق خط الإشارة")
    elif prev["macd"] > prev["macd_signal"] and last["macd"] < last["macd_signal"]:
        sell += 25
        reasons_sell.append("تقاطع MACD هبوطي ❌")
    elif last["macd"] < last["macd_signal"]:
        sell += 12
        reasons_sell.append("MACD تحت خط الإشارة")

    # ٣. Bollinger (20 نقطة)
    bb_pct = last["bb_pct"]
    if bb_pct < 0.2:
        buy += 20
        reasons_buy.append(f"السعر في الحزام السفلي ({bb_pct:.0%})")
    elif bb_pct < 0.4:
        buy += 10
        reasons_buy.append(f"السعر أسفل المتوسط ({bb_pct:.0%})")
    elif bb_pct > 0.8:
        sell += 20
        reasons_sell.append(f"السعر في الحزام العلوي ({bb_pct:.0%})")
    elif bb_pct > 0.6:
        sell += 10
        reasons_sell.append(f"السعر فوق المتوسط ({bb_pct:.0%})")

    # ٤. EMA (20 نقطة)
    if prev["ema9"] < prev["ema21"] and last["ema9"] > last["ema21"]:
        buy += 20
        reasons_buy.append("EMA9 تجاوزت EMA21 للأعلى ✅")
    elif last["ema9"] > last["ema21"]:
        buy += 10
        reasons_buy.append("EMA9 فوق EMA21")
    elif prev["ema9"] > prev["ema21"] and last["ema9"] < last["ema21"]:
        sell += 20
        reasons_sell.append("EMA9 تجاوزت EMA21 للأسفل ❌")
    elif last["ema9"] < last["ema21"]:
        sell += 10
        reasons_sell.append("EMA9 تحت EMA21")

    # ٥. OBV (15 نقطة) — فقط إذا في تحرك واضح
    obv_change_pct = abs(last["obv"] - last["obv_ema"]) / (abs(last["obv_ema"]) + 1) * 100
    if obv_change_pct >= 2:
        if last["obv"] > last["obv_ema"]:
            buy += 15
            reasons_buy.append("OBV: أموال تدخل السهم")
        else:
            sell += 15
            reasons_sell.append("OBV: أموال تخرج من السهم")
    # إذا OBV محايد — لا نعطي نقاط

    # ٦. ADX — فلتر قوة الاتجاه (خصم 15% إذا ضعيف)
    if last["adx"] <= 25:
        buy  = int(buy  * 0.85)
        sell = int(sell * 0.85)
    else:
        if buy > sell:
            reasons_buy.append(f"ADX={last['adx']:.0f} اتجاه قوي")
        elif sell > buy:
            reasons_sell.append(f"ADX={last['adx']:.0f} اتجاه قوي")

    # ٧. حجم التداول (بونص)
    if last["vol_ratio"] > 1.5:
        if buy > sell:
            reasons_buy.append(f"حجم مرتفع ({last['vol_ratio']:.1f}x)")
        elif sell > buy:
            reasons_sell.append(f"حجم مرتفع ({last['vol_ratio']:.1f}x)")

    # حساب ATR والمدة
    atr     = last["atr"]
    atr_pct = round(atr / price * 100, 1)
    adx     = last["adx"]

    if adx >= 35:
        speed_label = "اتجاه قوي جداً ⚡⚡"
        multiplier  = 0.8
    elif adx >= 25:
        speed_label = "اتجاه قوي ⚡"
        multiplier  = 1.2
    elif adx >= 15:
        speed_label = "اتجاه متوسط 〰️"
        multiplier  = 1.8
    else:
        speed_label = "اتجاه ضعيف 🐢"
        multiplier  = 2.5

    if mode == "investment":
        stop_loss = round(price - (atr * 1.5), 2)   # خسارة أقل
        target    = round(price + (atr * 3),   2)   # R:R = 3/1.5 = 2.0 ✅
        days_est  = max(2, round(abs(target - price) / atr * multiplier))
    else:
        stop_loss = round(price - (atr * 1.5), 2)
        target    = round(price + (atr * 4),   2)   # R:R = 4/1.5 = 2.67 ✅
        days_est  = max(1, round(abs(target - price) / atr * multiplier * 0.7))

    stop_pct   = round((price - stop_loss) / price * 100, 1)
    target_pct = round((target - price)    / price * 100, 1)

    # فلتر R:R — الهدف لازم ضعف وقف الخسارة (2:1)
    risk   = abs(price - stop_loss)
    reward = abs(target - price)
    if risk == 0 or reward / risk < 2.0:
        return None

    if buy > sell:
        conf = min(round(buy / 100 * 100), 99)
        if conf < MIN_CONFIDENCE:
            return None
        return {
            "ticker": ticker, "action": "🟢 شراء", "action_en": "BUY",
            "mode": mode, "price": price, "change_pct": price_info["change_pct"],
            "confidence": conf, "reasons": reasons_buy,
            "stop_loss": stop_loss, "stop_pct": stop_pct,
            "target": target, "target_pct": target_pct,
            "atr_pct": atr_pct, "days_est": days_est, "speed_label": speed_label,
        }

    # توصية البيع فقط للتنبيه — مو للبيع على المكشوف
    # تُستخدم فقط في check_portfolio لتنبيه صاحب السهم
    if sell > buy:
        conf = min(round(sell / 100 * 100), 99)
        if conf < MIN_CONFIDENCE:
            return None
        stop_sell   = round(price + (atr * 1.5), 2)
        target_sell = round(price - (atr * 3),   2)
        return {
            "ticker": ticker, "action": "🔴 تحذير بيع", "action_en": "SELL",
            "mode": mode, "price": price, "change_pct": price_info["change_pct"],
            "confidence": conf, "reasons": reasons_sell,
            "stop_loss": stop_sell,   "stop_pct":   round((stop_sell - price)   / price * 100, 1),
            "target":    target_sell, "target_pct": round((price - target_sell) / price * 100, 1),
            "atr_pct": atr_pct, "days_est": days_est, "speed_label": speed_label,
        }

    return None

# ══════════════════════════════════════════════
# إدارة رأس المال
# ══════════════════════════════════════════════
def calc_position_size(price, stop_loss, capital, risk_pct):
    risk_amount    = capital * (risk_pct / 100)
    loss_per_share = abs(price - stop_loss)
    if loss_per_share == 0:
        return 0
    shares = int(risk_amount / loss_per_share)
    # حد أقصى 25% من رأس المال في صفقة واحدة
    max_shares = int((capital * 0.25) / price)
    shares     = min(shares, max_shares)
    return max(1, shares)

def calc_expected_profit(price, target, shares):
    if not target or not shares:
        return 0
    return round(abs(target - price) * shares, 2)

# ══════════════════════════════════════════════
# تنسيق رسالة التوصية
# ══════════════════════════════════════════════
def format_signal_message(signal, number, capital, risk_pct):
    mode_icon       = "🔵 استثمار" if signal["mode"] == "investment" else "🟡 مضاربة ⚡"
    shares          = calc_position_size(signal["price"], signal["stop_loss"], capital, risk_pct)
    expected_profit = calc_expected_profit(signal["price"], signal["target"], shares)
    total_invest    = round(signal["price"] * shares, 2)

    lines = [
        "━━━━━━━━━━━━━━━━━━━",
        mode_icon,
        f"{number}⃣ *{signal['ticker']}* — ${signal['price']}",
        f"🎯 {signal['action']} — ثقة {signal['confidence']}%",
        f"📈 التغيير: {signal['change_pct']:+.2f}%",
        f"📊 تذبذب يومي: {signal['atr_pct']}%",
        "",
        "📋 *الأسباب:*",
    ]
    for r in signal["reasons"]:
        lines.append(f"  • {r}")

    lines += [
        "",
        f"🔴 *وقف الخسارة:* ${signal['stop_loss']} ({signal['stop_pct']}%)",
        f"🎯 *الهدف:* ${signal['target']} ({signal['target_pct']}%)",
        f"⏱ *المدة المتوقعة:* {signal['days_est']} أيام — {signal['speed_label']}",
    ]

    if shares > 0:
        allocation_pct = round(total_invest / capital * 100)
        lines += [
            "",
            f"💼 *بناءً على محفظتك (${capital:,}):*",
            f"  📦 الكمية المقترحة: {shares} سهم",
            f"  💵 إجمالي الاستثمار: ${total_invest:,} ({allocation_pct}% من رأس المال)",
        ]
        if expected_profit > 0:
            lines.append(f"  💰 الربح المتوقع: +${expected_profit}")

    if signal["mode"] == "speculative":
        lines.append("⚠️ مضاربة — خطر مرتفع")

    lines += ["", f"للتسجيل رد: /اشتريت {number}"]
    return "\n".join(lines)

# ══════════════════════════════════════════════
# التحليل الرئيسي
# ══════════════════════════════════════════════
def analyze_all():
    global TODAY_INVESTMENT, TODAY_SPECULATIVE

    if not is_market_open():
        logger.info("السوق مغلق")
        return

    data  = load_data()
    today = datetime.now().strftime("%Y-%m-%d")

    # فلتر ١: السوق هابط؟
    if is_market_bearish():
        send_telegram(
            "⚠️ *السوق هابط اليوم*\n"
            "━━━━━━━━━━━━━━━━━━━\n"
            "📉 SPY نازل أكثر من 1% — تم تعليق التوصيات حمايةً لرأس مالك"
        )
        return

    # فلتر ٢: الخسارة اليومية تجاوزت 3%؟
    today_loss = sum(
        t.get("profit", 0) for t in data.get("history", [])
        if t.get("date_sell", "") == today and t.get("profit", 0) < 0
    )
    if abs(today_loss) >= data["capital"] * 0.03:
        send_telegram(
            f"🛑 *تم إيقاف التداول اليوم*\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"📉 الخسارة اليومية وصلت ${abs(today_loss):.2f} (3% من رأس المال)\n"
            f"🛡️ لن تصدر توصيات اليوم"
        )
        return

    sent_today = data.get("sent_today", {})
    if sent_today.get("_date") != today:
        sent_today = {"_date": today}

    if not TODAY_INVESTMENT:
        TODAY_INVESTMENT = get_smart_investment_list()
    if not TODAY_SPECULATIVE:
        TODAY_SPECULATIVE = get_smart_speculative_list()

    # تنويع القطاعات
    def get_sector(t):
        for sector, tickers in SECTORS.items():
            if t in tickers:
                return sector
        return "أخرى"

    sectors_used        = set()
    investment_signals  = []
    speculative_signals = []

    for ticker in TODAY_INVESTMENT[:30]:
        try:
            time.sleep(0.5)
            last, prev, price_info, df = fetch_and_analyze(ticker)
            # استثمار: $20-$500 فقط — تحت $20 غير مستقر، فوق $500 كمية قليلة جداً
            if last is None or not (20 <= price_info["price"] <= 500):
                continue
            signal = generate_signal(ticker, last, prev, price_info, "investment")
            if signal and sent_today.get(ticker) != signal["action_en"]:
                sector = get_sector(ticker)
                if sector not in sectors_used or sector == "أخرى":
                    investment_signals.append(signal)
                    sectors_used.add(sector)
        except:
            pass

    for ticker in TODAY_SPECULATIVE[:20]:
        try:
            time.sleep(0.5)
            last, prev, price_info, df = fetch_and_analyze(ticker)
            # مضاربة: $5-$20 فقط — تحت $5 penny stocks خطيرة جداً
            if last is None or not (5 <= price_info["price"] <= 20):
                continue
            signal = generate_signal(ticker, last, prev, price_info, "speculative")
            if signal and sent_today.get(ticker) != signal["action_en"]:
                speculative_signals.append(signal)
        except:
            pass

    top_investment  = sorted(investment_signals,  key=lambda x: x["confidence"], reverse=True)[:3]
    top_speculative = sorted(speculative_signals, key=lambda x: x["confidence"], reverse=True)[:2]
    all_signals     = top_investment + top_speculative

    if not all_signals:
        send_telegram("📊 ما في إشارات واضحة الآن — سأحاول في الجلسة القادمة.")
        return

    for signal in all_signals:
        # لا نرسل توصية SELL إلا لو السهم في المحفظة
        if signal["action_en"] == "SELL":
            owned = any(t["ticker"] == signal["ticker"] for t in data["portfolio"].values())
            if not owned:
                continue  # لا نحفظ ولا نرسل
        data["signal_counter"] += 1
        num = data["signal_counter"]
        data["signals"][str(num)] = {
            **signal,
            "number":    num,
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "bought":    False,
        }
        data["weekly_signals"].append(str(num))
        send_telegram(format_signal_message(signal, num, data["capital"], data["risk_pct"]))
        sent_today[signal["ticker"]] = signal["action_en"]  # نحفظ فقط ما تم إرساله
        time.sleep(0.3)

    data["sent_today"] = sent_today
    save_data(data)

# ══════════════════════════════════════════════
# متابعة المحفظة + Trailing Stop
# ══════════════════════════════════════════════
def check_portfolio():
    if not is_market_open():
        return
    data = load_data()
    if not data["portfolio"]:
        return

    alerts = data.get("portfolio_alerts", {})
    now    = datetime.now().strftime("%Y-%m-%d %H")

    for num, trade in list(data["portfolio"].items()):
        try:
            price     = round(yf.Ticker(trade["ticker"]).fast_info.last_price, 2)
            buy_price = trade["buy_price"]
            shares    = trade["shares"]
            stop_loss = trade["stop_loss"]
            target    = trade["target"]
            profit    = round((price - buy_price) * shares, 2)
            profit_pct= round((price - buy_price) / buy_price * 100, 2)

            # Trailing Stop
            if price > buy_price:
                peak           = trade.get("peak_price", buy_price)
                new_peak       = max(peak, price)
                new_trail_stop = round(new_peak * 0.97, 2)
                if new_trail_stop > stop_loss:
                    data["portfolio"][num]["peak_price"] = new_peak
                    data["portfolio"][num]["stop_loss"]  = new_trail_stop
                    stop_loss = new_trail_stop
                    if alerts.get(f"{num}_trail") != now:
                        send_telegram(
                            f"📈 *Trailing Stop محدّث*\n"
                            f"━━━━━━━━━━━━━━━━━━━\n"
                            f"{num}⃣ *{trade['ticker']}*\n"
                            f"💰 السعر: ${price}\n"
                            f"🔴 وقف الخسارة الجديد: ${new_trail_stop}\n"
                            f"🛡️ ربحك المحمي: +${round((new_trail_stop - buy_price) * shares, 2)}"
                        )
                        alerts[f"{num}_trail"] = now

            # وصل الهدف
            if target and price >= target and alerts.get(f"{num}_target") != now:
                send_telegram(
                    f"🎯 *حان وقت البيع!*\n"
                    f"━━━━━━━━━━━━━━━━━━━\n"
                    f"{num}⃣ *{trade['ticker']}* وصل الهدف!\n"
                    f"💰 السعر: ${price}\n"
                    f"💵 ربحك: +${profit} (+{profit_pct}%)\n\n"
                    f"رد: /بعت {num}"
                )
                alerts[f"{num}_target"] = now

            # تحذير وقف الخسارة
            elif stop_loss and price <= stop_loss * 1.02 and price > stop_loss and alerts.get(f"{num}_warn") != now:
                send_telegram(
                    f"⚠️ *تحذير!*\n"
                    f"━━━━━━━━━━━━━━━━━━━\n"
                    f"{num}⃣ *{trade['ticker']}* اقترب من وقف الخسارة\n"
                    f"💰 السعر: ${price}\n"
                    f"🔴 وقف الخسارة: ${stop_loss}\n"
                    f"رد: /بعت {num}"
                )
                alerts[f"{num}_warn"] = now

            # كسر وقف الخسارة
            elif stop_loss and price <= stop_loss and alerts.get(f"{num}_stop") != now:
                send_telegram(
                    f"🔴 *بيع فوراً!*\n"
                    f"━━━━━━━━━━━━━━━━━━━\n"
                    f"{num}⃣ *{trade['ticker']}* كسر وقف الخسارة!\n"
                    f"💰 السعر: ${price}\n"
                    f"📉 الخسارة: ${abs(profit)} ({profit_pct}%)\n\n"
                    f"رد: /بعت {num}"
                )
                alerts[f"{num}_stop"] = now

        except Exception as e:
            logger.error(f"خطأ في متابعة {trade['ticker']}: {e}")

    data["portfolio_alerts"] = alerts
    save_data(data)

# ══════════════════════════════════════════════
# الملخص الصباحي
# ══════════════════════════════════════════════
def morning_briefing():
    global TODAY_INVESTMENT, TODAY_SPECULATIVE
    TODAY_INVESTMENT  = get_smart_investment_list()
    TODAY_SPECULATIVE = get_smart_speculative_list()
    data = load_data()

    send_telegram(
        f"🌅 *صباح الخير! السوق يفتح بعد 30 دقيقة*\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"💼 رأس مالك: ${data['capital']:,}\n"
        f"⚡ نسبة المخاطرة: {data['risk_pct']}%\n\n"
        f"🔍 جاري تحليل الأسهم..."
    )

    investment_signals  = []
    speculative_signals = []

    for ticker in TODAY_INVESTMENT[:30]:
        try:
            time.sleep(0.4)
            last, prev, price_info, df = fetch_and_analyze(ticker)
            if last is None or price_info["price"] < 20:
                continue
            signal = generate_signal(ticker, last, prev, price_info, "investment")
            if signal:
                investment_signals.append(signal)
        except:
            pass

    for ticker in TODAY_SPECULATIVE[:20]:
        try:
            time.sleep(0.4)
            last, prev, price_info, df = fetch_and_analyze(ticker)
            if last is None or not (1 <= price_info["price"] <= 20):
                continue
            signal = generate_signal(ticker, last, prev, price_info, "speculative")
            if signal:
                speculative_signals.append(signal)
        except:
            pass

    top_investment  = sorted(investment_signals,  key=lambda x: x["confidence"], reverse=True)[:3]
    top_speculative = sorted(speculative_signals, key=lambda x: x["confidence"], reverse=True)[:2]
    all_signals     = top_investment + top_speculative

    if not all_signals:
        inv_list  = "، ".join(TODAY_INVESTMENT[:10])
        spec_list = "، ".join(TODAY_SPECULATIVE[:5])
        send_telegram(
            f"📋 *الأسهم على الرادار اليوم:*\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"🔵 استثمار: {inv_list}\n\n"
            f"🟡 مضاربة: {spec_list}\n\n"
            f"⏰ التوصيات التفصيلية عند فتح السوق 9:35 نيويورك (4:35 مساءً السعودية)"
        )
        return

    today      = datetime.now().strftime("%Y-%m-%d")
    sent_today = data.get("sent_today", {})
    if sent_today.get("_date") != today:
        sent_today = {"_date": today}

    send_telegram(
        f"📊 *توصيات ما قبل الفتح — {today}*\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"⚠️ مبنية على بيانات الإغلاق — التوصيات النهائية تصدر عند 9:35 نيويورك"
    )

    for signal in all_signals:
        data["signal_counter"] += 1
        num = data["signal_counter"]
        data["signals"][str(num)] = {
            **signal,
            "number":    num,
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "bought":    False,
        }
        data["weekly_signals"].append(str(num))
        send_telegram(format_signal_message(signal, num, data["capital"], data["risk_pct"]))
        time.sleep(0.5)

    # لا نحفظ في sent_today عشان analyze_all يرسل من جديد بسعر حي
    save_data(data)

# ══════════════════════════════════════════════
# ملخص نهاية اليوم
# ══════════════════════════════════════════════
def end_of_day():
    global TODAY_INVESTMENT, TODAY_SPECULATIVE
    data             = load_data()
    portfolio_profit = 0

    lines = [
        "━━━━━━━━━━━━━━━━━━━",
        "📊 *ملخص اليوم*",
        "━━━━━━━━━━━━━━━━━━━",
        f"📋 التوصيات اليوم: {data['signal_counter']}",
        "",
    ]

    if data["portfolio"]:
        lines.append("💼 *محفظتك الآن:*")
        for num, trade in data["portfolio"].items():
            try:
                price  = round(yf.Ticker(trade["ticker"]).fast_info.last_price, 2)
                profit = round((price - trade["buy_price"]) * trade["shares"], 2)
                pct    = round((price - trade["buy_price"]) / trade["buy_price"] * 100, 2)
                icon   = "🟢" if profit >= 0 else "🔴"
                lines.append(f"{num}⃣ {trade['ticker']} — {icon} ${profit:+.2f} ({pct:+.2f}%)")
                portfolio_profit += profit
            except:
                pass
        lines.append(f"\n💰 إجمالي: ${portfolio_profit:+.2f}")

    send_telegram("\n".join(lines))

    data["signals"]          = {}
    data["sent_today"]       = {}
    data["portfolio_alerts"] = {}
    TODAY_INVESTMENT         = []
    TODAY_SPECULATIVE        = []
    save_data(data)

# ══════════════════════════════════════════════
# ملخص أسبوعي
# ══════════════════════════════════════════════
def weekly_summary():
    data        = load_data()
    history     = data.get("history", [])
    week_ago    = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
    week_trades = [t for t in history if t.get("date_sell", "") >= week_ago]

    if not week_trades:
        send_telegram("📊 *ملخص الأسبوع*\nما في صفقات مغلقة هذا الأسبوع.")
        return

    total_profit = sum(t.get("profit", 0) for t in week_trades)
    winning      = [t for t in week_trades if t.get("profit", 0) > 0]
    losing       = [t for t in week_trades if t.get("profit", 0) < 0]
    win_rate     = round(len(winning) / len(week_trades) * 100)
    best         = max(week_trades, key=lambda x: x.get("profit", 0))
    worst        = min(week_trades, key=lambda x: x.get("profit", 0))
    icon         = "💵" if total_profit >= 0 else "📉"

    send_telegram(
        f"📊 *ملخص الأسبوع*\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"📋 الصفقات: {len(week_trades)}\n"
        f"✅ رابحة: {len(winning)} ({win_rate}%)\n"
        f"❌ خاسرة: {len(losing)}\n"
        f"{icon} *صافي: ${total_profit:+.2f}*\n\n"
        f"🏆 أفضل: {best['ticker']} +${best.get('profit',0):.2f}\n"
        f"💀 أسوأ: {worst['ticker']} ${worst.get('profit',0):.2f}"
    )
    data["weekly_signals"] = []
    save_data(data)

# ══════════════════════════════════════════════
# سجل أداء البوت
# ══════════════════════════════════════════════
def bot_performance(chat_id=None):
    data    = load_data()
    history = data.get("history", [])

    if not history:
        send_telegram("📊 ما في صفقات مغلقة بعد.", chat_id)
        return

    total        = len(history)
    winning      = [t for t in history if t.get("profit", 0) > 0]
    losing       = [t for t in history if t.get("profit", 0) < 0]
    total_profit = sum(t.get("profit", 0) for t in history)
    win_rate     = round(len(winning) / total * 100)
    avg_win      = round(sum(t["profit"] for t in winning) / len(winning), 2) if winning else 0
    avg_loss     = round(sum(t["profit"] for t in losing)  / len(losing),  2) if losing  else 0
    best         = max(history, key=lambda x: x.get("profit", 0))
    worst        = min(history, key=lambda x: x.get("profit", 0))
    pf           = round(abs(sum(t["profit"] for t in winning) / sum(t["profit"] for t in losing)), 2) if losing else "∞"
    icon         = "💵" if total_profit >= 0 else "📉"

    send_telegram(
        f"📈 *أداء البوت الكلي*\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"📋 إجمالي الصفقات: {total}\n"
        f"✅ رابحة: {len(winning)} ({win_rate}%)\n"
        f"❌ خاسرة: {len(losing)}\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"💰 متوسط الربح: +${avg_win}\n"
        f"📉 متوسط الخسارة: ${avg_loss}\n"
        f"⚖️ Profit Factor: {pf}\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"{icon} *صافي الكل: ${total_profit:+.2f}*\n\n"
        f"🏆 أفضل: {best['ticker']} +${best.get('profit',0):.2f}\n"
        f"💀 أسوأ: {worst['ticker']} ${worst.get('profit',0):.2f}",
        chat_id
    )

# ══════════════════════════════════════════════
# Backtesting
# ══════════════════════════════════════════════
def backtest_ticker(ticker, chat_id=None):
    send_telegram(f"🔬 جاري اختبار {ticker} على آخر 3 أشهر...", chat_id)
    try:
        df = yf.Ticker(ticker).history(period="3mo", interval="1d")
        if df.empty or len(df) < 40:
            send_telegram(f"❌ بيانات {ticker} غير كافية", chat_id)
            return

        c = df["Close"]; h = df["High"]; l = df["Low"]
        df["rsi"]         = ta.momentum.RSIIndicator(c, window=14).rsi()
        macd_obj          = ta.trend.MACD(c)
        df["macd"]        = macd_obj.macd()
        df["macd_signal"] = macd_obj.macd_signal()
        df["ema9"]        = ta.trend.EMAIndicator(c, window=9).ema_indicator()
        df["ema21"]       = ta.trend.EMAIndicator(c, window=21).ema_indicator()
        df["atr"]         = ta.volatility.AverageTrueRange(h, l, c).average_true_range()
        df["adx"]         = ta.trend.ADXIndicator(h, l, c, window=14).adx()
        df = df.dropna()

        trades   = []
        in_trade = False
        buy_p = stop = target = 0

        for i in range(1, len(df)):
            row  = df.iloc[i]
            prev = df.iloc[i - 1]
            if not in_trade:
                if row["rsi"] < 40 and prev["macd"] < prev["macd_signal"] and row["macd"] > row["macd_signal"] and row["adx"] > 20:
                    buy_p    = round(row["Close"], 2)
                    atr      = row["atr"]
                    stop     = round(buy_p - atr * 1.5, 2)  # نفس generate_signal
                    target   = round(buy_p + atr * 3,   2)  # R:R = 2.0
                    in_trade = True
            else:
                price = row["Close"]
                if price >= target:
                    trades.append({"result": "win",  "profit": round(target - buy_p, 2)})
                    in_trade = False
                elif price <= stop:
                    trades.append({"result": "loss", "profit": round(stop - buy_p, 2)})
                    in_trade = False

        if not trades:
            send_telegram(f"📊 *Backtest {ticker}*\nما في إشارات في آخر 3 أشهر.", chat_id)
            return

        wins     = [t for t in trades if t["result"] == "win"]
        losses   = [t for t in trades if t["result"] == "loss"]
        win_rate = round(len(wins) / len(trades) * 100)
        net      = round(sum(t["profit"] for t in trades), 2)
        icon     = "💵" if net >= 0 else "📉"

        send_telegram(
            f"🔬 *Backtest {ticker} — آخر 3 أشهر*\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"📋 الصفقات: {len(trades)}\n"
            f"✅ رابحة: {len(wins)} ({win_rate}%)\n"
            f"❌ خاسرة: {len(losses)}\n"
            f"{icon} *صافي: {net:+.2f}$ للسهم*\n\n"
            f"⚠️ هذا اختبار تاريخي — لا يضمن المستقبل",
            chat_id
        )
    except Exception as e:
        send_telegram(f"❌ فشل الـ Backtest: {e}", chat_id)

# ══════════════════════════════════════════════
# تحليل القطاعات
# ══════════════════════════════════════════════
def analyze_sectors(chat_id=None):
    send_telegram("📊 جاري تحليل القطاعات...", chat_id)
    results = []
    for sector, tickers in SECTORS.items():
        gains = []
        for ticker in tickers[:5]:
            try:
                time.sleep(0.3)
                info = yf.Ticker(ticker).fast_info
                if info.last_price and info.previous_close:
                    gains.append((info.last_price - info.previous_close) / info.previous_close * 100)
            except:
                pass
        if gains:
            results.append((sector, round(sum(gains) / len(gains), 2)))

    if not results:
        send_telegram("❌ تعذر جلب بيانات القطاعات", chat_id)
        return

    results.sort(key=lambda x: x[1], reverse=True)
    lines = ["📊 *أداء القطاعات اليوم*", "━━━━━━━━━━━━━━━━━━━"]
    for sector, avg in results:
        icon = "🟢" if avg > 0 else "🔴"
        bar  = "▓" * min(abs(int(avg * 2)), 10)
        lines.append(f"{icon} {sector}: {avg:+.2f}% {bar}")
    lines += ["━━━━━━━━━━━━━━━━━━━", f"🏆 الأقوى: {results[0][0]}", f"📉 الأضعف: {results[-1][0]}"]
    send_telegram("\n".join(lines), chat_id)

# ══════════════════════════════════════════════
# أخبار المحفظة
# ══════════════════════════════════════════════
def check_portfolio_news():
    data = load_data()
    if not data["portfolio"]:
        return
    tickers  = list({t["ticker"] for t in data["portfolio"].values()})
    now_ts   = time.time()
    six_hrs  = 6 * 3600
    for ticker in tickers:
        try:
            for item in yf.Ticker(ticker).news[:3]:
                content  = item.get("content", {})
                title    = content.get("title", "")
                url      = content.get("canonicalUrl", {}).get("url", "")
                pub_date = content.get("pubDate", "")
                try:
                    pub_ts = datetime.fromisoformat(pub_date.replace("Z", "+00:00")).timestamp()
                    if now_ts - pub_ts > six_hrs:
                        continue
                except:
                    pass
                if title:
                    send_telegram(
                        f"📰 *خبر — {ticker}*\n"
                        f"━━━━━━━━━━━━━━━━━━━\n"
                        f"{title}\n"
                        f"{'🔗 ' + url if url else ''}"
                    )
                    break
        except:
            pass

def get_stock_news(ticker, chat_id=None):
    try:
        news = yf.Ticker(ticker).news
        if not news:
            send_telegram(f"📰 ما في أخبار حديثة لـ {ticker}", chat_id)
            return
        lines = [f"📰 *أحدث أخبار {ticker}*", "━━━━━━━━━━━━━━━━━━━"]
        for item in news[:5]:
            content = item.get("content", {})
            title   = content.get("title", "")
            url     = content.get("canonicalUrl", {}).get("url", "")
            if title:
                lines.append(f"• {title}")
                if url:
                    lines.append(f"  🔗 {url}")
        send_telegram("\n".join(lines), chat_id)
    except:
        send_telegram(f"❌ تعذر جلب أخبار {ticker}", chat_id)

# ══════════════════════════════════════════════
# معالجة الأوامر
# ══════════════════════════════════════════════
def process_command(msg, chat_id):
    data = load_data()
    msg  = msg.strip()

    # /اشتريت
    if msg.startswith("/اشتريت"):
        parts = msg.split()
        if len(parts) >= 2:
            num = ''.join(filter(str.isdigit, parts[1]))
            if num in data["signals"]:
                if num in data["portfolio"]:
                    send_telegram(f"⚠️ الصفقة {num} مسجلة مسبقاً.", chat_id)
                    return
                signal = data["signals"][num]
                try:
                    current_price = round(yf.Ticker(signal["ticker"]).fast_info.last_price, 2)
                except:
                    current_price = signal["price"]
                shares          = calc_position_size(current_price, signal["stop_loss"], data["capital"], data["risk_pct"])
                expected_profit = calc_expected_profit(current_price, signal["target"], shares)
                max_loss        = round(abs(current_price - signal["stop_loss"]) * shares, 2)
                data["portfolio"][num] = {
                    "ticker":    signal["ticker"],
                    "shares":    shares,
                    "buy_price": current_price,
                    "stop_loss": signal["stop_loss"],
                    "target":    signal["target"],
                    "mode":      signal["mode"],
                    "date":      datetime.now().strftime("%Y-%m-%d %H:%M"),
                }
                save_data(data)
                send_telegram(
                    f"✅ *تم التسجيل!*\n"
                    f"━━━━━━━━━━━━━━━━━━━\n"
                    f"📊 {signal['ticker']} — {shares} سهم\n"
                    f"💰 سعر الشراء: ${current_price}\n"
                    f"💵 الاستثمار: ${round(current_price * shares, 2):,}\n"
                    f"🔴 أقصى خسارة: ${max_loss}\n"
                    f"🎯 الربح المتوقع: +${expected_profit}",
                    chat_id
                )
                return
        send_telegram("❌ مثال: /اشتريت 1", chat_id)

    # /بعت
    elif msg.startswith("/بعت"):
        parts = msg.split()
        if len(parts) >= 2:
            if parts[1] == "كل":
                total_profit = 0
                failed       = []
                today_date   = datetime.now().strftime("%Y-%m-%d")
                for num, trade in list(data["portfolio"].items()):
                    try:
                        price  = round(yf.Ticker(trade["ticker"]).fast_info.last_price, 2)
                        profit = round((price - trade["buy_price"]) * trade["shares"], 2)
                        total_profit += profit
                        data["history"].append({**trade, "sell_price": price, "profit": profit, "date_sell": today_date})
                        del data["portfolio"][num]
                    except Exception as e:
                        failed.append(trade["ticker"])
                save_data(data)
                icon = "💵" if total_profit >= 0 else "📉"
                reply = f"✅ تم بيع الكل\n{icon} إجمالي: ${total_profit:+.2f}"
                if failed:
                    reply += f"\n⚠️ تعذر بيع: {', '.join(failed)}"
                send_telegram(reply, chat_id)
                return

            num = ''.join(filter(str.isdigit, parts[1]))
            if num in data["portfolio"]:
                trade = data["portfolio"][num]
                try:
                    price  = round(yf.Ticker(trade["ticker"]).fast_info.last_price, 2)
                    profit = round((price - trade["buy_price"]) * trade["shares"], 2)
                    pct    = round((price - trade["buy_price"]) / trade["buy_price"] * 100, 2)
                    icon   = "💵 ربحت" if profit >= 0 else "📉 خسرت"
                    data["history"].append({**trade, "sell_price": price, "profit": profit, "date_sell": datetime.now().strftime("%Y-%m-%d")})
                    del data["portfolio"][num]
                    # تحديث رأس المال تلقائياً
                    data["capital"] = round(data["capital"] + profit, 2)
                    save_data(data)
                    send_telegram(
                        f"✅ *تم تسجيل البيع!*\n"
                        f"━━━━━━━━━━━━━━━━━━━\n"
                        f"📊 {trade['ticker']}\n"
                        f"💰 سعر البيع: ${price}\n"
                        f"💰 سعر الشراء: ${trade['buy_price']}\n"
                        f"{icon}: ${abs(profit)} ({pct:+.2f}%)",
                        chat_id
                    )
                except:
                    send_telegram("❌ تعذر جلب السعر الحالي", chat_id)
                return
        send_telegram("❌ مثال: /بعت 1 أو /بعت كل", chat_id)

    # /محفظتي
    elif "/محفظتي" in msg:
        if not data["portfolio"]:
            send_telegram("📊 محفظتك فارغة حالياً", chat_id)
            return
        lines        = ["━━━━━━━━━━━━━━━━━━━\n📊 *محفظتك الآن*"]
        total_profit = 0
        for num, trade in data["portfolio"].items():
            try:
                price  = round(yf.Ticker(trade["ticker"]).fast_info.last_price, 2)
                profit = round((price - trade["buy_price"]) * trade["shares"], 2)
                pct    = round((price - trade["buy_price"]) / trade["buy_price"] * 100, 2)
                icon   = "🟢" if profit >= 0 else "🔴"
                total_profit += profit
                lines.append(
                    f"{num}⃣ *{trade['ticker']}* — {trade['shares']} سهم\n"
                    f"  شراء: ${trade['buy_price']} | الآن: ${price}\n"
                    f"  {icon} ${profit:+.2f} ({pct:+.2f}%)"
                )
            except:
                lines.append(f"{num}⃣ {trade['ticker']} — تعذر جلب السعر")
        icon = "🟢" if total_profit >= 0 else "🔴"
        lines.append(f"\n━━━━━━━━━━━━━━━━━━━\n{icon} الإجمالي: ${total_profit:+.2f}")
        send_telegram("\n".join(lines), chat_id)

    # /ربحي
    elif "/ربحي" in msg:
        if not data["portfolio"]:
            send_telegram("📊 ما عندك صفقات مفتوحة", chat_id)
            return
        total = 0
        for trade in data["portfolio"].values():
            try:
                total += (yf.Ticker(trade["ticker"]).fast_info.last_price - trade["buy_price"]) * trade["shares"]
            except:
                pass
        total = round(total, 2)
        icon  = "💵" if total >= 0 else "📉"
        send_telegram(f"{icon} إجمالي ربحك الآن: ${total:+.2f}", chat_id)

    # /capital
    elif msg.startswith("/capital"):
        parts = msg.split()
        if len(parts) >= 2:
            try:
                data["capital"] = float(parts[1])
                save_data(data)
                send_telegram(f"✅ رأس المال: ${data['capital']:,}", chat_id)
            except:
                send_telegram("❌ مثال: /capital 10000", chat_id)
        else:
            send_telegram(f"💼 رأس مالك: ${data['capital']:,}", chat_id)

    # /risk
    elif msg.startswith("/risk"):
        parts = msg.split()
        if len(parts) >= 2:
            try:
                new_risk = float(parts[1])
                if 0.1 <= new_risk <= 5:
                    data["risk_pct"] = new_risk
                    save_data(data)
                    send_telegram(f"✅ نسبة المخاطرة: {new_risk}%", chat_id)
                else:
                    send_telegram("❌ النسبة بين 0.1 و 5", chat_id)
            except:
                send_telegram("❌ مثال: /risk 1", chat_id)
        else:
            send_telegram(f"⚡ نسبة المخاطرة: {data['risk_pct']}%", chat_id)

    # /السوق
    elif "/السوق" in msg:
        try:
            spy = round(yf.Ticker("SPY").fast_info.last_price, 2)
            qqq = round(yf.Ticker("QQQ").fast_info.last_price, 2)
            send_telegram(
                f"📊 *السوق الآن*\n"
                f"━━━━━━━━━━━━━━━━━━━\n"
                f"🇺🇸 S&P 500 (SPY): ${spy}\n"
                f"💻 Nasdaq (QQQ): ${qqq}\n"
                f"⏰ {'🟢 مفتوح' if is_market_open() else '🔴 مغلق'}",
                chat_id
            )
        except:
            send_telegram("❌ تعذر جلب بيانات السوق", chat_id)

    # /قطاعات
    elif "/قطاعات" in msg:
        if not is_market_open():
            send_telegram("🔴 السوق مقفل — جرب خلال ساعات التداول", chat_id)
        else:
            threading.Thread(target=analyze_sectors, args=(chat_id,)).start()

    # /حلل_الكل
    elif "/حلل_الكل" in msg:
        if not is_market_open():
            send_telegram("🔴 السوق مقفل\n⏰ يفتح 9:30 صباحاً نيويورك (4:30 مساءً السعودية)", chat_id)
        else:
            send_telegram("🔍 جاري تحليل السوق الآن...", chat_id)
            threading.Thread(target=analyze_all).start()

    # /حلل
    elif msg.startswith("/حلل"):
        parts = msg.split()
        if len(parts) >= 2:
            ticker = parts[1].upper()
            try:
                last, prev, price_info, df = fetch_and_analyze(ticker)
                if last is not None:
                    signal = generate_signal(ticker, last, prev, price_info, "investment")
                    rec    = signal["action"] if signal else "🟡 انتظر"
                    conf   = f" — ثقة {signal['confidence']}%" if signal else ""
                    send_telegram(
                        f"📊 *{ticker}*\n"
                        f"━━━━━━━━━━━━━━━━━━━\n"
                        f"💰 السعر: ${price_info['price']}\n"
                        f"📈 التغيير: {price_info['change_pct']:+.2f}%\n"
                        f"━━━━━━━━━━━━━━━━━━━\n"
                        f"📊 RSI: {last['rsi']:.1f}\n"
                        f"📊 MACD: {last['macd']:.4f}\n"
                        f"📊 Bollinger: {last['bb_pct']:.0%}\n"
                        f"📊 ADX: {last['adx']:.1f} ({'قوي' if last['adx'] > 25 else 'ضعيف'})\n"
                        f"━━━━━━━━━━━━━━━━━━━\n"
                        f"🎯 التوصية: {rec}{conf}",
                        chat_id
                    )
                else:
                    send_telegram(f"❌ تعذر تحليل {ticker}", chat_id)
            except:
                send_telegram("❌ تعذر جلب البيانات", chat_id)
        else:
            send_telegram("❌ مثال: /حلل AAPL", chat_id)

    # /اختبر
    elif msg.startswith("/اختبر"):
        parts = msg.split()
        if len(parts) >= 2:
            threading.Thread(target=backtest_ticker, args=(parts[1].upper(), chat_id)).start()
        else:
            send_telegram("❌ مثال: /اختبر AAPL", chat_id)

    # /أخبار
    elif msg.startswith("/أخبار"):
        parts = msg.split()
        if len(parts) >= 2:
            threading.Thread(target=get_stock_news, args=(parts[1].upper(), chat_id)).start()
        else:
            send_telegram("❌ مثال: /أخبار AAPL", chat_id)

    # /أداء
    elif "/أداء" in msg:
        bot_performance(chat_id)

    # /اسبوع
    elif "/اسبوع" in msg:
        weekly_summary()

    # /مساعدة
    elif "/مساعدة" in msg or "/start" in msg or "/help" in msg:
        send_telegram(
            "📋 *الأوامر المتاحة:*\n"
            "━━━━━━━━━━━━━━━━━━━\n"
            "• /اشتريت 1 — تسجيل صفقة\n"
            "• /بعت 1 — إغلاق صفقة\n"
            "• /بعت كل — إغلاق الكل\n"
            "• /محفظتي — عرض محفظتك\n"
            "• /ربحي — إجمالي الربح\n"
            "• /حلل AAPL — تحليل سهم\n"
            "• /حلل_الكل — تحليل السوق الآن\n"
            "• /قطاعات — أداء القطاعات\n"
            "• /اختبر AAPL — Backtest سهم\n"
            "• /أخبار AAPL — أخبار سهم\n"
            "• /أداء — إحصائيات البوت\n"
            "• /السوق — حالة السوق\n"
            "• /capital 10000 — تعديل رأس المال\n"
            "• /risk 1 — تعديل نسبة المخاطرة\n"
            "• /اسبوع — ملخص الأسبوع",
            chat_id
        )

# ══════════════════════════════════════════════
# استقبال الأوامر
# ══════════════════════════════════════════════
def check_telegram_updates():
    data    = load_data()
    last_id = data.get("last_update_id", 0)
    try:
        r = requests.get(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates",
            params={"offset": last_id + 1, "timeout": 5},
            timeout=8,
        )
        updates = r.json().get("result", [])
        changed = False
        for update in updates:
            last_id = update["update_id"]
            changed = True
            msg     = update.get("message", {})
            text    = msg.get("text", "")
            chat_id = str(msg.get("chat", {}).get("id", ""))
            if text and chat_id:
                logger.info(f"أمر: {text}")
                process_command(text, chat_id)
        if changed:
            data = load_data()
            data["last_update_id"] = last_id
            save_data(data)
    except Exception as e:
        logger.error(f"خطأ في getUpdates: {e}")

# ══════════════════════════════════════════════
# Ping Server
# ══════════════════════════════════════════════
class PingHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is running!")
    def log_message(self, format, *args):
        pass

def run_ping_server():
    port   = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), PingHandler)
    logger.info(f"Ping server on port {port}")
    server.serve_forever()

def self_ping():
    try:
        requests.get(RENDER_URL, timeout=8)
        logger.info("Self-ping ✅")
    except Exception as e:
        logger.warning(f"Self-ping فشل: {e}")

# ══════════════════════════════════════════════
# التشغيل
# ══════════════════════════════════════════════
if __name__ == "__main__":
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        logger.error("TELEGRAM_TOKEN أو TELEGRAM_CHAT_ID غير موجود!")
        exit(1)

    logger.info("🚀 بوت الأسهم الذكي يعمل!")

    threading.Thread(target=run_ping_server, daemon=True).start()

    send_telegram(
        "🚀 *بوت الأسهم الذكي شغال!*\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        "8 مؤشرات فنية + إدارة رأس المال\n\n"
        "أرسل /مساعدة لقائمة الأوامر"
    )

    scheduler = BlockingScheduler(timezone="America/New_York")

    scheduler.add_job(analyze_all,            CronTrigger(hour=9,  minute=35, day_of_week="mon-fri", timezone="America/New_York"))
    scheduler.add_job(morning_briefing,       CronTrigger(hour=9,         minute=0,  day_of_week="mon-fri", timezone="America/New_York"))
    scheduler.add_job(end_of_day,             CronTrigger(hour=16,        minute=5,  day_of_week="mon-fri", timezone="America/New_York"))
    scheduler.add_job(weekly_summary,         CronTrigger(hour=16,        minute=30, day_of_week="fri",     timezone="America/New_York"))
    scheduler.add_job(check_portfolio_news,   CronTrigger(hour="10,14",   minute=0,  day_of_week="mon-fri", timezone="America/New_York"))
    scheduler.add_job(check_portfolio,        "interval", minutes=1)
    scheduler.add_job(check_telegram_updates, "interval", seconds=10, max_instances=1, coalesce=True)
    scheduler.add_job(self_ping,              "interval", minutes=5)

    try:
        scheduler.start()
    except KeyboardInterrupt:
        logger.info("تم الإيقاف")
