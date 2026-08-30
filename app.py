import os
os.environ['TZ'] = 'UTC'
import asyncio
import time
import sqlite3
import requests
import re
from datetime import datetime, timedelta
from curl_cffi import requests as cffi_requests
from flask import Flask
from telegram import Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler
import logging
import threading

TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN')
if not TELEGRAM_TOKEN:
    raise ValueError("TELEGRAM_TOKEN environment variable not set!")
INTERVAL = 60
TIMEOUT = 30

logging.basicConfig(level=logging.INFO)

CATEGORIES = {
    'crypto': {'name': 'ارزهای دیجیتال', 'emoji': '💰', 'symbols': [
        ('btc', 'BTC', '₿'), ('eth', 'ETH', '💎'), ('bnb', 'BNB', '🟡'),
        ('sol', 'SOL', '☀️'), ('ltc', 'LTC', '⚡'), ('bch', 'BCH', '🔶'),
        ('xrp', 'XRP', '💠'), ('trx', 'TRX', '🔴'), ('doge', 'DOGE', '🐕'),
        ('gram', 'GRAM', '🔷')
    ]},
    'metals': {'name': 'فلزات گرانبها', 'emoji': '🏆', 'symbols': [
        ('gold', 'GOLD', '🏆'), ('silver', 'SILVER', '🥈')
    ]},
    'energy': {'name': 'انرژی و نفت', 'emoji': '⛽', 'symbols': [
        ('oil', 'OIL', '🛢️'), ('brent', 'BRENT', '🛢️'), ('gas', 'GAS', '🔥')
    ]},
    'agriculture': {'name': 'کشاورزی', 'emoji': '🌾', 'symbols': [
        ('sugar', 'SUGAR', '🍬')
    ]}
}

# ======================== کش قیمت‌ها ========================
price_cache = {'data': {}, 'last_update': 0, 'lock': threading.Lock()}
holiday_cache = {'date': '', 'is_holiday': False, 'lock': threading.Lock()}
user_selections = {}
user_lock = threading.Lock()

def all_symbols():
    return [(k, n, e) for cat in CATEGORIES.values() for k, n, e in cat['symbols']]

def get_sels(uid):
    with user_lock:
        return user_selections.get(uid, [])

def set_sels(uid, symbols):
    with user_lock:
        user_selections[uid] = symbols

# ======================== دریافت قیمت ========================
def fetch_yahoo(symbol):
    try:
        resp = cffi_requests.get(
            f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1h&range=1d",
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"},
            impersonate="chrome120", timeout=8
        )
        data = resp.json()
        result = data['chart']['result'][0]
        return float(result['meta']['regularMarketPrice']) or float(result['indicators']['quote'][0]['close'][-1])
    except:
        return None

def is_holiday():
    with holiday_cache['lock']:
        today = datetime.now().strftime('%Y-%m-%d')
        if holiday_cache['date'] == today:
            return holiday_cache['is_holiday']
        api_key = os.environ.get('CALENDARIFIC_API_KEY')
        if not api_key:
            holiday_cache.update({'date': today, 'is_holiday': False})
            return False
        try:
            resp = requests.get(f"https://calendarific.com/api/v2/holidays?api_key={api_key}&country=US&year={datetime.now().year}&day={today}", timeout=5)
            data = resp.json()
            h = data.get('response', {}).get('holidays', [])
            holiday_cache.update({'date': today, 'is_holiday': bool(h)})
            if h: print(f"📅 تعطیل: {h[0].get('name')}")
            return bool(h)
        except:
            holiday_cache.update({'date': today, 'is_holiday': False})
            return False

def is_market_open(sym):
    now = datetime.now()
    today = now.weekday()
    if sym in ['btc','eth','bnb','gram','xrp','sol','doge','bch','ltc','trx']:
        return True
    if today in [5,6] or is_holiday():
        return False
    if sym == 'sugar':
        h = (now.hour + 3) % 24
        m = (now.minute + 30) % 60
        if now.minute + 30 >= 60: h = (h + 1) % 24
        return 12 <= h < 21 or (h == 21 and m <= 30)
    return True

def fetch_price(sym):
    if not is_market_open(sym):
        return None
    map_sym = {
        'gram':'GRAM-USD','btc':'BTC-USD','eth':'ETH-USD','bnb':'BNB-USD',
        'xrp':'XRP-USD','sol':'SOL-USD','doge':'DOGE-USD','bch':'BCH-USD',
        'ltc':'LTC-USD','trx':'TRX-USD','gold':'XAUT-USD','silver':'SI=F',
        'oil':'CL=F','brent':'BZ=F','gas':'NG=F','sugar':'SB=F'
    }
    price = fetch_yahoo(map_sym.get(sym))
    if sym == 'sugar' and price:
        return round(price / 100, 4)
    return price

