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
from telegram.error import TimedOut, NetworkError, Forbidden
import logging
import threading

TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN')
if not TELEGRAM_TOKEN:
    raise ValueError("TELEGRAM_TOKEN environment variable not set!")
CHAT_ID = os.environ.get('CHAT_ID', '483833953')
INTERVAL = 60
TIMEOUT = 30

logging.basicConfig(level=logging.INFO)

CATEGORIES = {
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

# =============== کش سراسری قیمت‌ها ===============
price_cache = {
    'data': {},
    'last_update': 0,
    'lock': threading.Lock()
}

# =============== کش تعطیلات رسمی (هر روز یک بار) ===============
holiday_cache = {
    'date': '',
    'is_holiday': False,
    'lock': threading.Lock()
}

# =============== دیکشنری‌های سراسری با قفل ===============
sending_active = {}
last_sent_summary = {}
sending_lock = threading.Lock()

def get_all_symbols_list():
    all_keys = []
    for category in CATEGORIES.values():
        for key, _, _ in category['symbols']:
            all_keys.append(key)
    return all_keys

# =============== توابع دریافت قیمت ===============

def fetch_yahoo(symbol):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1h&range=1d"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"}
    try:
        resp = cffi_requests.get(url, headers=headers, impersonate="chrome120", timeout=8)
        data = resp.json()
        if 'chart' in data and 'result' in data['chart'] and len(data['chart']['result']) > 0:
            result = data['chart']['result'][0]
            if 'meta' in result and 'regularMarketPrice' in result['meta']:
                price = result['meta']['regularMarketPrice']
                if price is not None:
                    return float(price)
            if 'indicators' in result and 'quote' in result['indicators'] and len(result['indicators']['quote']) > 0:
                quote = result['indicators']['quote'][0]
                if 'close' in quote and quote['close']:
                    for p in reversed(quote['close']):
                        if p is not None:
                            return float(p)
        return None
    except Exception as e:
        print(f"❌ Error fetching {symbol}: {e}")
        return None

# =============== تابع تشخیص تعطیلات رسمی (با کش روزانه) ===============

def is_holiday_today():
    """بررسی می‌کند که امروز در آمریکا تعطیل رسمی است یا نه (با کش روزانه)"""
    with holiday_cache['lock']:
        today = datetime.now().strftime('%Y-%m-%d')
        
        if holiday_cache['date'] == today:
            return holiday_cache['is_holiday']
        
        api_key = os.environ.get('CALENDARIFIC_API_KEY')
        if not api_key:
            print("⚠️ CALENDARIFIC_API_KEY تنظیم نشده است. تشخیص تعطیلات غیرفعال.")
            holiday_cache['date'] = today
            holiday_cache['is_holiday'] = False
            return False
        
        try:
            url = f"https://calendarific.com/api/v2/holidays?api_key={api_key}&country=US&year={datetime.now().year}&day={today}"
            resp = requests.get(url, timeout=5)
            if resp.status_code == 200:
                data = resp.json()
                if data.get('meta', {}).get('code') == 200:
                    holidays = data.get('response', {}).get('holidays', [])
                    if holidays:
                        print(f"📅 امروز تعطیل رسمی است: {holidays[0].get('name')}")
                        holiday_cache['date'] = today
                        holiday_cache['is_holiday'] = True
                        return True
            print("✅ امروز تعطیل رسمی نیست.")
            holiday_cache['date'] = today
            holiday_cache['is_holiday'] = False
            return False
        except Exception as e:
            print(f"⚠️ خطا در تشخیص تعطیلات: {e}")
            holiday_cache['date'] = today
            holiday_cache['is_holiday'] = False
            return False

# =============== تابع دریافت قیمت با بررسی بازار ===============

def fetch_price_from_source(symbol_key):
    if not is_market_open(symbol_key):
        return None
    
    if symbol_key == 'gram':
        return fetch_yahoo('GRAM-USD')
    elif symbol_key == 'btc':
        return fetch_yahoo('BTC-USD')
    elif symbol_key == 'eth':
        return fetch_yahoo('ETH-USD')
    elif symbol_key == 'bnb':
        return fetch_yahoo('BNB-USD')
    elif symbol_key == 'xrp':
        return fetch_yahoo('XRP-USD')
    elif symbol_key == 'sol':
        return fetch_yahoo('SOL-USD')
    elif symbol_key == 'doge':
        return fetch_yahoo('DOGE-USD')
    elif symbol_key == 'bch':
        return fetch_yahoo('BCH-USD')
    elif symbol_key == 'ltc':
        return fetch_yahoo('LTC-USD')
    elif symbol_key == 'trx':
        return fetch_yahoo('TRX-USD')
    elif symbol_key == 'gold':
        return fetch_yahoo('XAUT-USD')
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

