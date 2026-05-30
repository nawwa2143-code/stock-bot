#!/usr/bin/env python3
"""
بوت تحليل الأسهم الأمريكية مع إشعارات واتساب
ملف واحد - كل شيء هنا
"""

# ══════════════════════════════════════════════
#   ضع بياناتك هنا فقط
# ══════════════════════════════════════════════

TWILIO_ACCOUNT_SID = "ACfc1ed0aa9e92fec6115e44d4c5fa471f"   # من Twilio Dashboard
TWILIO_AUTH_TOKEN  = "537c2d84e0019a4bedaf00a6e48e6a8e"                # من Twilio Dashboard
TWILIO_FROM        = "whatsapp:+14155238886"               # رقم Twilio (لا تغيره)
YOUR_WHATSAPP      = "whatsapp:+966594296964"               # رقمك مع رمز الدولة

WATCHLIST = ["AAPL", "MSFT", "NVDA", "GOOGL", "TSLA", "AMZN", "META"]

RSI_OVERSOLD          = 35     # شراء إذا RSI أقل من هذا
RSI_OVERBOUGHT        = 65     # بيع إذا RSI أكبر من هذا
MIN_CONFIDENCE        = 60     # لا ترسل إشعاراً إلا إذا الثقة >= هذه النسبة %
CHECK_INTERVAL_MINUTES = 5     # فحص كل كم دقيقة

# ══════════════════════════════════════════════
#   الكود - لا تعدل هنا
# ══════════════════════════════════════════════

import sys
import logging
from datetime import datetime

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)
logger = logging.getLogger(__name__)

def install_deps():
    import subprocess
    pkgs = ["yfinance", "pandas", "numpy", "ta", "twilio", "apscheduler", "pytz"]
    for pkg in pkgs:
        try:
            __import__(pkg.replace("-","_"))
        except ImportError:
            logger.info(f"تثبيت {pkg}...")
            subprocess.check_call([sys.executable, "-m", "pip", "install", pkg, "-q"])

install_deps()

import yfinance as yf
import pandas as pd
import ta
import pytz
from twilio.rest import Client
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger


# ── جلب بيانات السهم ──────────────────────────────────────────
def fetch_data(ticker):
    try:
        df = yf.Ticker(ticker).history(period="3mo", interval="1d")
        if df.empty:
            return None, None
        info = yf.Ticker(ticker).fast_info
        price_info = {
            "ticker":     ticker,
            "price":      round(info.last_price, 2),
            "change_pct": round((info.last_price - info.previous_close) / info.previous_close * 100, 2),
        }
        return df, price_info
    except Exception as e:
        logger.error(f"خطأ في جلب {ticker}: {e}")
        return None, None


# ── المؤشرات الفنية ───────────────────────────────────────────
def add_indicators(df):
    c = df["Close"]
    df["rsi"]         = ta.momentum.RSIIndicator(c, window=14).rsi()
    macd              = ta.trend.MACD(c)
    df["macd"]        = macd.macd()
    df["macd_signal"] = macd.macd_signal()
    bb                = ta.volatility.BollingerBands(c)
    df["bb_pct"]      = bb.bollinger_pband()
    df["ema9"]        = ta.trend.EMAIndicator(c, window=9).ema_indicator()
    df["ema21"]       = ta.trend.EMAIndicator(c, window=21).ema_indicator()
    df["vol_ratio"]   = df["Volume"] / df["Volume"].rolling(20).mean()
    return df.dropna()