# ======================== دیتابیس ========================
DB_PATH = "market_data.db"

def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute('CREATE TABLE IF NOT EXISTS prices (symbol TEXT, timestamp TEXT, price REAL, PRIMARY KEY (symbol, timestamp))')
        c.execute('CREATE TABLE IF NOT EXISTS closing_prices (symbol TEXT, timestamp TEXT, price REAL, PRIMARY KEY (symbol, timestamp))')
        conn.commit()

def save_price(sym, price):
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute('INSERT INTO prices (symbol, timestamp, price) VALUES (?, ?, ?)', (sym, datetime.now().isoformat(), price))
        c.execute('INSERT OR REPLACE INTO closing_prices (symbol, timestamp, price) VALUES (?, ?, ?)', (sym, datetime.now().isoformat(), price))
        conn.commit()

def get_last(sym):
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute('SELECT price FROM prices WHERE symbol=? ORDER BY timestamp DESC LIMIT 1', (sym,))
        row = c.fetchone()
        return row[0] if row else None

def get_24h(sym):
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute('SELECT price FROM prices WHERE symbol=? AND timestamp <= ? ORDER BY timestamp DESC LIMIT 1', (sym, (datetime.now() - timedelta(hours=24)).isoformat()))
        row = c.fetchone()
        return row[0] if row else None

def clean_old():
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute('DELETE FROM prices WHERE timestamp < ?', ((datetime.now() - timedelta(days=30)).isoformat(),))
        c.execute('DELETE FROM closing_prices WHERE timestamp < ?', ((datetime.now() - timedelta(days=30)).isoformat(),))
        conn.commit()

def refresh_cache():
    with price_cache['lock']:
        if time.time() - price_cache['last_update'] < INTERVAL:
            return
        print(f"🔄 {datetime.now().isoformat()}")
        data = {}
        for k, _, _ in all_symbols():
            new = fetch_price(k)
            old24 = get_24h(k)
            if new is not None:
                data[k] = {'new': new, 'old24': old24}
                save_price(k, new)
            else:
                closing = get_last(k)
                if closing is not None:
                    data[k] = {'new': closing, 'old24': old24}
        price_cache['data'] = data
        price_cache['last_update'] = time.time()
        clean_old()

def get_cached(sym):
    refresh_cache()
    with price_cache['lock']:
        d = price_cache['data'].get(sym)
        return (d['new'], d['old24']) if d else (None, None)

# ======================== فرمت‌ها ========================
def fmt_price(p, sym):
    if p is None: return "⛔ در دسترس نیست"
    if sym == 'gram': return f"{p:,.4f}"
    if p < 0.001: return f"{p:.4e}"
    if p < 1: return f"{p:.6f}"
    return f"{p:,.2f}"

def fmt_change(c):
    if c is None: return ""
    if abs(c) < 0.0001: return "➖ بدون تغییر"
    return f"📈 {c:+.2f}%" if c > 0 else f"📉 {c:+.2f}%"

def gen_msg(symbols):
    lines = []
    for k, n, e in symbols:
        new, old = get_cached(k)
        if new is None:
            if not is_market_open(k):
                last = get_last(k)
                lines.append(f"{e} {n} : {fmt_price(last, k)} 🔒 بازار بسته" if last else f"{e} {n} : 🔒 بازار بسته")
            else:
                lines.append(f"{e} {n} : ⛔ در دسترس نیست")
            continue
        change = ((new - old) / old * 100) if old and old > 0 else None
        lines.append(f"{e} {n} : {fmt_price(new, k)} {fmt_change(change)}")
    return "\n".join(lines)

# ======================== ربات ========================
def get_keyboard(uid):
    sels = get_sels(uid)
    kb = []
    for k, n, e in all_symbols():
        kb.append([InlineKeyboardButton(f"{'✅ ' if k in sels else ''}{e} {n}", callback_data=f"t_{k}")])
    kb.append([InlineKeyboardButton("📊 انتخاب همه", callback_data="sel_all")])
    kb.append([InlineKeyboardButton("🗑️ پاک کردن", callback_data="clr_all")])
    kb.append([InlineKeyboardButton("🔙 بازگشت", callback_data="back")])
    return InlineKeyboardMarkup(kb)

