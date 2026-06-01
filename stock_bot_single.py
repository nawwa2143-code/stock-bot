#!/usr/bin/env python3
"""
بوت الأسهم الذكي v2
- تحليل S&P 500 كامل + مضاربة
- محادثة عربية كاملة
- تتبع المحفظة
- تنبيهات ذكية
"""

# ══════════════════════════════════════════════
#   ضع بياناتك هنا فقط
# ══════════════════════════════════════════════

TWILIO_ACCOUNT_SID = "ACfc1ed0aa9e92fec6115e44d4c5fa471f"
TWILIO_AUTH_TOKEN  = "537c2d84e0019a4bedaf00a6e48e6a8e"
TWILIO_FROM        = "whatsapp:+14155238886"
YOUR_WHATSAPP      = "whatsapp:+966594296964"

MIN_CONFIDENCE        = 60
CHECK_INTERVAL_MINUTES = 5

# ══════════════════════════════════════════════
#   الكود - لا تعدل هنا
# ══════════════════════════════════════════════

import sys, os, json, logging
from datetime import datetime

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger(__name__)

def install_deps():
    import subprocess
    pkgs = ["yfinance", "pandas", "numpy", "ta", "twilio", "apscheduler", "pytz", "requests"]
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
import requests
from twilio.rest import Client
from twilio.twiml.messaging_response import MessagingResponse
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading
import urllib.parse

# ══════════════════════════════════════════════
#   اختيار الأسهم تلقائياً كل يوم
# ══════════════════════════════════════════════

SP500_ALL = [
    "AAPL","MSFT","NVDA","GOOGL","AMZN","META","TSLA","BRK-B","JPM","JNJ",
    "V","PG","UNH","HD","MA","MRK","ABBV","PFE","KO","PEP","BAC","WMT",
    "DIS","CSCO","ADBE","CRM","NFLX","INTC","AMD","QCOM","TXN","AVGO",
    "COST","NKE","MCD","SBUX","GE","BA","CAT","MMM","IBM","ORCL","PYPL",
    "UBER","SHOP","ZM","DOCU","ROKU","NOW","SNOW","PLTR","COIN","HOOD",
    "F","GM","RIVN","NIO","XPEV","XOM","CVX","COP","SLB","OXY",
    "WFC","GS","MS","C","USB","LLY","BMY","AMGN","GILD","BIIB"
]

SPECULATIVE_BASE = [
    "SOUN","AMC","GME","BBIG","CLOV","SPCE","WISH","MVIS","EXPR",
    "SNDL","ACB","CGC","TLRY","NAKD","KOSS","PHUN","IDEX","XELA",
    "CTRM","GOVX","ATOS","NOK","BB","WKHS","RIDE","NKLA","LCID",
    "RIVN","HOOD","COIN","PLTR","NIO","XPEV","SOFI","OPEN","UWMC",
    "CANO","CTIC","LODE","OCGN","SAVA","VXRT","BBAI","VERSE","APLD"
]

def get_smart_investment_list():
    logger.info("🔍 جلب افضل اسهم الاستثمار من Yahoo Finance...")
    try:
        url = "https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved?scrIds=day_gainers&count=50&region=US"
        headers = {"User-Agent": "Mozilla/5.0"}
        r = requests.get(url, headers=headers, timeout=10)
        quotes = r.json()["finance"]["result"][0]["quotes"]
        tickers = [q["symbol"] for q in quotes
                   if q.get("regularMarketPrice", 0) > 10
                   and q.get("averageDailyVolume3Month", 0) > 1_000_000]
        logger.info(f"تم جلب {len(tickers)} سهم استثمار")
        return tickers[:30]
    except Exception as e:
        logger.error(f"خطأ في جلب اسهم الاستثمار: {e}")
        return []

def get_smart_speculative_list():
    logger.info("🔍 جلب افضل اسهم المضاربة من Yahoo Finance...")
    try:
        url = "https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved?scrIds=most_actives&count=100&region=US"
        headers = {"User-Agent": "Mozilla/5.0"}
        r = requests.get(url, headers=headers, timeout=10)
        quotes = r.json()["finance"]["result"][0]["quotes"]
        tickers = [q["symbol"] for q in quotes
                   if 1 <= q.get("regularMarketPrice", 0) <= 20]
        logger.info(f"تم جلب {len(tickers)} سهم مضاربة")
        return tickers[:20]
    except Exception as e:
        logger.error(f"خطأ في جلب اسهم المضاربة: {e}")
        return []

