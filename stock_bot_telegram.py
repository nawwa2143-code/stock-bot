#!/usr/bin/env python3
"""
بوت الأسهم الذكي - تلغرام
- 8 مؤشرات فنية (RSI, MACD, BB, EMA, VWAP, ADX, OBV, حجم)
- إدارة رأس المال
- تتبع المحفظة
- ملخص أسبوعي
- المدة والربح المتوقع
"""

import os, json, logging
from datetime import datetime, timedelta

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════
# الإعدادات - من متغيرات البيئة
# ══════════════════════════════════════════════
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
MIN_CONFIDENCE = 60
CHECK_INTERVAL_MINUTES = 5

# ══════════════════════════════════════════════
# المكتبات
# ══════════════════════════════════════════════
import yfinance as yf
import pandas as pd
import numpy as np
import ta
import pytz
import requests
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

# ══════════════════════════════════════════════
# قوائم الأسهم الاحتياطية
# ══════════════════════════════════════════════
SP500_BACKUP = [
    "AAPL","MSFT","NVDA","GOOGL","AMZN","META","TSLA","JPM","JNJ",
    "V","PG","UNH","HD","MA","MRK","ABBV","PFE","KO","PEP","BAC",
    "DIS","CSCO","ADBE","CRM","NFLX","AMD","QCOM","TXN","AVGO",
    "COST","NKE","MCD","GE","BA","CAT","IBM","ORCL","PYPL","UBER",
    "XOM","CVX","COP","WFC","GS","MS","LLY","AMGN","GILD"
]

SPECULATIVE_BACKUP = [
    "SOUN","AMC","GME","SPCE","MVIS","SNDL","ACB","CGC","TLRY",
    "NOK","BB","SOFI","OPEN","PLTR","NIO","XPEV","RIVN","HOOD",
    "COIN","BBAI","APLD","CTIC","VXRT","OCGN"
]

TODAY_INVESTMENT = []
TODAY_SPECULATIVE = []

# ══════════════════════════════════════════════
# ملف البيانات
# ══════════════════════════════════════════════
DATA_FILE = "bot_data.json"

def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {
        "portfolio":      {},
        "signals":        {},
        "signal_counter": 0,
        "history":        [],
        "waiting_input":  None,
        "capital":        10000,
        "risk_pct":       1.0,
        "weekly_signals": [],
    }

def save_data(data):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

# ══════════════════════════════════════════════
# إرسال تلغرام
# ══════════════════════════════════════════════
def send_telegram(message, chat_id=None):
    cid = chat_id or TELEGRAM_CHAT_ID
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        r = requests.post(url, json={
            "chat_id": cid,
            "text": message,
            "parse_mode": "Markdown"
        }, timeout=10)
        if r.status_code == 200:
            logger.info("✅ رسالة أُرسلت")
        else:
            logger.error(f"❌ خطأ: {r.text}")
    except Exception as e:
        logger.error(f"❌ فشل الإرسال: {e}")

# ══════════════════════════════════════════════
# جلب الأسهم التلقائي
# ══════════════════════════════════════════════
def get_smart_investment_list():
    logger.info("🔍 جلب أسهم الاستثمار...")
    headers = {"User-Agent": "Mozilla/5.0"}
    tickers = []
    for scr_id in ["day_gainers", "most_actives", "growth_technology_stocks"]:
        try:
            url = f"https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved?scrIds={scr_id}&count=30"
            r = requests.get(url, headers=headers, timeout=10)
            quotes = r.json()["finance"]["result"][0]["quotes"]
            found = [q["symbol"] for q in quotes
                     if q.get("regularMarketPrice", 0) > 10
                     and q.get("averageDailyVolume3Month", 0) > 1_000_000]
            tickers.extend(found)
        except:
            pass
    tickers = list(dict.fromkeys(tickers))
    return tickers[:30] if tickers else SP500_BACKUP[:30]

