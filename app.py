import os
os.environ['TZ'] = 'UTC'
import asyncio
import time
import sqlite3
import requests
import re
from datetime import datetime
from curl_cffi import requests as cffi_requests
from flask import Flask
from telegram import Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes, CallbackQueryHandler
from telegram.error import TimedOut, NetworkError
import logging
import threading

# ======== تنظیمات ========
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN')
if not TELEGRAM_TOKEN:
    raise ValueError("TELEGRAM_TOKEN environment variable not set!")
CHAT_ID = os.environ.get('CHAT_ID', '483833953')
INTERVAL = 60
TIMEOUT = 30
# =========================

logging.basicConfig(level=logging.ERROR)

# ======== دسته‌بندی نمادها ========
CATEGORIES = {
    'fiat': {
        'name': 'واحد پولی(تومان)',
        'emoji': '💳',
        'symbols': [
            ('usdt', 'USDT', '💵'),
            ('aed', 'AED', '🇦🇪'),
        ]
    },
    'crypto': {
        'name': 'ارزهای دیجیتال',
        'emoji': '💰',
        'symbols': [
            ('btc', 'BTC', '₿'),
            ('eth', 'ETH', '💎'),
            ('bnb', 'BNB', '🟡'),
            ('sol', 'SOL', '☀️'),
            ('ltc', 'LTC', '⚡'),
            ('bch', 'BCH', '🔶'),
            ('xrp', 'XRP', '💠'),
            ('trx', 'TRX', '🔴'),
            ('doge', 'DOGE', '🐕'),
            ('gram', 'GRAM', '🔷'),
        ]
    },
    'metals': {
        'name': 'فلزات گرانبها',
        'emoji': '🏆',
        'symbols': [
            ('gold', 'GOLD', '🏆'),
            ('silver', 'SILVER', '🥈'),
        ]
    },
    'energy': {
        'name': 'انرژی و نفت',
        'emoji': '⛽',
        'symbols': [
            ('oil', 'OIL', '🛢️'),
            ('brent', 'BRENT', '🛢️'),
            ('gas', 'GAS', '🔥'),
        ]
    },
    'agriculture': {
        'name': 'کشاورزی',
        'emoji': '🌾',
        'symbols': [
            ('sugar', 'SUGAR', '🍬'),
        ]
    }
}

# ======== دریافت قیمت از tgju.org ========
def fetch_tgju_data():
    try:
        resp = requests.get('https://www.tgju.org/', timeout=10)
        if resp.status_code != 200:
            return None
        return resp.text
    except:
        return None

def extract_price(html, pattern):
    match = re.search(pattern, html)
    if match:
        price = match.group(1).replace(',', '').strip()
        return int(float(price))
    return None

def fetch_usdt_price():
    html = fetch_tgju_data()
    if not html:
        return None
    patterns = [
        r'<span class="price">([\d,]+)</span>',
        r'data-price="([\d,]+)"',
        r'id="price" value="([\d,]+)"',
    ]
    for pattern in patterns:
        price = extract_price(html, pattern)
        if price:
            return price
    return None

def fetch_aed_price():
    html = fetch_tgju_data()
    if not html:
        return None
    patterns = [
        r'<span class="price">([\d,]+)</span>',
        r'data-price="([\d,]+)"',
        r'id="price" value="([\d,]+)"',
    ]
    for pattern in patterns:
        price = extract_price(html, pattern)
        if price:
            return price
    return None

def fetch_gold_price():
    html = fetch_tgju_data()
    if not html:
        return None
    patterns = [
        r'<span class="price">([\d,]+)</span>',
        r'data-price="([\d,]+)"',
        r'id="price" value="([\d,]+)"',
    ]
    for pattern in patterns:
        price = extract_price(html, pattern)
        if price:
            return price
    return None

# ======== دریافت قیمت از یاهو ========
def fetch_yahoo(symbol):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1h&range=1d"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"}
    try:
        resp = cffi_requests.get(url, headers=headers, impersonate="chrome120", timeout=10)
        data = resp.json()
        price = data['chart']['result'][0]['indicators']['quote'][0]['close'][-1]
        return float(price) if price is not None else None
    except:
        return None

def fetch_twelve(symbol):
    url = f"https://api.twelvedata.com/price?symbol={symbol}&apikey=f8f6fe94d43b454ba0c9431ff529c466"
    try:
        resp = requests.get(url, timeout=10)
        data = resp.json()
        return float(data['price']) if 'price' in data else None
    except:
        return None