TODAY_INVESTMENT  = []
TODAY_SPECULATIVE = []

# ══════════════════════════════════════════════
#   ملف المحفظة والبيانات
# ══════════════════════════════════════════════

DATA_FILE = "bot_data.json"

def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {
        "portfolio": {},      # الصفقات المفتوحة
        "signals": {},        # التوصيات اليومية {رقم: بيانات}
        "signal_counter": 0, # عداد التوصيات
        "history": [],        # تاريخ الصفقات المغلقة
        "waiting_input": None # ننتظر رد من المستخدم
    }

def save_data(data):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

# ══════════════════════════════════════════════
#   جلب البيانات والتحليل
# ══════════════════════════════════════════════

def fetch_and_analyze(ticker):
    try:
        stock = yf.Ticker(ticker)
        df = stock.history(period="3mo", interval="1d")
        if df.empty or len(df) < 30:
            return None, None

        info = stock.fast_info
        price = round(info.last_price, 2)
        prev_close = info.previous_close
        change_pct = round((price - prev_close) / prev_close * 100, 2)

        c = df["Close"]
        df["rsi"]         = ta.momentum.RSIIndicator(c, window=14).rsi()
        macd_obj          = ta.trend.MACD(c)
        df["macd"]        = macd_obj.macd()
        df["macd_signal"] = macd_obj.macd_signal()
        bb                = ta.volatility.BollingerBands(c)
        df["bb_pct"]      = bb.bollinger_pband()
        df["ema9"]        = ta.trend.EMAIndicator(c, window=9).ema_indicator()
        df["ema21"]       = ta.trend.EMAIndicator(c, window=21).ema_indicator()
        df["atr"]         = ta.volatility.AverageTrueRange(df["High"], df["Low"], c).average_true_range()
        df["vol_ratio"]   = df["Volume"] / df["Volume"].rolling(20).mean()
        df = df.dropna()

        if len(df) < 2:
            return None, None

        last, prev = df.iloc[-1], df.iloc[-2]
        price_info = {"ticker": ticker, "price": price, "change_pct": change_pct}

        return last, prev, price_info, df

    except Exception as e:
        return None, None, None, None