def get_smart_speculative_list():
    logger.info("🔍 جلب أسهم المضاربة...")
    headers = {"User-Agent": "Mozilla/5.0"}
    tickers = []
    for scr_id in ["most_actives", "day_gainers"]:
        try:
            url = f"https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved?scrIds={scr_id}&count=50"
            r = requests.get(url, headers=headers, timeout=10)
            quotes = r.json()["finance"]["result"][0]["quotes"]
            found = [q["symbol"] for q in quotes
                     if 1 <= q.get("regularMarketPrice", 0) <= 20]
            tickers.extend(found)
        except:
            pass
    tickers = list(dict.fromkeys(tickers))
    return tickers[:20] if tickers else SPECULATIVE_BACKUP[:20]

# ══════════════════════════════════════════════
# التحليل الفني - 8 مؤشرات
# ══════════════════════════════════════════════
def fetch_and_analyze(ticker):
    try:
        stock = yf.Ticker(ticker)
        df = stock.history(period="3mo", interval="1d")
        if df.empty or len(df) < 30:
            return None, None, None, None

        info = stock.fast_info
        price = round(info.last_price, 2)
        prev_close = info.previous_close
        change_pct = round((price - prev_close) / prev_close * 100, 2)

        c = df["Close"]
        h = df["High"]
        l = df["Low"]
        v = df["Volume"]

        # المؤشرات الأصلية
        df["rsi"] = ta.momentum.RSIIndicator(c, window=14).rsi()
        macd_obj = ta.trend.MACD(c)
        df["macd"] = macd_obj.macd()
        df["macd_signal"] = macd_obj.macd_signal()
        bb = ta.volatility.BollingerBands(c)
        df["bb_pct"] = bb.bollinger_pband()
        df["ema9"] = ta.trend.EMAIndicator(c, window=9).ema_indicator()
        df["ema21"] = ta.trend.EMAIndicator(c, window=21).ema_indicator()
        df["atr"] = ta.volatility.AverageTrueRange(h, l, c).average_true_range()
        df["vol_ratio"] = v / v.rolling(20).mean()

        # المؤشرات الجديدة
        # VWAP (يومي تقريبي)
        df["vwap"] = (c * v).cumsum() / v.cumsum()

        # ADX
        df["adx"] = ta.trend.ADXIndicator(h, l, c, window=14).adx()

        # OBV
        df["obv"] = ta.volume.OnBalanceVolumeIndicator(c, v).on_balance_volume()
        df["obv_ema"] = df["obv"].ewm(span=20).mean()

        df = df.dropna()
        if len(df) < 2:
            return None, None, None, None

        last, prev = df.iloc[-1], df.iloc[-2]
        price_info = {"ticker": ticker, "price": price, "change_pct": change_pct}
        return last, prev, price_info, df

    except Exception as e:
        logger.warning(f"خطأ في {ticker}: {e}")
        return None, None, None, None