# ── توليد الإشارة ─────────────────────────────────────────────
def generate_signal(ticker, df, price_info):
    last, prev = df.iloc[-1], df.iloc[-2]
    buy, sell, max_pts = 0, 0, 100
    reasons_buy, reasons_sell = [], []

    # RSI (25 نقطة)
    rsi = last["rsi"]
    if rsi < RSI_OVERSOLD:
        buy += 25
        reasons_buy.append(f"RSI={rsi:.0f} (مبالغ في بيعه)")
    elif rsi > RSI_OVERBOUGHT:
        sell += 25
        reasons_sell.append(f"RSI={rsi:.0f} (مبالغ في شرائه)")

    # MACD Cross (25 نقطة)
    if prev["macd"] < prev["macd_signal"] and last["macd"] > last["macd_signal"]:
        buy += 25
        reasons_buy.append("تقاطع MACD صعودي ✅")
    elif prev["macd"] > prev["macd_signal"] and last["macd"] < last["macd_signal"]:
        sell += 25
        reasons_sell.append("تقاطع MACD هبوطي ❌")

    # Bollinger Bands (20 نقطة)
    if last["bb_pct"] < 0.05:
        buy += 20
        reasons_buy.append("السعر لمس الحزام السفلي (BB)")
    elif last["bb_pct"] > 0.95:
        sell += 20
        reasons_sell.append("السعر لمس الحزام العلوي (BB)")

    # EMA Cross (20 نقطة)
    if prev["ema9"] < prev["ema21"] and last["ema9"] > last["ema21"]:
        buy += 20
        reasons_buy.append("EMA9 تجاوزت EMA21 للأعلى")
    elif prev["ema9"] > prev["ema21"] and last["ema9"] < last["ema21"]:
        sell += 20
        reasons_sell.append("EMA9 تجاوزت EMA21 للأسفل")

    # حجم التداول (10 نقاط - يؤكد الإشارة)
    if last["vol_ratio"] > 1.5:
        if buy > sell:
            buy += 10
            reasons_buy.append(f"حجم مرتفع ({last['vol_ratio']:.1f}x)")
        elif sell > buy:
            sell += 10
            reasons_sell.append(f"حجم مرتفع ({last['vol_ratio']:.1f}x)")

    # ── حساب وقف الخسارة والهدف بناءً على ATR (تذبذب السهم الحقيقي) ──
    atr         = last["atr"]
    price       = price_info["price"]
    atr_pct     = round(atr / price * 100, 1)
    # وقف الخسارة = 2× ATR تحت سعر الشراء (يتجنب التذبذب الطبيعي)
    stop_loss   = round(price - (atr * 2), 2)
    # الهدف = 3× ATR فوق سعر الشراء (نسبة ربح/خسارة 1.5:1)
    target      = round(price + (atr * 3), 2)
    stop_pct    = round((price - stop_loss) / price * 100, 1)
    target_pct  = round((target - price) / price * 100, 1)

    # القرار النهائي
    if buy > sell:
        conf = round(buy / max_pts * 100)
        if conf < MIN_CONFIDENCE:
            return None
        return {
            "ticker":     ticker,
            "action":     "شراء 🟢",
            "action_en":  "BUY",
            "price":      price,
            "change_pct": price_info["change_pct"],
            "confidence": conf,
            "reasons":    reasons_buy,
            "stop_loss":  stop_loss,
            "stop_pct":   stop_pct,
            "target":     target,
            "target_pct": target_pct,
            "atr_pct":    atr_pct,
        }
    if sell > buy:
        conf = round(sell / max_pts * 100)
        if conf < MIN_CONFIDENCE:
            return None
        return {
            "ticker":     ticker,
            "action":     "بيع 🔴",
            "action_en":  "SELL",
            "price":      price,
            "change_pct": price_info["change_pct"],
            "confidence": conf,
            "reasons":    reasons_sell,
            "stop_loss":  None,
            "stop_pct":   None,
            "target":     None,
            "target_pct": None,
            "atr_pct":    atr_pct,
        }
    return None


# ── إرسال واتساب ──────────────────────────────────────────────
def send_whatsapp(message):
    try:
        client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        msg = client.messages.create(body=message, from_=TWILIO_FROM, to=YOUR_WHATSAPP)
        logger.info(f"✅ رسالة أُرسلت: {msg.sid}")
        return True
    except Exception as e:
        logger.error(f"❌ فشل الإرسال: {e}")
        return False

