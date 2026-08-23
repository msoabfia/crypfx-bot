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
    'crypto': ('ارزهای دیجیتال', '💰', [
        ('usdt_rial', 'تتر (ریال)', '💰'),
        ('aed', 'درهم امارات', '🇦🇪'),
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
    ]),
    'metals': ('فلزات گرانبها', '🏆', [
        ('gold', 'GOLD', '🏆'),
        ('silver', 'SILVER', '🥈'),
    ]),
    'energy': ('انرژی و نفت', '⛽', [
        ('oil', 'OIL', '🛢️'),
        ('brent', 'BRENT', '🛢️'),
        ('gas', 'GAS', '🔥'),
    ]),
    'agriculture': ('کشاورزی', '🌾', [
        ('sugar', 'SUGAR', '🍬'),
    ])
}

# ======== توابع دریافت قیمت ========
def fetch_usd_price():
    try:
        resp = requests.get('https://www.tgju.org/', timeout=10)
        match = re.search(r'<span class="price">([\d,]+)</span>', resp.text)
        return int(match.group(1).replace(',', '')) if match else None
    except:
        return None

def fetch_yahoo(symbol):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1h&range=1d"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"}
    try:
        resp = cffi_requests.get(url, headers=headers, impersonate="chrome120", timeout=10)
        price = resp.json()['chart']['result'][0]['indicators']['quote'][0]['close'][-1]
        return float(price) if price is not None else None
    except:
        return None

def fetch_twelve(symbol):
    url = f"https://api.twelvedata.com/price?symbol={symbol}&apikey=f8f6fe94d43b454ba0c9431ff529c466"
    try:
        resp = requests.get(url, timeout=10)
        return float(resp.json()['price']) if 'price' in resp.json() else None
    except:
        return None

def fetch_coingecko(symbol):
    url = f"https://api.coingecko.com/api/v3/simple/price?ids={symbol}&vs_currencies=usd"
    try:
        resp = requests.get(url, timeout=10)
        return float(resp.json()[symbol]['usd'])
    except:
        return None

# ======== دریافت قیمت (با کش) ========
def get_price(symbol_key):
    usd_price = fetch_usd_price()
    
    # دیکشنری برای نگاشت نمادها به توابع دریافت قیمت
    fetch_map = {
        'usdt_rial': lambda: (fetch_twelve('USDT/USD') or fetch_coingecko('tether')) and usd_price and (fetch_twelve('USDT/USD') or fetch_coingecko('tether')) * usd_price,
        'aed': lambda: fetch_yahoo('AED=X') and usd_price and fetch_yahoo('AED=X') * usd_price,
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
        'gold': lambda: fetch_yahoo('GC=F'),
        'silver': lambda: fetch_yahoo('SI=F'),
        'oil': lambda: fetch_yahoo('CL=F'),
        'brent': lambda: fetch_yahoo('BZ=F'),
        'gas': lambda: fetch_yahoo('NG=F'),
        'sugar': lambda: (lambda p: round(p / 100, 4) if p else None)(fetch_yahoo('SB=F')),
    }
    
    price = fetch_map.get(symbol_key, lambda: None)()
    
    # برای تتر ریال و درهم که به usd_price نیاز دارند، جداگانه محاسبه می‌کنیم
    if symbol_key == 'usdt_rial':
        price = fetch_twelve('USDT/USD') or fetch_coingecko('tether')
        return price * usd_price if price and usd_price else None
    elif symbol_key == 'aed':
        price = fetch_yahoo('AED=X')
        return price * usd_price if price and usd_price else None
    
    return price

def get_price_change(symbol_key, current_price):
    old_price = get_last_price(symbol_key)
    if old_price and old_price > 0 and current_price:
        return ((current_price - old_price) / old_price) * 100
    return None

def is_market_open(symbol_key):
    now = datetime.now()
    today = now.weekday()
    
    if symbol_key in ['btc', 'eth', 'bnb', 'gram', 'xrp', 'sol', 'doge', 'bch', 'ltc', 'trx', 'dot', 'usdt_rial', 'aed']:
        return True
    if today == 6:
        return False
    if symbol_key == 'sugar':
        iran_hour = (now.hour + 3) % 24
        iran_minute = now.minute + 30
        if iran_minute >= 60:
            iran_hour = (iran_hour + 1) % 24
            iran_minute -= 60
        return 12 <= iran_hour < 21 or (iran_hour == 21 and iran_minute <= 30)
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