def fetch_coingecko(symbol):
    url = f"https://api.coingecko.com/api/v3/simple/price?ids={symbol}&vs_currencies=usd"
    try:
        resp = requests.get(url, timeout=10)
        data = resp.json()
        return float(data[symbol]['usd'])
    except:
        return None

# ======== دریافت قیمت اصلی ========
def get_price(symbol_key):
    if not is_market_open(symbol_key):
        return None

    if symbol_key == 'usdt':
        return fetch_usdt_price()
    elif symbol_key == 'aed':
        return fetch_aed_price()
    elif symbol_key == 'gold':
        price = fetch_gold_price()
        if price:
            return price
        return fetch_yahoo('GC=F')
    elif symbol_key == 'gram':
        return fetch_yahoo('TON-USD')
    elif symbol_key == 'btc':
        return fetch_yahoo('BTC-USD') or fetch_twelve('BTC/USD')
    elif symbol_key == 'eth':
        return fetch_yahoo('ETH-USD') or fetch_twelve('ETH/USD')
    elif symbol_key == 'bnb':
        return fetch_yahoo('BNB-USD') or fetch_twelve('BNB/USD')
    elif symbol_key == 'xrp':
        return fetch_yahoo('XRP-USD') or fetch_twelve('XRP/USD')
    elif symbol_key == 'sol':
        return fetch_yahoo('SOL-USD') or fetch_twelve('SOL/USD')
    elif symbol_key == 'doge':
        return fetch_yahoo('DOGE-USD') or fetch_twelve('DOGE/USD')
    elif symbol_key == 'bch':
        return fetch_yahoo('BCH-USD') or fetch_twelve('BCH/USD')
    elif symbol_key == 'ltc':
        return fetch_yahoo('LTC-USD') or fetch_twelve('LTC/USD')
    elif symbol_key == 'trx':
        return fetch_yahoo('TRX-USD') or fetch_twelve('TRX/USD')
    elif symbol_key == 'silver':
        return fetch_yahoo('SI=F')
    elif symbol_key == 'oil':
        return fetch_yahoo('CL=F')
    elif symbol_key == 'brent':
        return fetch_yahoo('BZ=F')
    elif symbol_key == 'gas':
        return fetch_yahoo('NG=F')
    elif symbol_key == 'sugar':
        price = fetch_yahoo('SB=F')
        return round(price / 100, 4) if price else None
    return None

def is_market_open(symbol_key):
    now = datetime.now()
    today = now.weekday()
    if symbol_key in ['btc', 'eth', 'bnb', 'gram', 'xrp', 'sol', 'doge', 'bch', 'ltc', 'trx', 'dot', 'usdt', 'aed']:
        return True
    if today == 6:
        return False
    if symbol_key == 'sugar':
        iran_hour = (now.hour + 3) % 24
        iran_minute = now.minute + 30
        if iran_minute >= 60:
            iran_hour = (iran_hour + 1) % 24
            iran_minute -= 60
        if iran_hour >= 12 and (iran_hour < 21 or (iran_hour == 21 and iran_minute <= 30)):
            return True
        return False
    return True