def send_signal(signal):
    lines = [
        "━━━━━━━━━━━━━━━━━━━",
        "📊 *توصية بوت الأسهم*",
        "━━━━━━━━━━━━━━━━━━━",
        f"🏷️ السهم: *{signal['ticker']}*",
        f"📌 التوصية: *{signal['action']}*",
        f"💵 السعر: *${signal['price']}*",
        f"📈 التغيير اليوم: {signal['change_pct']:+.2f}%",
        f"📊 تذبذب السهم اليومي: {signal['atr_pct']}%",
        f"🎯 الثقة: *{signal['confidence']}%*",
        "",
        "📋 *الأسباب:*",
    ]
    for r in signal["reasons"]:
        lines.append(f"  • {r}")
    if signal.get("stop_loss"):
        lines += [
            "",
            f"🛑 وقف الخسارة: *${signal['stop_loss']}* ({signal['stop_pct']}% تحت السعر)",
            f"🎯 الهدف: *${signal['target']}* ({signal['target_pct']}% فوق السعر)",
            "",
            f"💡 وقف الخسارة محسوب بناءً على تذبذب السهم الحقيقي",
        ]
    lines += ["", "⚠️ هذه ليست نصيحة مالية."]
    return send_whatsapp("\n".join(lines))

def send_daily_summary(signals):
    buys  = [s for s in signals if s["action_en"] == "BUY"]
    sells = [s for s in signals if s["action_en"] == "SELL"]
    lines = [
        "━━━━━━━━━━━━━━━━━━━",
        "📊 *ملخص اليوم*",
        "━━━━━━━━━━━━━━━━━━━",
        f"🟢 إشارات شراء: {len(buys)}",
        f"🔴 إشارات بيع: {len(sells)}",
        "",
    ]
    if buys:
        lines.append("*أفضل شراء:*")
        for s in sorted(buys, key=lambda x: x["confidence"], reverse=True)[:3]:
            lines.append(f"  • {s['ticker']} ${s['price']} — ثقة {s['confidence']}%")
    if sells:
        lines.append("\n*توصيات البيع:*")
        for s in sorted(sells, key=lambda x: x["confidence"], reverse=True)[:3]:
            lines.append(f"  • {s['ticker']} ${s['price']} — ثقة {s['confidence']}%")
    send_whatsapp("\n".join(lines))


# ── الدالة الرئيسية ───────────────────────────────────────────
sent_today   = {}   # {ticker: action_en} لتجنب التكرار
daily_signals = []

def is_market_open():
    now = datetime.now(pytz.timezone("America/New_York"))
    if now.weekday() >= 5:
        return False
    open_t  = now.replace(hour=9,  minute=30, second=0, microsecond=0)
    close_t = now.replace(hour=16, minute=0,  second=0, microsecond=0)
    return open_t <= now <= close_t

def analyze_all():
    if not is_market_open():
        logger.info("السوق مغلق ⏸")
        return
    logger.info(f"🔍 فحص {len(WATCHLIST)} سهم...")
    for ticker in WATCHLIST:
        try:
            df, price_info = fetch_data(ticker)
            if df is None:
                continue
            df = add_indicators(df)
            signal = generate_signal(ticker, df, price_info)
            if signal is None:
                logger.info(f"  {ticker}: لا توجد إشارة")
                continue
            logger.info(f"  {ticker}: {signal['action']} (ثقة {signal['confidence']}%)")
            if sent_today.get(ticker) == signal["action_en"]:
                logger.info(f"  {ticker}: أُرسلت مسبقاً، تخطي")
                continue
            if send_signal(signal):
                sent_today[ticker] = signal["action_en"]
                daily_signals.append(signal)
        except Exception as e:
            logger.error(f"خطأ في {ticker}: {e}")

def end_of_day():
    global sent_today, daily_signals
    if daily_signals:
        send_daily_summary(daily_signals)
    sent_today    = {}
    daily_signals = []
    logger.info("✅ تم إرسال ملخص اليوم وتصفير السجلات")


# ── التشغيل ───────────────────────────────────────────────────
if __name__ == "__main__":
    logger.info("🚀 بوت الأسهم يعمل — اضغط Ctrl+C للإيقاف")
    analyze_all()   # تشغيل فوري عند البدء

    scheduler = BlockingScheduler(timezone=pytz.timezone("America/New_York"))
    scheduler.add_job(analyze_all, "interval", minutes=CHECK_INTERVAL_MINUTES)
    scheduler.add_job(end_of_day, CronTrigger(hour=16, minute=5, day_of_week="mon-fri"))

    try:
        scheduler.start()
    except KeyboardInterrupt:
        logger.info("👋 تم الإيقاف")