# ======== مدیریت انتخاب‌های کاربر ========
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
    for _, _, symbols in CATEGORIES.values():
        for key, _, _ in symbols:
            c.execute('INSERT OR IGNORE INTO user_selections (user_id, symbol) VALUES (?, ?)', (user_id, key))
    conn.commit()
    conn.close()

# ======== توابع کمکی ربات ========
sending_active = {}
last_sent_summary = {}

async def send_message(chat_id, text, parse_mode='Markdown', reply_markup=None):
    bot = Bot(token=TELEGRAM_TOKEN)
    await bot.send_message(chat_id=chat_id, text=text, parse_mode=parse_mode, reply_markup=reply_markup)

def get_all_symbols():
    all_symbols = []
    for _, _, symbols in CATEGORIES.values():
        all_symbols.extend(symbols)
    return all_symbols

def format_price(price, symbol_key):
    if price is None:
        return "⛔ در دسترس نیست"
    if symbol_key in ['usdt_rial', 'aed']:
        return f"{price:,.0f} ریال"
    if price < 0.001:
        return f"{price:.4e}"
    if price < 1:
        return f"{price:.7f}"
    if price < 10:
        return f"{price:.4f}"
    if price < 100:
        return f"{price:.2f}"
    return f"{price:,.2f}"

def format_change(change):
    if change is None:
        return ""
    if abs(change) < 0.001:
        return "➖ بدون تغییر"
    return f"📈 {change:+.2f}%" if change > 0 else f"📉 {change:+.2f}%"

def format_price_with_market_status(symbol_key, price):
    if price is None:
        cached = get_last_price(symbol_key)
        if cached is not None:
            formatted = format_price(cached, symbol_key)
            change = get_price_change(symbol_key, cached)
            change_text = format_change(change)
            market_status = " 🔒 بازار بسته"
            return f"{formatted} {change_text}{market_status}" if change_text else f"{formatted}{market_status}"
        return "⛔ در دسترس نیست"
    
    formatted = format_price(price, symbol_key)
    change = get_price_change(symbol_key, price)
    change_text = format_change(change)
    market_status = " 🔒 بازار بسته" if not is_market_open(symbol_key) else ""
    return f"{formatted} {change_text}{market_status}".strip()

def generate_price_message(selections):
    lines = []
    for cat_name, cat_emoji, symbols in CATEGORIES.values():
        cat_selected = [s for s in symbols if s[0] in selections]
        if not cat_selected:
            continue
        lines.append(f"{cat_emoji} {cat_name}:")
        for key, name, emoji in cat_selected:
            price = get_price(key)
            if price is not None:
                save_price(key, price)
            formatted = format_price_with_market_status(key, price)
            lines.append(f"{emoji} {name} : {formatted}")
        lines.append("")
    return "\n".join(lines) if lines else "هیچ نمادی انتخاب نشده است."

# ======== منوها و دکمه‌ها ========
def get_selection_keyboard(user_id, symbols):
    selections = get_user_selections(user_id)
    keyboard = []
    for key, name, emoji in symbols:
        checked = "✅ " if key in selections else ""
        keyboard.append([InlineKeyboardButton(f"{checked}{emoji} {name}", callback_data=f"toggle_{key}")])
    return keyboard

async def show_main_menu(chat_id, user_id):
    text = "📊 **به ربات قیمت‌های لحظه‌ای خوش آمدید!**\n\nلطفاً یک دسته را انتخاب کنید:"
    keyboard = []
    for cat_key, (cat_name, cat_emoji, _) in CATEGORIES.items():
        keyboard.append([InlineKeyboardButton(f"{cat_emoji} {cat_name}", callback_data=f"cat_{cat_key}")])
    keyboard.append([InlineKeyboardButton("📊 نمایش همه", callback_data="show_all")])
    await send_message(chat_id, text, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(keyboard))

