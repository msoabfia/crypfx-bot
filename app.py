import os
os.environ['TZ'] = 'UTC'
import asyncio
import sqlite3
import requests
import re
import json
import time
from datetime import datetime, timedelta
from curl_cffi import requests as cffi_requests
from flask import Flask, request
from telegram import Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes, CallbackQueryHandler
from telegram.error import TimedOut, NetworkError, RetryAfter
import logging
import threading
from functools import lru_cache
from typing import Optional, Dict, List, Tuple

# ======== تنظیمات ========
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN')
if not TELEGRAM_TOKEN:
    raise ValueError("TELEGRAM_TOKEN environment variable not set!")

INTERVAL = int(os.environ.get('INTERVAL', 60))
TIMEOUT = 30
MAX_RETRIES = 3
CACHE_TTL = 30

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ======== کش قیمت‌ها ========
price_cache: Dict[str, Tuple[float, float]] = {}

def get_cached_price(symbol: str) -> Optional[float]:
    if symbol in price_cache:
        price, timestamp = price_cache[symbol]
        if time.time() - timestamp < CACHE_TTL:
            return price
    return None

def set_cached_price(symbol: str, price: float):
    price_cache[symbol] = (price, time.time())

# ======== دسته‌بندی نمادها ========
CATEGORIES = {
    'crypto': {
        'name': 'ارزهای دیجیتال',
        'emoji': '💰',
        'symbols': [
            ('btc', 'BTC', '₿'),
            ('eth', 'ETH', '💎'),
            ('bnb', 'BNB', '🟡'),
            ('gram', 'GRAM', '🔷'),
            ('xrp', 'XRP', '💠'),
            ('sol', 'SOL', '☀️'),
            ('doge', 'DOGE', '🐕'),
            ('bch', 'BCH', '🔶'),
            ('ltc', 'LTC', '⚡'),
            ('trx', 'TRX', '🔴'),
            ('dot', 'DOT', '🟣'),
            ('usdt', 'USDT', '💵'),
            ('aed', 'AED', '🇦🇪'),
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

# ======== دریافت قیمت‌ها ========
def fetch_tgju_price(symbol_key: str) -> Optional[int]:
    try:
        resp = requests.get('https://www.tgju.org/', timeout=10)
        html = resp.text
        patterns = {
            'usd': r'<span class="price">([\d,]+)</span>',
            'aed': r'<span[^>]*>.*?درهم.*?</span>.*?<span class="price">([\d,]+)</span>',
            'usdt': r'<span[^>]*>.*?تتر.*?</span>.*?<span class="price">([\d,]+)</span>',
        }
        pattern = patterns.get(symbol_key)
        if pattern:
            match = re.search(pattern, html, re.DOTALL | re.IGNORECASE)
            if match:
                return int(match.group(1).replace(',', ''))
        
        if symbol_key == 'aed':
            usd = fetch_tgju_price('usd')
            return int(usd / 3.6725) if usd else None
        if symbol_key == 'usdt':
            return fetch_tgju_price('usd')
        return None
    except Exception as e:
        logger.error(f"Error fetching tgju price for {symbol_key}: {e}")
        return None

def fetch_yahoo(symbol: str) -> Optional[float]:
    cached = get_cached_price(symbol)
    if cached:
        return cached
    
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1h&range=1d"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"}
    
    for attempt in range(MAX_RETRIES):
        try:
            resp = cffi_requests.get(url, headers=headers, impersonate="chrome120", timeout=10)
            data = resp.json()
            price = data['chart']['result'][0]['indicators']['quote'][0]['close'][-1]
            if price is not None:
                set_cached_price(symbol, float(price))
                return float(price)
        except Exception as e:
            logger.warning(f"Yahoo fetch attempt {attempt+1} failed for {symbol}: {e}")
            time.sleep(1)
    return None

def fetch_twelve(symbol: str) -> Optional[float]:
    url = f"https://api.twelvedata.com/price?symbol={symbol}&apikey=f8f6fe94d43b454ba0c9431ff529c466"
    try:
        resp = requests.get(url, timeout=10)
        data = resp.json()
        return float(data['price']) if 'price' in data else None
    except:
        return None

def fetch_coingecko(symbol: str) -> Optional[float]:
    url = f"https://api.coingecko.com/api/v3/simple/price?ids={symbol}&vs_currencies=usd"
    try:
        resp = requests.get(url, timeout=10)
        data = resp.json()
        return float(data[symbol]['usd'])
    except:
        return None

def get_price(symbol_key: str) -> Optional[float]:
    if symbol_key in ('usd', 'aed', 'usdt'):
        return fetch_tgju_price(symbol_key)
    
    price_map = {
        'btc': lambda: fetch_yahoo('BTC-USD') or fetch_twelve('BTC/USD'),
        'eth': lambda: fetch_yahoo('ETH-USD') or fetch_twelve('ETH/USD'),
        'bnb': lambda: fetch_yahoo('BNB-USD') or fetch_twelve('BNB/USD'),
        'gram': lambda: fetch_coingecko('the-open-network') or fetch_twelve('TON/USD'),
        'xrp': lambda: fetch_yahoo('XRP-USD') or fetch_twelve('XRP/USD'),
        'sol': lambda: fetch_yahoo('SOL-USD') or fetch_twelve('SOL/USD'),
        'doge': lambda: fetch_yahoo('DOGE-USD') or fetch_twelve('DOGE/USD'),
        'bch': lambda: fetch_yahoo('BCH-USD') or fetch_twelve('BCH/USD'),
        'ltc': lambda: fetch_yahoo('LTC-USD') or fetch_twelve('LTC/USD'),
        'trx': lambda: fetch_yahoo('TRX-USD') or fetch_twelve('TRX/USD'),
        'dot': lambda: fetch_yahoo('DOT-USD') or fetch_twelve('DOT/USD'),
        'gold': lambda: fetch_yahoo('GC=F'),
        'silver': lambda: fetch_yahoo('SI=F'),
        'oil': lambda: fetch_yahoo('CL=F'),
        'brent': lambda: fetch_yahoo('BZ=F'),
        'gas': lambda: fetch_yahoo('NG=F'),
        'sugar': lambda: (lambda p: round(p/100, 4) if p else None)(fetch_yahoo('SB=F')),
    }
    
    func = price_map.get(symbol_key)
    return func() if func else None

def is_market_open(symbol_key: str) -> bool:
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
        return 12 <= iran_hour <= 21 and not (iran_hour == 21 and iran_minute > 30)
    
    return True

# ======== دیتابیس ========
DB_PATH = "market_data.db"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS prices
                 (symbol TEXT, timestamp TEXT, price REAL, 
                  PRIMARY KEY (symbol, timestamp))''')
    c.execute('''CREATE TABLE IF NOT EXISTS user_selections
                 (user_id INTEGER, symbol TEXT, 
                  PRIMARY KEY (user_id, symbol))''')
    conn.commit()
    conn.close()

def save_price(symbol: str, price: float):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('INSERT INTO prices (symbol, timestamp, price) VALUES (?, ?, ?)',
              (symbol, datetime.now().isoformat(), price))
    conn.commit()
    conn.close()

def get_last_price(symbol: str) -> Optional[float]:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT price FROM prices WHERE symbol=? ORDER BY timestamp DESC LIMIT 1', (symbol,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else None

def get_user_selections(user_id: int) -> List[str]:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT symbol FROM user_selections WHERE user_id=?', (user_id,))
    rows = c.fetchall()
    conn.close()
    return [row[0] for row in rows]

def save_user_selection(user_id: int, symbol: str):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('INSERT OR IGNORE INTO user_selections (user_id, symbol) VALUES (?, ?)', (user_id, symbol))
    conn.commit()
    conn.close()

def remove_user_selection(user_id: int, symbol: str):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('DELETE FROM user_selections WHERE user_id=? AND symbol=?', (user_id, symbol))
    conn.commit()
    conn.close()

def clear_user_selections(user_id: int):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('DELETE FROM user_selections WHERE user_id=?', (user_id,))
    conn.commit()
    conn.close()

def select_all_symbols(user_id: int):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('DELETE FROM user_selections WHERE user_id=?', (user_id,))
    for category in CATEGORIES.values():
        for key, _, _ in category['symbols']:
            c.execute('INSERT OR IGNORE INTO user_selections (user_id, symbol) VALUES (?, ?)', (user_id, key))
    conn.commit()
    conn.close()

# ======== توابع ربات ========
sending_active: Dict[int, bool] = {}
last_sent_summary: Dict[int, str] = {}

def get_all_symbols() -> List[Tuple[str, str, str]]:
    all_symbols = []
    for category in CATEGORIES.values():
        all_symbols.extend(category['symbols'])
    return all_symbols

def get_unit(symbol_key: str) -> str:
    return 'تومان' if symbol_key in ('aed', 'usdt', 'usd') else '$'

def format_price(symbol_key: str, price: Optional[float]) -> str:
    if price is None:
        return '⛔ در دسترس نیست'
    unit = get_unit(symbol_key)
    if unit == 'تومان':
        return f"{int(price):,} {unit}"
    return f"${price:.4f}"

def format_change(old_price: Optional[float], new_price: Optional[float]) -> str:
    if old_price is None or new_price is None:
        return "💰 قیمت اولیه"
    if abs(new_price - old_price) < 0.0001:
        return "➖ بدون تغییر"
    change = ((new_price - old_price) / old_price) * 100
    if abs(change) < 0.001:
        return "➖ بدون تغییر"
    arrow = "📈" if change > 0 else "📉"
    return f"{arrow} {change:+.2f}%"

def generate_formatted_lines(selections: Optional[List[str]] = None) -> List[str]:
    lines = []
    for cat_key, cat in CATEGORIES.items():
        cat_symbols = cat['symbols']
        if selections is not None:
            cat_symbols = [(k, n, e) for k, n, e in cat['symbols'] if k in selections]
        if not cat_symbols:
            continue
        
        lines.append(f"{cat['emoji']} {cat['name']}:")
        for key, name, emoji in cat_symbols:
            price = get_price(key)
            old_price = get_last_price(key)
            if price is not None:
                save_price(key, price)
            
            price_str = format_price(key, price)
            change_str = format_change(old_price, price)
            market_status = " 🔒 بسته" if not is_market_open(key) else ""
            lines.append(f"{emoji} {name}: {price_str} {change_str}{market_status}")
        lines.append("")
    
    if lines and lines[-1] == "":
        lines.pop()
    return lines

async def send_message(chat_id: int, text: str, parse_mode: str = 'Markdown', reply_markup=None):
    bot = Bot(token=TELEGRAM_TOKEN)
    for attempt in range(MAX_RETRIES):
        try:
            await bot.send_message(
                chat_id=chat_id, 
                text=text, 
                parse_mode=parse_mode, 
                reply_markup=reply_markup,
                disable_web_page_preview=True
            )
            return
        except (TimedOut, NetworkError, RetryAfter) as e:
            wait_time = 2 ** attempt
            logger.warning(f"Send failed, retrying in {wait_time}s: {e}")
            await asyncio.sleep(wait_time)
        except Exception as e:
            logger.error(f"Send failed: {e}")
            return

# ======== منوها ========
async def show_main_menu(chat_id: int, user_id: int):
    text = "📊 **به ربات قیمت‌های لحظه‌ای خوش آمدید!**\n\nلطفاً یک دسته را انتخاب کنید:"
    keyboard = [
        [InlineKeyboardButton(f"{cat['emoji']} {cat['name']}", callback_data=f"cat_{cat_key}")]
        for cat_key, cat in CATEGORIES.items()
    ]
    keyboard.append([InlineKeyboardButton("📊 نمایش همه", callback_data="show_all")])
    await send_message(chat_id, text, reply_markup=InlineKeyboardMarkup(keyboard))

async def show_category_symbols(chat_id: int, user_id: int, category_key: str):
    cat = CATEGORIES[category_key]
    selections = get_user_selections(user_id)
    
    text = f"📊 **{cat['emoji']} {cat['name']}**\n\n"
    text += "✅ روی هر نماد کلیک کنید تا انتخاب/لغو شود.\n"
    text += "بعد از انتخاب، روی **شروع ارسال** کلیک کنید.\n\n"
    text += "**انتخاب‌شده:**\n"
    selected = [f"{emoji} {name}" for key, name, emoji in cat['symbols'] if key in selections]
    text += "\n".join(selected) if selected else "هیچ نمادی انتخاب نشده است."
    
    keyboard = []
    for key, name, emoji in cat['symbols']:
        checked = "✅ " if key in selections else ""
        keyboard.append([InlineKeyboardButton(f"{checked}{emoji} {name}", callback_data=f"toggle_{key}")])
    
    keyboard.extend([
        [InlineKeyboardButton("🔙 بازگشت به دسته‌ها", callback_data="back_categories")],
        [InlineKeyboardButton("📊 انتخاب همه", callback_data=f"select_all_cat_{category_key}")],
        [InlineKeyboardButton("🚀 شروع ارسال", callback_data="start_sending")],
        [InlineKeyboardButton("🛑 توقف ارسال", callback_data="stop_sending")],
        [InlineKeyboardButton("🗑️ پاک کردن همه انتخاب‌ها", callback_data="clear_all")],
    ])
    await send_message(chat_id, text, reply_markup=InlineKeyboardMarkup(keyboard))

async def show_all_symbols_menu(chat_id: int, user_id: int):
    selections = get_user_selections(user_id)
    text = "📊 **همه نمادها**\n\n"
    text += "✅ روی هر نماد کلیک کنید تا انتخاب/لغو شود.\n"
    text += "بعد از انتخاب، روی **شروع ارسال** کلیک کنید.\n\n"
    text += "**انتخاب‌شده:**\n"
    selected = [f"{emoji} {name}" for key, name, emoji in get_all_symbols() if key in selections]
    text += "\n".join(selected) if selected else "هیچ نمادی انتخاب نشده است."
    
    keyboard = []
    for key, name, emoji in get_all_symbols():
        checked = "✅ " if key in selections else ""
        keyboard.append([InlineKeyboardButton(f"{checked}{emoji} {name}", callback_data=f"toggle_{key}")])
    
    keyboard.extend([
        [InlineKeyboardButton("🔙 بازگشت به دسته‌ها", callback_data="back_categories")],
        [InlineKeyboardButton("📊 انتخاب همه", callback_data="select_all")],
        [InlineKeyboardButton("🚀 شروع ارسال", callback_data="start_sending")],
        [InlineKeyboardButton("🛑 توقف ارسال", callback_data="stop_sending")],
        [InlineKeyboardButton("🗑️ پاک کردن همه انتخاب‌ها", callback_data="clear_all")],
    ])
    await send_message(chat_id, text, reply_markup=InlineKeyboardMarkup(keyboard))

# ======== دستورات ربات ========
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_main_menu(update.effective_chat.id, update.effective_user.id)

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    chat_id = query.message.chat.id
    data = query.data
    
    if data == "back_categories":
        await show_main_menu(chat_id, user_id)
        return
    
    if data == "clear_all":
        clear_user_selections(user_id)
        await query.edit_message_text("🗑️ همه انتخاب‌ها پاک شد.")
        await show_main_menu(chat_id, user_id)
        return
    
    if data == "select_all":
        select_all_symbols(user_id)
        await query.edit_message_text("📊 همه نمادها انتخاب شدند.")
        await show_all_symbols_menu(chat_id, user_id)
        return
    
    if data == "show_all":
        await show_all_symbols_menu(chat_id, user_id)
        return
    
    if data.startswith("cat_"):
        await show_category_symbols(chat_id, user_id, data.replace("cat_", ""))
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
            await query.edit_message_text(
                "⚠️ حداقل یک نماد انتخاب کنید.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 بازگشت", callback_data="back_categories")]])
            )
            return
        sending_active[user_id] = True
        last_sent_summary[user_id] = ""
        await query.edit_message_text("🚀 **ارسال خودکار شروع شد!**\nهر ۱ دقیقه قیمت‌های انتخاب‌شده ارسال می‌شود.", parse_mode='Markdown')
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
        await show_all_symbols_menu(chat_id, user_id)
        return

async def status_single(update: Update, symbol_key: str, name: str, emoji: str):
    chat_id = update.effective_chat.id
    price = get_price(symbol_key)
    if price is None:
        await send_message(chat_id, f"{emoji} {name}: ⛔ در دسترس نیست.")
        return
    
    old_price = get_last_price(symbol_key)
    save_price(symbol_key, price)
    price_str = format_price(symbol_key, price)
    change_str = format_change(old_price, price)
    market_status = " 🔒 بسته" if not is_market_open(symbol_key) else ""
    await send_message(chat_id, f"{emoji} **{name}**\n💰 قیمت: {price_str}\n{change_str}{market_status}", parse_mode='Markdown')

# ======== تعریف دستورات ========
commands = [
    ('gold', 'GOLD', '🏆'), ('silver', 'SILVER', '🥈'),
    ('btc', 'BTC', '₿'), ('eth', 'ETH', '💎'), ('bnb', 'BNB', '🟡'),
    ('gram', 'GRAM', '🔷'), ('xrp', 'XRP', '💠'), ('sol', 'SOL', '☀️'),
    ('doge', 'DOGE', '🐕'), ('bch', 'BCH', '🔶'), ('ltc', 'LTC', '⚡'),
    ('trx', 'TRX', '🔴'), ('dot', 'DOT', '🟣'),
    ('usdt', 'USDT', '💵'), ('aed', 'AED', '🇦🇪'),
    ('oil', 'OIL', '🛢️'), ('brent', 'BRENT', '🛢️'), ('gas', 'GAS', '🔥'),
    ('sugar', 'SUGAR', '🍬'),
]

async def all_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    lines = generate_formatted_lines()
    text = "📊 **خلاصه قیمت‌ها**\n━━━━━━━━━━━━━━━━━━━\n" + "\n".join(lines)
    await send_message(chat_id, text)

async def usd_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    price = fetch_tgju_price('usd')
    text = f"💰 **قیمت دلار (بازار آزاد):**\n{price:,} تومان" if price else "⛔ خطا در دریافت قیمت دلار."
    await send_message(chat_id, text)

async def status_db(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    report = "📊 **وضعیت دیتابیس**\n"
    for key, name, emoji in get_all_symbols():
        price = get_last_price(key)
        report += f"🔹 {name}: {price if price else 'ندارد'}\n"
    await send_message(chat_id, report)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    await send_message(chat_id,
        "📋 **دستورات:**\n"
        "/start - منوی اصلی\n"
        "/gold - طلا\n/silver - نقره\n/btc - بیت‌کوین\n/eth - اتریوم\n/bnb - بایننس کوین\n"
        "/gram - گرم\n/xrp - ریپل\n/sol - سولانا\n/doge - دوج کوین\n/bch - بیت‌کوین کش\n/ltc - لایت‌کوین\n/trx - ترون\n/dot - دات‌کوین\n"
        "/usdt - تتر (تومان)\n/aed - درهم امارات (تومان)\n"
        "/oil - نفت خام\n/brent - نفت برنت\n/gas - گاز طبیعی\n/sugar - شکر\n"
        "/usd - قیمت دلار\n/all - خلاصه همه\n/status - وضعیت دیتابیس"
    )

def setup_handlers(app: Application):
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("all", all_status))
    app.add_handler(CommandHandler("status", status_db))
    app.add_handler(CommandHandler("usd", usd_price))
    app.add_handler(CallbackQueryHandler(button_handler))
    
    for cmd, name, emoji in commands:
        async def handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE, cmd=cmd, name=name, emoji=emoji):
            await status_single(update, cmd, name, emoji)
        app.add_handler(CommandHandler(cmd, handler))

# ======== حلقه خودکار ========
async def auto_send_loop():
    bot = Bot(token=TELEGRAM_TOKEN)
    logger.info("🔄 Auto-send loop started")
    
    while True:
        try:
            for user_id in list(sending_active.keys()):
                if not sending_active.get(user_id, False):
                    continue
                
                selections = get_user_selections(user_id)
                if not selections:
                    sending_active[user_id] = False
                    continue
                
                lines = generate_formatted_lines(selections)
                summary = "\n".join(lines)
                
                if summary and summary != last_sent_summary.get(user_id, ""):
                    try:
                        await bot.send_message(
                            user_id,
                            f"🔔 **به‌روزرسانی قیمت‌ها**\n━━━━━━━━━━━━━━━━━━━\n{summary}",
                            parse_mode='Markdown'
                        )
                        last_sent_summary[user_id] = summary
                    except Exception as e:
                        logger.error(f"Error sending to user {user_id}: {e}")
            
            await asyncio.sleep(INTERVAL)
        except Exception as e:
            logger.error(f"Error in auto-send loop: {e}")
            await asyncio.sleep(INTERVAL)

# ======== وب‌سرور Flask ========
flask_app = Flask(__name__)
application = None

@flask_app.route('/')
def home():
    return "✅ ربات در حال اجراست!"

@flask_app.route('/health')
def health():
    return "OK"

@flask_app.route('/webhook', methods=['POST'])
async def webhook():
    if request.headers.get('content-type') == 'application/json':
        try:
            update_data = request.get_json(force=True)
            if application:
                update = Update.de_json(update_data, application.bot)
                await application.process_update(update)
            return "OK", 200
        except Exception as e:
            logger.error(f"Webhook error: {e}")
            return "Error", 500
    return "Unsupported", 400

def run_flask():
    port = int(os.environ.get('PORT', 10000))
    flask_app.run(host='0.0.0.0', port=port, debug=False)

async def setup_webhook():
    bot = Bot(token=TELEGRAM_TOKEN)
    webhook_url = f"https://crypfx-bot-2.onrender.com/webhook"
    try:
        await bot.set_webhook(webhook_url, drop_pending_updates=True)
        logger.info(f"✅ Webhook set to: {webhook_url}")
        return True
    except Exception as e:
        logger.error(f"❌ Failed to set webhook: {e}")
        return False

# ======== اجرای اصلی ========
if __name__ == '__main__':
    init_db()
    logger.info("📊 Database initialized")
    
    # اجرای Flask در ترد جداگانه
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    logger.info("🌐 Flask server started")
    
    # اجرای حلقه خودکار
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    auto_thread = threading.Thread(
        target=lambda: loop.run_until_complete(auto_send_loop()),
        daemon=True
    )
    auto_thread.start()
    logger.info("🔄 Auto-send loop started")
    
    # اجرای ربات با Webhook
    try:
        use_webhook = os.environ.get('USE_WEBHOOK', 'true').lower() == 'true'
        
        if use_webhook:
            # روش Webhook
            application = Application.builder().token(TELEGRAM_TOKEN).build()
            setup_handlers(application)
            
            loop2 = asyncio.new_event_loop()
            asyncio.set_event_loop(loop2)
            loop2.run_until_complete(setup_webhook())
            
            logger.info("🤖 Bot running with webhook")
            logger.info("⏳ Keeping application alive...")
            
            # حلقه بی‌نهایت برای زنده نگه داشتن برنامه
            while True:
                time.sleep(60)
        else:
            # روش Polling
            application = Application.builder().token(TELEGRAM_TOKEN).build()
            setup_handlers(application)
            logger.info("🤖 Bot running with polling")
            application.run_polling(
                drop_pending_updates=True,
                poll_interval=1.0,
                timeout=30
            )
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        import traceback
        traceback.print_exc()
