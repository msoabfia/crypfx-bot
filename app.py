import os
os.environ['TZ'] = 'UTC'
import asyncio
import time
import sqlite3
import requests
import re
import json
from datetime import datetime, timedelta
from curl_cffi import requests as cffi_requests
from flask import Flask
from telegram import Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes, CallbackQueryHandler
from telegram.error import Forbidden
import logging
import threading

# ============ تنظیمات اولیه ============
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN')
if not TELEGRAM_TOKEN: raise ValueError("TELEGRAM_TOKEN not set!")
INTERVAL = 60
TIMEOUT = 30
logging.basicConfig(level=logging.INFO)

# ============ دسته‌بندی نمادها ============
CATEGORIES = {
    'crypto': {'name': 'ارزهای دیجیتال', 'emoji': '💰', 'symbols': [
        ('btc', 'BTC', '₿'), ('eth', 'ETH', '💎'), ('bnb', 'BNB', '🟡'),
        ('sol', 'SOL', '☀️'), ('ltc', 'LTC', '⚡'), ('bch', 'BCH', '🔶'),
        ('xrp', 'XRP', '💠'), ('trx', 'TRX', '🔴'), ('doge', 'DOGE', '🐕'),
        ('gram', 'GRAM', '🔷')]},
    'metals': {'name': 'فلزات گرانبها', 'emoji': '🏆', 'symbols': [
        ('gold', 'GOLD', '🏆'), ('silver', 'SILVER', '🥈')]},
    'energy': {'name': 'انرژی و نفت', 'emoji': '⛽', 'symbols': [
        ('oil', 'OIL', '🛢️'), ('brent', 'BRENT', '🛢️'), ('gas', 'GAS', '🔥')]},
    'agriculture': {'name': 'کشاورزی', 'emoji': '🌾', 'symbols': [
        ('sugar', 'SUGAR', '🍬')]}
}