async def show_category_symbols(chat_id, user_id, category_key):
    cat_name, cat_emoji, symbols = CATEGORIES[category_key]
    selections = get_user_selections(user_id)
    text = f"📊 **{cat_emoji} {cat_name}**\n\n✅ روی هر نماد کلیک کنید تا انتخاب/لغو شود.\nبعد از انتخاب، روی **شروع ارسال** کلیک کنید.\n\n**انتخاب‌شده:**\n"
    selected = [f"{emoji} {name}" for key, name, emoji in symbols if key in selections]
    text += "\n".join(selected) if selected else "هیچ نمادی انتخاب نشده است."
    
    keyboard = get_selection_keyboard(user_id, symbols)
    keyboard.extend([
        [InlineKeyboardButton("🔙 بازگشت به دسته‌ها", callback_data="back_categories")],
        [InlineKeyboardButton("📊 انتخاب همه", callback_data=f"select_all_cat_{category_key}")],
        [InlineKeyboardButton("🚀 شروع ارسال", callback_data="start_sending")],
        [InlineKeyboardButton("🛑 توقف ارسال", callback_data="stop_sending")],
        [InlineKeyboardButton("🗑️ پاک کردن همه انتخاب‌ها", callback_data="clear_all")],
    ])
    await send_message(chat_id, text, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(keyboard))

async def show_all_symbols(chat_id, user_id):
    selections = get_user_selections(user_id)
    all_symbols = get_all_symbols()
    text = "📊 **همه نمادها**\n\n✅ روی هر نماد کلیک کنید تا انتخاب/لغو شود.\nبعد از انتخاب، روی **شروع ارسال** کلیک کنید.\n\n**انتخاب‌شده:**\n"
    selected = [f"{emoji} {name}" for key, name, emoji in all_symbols if key in selections]
    text += "\n".join(selected) if selected else "هیچ نمادی انتخاب نشده است."
    
    keyboard = get_selection_keyboard(user_id, all_symbols)
    keyboard.extend([
        [InlineKeyboardButton("🔙 بازگشت به دسته‌ها", callback_data="back_categories")],
        [InlineKeyboardButton("📊 انتخاب همه", callback_data="select_all")],
        [InlineKeyboardButton("🚀 شروع ارسال", callback_data="start_sending")],
        [InlineKeyboardButton("🛑 توقف ارسال", callback_data="stop_sending")],
        [InlineKeyboardButton("🗑️ پاک کردن همه انتخاب‌ها", callback_data="clear_all")],
    ])
    await send_message(chat_id, text, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(keyboard))

# ======== دستورات ربات ========
async def start(update, context):
    await show_main_menu(update.effective_chat.id, update.effective_user.id)

async def button_handler(update, context):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    chat_id = query.message.chat.id
    data = query.data
    
    if data == "back_categories":
        await show_main_menu(chat_id, user_id)
    elif data == "clear_all":
        clear_user_selections(user_id)
        await query.edit_message_text("🗑️ همه انتخاب‌ها پاک شد.")
        await show_main_menu(chat_id, user_id)
    elif data == "select_all":
        select_all_symbols(user_id)
        await query.edit_message_text("📊 همه نمادها انتخاب شدند.")
        await show_all_symbols(chat_id, user_id)
    elif data == "show_all":
        await show_all_symbols(chat_id, user_id)
    elif data.startswith("cat_"):
        category_key = data.replace("cat_", "")
        await show_category_symbols(chat_id, user_id, category_key)
    elif data.startswith("select_all_cat_"):
        category_key = data.replace("select_all_cat_", "")
        for key, _, _ in CATEGORIES[category_key][2]:
            save_user_selection(user_id, key)
        await query.edit_message_text(f"📊 همه نمادهای {CATEGORIES[category_key][0]} انتخاب شدند.")
        await show_category_symbols(chat_id, user_id, category_key)
    elif data == "start_sending":
        if not get_user_selections(user_id):
            await query.edit_message_text("⚠️ حداقل یک نماد انتخاب کنید.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 بازگشت", callback_data="back_categories")]]))
            return
        sending_active[user_id] = True
        last_sent_summary[user_id] = ""
        await query.edit_message_text("🚀 **ارسال خودکار شروع شد!**\nهر ۱ دقیقه قیمت‌های انتخاب‌شده ارسال می‌شود.", parse_mode='Markdown')
    elif data == "stop_sending":
        sending_active[user_id] = False
        await query.edit_message_text("🛑 **ارسال خودکار متوقف شد.**", parse_mode='Markdown')
    elif data.startswith("toggle_"):
        symbol = data.replace("toggle_", "")
        selections = get_user_selections(user_id)
        if symbol in selections:
            remove_user_selection(user_id, symbol)
        else:
            save_user_selection(user_id, symbol)
        await show_all_symbols(chat_id, user_id)