def generate_signal(ticker, last, prev, price_info, mode="investment"):
    buy, sell, max_pts = 0, 0, 100
    reasons_buy, reasons_sell = [], []
    rsi = last["rsi"]

    # RSI
    oversold  = 35 if mode == "investment" else 40
    overbought = 65 if mode == "investment" else 60

    if rsi < oversold:
        buy += 25
        reasons_buy.append(f"RSI={rsi:.0f} (مبالغ في بيعه)")
    elif rsi > overbought:
        sell += 25
        reasons_sell.append(f"RSI={rsi:.0f} (مبالغ في شرائه)")

    # MACD
    if prev["macd"] < prev["macd_signal"] and last["macd"] > last["macd_signal"]:
        buy += 25
        reasons_buy.append("تقاطع MACD صعودي ✅")
    elif prev["macd"] > prev["macd_signal"] and last["macd"] < last["macd_signal"]:
        sell += 25
        reasons_sell.append("تقاطع MACD هبوطي ❌")

    # Bollinger
    if last["bb_pct"] < 0.05:
        buy += 20
        reasons_buy.append("السعر لمس الحزام السفلي")
    elif last["bb_pct"] > 0.95:
        sell += 20
        reasons_sell.append("السعر لمس الحزام العلوي")

    # EMA
    if prev["ema9"] < prev["ema21"] and last["ema9"] > last["ema21"]:
        buy += 20
        reasons_buy.append("EMA9 تجاوزت EMA21 للأعلى")
    elif prev["ema9"] > prev["ema21"] and last["ema9"] < last["ema21"]:
        sell += 20
        reasons_sell.append("EMA9 تجاوزت EMA21 للأسفل")

    # حجم التداول
    if last["vol_ratio"] > 1.5:
        if buy > sell:
            buy += 10
            reasons_buy.append(f"حجم مرتفع ({last['vol_ratio']:.1f}x)")
        elif sell > buy:
            sell += 10
            reasons_sell.append(f"حجم مرتفع ({last['vol_ratio']:.1f}x)")

    # حساب وقف الخسارة والهدف
    price   = price_info["price"]
    atr     = last["atr"]
    atr_pct = round(atr / price * 100, 1)

    if mode == "investment":
        stop_loss = round(price - (atr * 2), 2)
        target    = round(price + (atr * 3), 2)
    else:  # مضاربة
        stop_loss = round(price - (atr * 1.5), 2)
        target    = round(price + (atr * 4), 2)

    stop_pct   = round((price - stop_loss) / price * 100, 1)
    target_pct = round((target - price) / price * 100, 1)

    if buy > sell:
        conf = round(buy / max_pts * 100)
        if conf < MIN_CONFIDENCE:
            return None
        return {
            "ticker":     ticker,
            "action":     "شراء 🟢",
            "action_en":  "BUY",
            "mode":       mode,
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
            "mode":       mode,
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

# ══════════════════════════════════════════════
#   إرسال واتساب
# ══════════════════════════════════════════════

def send_whatsapp(message):
    try:
        client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        msg = client.messages.create(body=message, from_=TWILIO_FROM, to=YOUR_WHATSAPP)
        logger.info(f"✅ رسالة أُرسلت: {msg.sid}")
        return True
    except Exception as e:
        logger.error(f"❌ فشل الإرسال: {e}")
        return False

def format_signal_message(signal, number):
    mode_icon = "🔵 استثمار" if signal["mode"] == "investment" else "🟡 مضاربة ⚡"
    lines = [
        f"━━━━━━━━━━━━━━━━━━━",
        f"{mode_icon}",
        f"{number}️⃣ *{signal['ticker']}* — ${signal['price']}",
        f"📌 {signal['action']} — ثقة {signal['confidence']}%",
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
            f"🛑 وقف الخسارة: *${signal['stop_loss']}* ({signal['stop_pct']}%)",
            f"🎯 الهدف: *${signal['target']}* (+{signal['target_pct']}%)",
        ]
    if signal["mode"] == "speculative":
        lines.append("⚠️ مضاربة — خطر مرتفع")
    lines += ["", f"اشتريت؟ رد: *اشتريت {number}*"]
    return "\n".join(lines)

# ══════════════════════════════════════════════
#   تحليل الأسهم
# ══════════════════════════════════════════════

sent_today = {}

def is_market_open():
    now = datetime.now(pytz.timezone("America/New_York"))
    if now.weekday() >= 5:
        return False
    open_t  = now.replace(hour=9,  minute=30, second=0, microsecond=0)
    close_t = now.replace(hour=16, minute=0,  second=0, microsecond=0)
    return open_t <= now <= close_t

def analyze_all():
    global TODAY_INVESTMENT, TODAY_SPECULATIVE
    if not is_market_open():
        logger.info("السوق مغلق ⏸")
        return

    data = load_data()
    logger.info("🔍 تحليل الأسهم...")

    # اختيار الأسهم الذكي (مرة واحدة في اليوم)
    if not TODAY_INVESTMENT:
        TODAY_INVESTMENT  = get_smart_investment_list()
    if not TODAY_SPECULATIVE:
        TODAY_SPECULATIVE = get_smart_speculative_list()

    # فلتر الاستثمار
    investment_signals = []
    for ticker in TODAY_INVESTMENT[:30]:
        try:
            result = fetch_and_analyze(ticker)
            if result[0] is None:
                continue
            last, prev, price_info, df = result
            if price_info["price"] < 20:
                continue
            signal = generate_signal(ticker, last, prev, price_info, "investment")
            if signal and sent_today.get(ticker) != signal["action_en"]:
                investment_signals.append(signal)
        except:
            pass

    # فلتر المضاربة
    speculative_signals = []
    for ticker in TODAY_SPECULATIVE[:20]:
        try:
            result = fetch_and_analyze(ticker)
            if result[0] is None:
                continue
            last, prev, price_info, df = result
            if not (1 <= price_info["price"] <= 20):
                continue
            signal = generate_signal(ticker, last, prev, price_info, "speculative")
            if signal and sent_today.get(ticker) != signal["action_en"]:
                speculative_signals.append(signal)
        except:
            pass

    # إرسال أفضل التوصيات
    top_investment  = sorted(investment_signals,  key=lambda x: x["confidence"], reverse=True)[:3]
    top_speculative = sorted(speculative_signals, key=lambda x: x["confidence"], reverse=True)[:2]
    all_signals     = top_investment + top_speculative

    for signal in all_signals:
        data["signal_counter"] += 1
        num = data["signal_counter"]
        data["signals"][str(num)] = {
            **signal,
            "number":    num,
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "bought":    False
        }
        msg = format_signal_message(signal, num)
        send_whatsapp(msg)
        sent_today[signal["ticker"]] = signal["action_en"]

    save_data(data)

def check_portfolio():
    """تحقق من المحفظة وأرسل تنبيهات"""
    if not is_market_open():
        return

    data = load_data()
    if not data["portfolio"]:
        return

    for num, trade in list(data["portfolio"].items()):
        try:
            ticker = trade["ticker"]
            stock  = yf.Ticker(ticker)
            price  = round(stock.fast_info.last_price, 2)
            buy_price  = trade["buy_price"]
            shares     = trade["shares"]
            stop_loss  = trade["stop_loss"]
            target     = trade["target"]
            profit     = round((price - buy_price) * shares, 2)
            profit_pct = round((price - buy_price) / buy_price * 100, 2)

            # وصل الهدف
            if price >= target:
                send_whatsapp(
                    f"🎯 حان وقت البيع!\n"
                    f"━━━━━━━━━━━━━━━━━━━\n"
                    f"{num}️⃣ *{ticker}* وصل الهدف!\n"
                    f"💵 السعر الآن: ${price}\n"
                    f"🎯 الهدف كان: ${target}\n"
                    f"💰 ربحك: +${profit} (+{profit_pct}%)\n\n"
                    f"📌 بيع الآن؟ رد: *بعت {num}*"
                )

            # اقترب من وقف الخسارة
            elif price <= stop_loss * 1.02 and price > stop_loss:
                send_whatsapp(
                    f"⚠️ تحذير!\n"
                    f"━━━━━━━━━━━━━━━━━━━\n"
                    f"{num}️⃣ *{ticker}* اقترب من وقف الخسارة\n"
                    f"💵 السعر الآن: ${price}\n"
                    f"🛑 وقف الخسارة: ${stop_loss}\n"
                    f"📌 استعد للبيع — رد: *بعت {num}*"
                )

            # كسر وقف الخسارة
            elif price <= stop_loss:
                send_whatsapp(
                    f"🛑 بيع فوراً!\n"
                    f"━━━━━━━━━━━━━━━━━━━\n"
                    f"{num}️⃣ *{ticker}* كسر وقف الخسارة!\n"
                    f"💵 السعر الآن: ${price}\n"
                    f"💸 الخسارة: ${abs(profit)} ({profit_pct}%)\n\n"
                    f"📌 رد: *بعت {num}*"
                )

        except Exception as e:
            logger.error(f"خطأ في متابعة {trade['ticker']}: {e}")

    save_data(data)

def morning_briefing():
    """ملخص صباحي قبل فتح السوق"""
    global TODAY_INVESTMENT, TODAY_SPECULATIVE
    TODAY_INVESTMENT  = get_smart_investment_list()
    TODAY_SPECULATIVE = get_smart_speculative_list()

    inv_list  = "، ".join(TODAY_INVESTMENT[:10])
    spec_list = "، ".join(TODAY_SPECULATIVE[:5])

    send_whatsapp(
        f"🌅 صباح الخير! السوق يفتح بعد 30 دقيقة\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"🔵 *أفضل أسهم الاستثمار اليوم:*\n"
        f"{inv_list}\n\n"
        f"🟡 *أفضل أسهم المضاربة اليوم:*\n"
        f"{spec_list}\n\n"
        f"⏰ سأبدأ إرسال التوصيات عند الفتح"
    )

def end_of_day():
    """ملخص نهاية اليوم"""
    global sent_today
    data = load_data()

    portfolio_value = 0
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
                lines.append(f"{num}️⃣ {trade['ticker']} — {icon} ${profit:+.2f} ({pct:+.2f}%)")
                portfolio_profit += profit
            except:
                pass
        lines.append(f"\n💰 إجمالي الربح/الخسارة: ${portfolio_profit:+.2f}")

    send_whatsapp("\n".join(lines))

    # تصفير العداد اليومي
    global TODAY_INVESTMENT, TODAY_SPECULATIVE
    data["signal_counter"] = 0
    data["signals"] = {}
    sent_today = {}
    TODAY_INVESTMENT  = []
    TODAY_SPECULATIVE = []
    save_data(data)

# ══════════════════════════════════════════════
#   معالجة رسائل واتساب الواردة
# ══════════════════════════════════════════════

def process_incoming_message(msg):
    """معالجة رسائل المستخدم"""
    msg = msg.strip()
    data = load_data()

    # ── اشتريت ──
    if msg.startswith("اشتريت"):
        parts = msg.split()
        if len(parts) >= 2:
            num = parts[1]
            if num in data["signals"] or any(c.isdigit() for c in num):
                num = ''.join(filter(str.isdigit, num))
                if num in data["signals"]:
                    signal = data["signals"][num]
                    data["waiting_input"] = {"type": "shares", "signal_num": num}
                    save_data(data)
                    return (
                        f"✅ {signal['ticker']} — كم سهم اشتريت؟\n"
                        f"💵 بسعر ${signal['price']}"
                    )
        return "❌ رقم التوصية غير صحيح. مثال: اشتريت 1"

    # ── رد على سؤال كم سهم ──
    if data.get("waiting_input") and data["waiting_input"]["type"] == "shares":
        try:
            shares = int(msg)
            num    = data["waiting_input"]["signal_num"]
            signal = data["signals"][num]
            total  = round(signal["price"] * shares, 2)
            max_loss   = round((signal["price"] - signal["stop_loss"]) * shares, 2) if signal.get("stop_loss") else 0
            max_profit = round((signal["target"] - signal["price"]) * shares, 2) if signal.get("target") else 0

            data["portfolio"][num] = {
                "ticker":    signal["ticker"],
                "shares":    shares,
                "buy_price": signal["price"],
                "stop_loss": signal["stop_loss"],
                "target":    signal["target"],
                "mode":      signal["mode"],
                "date":      datetime.now().strftime("%Y-%m-%d %H:%M"),
            }
            data["waiting_input"] = None
            save_data(data)

            return (
                f"✅ تم التسجيل!\n"
                f"━━━━━━━━━━━━━━━━━━━\n"
                f"🏷️ {signal['ticker']} — {shares} سهم\n"
                f"💵 سعر الشراء: ${signal['price']}\n"
                f"💰 إجمالي الاستثمار: ${total}\n"
                f"🛑 أقصى خسارة: ${max_loss}\n"
                f"🎯 الربح المتوقع: ${max_profit}\n"
                f"📊 نسبة ربح/خسارة: {round(max_profit/max_loss, 1) if max_loss else 0}:1"
            )
        except:
            return "❌ أرسل عدد الأسهم فقط. مثال: 10"

    # ── بعت ──
    if msg.startswith("بعت"):
        parts = msg.split()
        if len(parts) >= 2:
            num = ''.join(filter(str.isdigit, parts[1]))
            if num in data["portfolio"]:
                trade = data["portfolio"][num]
                try:
                    price  = round(yf.Ticker(trade["ticker"]).fast_info.last_price, 2)
                    profit = round((price - trade["buy_price"]) * trade["shares"], 2)
                    pct    = round((price - trade["buy_price"]) / trade["buy_price"] * 100, 2)
                    icon   = "💰 ربحت" if profit >= 0 else "💸 خسرت"

                    data["history"].append({**trade, "sell_price": price, "profit": profit})
                    del data["portfolio"][num]
                    save_data(data)

                    return (
                        f"✅ تم تسجيل البيع!\n"
                        f"━━━━━━━━━━━━━━━━━━━\n"
                        f"🏷️ {trade['ticker']}\n"
                        f"💵 سعر البيع: ${price}\n"
                        f"💵 سعر الشراء: ${trade['buy_price']}\n"
                        f"{icon}: ${abs(profit)} ({pct:+.2f}%)"
                    )
                except:
                    return "❌ تعذر جلب السعر الحالي"
        if "كل شيء" in msg:
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
            icon = "💰" if total_profit >= 0 else "💸"
            return f"✅ تم بيع كل الصفقات\n{icon} إجمالي: ${total_profit:+.2f}"
        return "❌ مثال: بعت 1 أو بعت كل شيء"

    # ── ألغ ──
    if msg.startswith("ألغ") or msg.startswith("الغ"):
        parts = msg.split()
        if len(parts) >= 2:
            num = ''.join(filter(str.isdigit, parts[1]))
            if num in data["portfolio"]:
                ticker = data["portfolio"][num]["ticker"]
                del data["portfolio"][num]
                save_data(data)
                return f"✅ تم حذف صفقة {ticker}"
        return "❌ مثال: ألغ 1"

    # ── غلط ──
    if msg == "غلط":
        if data.get("waiting_input"):
            data["waiting_input"] = None
            save_data(data)
            return "✅ تم الإلغاء"
        return "لا يوجد شيء لإلغائه"

    # ── ما اشتريت ──
    if "ما اشتريت" in msg or "لم اشتري" in msg:
        data["waiting_input"] = None
        save_data(data)
        return "✅ تم — لن أتابع هذه الصفقة"

    # ── محفظتي ──
    if "محفظتي" in msg or "محفظة" in msg:
        if not data["portfolio"]:
            return "📂 محفظتك فارغة حالياً"
        lines = ["📊 *محفظتك الآن*\n━━━━━━━━━━━━━━━━━━━"]
        total_profit = 0
        for num, trade in data["portfolio"].items():
            try:
                price  = round(yf.Ticker(trade["ticker"]).fast_info.last_price, 2)
                profit = round((price - trade["buy_price"]) * trade["shares"], 2)
                pct    = round((price - trade["buy_price"]) / trade["buy_price"] * 100, 2)
                icon   = "🟢" if profit >= 0 else "🔴"
                total_profit += profit
                lines.append(
                    f"{num}️⃣ *{trade['ticker']}* — {trade['shares']} سهم\n"
                    f"   شراء: ${trade['buy_price']} | الآن: ${price}\n"
                    f"   {icon} ${profit:+.2f} ({pct:+.2f}%)"
                )
            except:
                lines.append(f"{num}️⃣ {trade['ticker']} — تعذر جلب السعر")
        icon = "💰" if total_profit >= 0 else "💸"
        lines.append(f"\n━━━━━━━━━━━━━━━━━━━\n{icon} الإجمالي: ${total_profit:+.2f}")
        return "\n".join(lines)

    # ── كم ربحي ──
    if "ربحي" in msg or "ربح" in msg:
        if not data["portfolio"]:
            return "📂 ما عندك صفقات مفتوحة"
        total = 0
        for trade in data["portfolio"].values():
            try:
                price  = yf.Ticker(trade["ticker"]).fast_info.last_price
                total += (price - trade["buy_price"]) * trade["shares"]
            except:
                pass
        total = round(total, 2)
        icon = "💰" if total >= 0 else "💸"
        return f"{icon} إجمالي ربحك الآن: ${total:+.2f}"

    # ── كم خسارتي ──
    if "خسارتي" in msg or "خسارة" in msg:
        if not data["portfolio"]:
            return "📂 ما عندك صفقات مفتوحة"
        total = 0
        for trade in data["portfolio"].values():
            try:
                price  = yf.Ticker(trade["ticker"]).fast_info.last_price
                profit = (price - trade["buy_price"]) * trade["shares"]
                if profit < 0:
                    total += profit
            except:
                pass
        total = round(total, 2)
        return f"💸 إجمالي خسارتك الآن: ${total:.2f}"

    # ── وين وصل / حلل سهم ──
    for keyword in ["وين وصل", "حلل", "وش رأيك", "هل أشتري", "هل اشتري"]:
        if keyword in msg:
            parts = msg.upper().split()
            ticker = None
            for p in parts:
                if p.isalpha() and 2 <= len(p) <= 5:
                    ticker = p
                    break
            if ticker:
                try:
                    stock  = yf.Ticker(ticker)
                    price  = round(stock.fast_info.last_price, 2)
                    result = fetch_and_analyze(ticker)
                    if result[0] is not None:
                        last, prev, price_info, df = result
                        rsi = round(last["rsi"], 1)
                        if rsi < 35:
                            rec = "شراء 🟢"
                        elif rsi > 65:
                            rec = "بيع 🔴"
                        else:
                            rec = "انتظر 🟡"
                        return (
                            f"📊 *{ticker}*\n"
                            f"━━━━━━━━━━━━━━━━━━━\n"
                            f"💵 السعر: ${price}\n"
                            f"📈 التغيير: {price_info['change_pct']:+.2f}%\n"
                            f"📊 RSI: {rsi}\n"
                            f"📌 التوصية: {rec}"
                        )
                    return f"💵 {ticker} الآن: ${price}"
                except:
                    return "❌ تعذر جلب بيانات السهم"

    # ── السوق كيف ──
    if "السوق" in msg:
        try:
            spy = round(yf.Ticker("SPY").fast_info.last_price, 2)
            qqq = round(yf.Ticker("QQQ").fast_info.last_price, 2)
            return (
                f"📊 *السوق الآن*\n"
                f"━━━━━━━━━━━━━━━━━━━\n"
                f"🇺🇸 S&P 500 (SPY): ${spy}\n"
                f"💻 Nasdaq (QQQ): ${qqq}\n"
                f"⏰ {'مفتوح 🟢' if is_market_open() else 'مغلق 🔴'}"
            )
        except:
            return "❌ تعذر جلب بيانات السوق"

    # ── مساعدة ──
    if "مساعدة" in msg or "help" in msg.lower() or "وش" in msg:
        return (
            "📋 *الأوامر المتاحة:*\n"
            "━━━━━━━━━━━━━━━━━━━\n"
            "• اشتريت 1 — تسجيل صفقة\n"
            "• بعت 1 — إغلاق صفقة\n"
            "• بعت كل شيء — إغلاق الكل\n"
            "• ألغ 1 — حذف صفقة\n"
            "• محفظتي — عرض صفقاتك\n"
            "• كم ربحي — إجمالي الربح\n"
            "• وين وصل AAPL — سعر سهم\n"
            "• حلل AAPL — تحليل سهم\n"
            "• السوق كيف — حالة السوق\n"
            "• غلط — إلغاء آخر أمر"
        )

    return (
        "😅 ما فهمت! جرب:\n"
        "• محفظتي\n"
        "• حلل AAPL\n"
        "• السوق كيف\n"
        "• مساعدة"
    )

# ══════════════════════════════════════════════
#   خادم HTTP لاستقبال رسائل واتساب
# ══════════════════════════════════════════════

class WebhookHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        content_length = int(self.headers['Content-Length'])
        post_data = self.rfile.read(content_length)
        params = urllib.parse.parse_qs(post_data.decode('utf-8'))
        incoming_msg = params.get('Body', [''])[0]

        logger.info(f"📩 رسالة واردة: {incoming_msg}")
        reply = process_incoming_message(incoming_msg)

        send_whatsapp(reply)

        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'OK')

    def log_message(self, format, *args):
        pass