def generate_signal(ticker, last, prev, price_info, mode="investment"):
    buy, sell, max_pts = 0, 0, 140
    reasons_buy, reasons_sell = [], []

    rsi = last["rsi"]
    oversold  = 35 if mode == "investment" else 40
    overbought = 65 if mode == "investment" else 60
    price = price_info["price"]

    # ١. RSI (25 نقطة)
    if rsi < oversold:
        buy += 25
        reasons_buy.append(f"RSI={rsi:.0f} (مبالغ في بيعه)")
    elif rsi > overbought:
        sell += 25
        reasons_sell.append(f"RSI={rsi:.0f} (مبالغ في شرائه)")

    # ٢. MACD (25 نقطة)
    if prev["macd"] < prev["macd_signal"] and last["macd"] > last["macd_signal"]:
        buy += 25
        reasons_buy.append("تقاطع MACD صعودي ✅")
    elif prev["macd"] > prev["macd_signal"] and last["macd"] < last["macd_signal"]:
        sell += 25
        reasons_sell.append("تقاطع MACD هبوطي ❌")

    # ٣. Bollinger (20 نقطة)
    if last["bb_pct"] < 0.05:
        buy += 20
        reasons_buy.append("السعر لمس الحزام السفلي")
    elif last["bb_pct"] > 0.95:
        sell += 20
        reasons_sell.append("السعر لمس الحزام العلوي")

    # ٤. EMA (20 نقطة)
    if prev["ema9"] < prev["ema21"] and last["ema9"] > last["ema21"]:
        buy += 20
        reasons_buy.append("EMA9 تجاوزت EMA21 للأعلى")
    elif prev["ema9"] > prev["ema21"] and last["ema9"] < last["ema21"]:
        sell += 20
        reasons_sell.append("EMA9 تجاوزت EMA21 للأسفل")

    # ٥. VWAP (15 نقطة) - جديد
    if price > last["vwap"] * 1.005:
        buy += 15
        reasons_buy.append(f"السعر فوق VWAP ({last['vwap']:.2f})")
    elif price < last["vwap"] * 0.995:
        sell += 15
        reasons_sell.append(f"السعر تحت VWAP ({last['vwap']:.2f})")

    # ٦. ADX (15 نقطة) - جديد - فلتر قوة الاتجاه
    adx_strong = last["adx"] > 25
    if not adx_strong:
        # اتجاه ضعيف - نقلل الثقة
        buy = int(buy * 0.7)
        sell = int(sell * 0.7)
    else:
        if buy > sell:
            buy += 15
            reasons_buy.append(f"ADX={last['adx']:.0f} اتجاه قوي")
        elif sell > buy:
            sell += 15
            reasons_sell.append(f"ADX={last['adx']:.0f} اتجاه قوي")

    # ٧. OBV (10 نقطة) - جديد
    if last["obv"] > last["obv_ema"] and prev["obv"] <= prev["obv_ema"]:
        buy += 10
        reasons_buy.append("OBV: أموال تدخل السهم")
    elif last["obv"] < last["obv_ema"] and prev["obv"] >= prev["obv_ema"]:
        sell += 10
        reasons_sell.append("OBV: أموال تخرج من السهم")

    # ٨. حجم التداول (10 نقطة)
    if last["vol_ratio"] > 1.5:
        if buy > sell:
            buy += 10
            reasons_buy.append(f"حجم مرتفع ({last['vol_ratio']:.1f}x)")
        elif sell > buy:
            sell += 10
            reasons_sell.append(f"حجم مرتفع ({last['vol_ratio']:.1f}x)")

    # حساب وقف الخسارة والهدف
    atr = last["atr"]
    atr_pct = round(atr / price * 100, 1)

    if mode == "investment":
        stop_loss = round(price - (atr * 2), 2)
        target    = round(price + (atr * 3), 2)
        days_est  = max(3, round(abs(target - price) / atr * 1.5))
    else:
        stop_loss = round(price - (atr * 1.5), 2)
        target    = round(price + (atr * 4),   2)
        days_est  = max(1, round(abs(target - price) / atr))

    stop_pct   = round((price - stop_loss) / price * 100, 1)
    target_pct = round((target - price) / price * 100, 1)

    if buy > sell:
        conf = round(buy / max_pts * 100)
        if conf < MIN_CONFIDENCE:
            return None
        return {
            "ticker": ticker, "action": "🟢 شراء", "action_en": "BUY",
            "mode": mode, "price": price, "change_pct": price_info["change_pct"],
            "confidence": conf, "reasons": reasons_buy,
            "stop_loss": stop_loss, "stop_pct": stop_pct,
            "target": target, "target_pct": target_pct,
            "atr_pct": atr_pct, "days_est": days_est,
        }

    if sell > buy:
        conf = round(sell / max_pts * 100)
        if conf < MIN_CONFIDENCE:
            return None
        return {
            "ticker": ticker, "action": "🔴 بيع", "action_en": "SELL",
            "mode": mode, "price": price, "change_pct": price_info["change_pct"],
            "confidence": conf, "reasons": reasons_sell,
            "stop_loss": None, "stop_pct": None,
            "target": None, "target_pct": None,
            "atr_pct": atr_pct, "days_est": None,
        }

    return None