# ======== دیتابیس ========
DB_PATH = "market_data.db"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS prices
                 (symbol TEXT, timestamp TEXT, price REAL, PRIMARY KEY (symbol, timestamp))''')
    c.execute('''CREATE TABLE IF NOT EXISTS history
                 (symbol TEXT, timestamp TEXT, price REAL)''')
    c.execute('''CREATE TABLE IF NOT EXISTS user_selections
                 (user_id INTEGER, symbol TEXT, PRIMARY KEY (user_id, symbol))''')
    conn.commit()
    conn.close()

def save_price(symbol, price):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('INSERT INTO prices (symbol, timestamp, price) VALUES (?, ?, ?)',
              (symbol, datetime.now().isoformat(), price))
    conn.commit()
    conn.close()

def get_last_price(symbol):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT price FROM prices WHERE symbol=? ORDER BY timestamp DESC LIMIT 1', (symbol,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else None

def get_user_selections(user_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT symbol FROM user_selections WHERE user_id=?', (user_id,))
    rows = c.fetchall()
    conn.close()
    return [row[0] for row in rows]

def save_user_selection(user_id, symbol):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('INSERT OR IGNORE INTO user_selections (user_id, symbol) VALUES (?, ?)', (user_id, symbol))
    conn.commit()
    conn.close()

def remove_user_selection(user_id, symbol):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('DELETE FROM user_selections WHERE user_id=? AND symbol=?', (user_id, symbol))
    conn.commit()
    conn.close()

def clear_user_selections(user_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('DELETE FROM user_selections WHERE user_id=?', (user_id,))
    conn.commit()
    conn.close()

def select_all_symbols(user_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('DELETE FROM user_selections WHERE user_id=?', (user_id,))
    for category in CATEGORIES.values():
        for key, _, _ in category['symbols']:
            c.execute('INSERT OR IGNORE INTO user_selections (user_id, symbol) VALUES (?, ?)', (user_id, key))
    conn.commit()
    conn.close()

# ======== توابع ربات ========
sending_active = {}
last_sent_summary = {}

def get_all_symbols():
    all_symbols = []
    for category in CATEGORIES.values():
        all_symbols.extend(category['symbols'])
    return all_symbols

async def send_message(chat_id, text, parse_mode='Markdown', reply_markup=None):
    bot = Bot(token=TELEGRAM_TOKEN)
    await bot.send_message(chat_id=chat_id, text=text, parse_mode=parse_mode, reply_markup=reply_markup)

def format_price(price, symbol_key):
    if price is None:
        return "⛔ در دسترس نیست"
    if symbol_key in ['usdt', 'aed']:
        return f"{price:,.0f}"
    if price < 0.001:
        return f"{price:.4e}"
    elif price < 1:
        return f"{price:.6f}"
    else:
        return f"{price:,.2f}"

def format_change(change):
    if change is None:
        return ""
    if abs(change) < 0.001:
        return "➖ بدون تغییر"
    elif change > 0:
        return f"📈 {change:+.2f}%"
    else:
        return f"📉 {change:+.2f}%"

def get_price_with_old(symbol_key):
    old_price = get_last_price(symbol_key)
    new_price = get_price(symbol_key)
    if new_price is not None:
        save_price(symbol_key, new_price)
    return new_price, old_price

def generate_price_message(selections):
    lines = []
    for cat_key, cat in CATEGORIES.items():
        cat_selected = [s for s in cat['symbols'] if s[0] in selections]
        if not cat_selected:
            continue
        lines.append(f"{cat['emoji']} {cat['name']}:")
        for key, name, emoji in cat_selected:
            new_price, old_price = get_price_with_old(key)
            if new_price is None:
                last = get_last_price(key)
                if last is not None:
                    formatted = format_price(last, key)
                    lines.append(f"{emoji} {name} : {formatted} 🔒 بازار بسته")
                else:
                    lines.append(f"{emoji} {name} : ⛔ در دسترس نیست")
                continue

            formatted = format_price(new_price, key)
            change = None
            if old_price and old_price > 0:
                change = ((new_price - old_price) / old_price) * 100
            change_text = format_change(change)
            if change_text:
                lines.append(f"{emoji} {name} : {formatted} {change_text}")
            else:
                lines.append(f"{emoji} {name} : {formatted}")
        lines.append("")
    return "\n".join(lines) if lines else "هیچ نمادی انتخاب نشده است."

async def show_main_menu(chat_id, user_id):
    text = "📊 **به ربات قیمت‌های لحظه‌ای خوش آمدید!**\n\n"
    text += "لطفاً یک دسته را انتخاب کنید:\n"
    keyboard = []
    for cat_key, cat in CATEGORIES.items():
        keyboard.append([InlineKeyboardButton(f"{cat['emoji']} {cat['name']}", callback_data=f"cat_{cat_key}")])
    keyboard.append([InlineKeyboardButton("📊 نمایش همه", callback_data="show_all")])
    keyboard.append([InlineKeyboardButton("📋 وضعیت دیتابیس", callback_data="status")])
    reply_markup = InlineKeyboardMarkup(keyboard)
    await send_message(chat_id, text, parse_mode='Markdown', reply_markup=reply_markup)

async def show_category_symbols(chat_id, user_id, category_key):
    cat = CATEGORIES[category_key]
    selections = get_user_selections(user_id)
    text = f"📊 **{cat['emoji']} {cat['name']}**\n\n"
    text += "✅ روی هر نماد کلیک کنید تا انتخاب/لغو شود.\n"
    text += "بعد از انتخاب، روی **شروع ارسال** کلیک کنید.\n\n"
    text += "**انتخاب‌شده:**\n"
    selected = []
    for key, name, emoji in cat['symbols']:
        if key in selections:
            selected.append(f"{emoji} {name}")
    if selected:
        text += "\n".join(selected)
    else:
        text += "هیچ نمادی انتخاب نشده است."

    keyboard = []
    for key, name, emoji in cat['symbols']:
        checked = "✅ " if key in selections else ""
        keyboard.append([InlineKeyboardButton(f"{checked}{emoji} {name}", callback_data=f"toggle_{key}")])
    keyboard.append([InlineKeyboardButton("🔙 بازگشت به دسته‌ها", callback_data="back_categories")])
    keyboard.append([InlineKeyboardButton("📊 انتخاب همه", callback_data=f"select_all_cat_{category_key}")])
    keyboard.append([InlineKeyboardButton("🚀 شروع ارسال", callback_data="start_sending")])
    keyboard.append([InlineKeyboardButton("🛑 توقف ارسال", callback_data="stop_sending")])
    keyboard.append([InlineKeyboardButton("🗑️ پاک کردن همه انتخاب‌ها", callback_data="clear_all")])
    reply_markup = InlineKeyboardMarkup(keyboard)
    await send_message(chat_id, text, parse_mode='Markdown', reply_markup=reply_markup)

async def show_all_symbols(chat_id, user_id):
    selections = get_user_selections(user_id)
    text = "📊 **همه نمادها**\n\n"
    text += "✅ روی هر نماد کلیک کنید تا انتخاب/لغو شود.\n"
    text += "بعد از انتخاب، روی **شروع ارسال** کلیک کنید.\n\n"
    text += "**انتخاب‌شده:**\n"
    selected = []
    for key, name, emoji in get_all_symbols():
        if key in selections:
            selected.append(f"{emoji} {name}")
    if selected:
        text += "\n".join(selected)
    else:
        text += "هیچ نمادی انتخاب نشده است."

    keyboard = []
    for key, name, emoji in get_all_symbols():
        checked = "✅ " if key in selections else ""
        keyboard.append([InlineKeyboardButton(f"{checked}{emoji} {name}", callback_data=f"toggle_{key}")])
    keyboard.append([InlineKeyboardButton("🔙 بازگشت به دسته‌ها", callback_data="back_categories")])
    keyboard.append([InlineKeyboardButton("📊 انتخاب همه", callback_data="select_all")])
    keyboard.append([InlineKeyboardButton("🚀 شروع ارسال", callback_data="start_sending")])
    keyboard.append([InlineKeyboardButton("🛑 توقف ارسال", callback_data="stop_sending")])
    keyboard.append([InlineKeyboardButton("🗑️ پاک کردن همه انتخاب‌ها", callback_data="clear_all")])
    reply_markup = InlineKeyboardMarkup(keyboard)
    await send_message(chat_id, text, parse_mode='Markdown', reply_markup=reply_markup)

async def start(update, context):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    await show_main_menu(chat_id, user_id)

async def button_handler(update, context):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    chat_id = query.message.chat.id
    data = query.data

    if data == "back_categories":
        await show_main_menu(chat_id, user_id)
        return
    if data == "status":
        await status_db(chat_id)
        return
    if data == "clear_all":
        clear_user_selections(user_id)
        await query.edit_message_text("🗑️ همه انتخاب‌ها پاک شد.")
        await show_main_menu(chat_id, user_id)
        return
    if data == "select_all":
        select_all_symbols(user_id)
        await query.edit_message_text("📊 همه نمادها انتخاب شدند.")
        await show_all_symbols(chat_id, user_id)
        return
    if data == "show_all":
        await show_all_symbols(chat_id, user_id)
        return
    if data.startswith("cat_"):
        category_key = data.replace("cat_", "")
        await show_category_symbols(chat_id, user_id, category_key)
        return
    if data.startswith("select_all_cat_"):
        category_key = data.replace("select_all_cat_", "")
        cat = CATEGORIES[category_key]
        for key, _, _ in cat['symbols']:
            save_user_selection(user_id, key)
        await query.edit_message_text(f"📊 همه نمادهای {cat['name']} انتخاب شدند.")
        await show_category_symbols(chat_id, user_id, category_key)
        return
    if data == "start_sending":
        selections = get_user_selections(user_id)
        if not selections:
            await query.edit_message_text("⚠️ حداقل یک نماد انتخاب کنید.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 بازگشت", callback_data="back_categories")]]))
            return
        sending_active[user_id] = True
        last_sent_summary[user_id] = ""
        await query.edit_message_text("🚀 **ارسال خودکار شروع شد!**\nهر １ دقیقه قیمت‌های انتخاب‌شده ارسال می‌شود.", parse_mode='Markdown')
        return
    if data == "stop_sending":
        sending_active[user_id] = False
        await query.edit_message_text("🛑 **ارسال خودکار متوقف شد.**", parse_mode='Markdown')
        return
    if data.startswith("toggle_"):
        symbol = data.replace("toggle_", "")
        selections = get_user_selections(user_id)
        if symbol in selections:
            remove_user_selection(user_id, symbol)
        else:
            save_user_selection(user_id, symbol)
        await show_all_symbols(chat_id, user_id)
        return

async def status_single(update, symbol_key, name, emoji):
    chat_id = update.effective_chat.id
    new_price, old_price = get_price_with_old(symbol_key)

    if new_price is None:
        last = get_last_price(symbol_key)
        if last is not None:
            formatted = format_price(last, symbol_key)
            await send_message(chat_id, f"{emoji} **{name}**\n💰 {formatted}\n🔒 بازار بسته", parse_mode='Markdown')
        else:
            await send_message(chat_id, f"{emoji} {name}: ⛔ در دسترس نیست.")
        return

    formatted = format_price(new_price, symbol_key)
    change = None
    if old_price and old_price > 0:
        change = ((new_price - old_price) / old_price) * 100
    change_text = format_change(change)
    if change_text:
        await send_message(chat_id, f"{emoji} **{name}**\n💰 {formatted}\n{change_text}", parse_mode='Markdown')
    else:
        await send_message(chat_id, f"{emoji} **{name}**\n💰 {formatted}", parse_mode='Markdown')

async def gold(update, context): await status_single(update, 'gold', 'GOLD', '🏆')
async def silver(update, context): await status_single(update, 'silver', 'SILVER', '🥈')
async def btc(update, context): await status_single(update, 'btc', 'BTC', '₿')
async def eth(update, context): await status_single(update, 'eth', 'ETH', '💎')
async def bnb(update, context): await status_single(update, 'bnb', 'BNB', '🟡')
async def gram(update, context): await status_single(update, 'gram', 'GRAM', '🔷')
async def xrp(update, context): await status_single(update, 'xrp', 'XRP', '💠')
async def sol(update, context): await status_single(update, 'sol', 'SOL', '☀️')
async def doge(update, context): await status_single(update, 'doge', 'DOGE', '🐕')
async def bch(update, context): await status_single(update, 'bch', 'BCH', '🔶')
async def ltc(update, context): await status_single(update, 'ltc', 'LTC', '⚡')
async def trx(update, context): await status_single(update, 'trx', 'TRX', '🔴')
async def usdt(update, context): await status_single(update, 'usdt', 'USDT', '💵')
async def aed(update, context): await status_single(update, 'aed', 'AED', '🇦🇪')
async def oil(update, context): await status_single(update, 'oil', 'OIL', '🛢️')
async def brent(update, context): await status_single(update, 'brent', 'BRENT', '🛢️')
async def gas(update, context): await status_single(update, 'gas', 'GAS', '🔥')
async def sugar(update, context): await status_single(update, 'sugar', 'SUGAR', '🍬')

async def all_status(update, context):
    chat_id = update.effective_chat.id
    selections = get_user_selections(update.effective_user.id)
    if not selections:
        await send_message(chat_id, "⚠️ هیچ نمادی انتخاب نشده است. لطفاً ابتدا نمادهای مورد نظر را انتخاب کنید.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 بازگشت به منو", callback_data="back_categories")]]))
        return
    message = generate_price_message(selections)
    await send_message(chat_id, f"📊 **قیمت‌های لحظه‌ای**\n━━━━━━━━━━━━━━━━━━━\n{message}", parse_mode='Markdown')

async def status_cmd(update, context):
    await status_db(update.effective_chat.id)

async def status_db(chat_id):
    report = "📊 **وضعیت دیتابیس**\n"
    for key, name, emoji in get_all_symbols():
        price = get_last_price(key)
        report += f"🔹 {name}: {price if price else 'ندارد'}\n"
    await send_message(chat_id, report)

async def help_command(update, context):
    chat_id = update.effective_chat.id
    await send_message(chat_id,
        "📋 **دستورات:**\n"
        "/start - منوی اصلی\n"
        "/all - نمایش قیمت‌های انتخاب‌شده\n"
        "/status - وضعیت دیتابیس\n"
        "/aed - قیمت درهم امارات"
    )

# ======== ارسال پیام به‌روزرسانی (بدون دکمه) ========
async def send_startup_message():
    try:
        bot = Bot(token=TELEGRAM_TOKEN)
        await asyncio.sleep(3)
        await bot.send_message(
            chat_id=CHAT_ID,
            text="✅ **ربات با موفقیت به‌روزرسانی شد!**\n"
                 "📊 قیمت‌ها از منابع جدید دریافت می‌شوند:\n"
                 "💵 **تتر**: از tgju.org\n"
                 "🇦🇪 **درهم**: از tgju.org\n"
                 "🔷 **GRAM**: فقط از یاهو فایننس\n"
                 "⏱️ هر ۱ دقیقه به‌روزرسانی خودکار",
            parse_mode='Markdown'
        )
        print("✅ پیام به‌روزرسانی ارسال شد.")
    except Exception as e:
        print(f"⚠️ خطا در ارسال پیام به‌روزرسانی: {e}")

async def auto_send_loop():
    bot = Bot(token=TELEGRAM_TOKEN)
    while True:
        try:
            for user_id in list(sending_active.keys()):
                if not sending_active.get(user_id, False):
                    continue
                selections = get_user_selections(user_id)
                if not selections:
                    sending_active[user_id] = False
                    continue
                message = generate_price_message(selections)
                if message and message != last_sent_summary.get(user_id, ""):
                    await bot.send_message(
                        user_id,
                        f"🔔 **به‌روزرسانی قیمت‌ها**\n━━━━━━━━━━━━━━━━━━━\n{message}",
                        parse_mode='Markdown'
                    )
                    last_sent_summary[user_id] = message
            await asyncio.sleep(INTERVAL)
        except Exception as e:
            print(f"⚠️ خطا در حلقه خودکار: {e}")
            await asyncio.sleep(INTERVAL)

def start_auto_send():
    asyncio.run(auto_send_loop())

def run_bot_in_main_thread():
    app = Application.builder().token(TELEGRAM_TOKEN).connect_timeout(TIMEOUT).read_timeout(TIMEOUT).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("gold", gold))
    app.add_handler(CommandHandler("silver", silver))
    app.add_handler(CommandHandler("btc", btc))
    app.add_handler(CommandHandler("eth", eth))
    app.add_handler(CommandHandler("bnb", bnb))
    app.add_handler(CommandHandler("gram", gram))
    app.add_handler(CommandHandler("xrp", xrp))
    app.add_handler(CommandHandler("sol", sol))
    app.add_handler(CommandHandler("doge", doge))
    app.add_handler(CommandHandler("bch", bch))
    app.add_handler(CommandHandler("ltc", ltc))
    app.add_handler(CommandHandler("trx", trx))
    app.add_handler(CommandHandler("usdt", usdt))
    app.add_handler(CommandHandler("aed", aed))
    app.add_handler(CommandHandler("oil", oil))
    app.add_handler(CommandHandler("brent", brent))
    app.add_handler(CommandHandler("gas", gas))
    app.add_handler(CommandHandler("sugar", sugar))
    app.add_handler(CommandHandler("all", all_status))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CallbackQueryHandler(button_handler))
    print("🤖 ربات در حال اجرا...")
    app.run_polling()

flask_app = Flask(__name__)

@flask_app.route('/')
def home():
    return "✅ ربات در حال اجراست!"

@flask_app.route('/health')
def health():
    return "OK"

def run_flask():
    port = int(os.environ.get('PORT', 10000))
    flask_app.run(host='0.0.0.0', port=port)

if __name__ == '__main__':
    init_db()

    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()

    auto_thread = threading.Thread(target=start_auto_send, daemon=True)
    auto_thread.start()

    bot_thread = threading.Thread(target=run_bot_in_main_thread, daemon=True)
    bot_thread.start()

    asyncio.run(send_startup_message())

    while True:
        time.sleep(1)