async def start(upd, ctx):
    await upd.message.reply_text(
        "📊 **به ربات قیمت‌های لحظه‌ای خوش آمدید!**\nلطفاً یک دسته را انتخاب کنید:",
        parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(f"{cat['emoji']} {cat['name']}", callback_data=f"cat_{k}")] for k, cat in CATEGORIES.items()
        ] + [
            [InlineKeyboardButton("📊 نمایش همه", callback_data="show_all")],
            [InlineKeyboardButton("⚙️ انتخاب نمادها", callback_data="edit")]
        ])
    )

async def button(upd, ctx):
    q = upd.callback_query
    await q.answer()
    uid = q.from_user.id
    data = q.data

    if data == "back":
        await start(upd, ctx)
        return

    if data == "edit":
        await q.edit_message_text(
            "📊 **انتخاب نمادها**\nروی هر نماد کلیک کنید تا انتخاب/لغو شود.\n\n**انتخاب‌شده:**\n" +
            ("\n".join([f"{e} {n}" for k,n,e in all_symbols() if k in get_sels(uid)]) or "هیچ"),
            parse_mode='Markdown', reply_markup=get_keyboard(uid)
        )
        return

    if data == "sel_all":
        set_sels(uid, [k for k,_,_ in all_symbols()])
        await q.edit_message_text("📊 **انتخاب نمادها**", parse_mode='Markdown', reply_markup=get_keyboard(uid))
        return

    if data == "clr_all":
        set_sels(uid, [])
        await q.edit_message_text("📊 **انتخاب نمادها**", parse_mode='Markdown', reply_markup=get_keyboard(uid))
        return

    if data.startswith("t_"):
        sym = data[2:]
        sels = get_sels(uid)
        if sym in sels:
            sels.remove(sym)
        else:
            sels.append(sym)
        set_sels(uid, sels)
        await q.edit_message_text("📊 **انتخاب نمادها**", parse_mode='Markdown', reply_markup=get_keyboard(uid))
        return

    if data == "show_all":
        await q.edit_message_text(
            f"📊 **همه نمادها**\n━━━━━━━━━━━━━━━━━━━\n{gen_msg(all_symbols())}",
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 بازگشت", callback_data="back")]])
        )
        return

    if data.startswith("cat_"):
        key = data[4:]
        cat = CATEGORIES[key]
        await q.edit_message_text(
            f"📊 **{cat['emoji']} {cat['name']}**\n━━━━━━━━━━━━━━━━━━━\n{gen_msg(cat['symbols'])}",
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 بازگشت", callback_data="back")]])
        )
        return

async def myprices(upd, ctx):
    uid = upd.effective_user.id
    sels = get_sels(uid)
    if not sels:
        await upd.message.reply_text("⚠️ هیچ نمادی انتخاب نشده است.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⚙️ انتخاب نمادها", callback_data="edit")]]))
        return
    selected = [(k,n,e) for k,n,e in all_symbols() if k in sels]
    await upd.message.reply_text(
        f"📊 **نمادهای انتخاب‌شده**\n━━━━━━━━━━━━━━━━━━━\n{gen_msg(selected)}",
        parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 بازگشت", callback_data="back")]])
    )

async def all_cmd(upd, ctx):
    await upd.message.reply_text(
        f"📊 **همه نمادها**\n━━━━━━━━━━━━━━━━━━━\n{gen_msg(all_symbols())}",
        parse_mode='Markdown'
    )

async def help_cmd(upd, ctx):
    await upd.message.reply_text(
        "📋 **دستورات:**\n/start - منوی اصلی\n/all - همه قیمت‌ها\n/myprices - قیمت‌های انتخاب‌شده"
    )

# ======================== وب سرویس ========================
flask_app = Flask(__name__)
@flask_app.route('/')
def home(): return "✅ ربات در حال اجراست!"
@flask_app.route('/health')
def health(): return "OK", 200
def run_flask():
    flask_app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 10000)))

# ======================== اجرا ========================
def run_bot_in_main_thread():
    app = Application.builder().token(TELEGRAM_TOKEN).connect_timeout(TIMEOUT).read_timeout(TIMEOUT).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("all", all_cmd))
    app.add_handler(CommandHandler("myprices", myprices))
    app.add_handler(CallbackQueryHandler(button))
    
    # حذف Webhook با استفاده از حلقه‌ی جاری (رفع خطای Event loop is closed)
    try:
        loop = asyncio.get_event_loop()
        if loop.is_closed():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        loop.run_until_complete(app.bot.delete_webhook())
    except Exception as e:
        print(f"⚠️ خطا در حذف Webhook: {e}")
    
    print("🤖 ربات در حال اجرا...")
    app.run_polling()

if __name__ == '__main__':
    init_db()
    threading.Thread(target=run_flask, daemon=True).start()
    run_bot_in_main_thread()