# ══════════════════════════════════════════════
# إدارة رأس المال
# ══════════════════════════════════════════════
def calc_position_size(price, stop_loss, capital, risk_pct):
    """يحسب كم سهم تشتري"""
    if not stop_loss or price <= stop_loss:
        return 0
    risk_amount = capital * (risk_pct / 100)
    loss_per_share = price - stop_loss
    shares = int(risk_amount / loss_per_share)
    return max(1, shares)

def calc_expected_profit(price, target, shares):
    if not target or not shares:
        return 0
    return round((target - price) * shares, 2)

# ══════════════════════════════════════════════
# تنسيق رسالة التوصية
# ══════════════════════════════════════════════
def format_signal_message(signal, number, capital, risk_pct):
    mode_icon = "🔵 استثمار" if signal["mode"] == "investment" else "🟡 مضاربة ⚡"

    # حساب حجم الصفقة
    shares = calc_position_size(signal["price"], signal["stop_loss"], capital, risk_pct)
    expected_profit = calc_expected_profit(signal["price"], signal["target"], shares)
    total_investment = round(signal["price"] * shares, 2)

    lines = [
        "━━━━━━━━━━━━━━━━━━━",
        f"{mode_icon}",
        f"{number}⃣ *{signal['ticker']}* — ${signal['price']}",
        f"🎯 {signal['action']} — ثقة {signal['confidence']}%",
        f"📈 التغيير: {signal['change_pct']:+.2f}%",
        f"📊 تذبذب يومي: {signal['atr_pct']}%",
        "",
        "📋 *الأسباب:*",
    ]

    for r in signal["reasons"]:
        lines.append(f"  • {r}")

    if signal.get("stop_loss"):
        lines += [
            "",
            f"🔴 *وقف الخسارة:* ${signal['stop_loss']} ({signal['stop_pct']}%)",
            f"🎯 *الهدف:* ${signal['target']} (+{signal['target_pct']}%)",
        ]

    if signal.get("days_est"):
        lines.append(f"⏱ *المدة المتوقعة:* {signal['days_est']} أيام")

    if shares > 0 and expected_profit > 0:
        lines += [
            "",
            f"💼 *بناءً على محفظتك (${capital:,}):*",
            f"  📦 الكمية المقترحة: {shares} سهم",
            f"  💵 إجمالي الاستثمار: ${total_investment:,}",
            f"  💰 الربح المتوقع: +${expected_profit}",
        ]

    if signal["mode"] == "speculative":
        lines.append("⚠️ مضاربة — خطر مرتفع")

    lines += ["", f"للتسجيل رد: /اشتريت {number}"]
    return "\n".join(lines)

# ══════════════════════════════════════════════
# السوق
# ══════════════════════════════════════════════
sent_today = {}

def is_market_open():
    now = datetime.now(pytz.timezone("America/New_York"))
    if now.weekday() >= 5:
        return False
    open_t  = now.replace(hour=9,  minute=30, second=0, microsecond=0)
    close_t = now.replace(hour=16, minute=0,  second=0, microsecond=0)
    return open_t <= now <= close_t