# =============== تابع تشخیص بازار باز/بسته (با تعطیلات رسمی) ===============

def is_market_open(symbol_key):
    now = datetime.now()
    today = now.weekday()

    # ارزهای دیجیتال (همیشه باز)
    if symbol_key in ['btc', 'eth', 'bnb', 'gram', 'xrp', 'sol', 'doge', 'bch', 'ltc', 'trx', 'dot']:
        return True

    # بازارهای جهانی: شنبه و یکشنبه تعطیل
    if today in [5, 6]:
        return False

    # تعطیلات رسمی (آمریکا)
    if is_holiday_today():
        return False

    # قوانین خاص برای شکر (ساعت کاری)
    if symbol_key == 'sugar':
        iran_hour = (now.hour + 3) % 24
        iran_minute = now.minute + 30
        if iran_minute >= 60:
            iran_hour = (iran_hour + 1) % 24
            iran_minute -= 60
        return 12 <= iran_hour < 21 or (iran_hour == 21 and iran_minute <= 30)

    return True

DB_PATH = "market_data.db"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS prices
                 (symbol TEXT, timestamp TEXT, price REAL, PRIMARY KEY (symbol, timestamp))''')
    c.execute('''CREATE TABLE IF NOT EXISTS closing_prices
                 (symbol TEXT, timestamp TEXT, price REAL, PRIMARY KEY (symbol, timestamp))''')
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

def save_closing_price(symbol, price):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('INSERT OR REPLACE INTO closing_prices (symbol, timestamp, price) VALUES (?, ?, ?)',
              (symbol, datetime.now().isoformat(), price))
    conn.commit()
    conn.close()

def get_closing_price(symbol):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT price FROM closing_prices WHERE symbol=? ORDER BY timestamp DESC LIMIT 1', (symbol,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else None

def get_last_price(symbol):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT price FROM prices WHERE symbol=? ORDER BY timestamp DESC LIMIT 1', (symbol,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else None

def get_price_24h_ago(symbol):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    target_time = (datetime.now() - timedelta(hours=24)).isoformat()
    c.execute('SELECT price FROM prices WHERE symbol=? AND timestamp <= ? ORDER BY timestamp DESC LIMIT 1', (symbol, target_time))
    row = c.fetchone()
    conn.close()
    return row[0] if row else None

def clean_old_prices(days=30):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()
    c.execute('DELETE FROM prices WHERE timestamp < ?', (cutoff,))
    c.execute('DELETE FROM closing_prices WHERE timestamp < ?', (cutoff,))
    conn.commit()
    conn.close()
    print(f"🗑️ قیمت‌های قدیمی‌تر از {days} روز حذف شدند.")

def clean_inactive_users():
    with sending_lock:
        inactive_users = [uid for uid, active in sending_active.items() if not active]
        for uid in inactive_users:
            del sending_active[uid]
            if uid in last_sent_summary:
                del last_sent_summary[uid]
        if inactive_users:
            print(f"🧹 {len(inactive_users)} کاربر غیرفعال پاک شدند.")

def refresh_price_cache():
    with price_cache['lock']:
        now = time.time()
        if now - price_cache['last_update'] < INTERVAL:
            return
        
        print(f"🔄 به‌روزرسانی کش قیمت‌ها در {datetime.now().isoformat()}")
        new_data = {}
        for symbol in get_all_symbols_list():
            new_price = fetch_price_from_source(symbol)
            old_24h = get_price_24h_ago(symbol)
            
            if new_price is not None:
                new_data[symbol] = {'new': new_price, 'old_24h': old_24h}
                save_price(symbol, new_price)
                save_closing_price(symbol, new_price)
            else:
                closing_price = get_closing_price(symbol)
                if closing_price is not None:
                    new_data[symbol] = {'new': closing_price, 'old_24h': old_24h}
                else:
                    last = get_last_price(symbol)
                    if last is not None:
                        new_data[symbol] = {'new': last, 'old_24h': old_24h}
        price_cache['data'] = new_data
        price_cache['last_update'] = now
        
        clean_old_prices(30)

def get_cached_price_with_24h(symbol):
    refresh_price_cache()
    with price_cache['lock']:
        data = price_cache['data'].get(symbol)
        if data:
            return data.get('new'), data.get('old_24h')
        return None, None

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
    if symbol_key == 'gram':
        return f"{price:,.4f}"
    if price < 0.001:
        return f"{price:.4e}"
    elif price < 1:
        return f"{price:.6f}"
    else:
        return f"{price:,.2f}"

def format_change(change):
    if change is None:
        return ""
    if abs(change) < 0.0001:
        return "➖ بدون تغییر"
    elif change > 0:
        return f"📈 {change:+.2f}%"
    else:
        return f"📉 {change:+.2f}%"

def generate_price_message(selections):
    lines = []
    for cat_key, cat in CATEGORIES.items():
        cat_selected = [s for s in cat['symbols'] if s[0] in selections]
        if not cat_selected:
            continue
        lines.append(f"{cat['emoji']} {cat['name']}:")
        for key, name, emoji in cat_selected:
            new_price, old_24h = get_cached_price_with_24h(key)
            if new_price is None:
                if not is_market_open(key):
                    last = get_last_price(key)
                    if last is not None:
                        formatted = format_price(last, key)
                        lines.append(f"{emoji} {name} : {formatted} 🔒 بازار بسته")
                    else:
                        lines.append(f"{emoji} {name} : 🔒 بازار بسته")
                else:
                    lines.append(f"{emoji} {name} : ⛔ در دسترس نیست")
                continue
            formatted = format_price(new_price, key)
            change = None
            if old_24h is not None and old_24h > 0:
                change = ((new_price - old_24h) / old_24h) * 100
            change_text = format_change(change)
            if change_text:
                lines.append(f"{emoji} {name} : {formatted} {change_text}")
            else:
                lines.append(f"{emoji} {name} : {formatted}")
        lines.append("")
    return "\n".join(lines) if lines else "هیچ نمادی انتخاب نشده است."

async def show_main_menu(chat_id, user_id, query=None):
    text = "📊 **به ربات قیمت‌های لحظه‌ای خوش آمدید!**\n\n"
    text += "لطفاً یک دسته را انتخاب کنید:\n"
    keyboard = []
    for cat_key, cat in CATEGORIES.items():
        keyboard.append([InlineKeyboardButton(f"{cat['emoji']} {cat['name']}", callback_data=f"cat_{cat_key}")])
    keyboard.append([InlineKeyboardButton("📊 نمایش همه", callback_data="show_all")])
    keyboard.append([InlineKeyboardButton("📋 وضعیت دیتابیس", callback_data="status")])
    keyboard.append([InlineKeyboardButton("⚙️ ویرایش نمادها", callback_data="show_all")])
    reply_markup = InlineKeyboardMarkup(keyboard)

    if query:
        await query.edit_message_text(text, parse_mode='Markdown', reply_markup=reply_markup)
    else:
        await send_message(chat_id, text, parse_mode='Markdown', reply_markup=reply_markup)

async def show_category_symbols(chat_id, user_id, category_key, query=None):
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

    if query:
        await query.edit_message_text(text, parse_mode='Markdown', reply_markup=reply_markup)
    else:
        await send_message(chat_id, text, parse_mode='Markdown', reply_markup=reply_markup)

async def show_all_symbols(chat_id, user_id, query=None):
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

    if query:
        await query.edit_message_text(text, parse_mode='Markdown', reply_markup=reply_markup)
    else:
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
        await show_main_menu(chat_id, user_id, query)
        return

    if data == "status":
        report = "📊 **وضعیت دیتابیس**\n"
        for key, name, emoji in get_all_symbols():
            price = get_last_price(key)
            report += f"🔹 {name}: {price if price else 'ندارد'}\n"
        await query.edit_message_text(report, parse_mode='Markdown')
        return

    if data == "clear_all":
        clear_user_selections(user_id)
        await query.edit_message_text("🗑️ همه انتخاب‌ها پاک شد.")
        await show_main_menu(chat_id, user_id)
        return

    if data == "select_all":
        select_all_symbols(user_id)
        await query.edit_message_text("📊 همه نمادها انتخاب شدند.")
        await show_all_symbols(chat_id, user_id, query)
        return

    if data == "show_all":
        await show_all_symbols(chat_id, user_id, query)
        return

    if data.startswith("cat_"):
        category_key = data.replace("cat_", "")
        await show_category_symbols(chat_id, user_id, category_key, query)
        return

    if data.startswith("select_all_cat_"):
        category_key = data.replace("select_all_cat_", "")
        cat = CATEGORIES[category_key]
        for key, _, _ in cat['symbols']:
            save_user_selection(user_id, key)
        await query.edit_message_text(f"📊 همه نمادهای {cat['name']} انتخاب شدند.")
        await show_category_symbols(chat_id, user_id, category_key, query)
        return

    if data == "start_sending":
        selections = get_user_selections(user_id)
        if not selections:
            await query.edit_message_text("⚠️ حداقل یک نماد انتخاب کنید.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 بازگشت", callback_data="back_categories")]]))
            return
        with sending_lock:
            sending_active[user_id] = True
            last_sent_summary[user_id] = ""
        await query.edit_message_text("🚀 **ارسال خودکار شروع شد!**\nهر ۱ دقیقه قیمت‌های انتخاب‌شده ارسال می‌شود.", parse_mode='Markdown')
        return

    if data == "stop_sending":
        with sending_lock:
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
        await show_all_symbols(chat_id, user_id, query)
        return

async def status_single(update, symbol_key, name, emoji):
    chat_id = update.effective_chat.id
    new_price, old_24h = get_cached_price_with_24h(symbol_key)

    if new_price is None:
        last = get_last_price(symbol_key)
        if last is not None:
            formatted = format_price(last, symbol_key)
            if not is_market_open(symbol_key):
                await send_message(chat_id, f"{emoji} **{name}**\n💰 {formatted}\n🔒 بازار بسته", parse_mode='Markdown')
            else:
                await send_message(chat_id, f"{emoji} **{name}**\n💰 {formatted}", parse_mode='Markdown')
        else:
            if not is_market_open(symbol_key):
                await send_message(chat_id, f"{emoji} **{name}**\n🔒 بازار بسته", parse_mode='Markdown')
            else:
                await send_message(chat_id, f"{emoji} {name}: ⛔ در دسترس نیست.")
        return

    formatted = format_price(new_price, symbol_key)
    change = None
    if old_24h is not None and old_24h > 0:
        change = ((new_price - old_24h) / old_24h) * 100
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
    )

async def auto_send_loop():
    bot = Bot(token=TELEGRAM_TOKEN)
    last_cleanup = time.time()
    while True:
        try:
            refresh_price_cache()
            
            with sending_lock:
                active_users = list(sending_active.items())
            
            for user_id, is_active in active_users:
                if not is_active:
                    continue
                selections = get_user_selections(user_id)
                if not selections:
                    with sending_lock:
                        sending_active[user_id] = False
                    continue
                message = generate_price_message(selections)
                if message:
                    with sending_lock:
                        last_msg = last_sent_summary.get(user_id, "")
                    if message != last_msg:
                        keyboard = [[InlineKeyboardButton("⚙️ ویرایش نمادها", callback_data="show_all")]]
                        reply_markup = InlineKeyboardMarkup(keyboard)
                        
                        try:
                            await bot.send_message(
                                user_id,
                                f"🔔 **به‌روزرسانی قیمت‌ها**\n━━━━━━━━━━━━━━━━━━━\n{message}",
                                parse_mode='Markdown',
                                reply_markup=reply_markup
                            )
                            with sending_lock:
                                last_sent_summary[user_id] = message
                        except Forbidden as e:
                            print(f"🚫 کاربر {user_id} ربات را بلاک/حذف کرده است. ارسال متوقف شد.")
                            with sending_lock:
                                sending_active[user_id] = False
                            clear_user_selections(user_id)
                        except Exception as e:
                            print(f"⚠️ خطا در ارسال به {user_id}: {e}")
            
            if time.time() - last_cleanup > 600:
                clean_inactive_users()
                last_cleanup = time.time()
            
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
    return "OK", 200

def run_flask():
    port = int(os.environ.get('PORT', 10000))
    flask_app.run(host='0.0.0.0', port=port)

if __name__ == '__main__':
    init_db()

    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()

    auto_thread = threading.Thread(target=start_auto_send, daemon=True)
    auto_thread.start()

    run_bot_in_main_thread()