# ============ دیتابیس قیمت‌ها (محلی) ============
DB_PATH = "market_data.db"
def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS prices (symbol TEXT, timestamp TEXT, price REAL, PRIMARY KEY (symbol, timestamp))''')
        c.execute('''CREATE TABLE IF NOT EXISTS closing_prices (symbol TEXT, timestamp TEXT, price REAL, PRIMARY KEY (symbol, timestamp))''')
        conn.commit()
init_db()

def save_price(s, p):
    with sqlite3.connect(DB_PATH) as conn:
        conn.cursor().execute('INSERT INTO prices (symbol, timestamp, price) VALUES (?, ?, ?)', (s, datetime.now().isoformat(), p))
def save_closing(s, p):
    with sqlite3.connect(DB_PATH) as conn:
        conn.cursor().execute('INSERT OR REPLACE INTO closing_prices (symbol, timestamp, price) VALUES (?, ?, ?)', (s, datetime.now().isoformat(), p))

def get_last(s):
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.cursor().execute('SELECT price FROM prices WHERE symbol=? ORDER BY timestamp DESC LIMIT 1', (s,)).fetchone()
        return row[0] if row else None

def get_closing(s):
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.cursor().execute('SELECT price FROM closing_prices WHERE symbol=? ORDER BY timestamp DESC LIMIT 1', (s,)).fetchone()
        return row[0] if row else None

def get_24h(s):
    with sqlite3.connect(DB_PATH) as conn:
        t = (datetime.now() - timedelta(hours=24)).isoformat()
        row = conn.cursor().execute('SELECT price FROM prices WHERE symbol=? AND timestamp <= ? ORDER BY timestamp DESC LIMIT 1', (s, t)).fetchone()
        return row[0] if row else None

def clean_old():
    with sqlite3.connect(DB_PATH) as conn:
        cutoff = (datetime.now() - timedelta(days=30)).isoformat()
        conn.cursor().execute('DELETE FROM prices WHERE timestamp < ?', (cutoff,))
        conn.cursor().execute('DELETE FROM closing_prices WHERE timestamp < ?', (cutoff,))
        conn.commit()

# ============ تشخیص تعطیلات رسمی (Calendarific) ============
holiday_cache = {'date': '', 'is_holiday': False, 'lock': threading.Lock()}
def is_holiday_today():
    with holiday_cache['lock']:
        today = datetime.now().strftime('%Y-%m-%d')
        if holiday_cache['date'] == today: return holiday_cache['is_holiday']
        api_key = os.environ.get('CALENDARIFIC_API_KEY')
        if not api_key: return False
        try:
            resp = requests.get(f"https://calendarific.com/api/v2/holidays?api_key={api_key}&country=US&year={datetime.now().year}&day={today}", timeout=5)
            if resp.status_code == 200:
                data = resp.json()
                if data.get('meta', {}).get('code') == 200 and data.get('response', {}).get('holidays'):
                    holiday_cache['date'] = today; holiday_cache['is_holiday'] = True
                    return True
        except: pass
        holiday_cache['date'] = today; holiday_cache['is_holiday'] = False
        return False

# ============ دریافت قیمت از یاهو ============
def fetch_yahoo(symbol):
    try:
        resp = cffi_requests.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1h&range=1d",
                                 headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"},
                                 impersonate="chrome120", timeout=8)
        data = resp.json()
        result = data['chart']['result'][0]
        price = result['meta'].get('regularMarketPrice')
        if price is not None: return float(price)
        for p in reversed(result['indicators']['quote'][0]['close']):
            if p is not None: return float(p)
    except: return None

# ============ تشخیص بازار باز/بسته ============
def is_market_open(symbol_key):
    now = datetime.now(); today = now.weekday()
    if symbol_key in ['btc','eth','bnb','gram','xrp','sol','doge','bch','ltc','trx','dot']: return True
    if today in [5,6]: return False
    if is_holiday_today(): return False
    if symbol_key == 'sugar':
        iran_hour = (now.hour + 3) % 24; iran_minute = now.minute + 30
        if iran_minute >= 60: iran_hour = (iran_hour + 1) % 24; iran_minute -= 60
        return 12 <= iran_hour < 21 or (iran_hour == 21 and iran_minute <= 30)
    return True

# ============ کش قیمت‌ها ============
price_cache = {'data': {}, 'last_update': 0, 'lock': threading.Lock()}
SYMBOL_MAP = {
    'gram':'GRAM-USD','btc':'BTC-USD','eth':'ETH-USD','bnb':'BNB-USD','xrp':'XRP-USD',
    'sol':'SOL-USD','doge':'DOGE-USD','bch':'BCH-USD','ltc':'LTC-USD','trx':'TRX-USD',
    'gold':'XAUT-USD','silver':'SI=F','oil':'CL=F','brent':'BZ=F','gas':'NG=F','sugar':'SB=F'
}
def refresh_cache():
    with price_cache['lock']:
        if time.time() - price_cache['last_update'] < INTERVAL: return
        print(f"🔄 به‌روزرسانی کش قیمت‌ها در {datetime.now().isoformat()}")
        new_data = {}
        for symbol in SYMBOL_MAP.keys():
            new_price = fetch_yahoo(SYMBOL_MAP[symbol]) if is_market_open(symbol) else None
            old_24h = get_24h(symbol)
            if new_price is not None:
                new_data[symbol] = {'new': new_price, 'old_24h': old_24h}
                save_price(symbol, new_price); save_closing(symbol, new_price)
            else:
                closing = get_closing(symbol) or get_last(symbol)
                if closing is not None:
                    new_data[symbol] = {'new': closing, 'old_24h': old_24h}
        price_cache['data'] = new_data
        price_cache['last_update'] = time.time()
        clean_old()

# ============ حافظه موقت (RAM) برای کاربران ============
user_selections = {}; sending_active = {}; last_sent_summary = {}; user_lock = threading.Lock()
def get_sel(u): return user_selections.get(u, [])
def save_sel(u, s):
    with user_lock:
        if u not in user_selections: user_selections[u] = []
        if s not in user_selections[u]: user_selections[u].append(s)
def rem_sel(u, s):
    with user_lock:
        if u in user_selections and s in user_selections[u]:
            user_selections[u].remove(s)
            if not user_selections[u]: del user_selections[u]
def clear_sel(u):
    with user_lock:
        if u in user_selections: del user_selections[u]

def all_symbols(): return [(k, SYMBOL_MAP[k], emoji) for cat in CATEGORIES.values() for k, _, emoji in cat['symbols']]
def format_price(p, k):
    if p is None: return "⛔ در دسترس نیست"
    if k == 'gram': return f"{p:,.4f}"
    return f"{p:.4e}" if p < 0.001 else f"{p:.6f}" if p < 1 else f"{p:,.2f}"
def format_change(c):
    if c is None: return ""
    return "➖ بدون تغییر" if abs(c) < 0.0001 else f"📈 {c:+.2f}%" if c > 0 else f"📉 {c:+.2f}%"

def generate_message(selections):
    refresh_cache()
    lines = []
    for cat in CATEGORIES.values():
        selected = [s for s in cat['symbols'] if s[0] in selections]
        if not selected: continue
        lines.append(f"{cat['emoji']} {cat['name']}:")
        for key, name, emoji in selected:
            data = price_cache['data'].get(key)
            if not data:
                lines.append(f"{emoji} {name} : {'🔒 بازار بسته' if not is_market_open(key) else '⛔ در دسترس نیست'}")
                continue
            new_price, old_24h = data['new'], data['old_24h']
            formatted = format_price(new_price, key)
            change = None
            if old_24h and old_24h > 0:
                change = ((new_price - old_24h) / old_24h) * 100
            change_text = format_change(change)
            lines.append(f"{emoji} {name} : {formatted} {change_text}".strip())
        lines.append("")
    return "\n".join(lines)

# ============ توابع ربات ============
async def send_msg(chat_id, text, parse_mode='Markdown', reply_markup=None):
    await Bot(token=TELEGRAM_TOKEN).send_message(chat_id=chat_id, text=text, parse_mode=parse_mode, reply_markup=reply_markup)

def main_keyboard():
    kb = [[InlineKeyboardButton(f"{cat['emoji']} {cat['name']}", callback_data=f"cat_{k}")] for k, cat in CATEGORIES.items()]
    kb.append([InlineKeyboardButton("📊 نمایش همه", callback_data="show_all")])
    kb.append([InlineKeyboardButton("📋 وضعیت دیتابیس", callback_data="status")])
    return InlineKeyboardMarkup(kb)

def symbol_keyboard(selections, all_symbols, back_data=None):
    kb = [[InlineKeyboardButton(f"{'✅ ' if s[0] in selections else ''}{s[2]} {s[1]}", callback_data=f"toggle_{s[0]}")] for s in all_symbols]
    kb.append([InlineKeyboardButton("🔙 بازگشت", callback_data=back_data or "back_categories")])
    kb.append([InlineKeyboardButton("📊 انتخاب همه", callback_data="select_all")])
    kb.append([InlineKeyboardButton("🚀 شروع ارسال", callback_data="start_sending")])
    kb.append([InlineKeyboardButton("🛑 توقف ارسال", callback_data="stop_sending")])
    kb.append([InlineKeyboardButton("🗑️ پاک کردن همه انتخاب‌ها", callback_data="clear_all")])
    return InlineKeyboardMarkup(kb)

async def show_main(chat_id, user_id, query=None):
    text = "📊 **به ربات قیمت‌های لحظه‌ای خوش آمدید!**\nلطفاً یک دسته را انتخاب کنید:"
    if query: await query.edit_message_text(text, parse_mode='Markdown', reply_markup=main_keyboard())
    else: await send_msg(chat_id, text, parse_mode='Markdown', reply_markup=main_keyboard())

async def show_category(chat_id, user_id, cat_key, query=None):
    cat = CATEGORIES[cat_key]
    selections = get_sel(user_id)
    text = f"📊 **{cat['emoji']} {cat['name']}**\n✅ روی هر نماد کلیک کنید تا انتخاب/لغو شود.\nبعد از انتخاب، روی **شروع ارسال** کلیک کنید.\n\n**انتخاب‌شده:**\n"
    selected = [f"{s[2]} {s[1]}" for s in cat['symbols'] if s[0] in selections]
    text += "\n".join(selected) if selected else "هیچ نمادی انتخاب نشده است."
    kb = symbol_keyboard(selections, cat['symbols'], "back_categories")
    if query: await query.edit_message_text(text, parse_mode='Markdown', reply_markup=kb)
    else: await send_msg(chat_id, text, parse_mode='Markdown', reply_markup=kb)

async def show_all(chat_id, user_id, query=None):
    selections = get_sel(user_id)
    all_syms = [(k, name, emoji) for cat in CATEGORIES.values() for k, name, emoji in cat['symbols']]
    text = "📊 **همه نمادها**\n✅ روی هر نماد کلیک کنید تا انتخاب/لغو شود.\nبعد از انتخاب، روی **شروع ارسال** کلیک کنید.\n\n**انتخاب‌شده:**\n"
    selected = [f"{s[2]} {s[1]}" for s in all_syms if s[0] in selections]
    text += "\n".join(selected) if selected else "هیچ نمادی انتخاب نشده است."
    kb = symbol_keyboard(selections, all_syms, "back_categories")
    if query: await query.edit_message_text(text, parse_mode='Markdown', reply_markup=kb)
    else: await send_msg(chat_id, text, parse_mode='Markdown', reply_markup=kb)

async def start(update, context):
    await show_main(update.effective_chat.id, update.effective_user.id)

async def button_handler(update, context):
    query = update.callback_query; await query.answer()
    user_id, chat_id, data = query.from_user.id, query.message.chat.id, query.data
    if data == "back_categories": await show_main(chat_id, user_id, query); return
    if data == "status":
        report = "📊 **وضعیت دیتابیس (قیمت‌ها)**\n" + "\n".join(f"🔹 {n}: {get_last(k) or 'ندارد'}" for k, n, _ in all_symbols())
        await query.edit_message_text(report, parse_mode='Markdown'); return
    if data == "clear_all": clear_sel(user_id); await query.edit_message_text("🗑️ همه انتخاب‌ها پاک شد."); await show_main(chat_id, user_id); return
    if data == "select_all":
        with user_lock:
            user_selections[user_id] = [s[0] for s in all_symbols()]
        await query.edit_message_text("📊 همه نمادها انتخاب شدند."); await show_all(chat_id, user_id, query); return
    if data == "show_all": await show_all(chat_id, user_id, query); return
    if data.startswith("cat_"): await show_category(chat_id, user_id, data[4:], query); return
    if data.startswith("toggle_"):
        symbol = data[7:]
        if symbol in get_sel(user_id): rem_sel(user_id, symbol)
        else: save_sel(user_id, symbol)
        await show_all(chat_id, user_id, query); return
    if data == "start_sending":
        if not get_sel(user_id):
            await query.edit_message_text("⚠️ حداقل یک نماد انتخاب کنید.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 بازگشت", callback_data="back_categories")]])); return
        with user_lock: sending_active[user_id] = True; last_sent_summary[user_id] = ""
        await query.edit_message_text("🚀 **ارسال خودکار شروع شد!**\nهر ۱ دقیقه قیمت‌های انتخاب‌شده ارسال می‌شود.", parse_mode='Markdown'); return
    if data == "stop_sending":
        with user_lock: sending_active[user_id] = False
        await query.edit_message_text("🛑 **ارسال خودکار متوقف شد.**", parse_mode='Markdown'); return

async def single_status(update, symbol_key, name, emoji):
    chat_id = update.effective_chat.id
    refresh_cache()
    data = price_cache['data'].get(symbol_key)
    if not data:
        await send_msg(chat_id, f"{emoji} **{name}**\n{'🔒 بازار بسته' if not is_market_open(symbol_key) else '⛔ در دسترس نیست'}", parse_mode='Markdown')
        return
    new_price, old_24h = data['new'], data['old_24h']
    formatted = format_price(new_price, symbol_key)
    change = None
    if old_24h and old_24h > 0: change = ((new_price - old_24h) / old_24h) * 100
    change_text = format_change(change)
    await send_msg(chat_id, f"{emoji} **{name}**\n💰 {formatted}\n{change_text}".strip(), parse_mode='Markdown')

# ============ دستورات هر نماد ============
for k, n, e in all_symbols():
    async def handler(update, context, key=k, name=n, emoji=e):
        await single_status(update, key, name, emoji)
    globals()[k] = handler

async def all_status(update, context):
    selections = get_sel(update.effective_user.id)
    if not selections:
        await send_msg(update.effective_chat.id, "⚠️ هیچ نمادی انتخاب نشده است. لطفاً ابتدا نمادهای مورد نظر را انتخاب کنید.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 بازگشت به منو", callback_data="back_categories")]]))
        return
    await send_msg(update.effective_chat.id, f"📊 **قیمت‌های لحظه‌ای**\n━━━━━━━━━━━━━━━━━━━\n{generate_message(selections)}", parse_mode='Markdown')

async def status_cmd(update, context):
    await send_msg(update.effective_chat.id, "📊 **وضعیت دیتابیس (قیمت‌ها)**\n" + "\n".join(f"🔹 {n}: {get_last(k) or 'ندارد'}" for k, n, _ in all_symbols()))

async def help_command(update, context):
    await send_msg(update.effective_chat.id, "📋 **دستورات:**\n/start - منوی اصلی\n/all - نمایش قیمت‌های انتخاب‌شده\n/status - وضعیت دیتابیس")

# ============ ارسال خودکار (با JobQueue) ============
async def auto_send_job(context: ContextTypes.DEFAULT_TYPE):
    print("⏳ [JOB] شروع سیکل ارسال خودکار...")
    try: refresh_cache()
    except Exception as e: print(f"❌ [JOB] خطا در refresh_cache: {e}"); return
    with user_lock:
        active = list(sending_active.items())
    for uid, active_flag in active:
        if not active_flag: continue
        selections = get_sel(uid)
        if not selections:
            with user_lock: sending_active[uid] = False
            continue
        msg = generate_message(selections)
        if msg:
            with user_lock:
                last = last_sent_summary.get(uid, "")
            if msg != last:
                try:
                    await context.bot.send_message(uid, f"🔔 **به‌روزرسانی قیمت‌ها**\n━━━━━━━━━━━━━━━━━━━\n{msg}", parse_mode='Markdown',
                                                   reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⚙️ ویرایش نمادها", callback_data="show_all")]]))
                    with user_lock: last_sent_summary[uid] = msg
                except Forbidden:
                    with user_lock: sending_active[uid] = False
                    clear_sel(uid)

# ============ راه‌اندازی ============
async def run_bot():
    app = Application.builder().token(TELEGRAM_TOKEN).connect_timeout(TIMEOUT).read_timeout(TIMEOUT).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("all", all_status))
    app.add_handler(CommandHandler("status", status_cmd))
    for k, n, e in all_symbols():
        app.add_handler(CommandHandler(k, globals()[k]))
    app.add_handler(CallbackQueryHandler(button_handler))
    if app.job_queue:
        app.job_queue.run_repeating(auto_send_job, interval=INTERVAL, first=5)
        print(f"✅ JobQueue تنظیم شد (هر {INTERVAL} ثانیه).")
    else:
        print("⚠️ JobQueue در دسترس نیست! لطفاً `python-telegram-bot[job-queue]` را نصب کنید.")
    await app.bot.delete_webhook()
    print("🤖 ربات در حال اجرا...")
    await app.initialize(); await app.start(); await app.updater.start_polling()
    try:
        while True: await asyncio.sleep(60)
    except KeyboardInterrupt:
        await app.stop(); await app.shutdown()

# ============ Flask برای Health Check ============
flask_app = Flask(__name__)
@flask_app.route('/')
def home(): return "✅ ربات در حال اجراست!"
@flask_app.route('/health')
def health(): return "OK", 200

def run_flask():
    flask_app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 10000)))

# ============ اجرای اصلی ============
if __name__ == '__main__':
    threading.Thread(target=run_flask, daemon=True).start()
    asyncio.run(run_bot())