# ══════════════════════════════════════════════
# التحليل الرئيسي
# ══════════════════════════════════════════════
def analyze_all():
    global TODAY_INVESTMENT, TODAY_SPECULATIVE

    if not is_market_open():
        logger.info("🔒 السوق مغلق")
        return

    data = load_data()
    logger.info("🔍 تحليل الأسهم...")

    if not TODAY_INVESTMENT:
        TODAY_INVESTMENT = get_smart_investment_list()
    if not TODAY_SPECULATIVE:
        TODAY_SPECULATIVE = get_smart_speculative_list()

    investment_signals = []
    for ticker in TODAY_INVESTMENT[:30]:
        try:
            last, prev, price_info, df = fetch_and_analyze(ticker)
            if last is None or price_info["price"] < 20:
                continue
            signal = generate_signal(ticker, last, prev, price_info, "investment")
            if signal and sent_today.get(ticker) != signal["action_en"]:
                investment_signals.append(signal)
        except:
            pass

    speculative_signals = []
    for ticker in TODAY_SPECULATIVE[:20]:
        try:
            last, prev, price_info, df = fetch_and_analyze(ticker)
            if last is None or not (1 <= price_info["price"] <= 20):
                continue
            signal = generate_signal(ticker, last, prev, price_info, "speculative")
            if signal and sent_today.get(ticker) != signal["action_en"]:
                speculative_signals.append(signal)
        except:
            pass

    top_investment  = sorted(investment_signals,  key=lambda x: x["confidence"], reverse=True)[:3]
    top_speculative = sorted(speculative_signals, key=lambda x: x["confidence"], reverse=True)[:2]

    for signal in top_investment + top_speculative:
        data["signal_counter"] += 1
        num = data["signal_counter"]
        data["signals"][str(num)] = {
            **signal,
            "number":    num,
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "bought":    False,
            "result":    None,
        }
        data["weekly_signals"].append(str(num))
        msg = format_signal_message(signal, num, data["capital"], data["risk_pct"])
        send_telegram(msg)
        sent_today[signal["ticker"]] = signal["action_en"]

    save_data(data)

# ══════════════════════════════════════════════
# متابعة المحفظة
# ══════════════════════════════════════════════
def check_portfolio():
    if not is_market_open():
        return
    data = load_data()
    if not data["portfolio"]:
        return

    for num, trade in list(data["portfolio"].items()):
        try:
            price     = round(yf.Ticker(trade["ticker"]).fast_info.last_price, 2)
            buy_price = trade["buy_price"]
            shares    = trade["shares"]
            stop_loss = trade["stop_loss"]
            target    = trade["target"]
            profit    = round((price - buy_price) * shares, 2)
            profit_pct= round((price - buy_price) / buy_price * 100, 2)

            if target and price >= target:
                send_telegram(
                    f"🎯 *حان وقت البيع!*\n"
                    f"━━━━━━━━━━━━━━━━━━━\n"
                    f"{num}⃣ *{trade['ticker']}* وصل الهدف!\n"
                    f"💰 السعر الآن: ${price}\n"
                    f"🎯 الهدف كان: ${target}\n"
                    f"💵 ربحك: +${profit} (+{profit_pct}%)\n\n"
                    f"رد: /بعت {num}"
                )
            elif stop_loss and price <= stop_loss * 1.02 and price > stop_loss:
                send_telegram(
                    f"⚠️ *تحذير!*\n"
                    f"━━━━━━━━━━━━━━━━━━━\n"
                    f"{num}⃣ *{trade['ticker']}* اقترب من وقف الخسارة\n"
                    f"💰 السعر الآن: ${price}\n"
                    f"🔴 وقف الخسارة: ${stop_loss}\n"
                    f"رد: /بعت {num}"
                )
            elif stop_loss and price <= stop_loss:
                send_telegram(
                    f"🔴 *بيع فوراً!*\n"
                    f"━━━━━━━━━━━━━━━━━━━\n"
                    f"{num}⃣ *{trade['ticker']}* كسر وقف الخسارة!\n"
                    f"💰 السعر الآن: ${price}\n"
                    f"📉 الخسارة: -${abs(profit)} ({profit_pct}%)\n\n"
                    f"رد: /بعت {num}"
                )
        except Exception as e:
            logger.error(f"خطأ في متابعة {trade['ticker']}: {e}")

    save_data(data)