async def status_single(update, symbol_key, name, emoji):
    price = get_price(symbol_key)
    if price is not None:
        save_price(symbol_key, price)
    formatted = format_price_with_market_status(symbol_key, price)
    await send_message(update.effective_chat.id, f"{emoji} **{name}**\n💰 {formatted}", parse_mode='Markdown')

async def all_status(update, context):
    selections = get_user_selections(update.effective_user.id)
    if not selections:
        await send_message(update.effective_chat.id, "⚠️ هیچ نمادی انتخاب نشده است. لطفاً ابتدا نمادهای مورد نظر را انتخاب کنید.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 بازگشت به منو", callback_data="back_categories")]]))
        return
    message = generate_price_message(selections)
    await send_message(update.effective_chat.id, f"📊 **قیمت‌های لحظه‌ای**\n━━━━━━━━━━━━━━━━━━━\n{message}", parse_mode='Markdown')

async def help_command(update, context):
    await send_message(update.effective_chat.id,
        "📋 **دستورات:**\n"
        "/start - منوی اصلی\n"
        "/all - نمایش قیمت‌های انتخاب‌شده با فرمت جدید\n"
        "/usdt_rial - قیمت تتر (ریال)\n"
        "/aed - قیمت درهم امارات"
    )

# ======== حلقه خودکار ========
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
                    await bot.send_message(user_id, f"🔔 **به‌روزرسانی قیمت‌ها**\n━━━━━━━━━━━━━━━━━━━\n{message}", parse_mode='Markdown')
                    last_sent_summary[user_id] = message
            await asyncio.sleep(INTERVAL)
        except Exception as e:
            print(f"⚠️ خطا در حلقه خودکار: {e}")
            await asyncio.sleep(INTERVAL)

def start_auto_send():
    asyncio.run(auto_send_loop())

# ======== اجرای ربات ========
def run_bot_in_main_thread():
    app = Application.builder().token(TELEGRAM_TOKEN).connect_timeout(TIMEOUT).read_timeout(TIMEOUT).build()
    
    # دستورات عمومی
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("all", all_status))
    app.add_handler(CommandHandler("usdt_rial", lambda u,c: status_single(u, 'usdt_rial', 'تتر (ریال)', '💰')))
    app.add_handler(CommandHandler("aed", lambda u,c: status_single(u, 'aed', 'درهم امارات', '🇦🇪')))
    
    # دستورات نمادها
    for key, name, emoji in get_all_symbols():
        if key not in ['usdt_rial', 'aed']:  # قبلاً اضافه شده‌اند
            app.add_handler(CommandHandler(key, lambda u,c, k=key, n=name, e=emoji: status_single(u, k, n, e)))
    
    app.add_handler(CallbackQueryHandler(button_handler))
    print("🤖 ربات در حال اجرا...")
    app.run_polling()

# ======== وب‌سرور Flask ========
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

async def send_update_message():
    try:
        bot = Bot(token=TELEGRAM_TOKEN)
        await bot.send_message(chat_id=CHAT_ID, text="✅ ربات آپدیت شد")
    except Exception as e:
        print(f"⚠️ خطا در ارسال پیام آپدیت: {e}")

def send_update_message_sync():
    asyncio.run(send_update_message())

if __name__ == '__main__':
    init_db()
    send_update_message_sync()
    
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    
    auto_thread = threading.Thread(target=start_auto_send, daemon=True)
    auto_thread.start()
    
    run_bot_in_main_thread()