def run_server():
    server = HTTPServer(('0.0.0.0', 8080), WebhookHandler)
    logger.info("🌐 خادم الرسائل يعمل على port 8080")
    server.serve_forever()

# ══════════════════════════════════════════════
#   التشغيل
# ══════════════════════════════════════════════

if __name__ == "__main__":
    logger.info("🚀 بوت الأسهم الذكي v2 يعمل!")

    # تشغيل خادم استقبال الرسائل في خلفية
    server_thread = threading.Thread(target=run_server, daemon=True)
    server_thread.start()

    # تحليل فوري عند البدء
    analyze_all()

    scheduler = BlockingScheduler(timezone=pytz.timezone("America/New_York"))

    # تحليل الأسهم كل 5 دقائق
    scheduler.add_job(analyze_all, "interval", minutes=CHECK_INTERVAL_MINUTES)

    # متابعة المحفظة كل دقيقة
    scheduler.add_job(check_portfolio, "interval", minutes=1)

    # ملخص نهاية اليوم
    scheduler.add_job(end_of_day, CronTrigger(hour=16, minute=5, day_of_week="mon-fri"))

    # ملخص صباحي (9:00 صباحاً بتوقيت نيويورك = قبل الفتح بـ 30 دقيقة)
    scheduler.add_job(morning_briefing, CronTrigger(hour=9, minute=0, day_of_week="mon-fri"))

    try:
        scheduler.start()
    except KeyboardInterrupt:
        logger.info("👋 تم الإيقاف")