# ══════════════════════════════════════════════
# الملخص الصباحي
# ══════════════════════════════════════════════
def morning_briefing():
    global TODAY_INVESTMENT, TODAY_SPECULATIVE
    TODAY_INVESTMENT  = get_smart_investment_list()
    TODAY_SPECULATIVE = get_smart_speculative_list()

    inv_list  = "، ".join(TODAY_INVESTMENT[:10])
    spec_list = "، ".join(TODAY_SPECULATIVE[:5])

    data = load_data()

    send_telegram(
        f"🌅 *صباح الخير! السوق يفتح بعد 30 دقيقة*\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"💼 رأس مالك: ${data['capital']:,}\n"
        f"⚡ نسبة المخاطرة: {data['risk_pct']}%\n\n"
        f"🔵 *أفضل أسهم الاستثمار اليوم:*\n{inv_list}\n\n"
        f"🟡 *أفضل أسهم المضاربة اليوم:*\n{spec_list}\n\n"
        f"⏰ سأبدأ إرسال التوصيات عند الفتح"
    )

# ══════════════════════════════════════════════
# ملخص نهاية اليوم
# ══════════════════════════════════════════════
def end_of_day():
    global sent_today, TODAY_INVESTMENT, TODAY_SPECULATIVE
    data = load_data()

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

    data["signal_counter"] = 0
    data["signals"]        = {}
    sent_today             = {}
    TODAY_INVESTMENT       = []
    TODAY_SPECULATIVE      = []
    save_data(data)

# ══════════════════════════════════════════════
# ملخص أسبوعي
# ══════════════════════════════════════════════
def weekly_summary():
    data = load_data()
    history = data.get("history", [])

    # صفقات هذا الأسبوع فقط
    week_ago = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
    week_trades = [t for t in history if t.get("date", "") >= week_ago]

    if not week_trades:
        send_telegram("📊 *ملخص الأسبوع*\nما في صفقات مغلقة هذا الأسبوع.")
        return

    total_profit = sum(t.get("profit", 0) for t in week_trades)
    winning = [t for t in week_trades if t.get("profit", 0) > 0]
    losing  = [t for t in week_trades if t.get("profit", 0) < 0]
    win_rate = round(len(winning) / len(week_trades) * 100) if week_trades else 0

    best  = max(week_trades, key=lambda x: x.get("profit", 0))
    worst = min(week_trades, key=lambda x: x.get("profit", 0))

    icon = "💵" if total_profit >= 0 else "📉"

    send_telegram(
        f"📊 *ملخص الأسبوع*\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"📋 الصفقات المغلقة: {len(week_trades)}\n"
        f"✅ الرابحة: {len(winning)}\n"
        f"❌ الخاسرة: {len(losing)}\n"
        f"📈 نسبة النجاح: {win_rate}%\n\n"
        f"{icon} *صافي الربح: ${total_profit:+.2f}*\n\n"
        f"🏆 أفضل صفقة: {best['ticker']} +${best.get('profit', 0):.2f}\n"
        f"💀 أسوأ صفقة: {worst['ticker']} ${worst.get('profit', 0):.2f}"
    )

    # تصفير السجل الأسبوعي
    data["weekly_signals"] = []
    save_data(data)

# ══════════════════════════════════════════════
# معالجة الأوامر
# ══════════════════════════════════════════════
def process_command(msg, chat_id):
    data = load_data()
    msg = msg.strip()

    # /اشتريت
    if msg.startswith("/اشتريت"):
        parts = msg.split()
        if len(parts) >= 2:
            num = ''.join(filter(str.isdigit, parts[1]))
            if num in data["signals"]:
                signal = data["signals"][num]
                shares = calc_position_size(
                    signal["price"], signal["stop_loss"],
                    data["capital"], data["risk_pct"]
                )
                expected_profit = calc_expected_profit(signal["price"], signal["target"], shares)
                max_loss = round((signal["price"] - signal["stop_loss"]) * shares, 2) if signal.get("stop_loss") else 0

                data["portfolio"][num] = {
                    "ticker":    signal["ticker"],
                    "shares":    shares,
                    "buy_price": signal["price"],
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
                    f"💰 سعر الشراء: ${signal['price']}\n"
                    f"💵 إجمالي الاستثمار: ${round(signal['price'] * shares, 2):,}\n"
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
                for num, trade in list(data["portfolio"].items()):
                    try:
                        price  = round(yf.Ticker(trade["ticker"]).fast_info.last_price, 2)
                        profit = round((price - trade["buy_price"]) * trade["shares"], 2)
                        total_profit += profit
                        data["history"].append({**trade, "sell_price": price, "profit": profit})
                    except:
                        pass
                data["portfolio"] = {}
                save_data(data)
                icon = "💵" if total_profit >= 0 else "📉"
                send_telegram(f"✅ تم بيع كل الصفقات\n{icon} إجمالي: ${total_profit:+.2f}", chat_id)
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
    elif "/محفظتي" in msg or "/portfolio" in msg:
        if not data["portfolio"]:
            send_telegram("📊 محفظتك فارغة حالياً", chat_id)
            return
        lines = ["━━━━━━━━━━━━━━━━━━━\n📊 *محفظتك الآن*"]
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

    # /capital
    elif msg.startswith("/capital"):
        parts = msg.split()
        if len(parts) >= 2:
            try:
                new_capital = float(parts[1])
                data["capital"] = new_capital
                save_data(data)
                send_telegram(f"✅ تم تحديث رأس المال إلى ${new_capital:,}", chat_id)
            except:
                send_telegram("❌ مثال: /capital 10000", chat_id)
        else:
            send_telegram(f"💼 رأس مالك الحالي: ${data['capital']:,}", chat_id)

    # /risk
    elif msg.startswith("/risk"):
        parts = msg.split()
        if len(parts) >= 2:
            try:
                new_risk = float(parts[1])
                if 0.1 <= new_risk <= 5:
                    data["risk_pct"] = new_risk
                    save_data(data)
                    send_telegram(f"✅ نسبة المخاطرة: {new_risk}% لكل صفقة", chat_id)
                else:
                    send_telegram("❌ النسبة بين 0.1 و 5", chat_id)
            except:
                send_telegram("❌ مثال: /risk 1", chat_id)
        else:
            send_telegram(f"⚡ نسبة المخاطرة الحالية: {data['risk_pct']}%", chat_id)

    # /اسبوع
    elif "/اسبوع" in msg or "/weekly" in msg:
        weekly_summary()

    # /حلل
    elif msg.startswith("/حلل"):
        parts = msg.split()
        if len(parts) >= 2:
            ticker = parts[1].upper()
            try:
                last, prev, price_info, df = fetch_and_analyze(ticker)
                if last is not None:
                    rsi = round(last["rsi"], 1)
                    adx = round(last["adx"], 1)
                    vwap = round(last["vwap"], 2)
                    rec = "🟢 شراء" if rsi < 35 else ("🔴 بيع" if rsi > 65 else "🟡 انتظر")
                    send_telegram(
                        f"📊 *{ticker}*\n"
                        f"━━━━━━━━━━━━━━━━━━━\n"
                        f"💰 السعر: ${price_info['price']}\n"
                        f"📈 التغيير: {price_info['change_pct']:+.2f}%\n"
                        f"📊 RSI: {rsi}\n"
                        f"📊 ADX: {adx} ({'اتجاه قوي' if adx > 25 else 'اتجاه ضعيف'})\n"
                        f"📊 VWAP: ${vwap}\n"
                        f"🎯 التوصية: {rec}",
                        chat_id
                    )
                else:
                    send_telegram(f"❌ تعذر تحليل {ticker}", chat_id)
            except:
                send_telegram("❌ تعذر جلب البيانات", chat_id)

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

    # /ربحي
    elif "/ربحي" in msg:
        if not data["portfolio"]:
            send_telegram("📊 ما عندك صفقات مفتوحة", chat_id)
            return
        total = 0
        for trade in data["portfolio"].values():
            try:
                price = yf.Ticker(trade["ticker"]).fast_info.last_price
                total += (price - trade["buy_price"]) * trade["shares"]
            except:
                pass
        total = round(total, 2)
        icon  = "💵" if total >= 0 else "📉"
        send_telegram(f"{icon} إجمالي ربحك الآن: ${total:+.2f}", chat_id)

    # /مساعدة
    elif "/مساعدة" in msg or "/start" in msg or "/help" in msg:
        send_telegram(
            "📋 *الأوامر المتاحة:*\n"
            "━━━━━━━━━━━━━━━━━━━\n"
            "• /اشتريت 1 — تسجيل صفقة\n"
            "• /بعت 1 — إغلاق صفقة\n"
            "• /بعت كل — إغلاق الكل\n"
            "• /محفظتي — عرض صفقاتك\n"
            "• /ربحي — إجمالي الربح\n"
            "• /حلل AAPL — تحليل سهم\n"
            "• /السوق — حالة السوق\n"
            "• /capital 10000 — تعديل رأس المال\n"
            "• /risk 1 — تعديل نسبة المخاطرة\n"
            "• /اسبوع — ملخص الأسبوع",
            chat_id
        )

# ══════════════════════════════════════════════
# استقبال الأوامر من تلغرام
# ══════════════════════════════════════════════
last_update_id = 0

def check_telegram_updates():
    global last_update_id
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
        r = requests.get(url, params={"offset": last_update_id + 1, "timeout": 10}, timeout=15)
        updates = r.json().get("result", [])
        for update in updates:
            last_update_id = update["update_id"]
            msg = update.get("message", {})
            text = msg.get("text", "")
            chat_id = str(msg.get("chat", {}).get("id", ""))
            if text and chat_id:
                logger.info(f"📩 أمر: {text}")
                process_command(text, chat_id)
    except Exception as e:
        logger.error(f"خطأ في getUpdates: {e}")

# ══════════════════════════════════════════════
# التشغيل
# ══════════════════════════════════════════════
if __name__ == "__main__":
    logger.info("🚀 يعمل بوت الأسهم الذكي - تلغرام!")

    # إرسال رسالة ترحيب
    send_telegram(
        "🚀 *بوت الأسهم الذكي شغال!*\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        "8 مؤشرات فنية + إدارة رأس المال\n\n"
        "أرسل /مساعدة لقائمة الأوامر"
    )

    analyze_all()

    scheduler = BlockingScheduler(timezone=pytz.timezone("America/New_York"))

    # تحليل الأسهم كل 5 دقائق
    scheduler.add_job(analyze_all, "interval", minutes=CHECK_INTERVAL_MINUTES)

    # متابعة المحفظة كل دقيقة
    scheduler.add_job(check_portfolio, "interval", minutes=1)

    # استقبال الأوامر كل 10 ثواني
    scheduler.add_job(check_telegram_updates, "interval", seconds=10)

    # ملخص نهاية اليوم
    scheduler.add_job(end_of_day, CronTrigger(hour=16, minute=5, day_of_week="mon-fri"))

    # ملخص صباحي
    scheduler.add_job(morning_briefing, CronTrigger(hour=9, minute=0, day_of_week="mon-fri"))

    # ملخص أسبوعي (الجمعة بعد إغلاق السوق)
    scheduler.add_job(weekly_summary, CronTrigger(hour=16, minute=30, day_of_week="fri"))

    try:
        scheduler.start()
    except KeyboardInterrupt:
        logger.info("🛑 تم الإيقاف")
