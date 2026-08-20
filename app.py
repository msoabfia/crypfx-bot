import os
os.environ['TZ'] = 'UTC'
import asyncio
import time
import sqlite3
import requests
from datetime import datetime
from curl_cffi import requests as cffi_requests
from flask import Flask
from telegram import Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes, CallbackQueryHandler
from telegram.error import TimedOut, NetworkError
import logging
import threading

# ======== تنظیمات از متغیرهای محیطی ========
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN')
if not TELEGRAM_TOKEN:
    raise ValueError("TELEGRAM_TOKEN environment variable not set!")
CHAT_ID = os.environ.get('CHAT_ID', '483833953')
INTERVAL = int(os.environ.get('INTERVAL', 60))
TIMEOUT = 30
# =========================

logging.basicConfig(level=logging.ERROR)

# ======== لیست نمادها ========
SYMBOLS = [
    ('btc', 'BTC', '₿'), ('eth', 'ETH', '💎'), ('bnb', 'BNB', '🟡'),
    ('gram', 'GRAM', '🔷'), ('xrp', 'XRP', '💠'), ('sol', 'SOL', '☀️'),
    ('doge', 'DOGE', '🐕'), ('bch', 'BCH', '🔶'), ('ltc', 'LTC', '⚡'),
    ('trx', 'TRX', '🔴'), ('dot', 'DOT', '🟣'), ('gold', 'GOLD', '🏆'),
    ('silver', 'SILVER', '🥈'), ('oil', 'OIL', '🛢️'), ('brent', 'BRENT', '🛢️'),
    ('gas', 'GAS', '🔥'), ('sugar', 'SUGAR', '🍬')
]

# ======== توابع دریافت قیمت ========
def is_market_open(symbol_key):
    now = datetime.now()
    today = now.weekday()
    if symbol_key in ['btc', 'eth', 'bnb', 'gram', 'xrp', 'sol', 'doge', 'bch', 'ltc', 'trx', 'dot']:
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

def get_price(symbol_key):
    if symbol_key == 'btc':
        return fetch_yahoo('BTC-USD') or fetch_twelve('BTC/USD')
    elif symbol_key == 'eth':
        return fetch_yahoo('ETH-USD') or fetch_twelve('ETH/USD')
    elif symbol_key == 'bnb':
        return fetch_yahoo('BNB-USD') or fetch_twelve('BNB/USD')
    elif symbol_key == 'gram':
        return fetch_coingecko('the-open-network') or fetch_twelve('TON/USD')
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
    elif symbol_key == 'dot':
        return fetch_yahoo('DOT-USD') or fetch_twelve('DOT/USD')
    elif symbol_key == 'gold':
        return fetch_yahoo('GC=F')
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
    for key, _, _ in SYMBOLS:
        c.execute('INSERT OR IGNORE INTO user_selections (user_id, symbol) VALUES (?, ?)', (user_id, key))
    conn.commit()
    conn.close()

# ======== توابع ربات ========
def get_selection_keyboard(user_id):
    selections = get_user_selections(user_id)
    keyboard = []
    for key, name, emoji in SYMBOLS:
        checked = "✅ " if key in selections else ""
        keyboard.append([InlineKeyboardButton(f"{checked}{emoji} {name}", callback_data=f"toggle_{key}")])
    keyboard.append([InlineKeyboardButton("📊 SHOW ALL", callback_data="select_all")])
    keyboard.append([InlineKeyboardButton("🚀 START", callback_data="start_sending")])
    keyboard.append([InlineKeyboardButton("🛑 STOP", callback_data="stop_sending")])
    keyboard.append([InlineKeyboardButton("🗑️ CLEAR ALL", callback_data="clear_all")])
    keyboard.append([InlineKeyboardButton("📋 DATABASE", callback_data="status")])
    return InlineKeyboardMarkup(keyboard)

def get_simple_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("📋 MENU", callback_data="menu")]])

sending_active = {}
last_sent_summary = {}

async def send_message(chat_id, text, parse_mode='Markdown', reply_markup=None):
    bot = Bot(token=TELEGRAM_TOKEN)
    await bot.send_message(chat_id=chat_id, text=text, parse_mode=parse_mode, reply_markup=reply_markup)

async def show_menu(chat_id, user_id):
    text = "📊 **SELECT SYMBOLS**\n\n"
    text += "✅ Click to select/deselect.\n"
    text += "📊 **SHOW ALL** = select all symbols & start.\n"
    text += "After selection, click **🚀 START**.\n\n"
    text += "**SELECTED:**\n"
    selections = get_user_selections(user_id)
    if selections:
        for key in selections:
            for k, name, emoji in SYMBOLS:
                if k == key:
                    text += f"{emoji} {name}\n"
                    break
    else:
        text += "No symbols selected."
    await send_message(chat_id, text, parse_mode='Markdown', reply_markup=get_selection_keyboard(user_id))

async def start(update, context):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    await show_menu(chat_id, user_id)

async def button_handler(update, context):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    chat_id = query.message.chat.id
    data = query.data
    if data == "menu":
        await show_menu(chat_id, user_id)
        return
    if data == "status":
        await status_db(chat_id)
        return
    if data == "clear_all":
        clear_user_selections(user_id)
        await query.edit_message_text("🗑️ All selections cleared.", reply_markup=get_simple_keyboard())
        await asyncio.sleep(1)
        await show_menu(chat_id, user_id)
        return
    if data == "select_all":
        select_all_symbols(user_id)
        sending_active[user_id] = True
        last_sent_summary[user_id] = ""
        await query.edit_message_text("📊 **SHOW ALL activated!**\nAll symbols selected.\nAuto-send started.\nUpdates every 1 minute.", parse_mode='Markdown', reply_markup=get_simple_keyboard())
        return
    if data == "start_sending":
        selections = get_user_selections(user_id)
        if not selections:
            await query.edit_message_text("⚠️ Select at least one symbol.", reply_markup=get_simple_keyboard())
            await asyncio.sleep(1)
            await show_menu(chat_id, user_id)
            return
        sending_active[user_id] = True
        last_sent_summary[user_id] = ""
        await query.edit_message_text("🚀 **Auto-send started!**\nUpdates every 1 minute.", parse_mode='Markdown', reply_markup=get_simple_keyboard())
        return
    if data == "stop_sending":
        sending_active[user_id] = False
        await query.edit_message_text("🛑 **Auto-send stopped.**", parse_mode='Markdown', reply_markup=get_simple_keyboard())
        return
    if data.startswith("toggle_"):
        symbol = data.replace("toggle_", "")
        selections = get_user_selections(user_id)
        if symbol in selections:
            remove_user_selection(user_id, symbol)
        else:
            save_user_selection(user_id, symbol)
        await show_menu(chat_id, user_id)
        return

async def status_single(update, symbol_key, name, emoji):
    chat_id = update.effective_chat.id
    new_price = get_price(symbol_key)
    if not new_price:
        await send_message(chat_id, f"{emoji} {name}: ⛔ Unavailable.", reply_markup=get_simple_keyboard())
        return
    old_price = get_last_price(symbol_key) or 0
    save_price(symbol_key, new_price)
    market_status = ""
    if not is_market_open(symbol_key):
        market_status = " 🔒 Closed"
    if old_price and abs(new_price - old_price) > 0.0001:
        change = ((new_price - old_price) / old_price) * 100
        if abs(change) > 0.001:
            arrow = f"📈 {change:+.2f}%" if change > 0 else f"📉 {change:+.2f}%"
            report = f"{emoji} **{name}**\n💰 New: ${new_price:.4f}\n📊 Old: ${old_price:.4f}\n{arrow}{market_status}"
        else:
            report = f"{emoji} **{name}**\n💰 Price: ${new_price:.4f}\n➖ No change{market_status}"
    else:
        report = f"{emoji} **{name}**\n💰 Price: ${new_price:.4f}\n💰 Initial price{market_status}"
    await send_message(chat_id, report, parse_mode='Markdown', reply_markup=get_simple_keyboard())

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
async def dot(update, context): await status_single(update, 'dot', 'DOT', '🟣')
async def oil(update, context): await status_single(update, 'oil', 'OIL', '🛢️')
async def brent(update, context): await status_single(update, 'brent', 'BRENT', '🛢️')
async def gas(update, context): await status_single(update, 'gas', 'GAS', '🔥')
async def sugar(update, context): await status_single(update, 'sugar', 'SUGAR', '🍬')

async def all_status(update, context):
    chat_id = update.effective_chat.id
    lines = []
    for key, name, emoji in SYMBOLS:
        new_price = get_price(key)
        if not new_price:
            old = get_last_price(key)
            if old:
                lines.append(f"{emoji} {name}: ${old:.4f} | 📴 Last price")
            else:
                lines.append(f"{emoji} {name}: ⛔ Unavailable")
            continue
        old_price = get_last_price(key)
        save_price(key, new_price)
        arrow = ""
        if old_price and abs(new_price - old_price) > 0.0001:
            change = ((new_price - old_price) / old_price) * 100
            if abs(change) > 0.001:
                arrow = f"📈 {change:+.2f}%" if change > 0 else f"📉 {change:+.2f}%"
            else:
                arrow = "➖ No change"
        else:
            arrow = "💰 Initial" if not old_price else "➖ No change"
        if not is_market_open(key):
            arrow += " 🔒"
        lines.append(f"{emoji} {name}: ${new_price:.4f} | {arrow}")
    text = "📊 **SUMMARY**\n━━━━━━━━━━━━━━━━━━━\n" + "\n".join(lines)
    await send_message(chat_id, text, parse_mode='Markdown', reply_markup=get_simple_keyboard())

async def status_cmd(update, context):
    await status_db(update.effective_chat.id)

async def status_db(chat_id):
    report = "📊 **DATABASE STATUS**\n"
    for key, name, emoji in SYMBOLS:
        price = get_last_price(key)
        report += f"🔹 {name}: {price if price else 'N/A'}\n"
    await send_message(chat_id, report, reply_markup=get_simple_keyboard())

async def help_command(update, context):
    chat_id = update.effective_chat.id
    await send_message(
        chat_id,
        "📋 **COMMANDS:**\n/start - Menu\n"
        "/gold - GOLD\n/silver - SILVER\n/btc - BTC\n/eth - ETH\n/bnb - BNB\n"
        "/gram - GRAM\n/xrp - XRP\n/sol - SOL\n/doge - DOGE\n/bch - BCH\n/ltc - LTC\n/trx - TRX\n/dot - DOT\n"
        "/oil - OIL (WTI)\n/brent - BRENT\n/gas - GAS\n/sugar - SUGAR\n/all - Summary\n/status - Database",
        reply_markup=get_simple_keyboard()
    )

# ======== حلقه خودکار (در ترد جداگانه) ========
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
                lines = []
                for key, name, emoji in SYMBOLS:
                    if key not in selections:
                        continue
                    new_price = get_price(key)
                    if not new_price:
                        old = get_last_price(key)
                        if old:
                            lines.append(f"{emoji} {name}: ${old:.4f} | 📴 Last price")
                        else:
                            lines.append(f"{emoji} {name}: ⛔ Unavailable")
                        continue
                    old_price = get_last_price(key)
                    save_price(key, new_price)
                    arrow = ""
                    if old_price and abs(new_price - old_price) > 0.0001:
                        change = ((new_price - old_price) / old_price) * 100
                        if abs(change) > 0.001:
                            arrow = f"📈 {change:+.2f}%" if change > 0 else f"📉 {change:+.2f}%"
                        else:
                            arrow = "➖ No change"
                    else:
                        arrow = "💰 Initial" if not old_price else "➖ No change"
                    if not is_market_open(key):
                        arrow += " 🔒"
                    lines.append(f"{emoji} {name}: ${new_price:.4f} | {arrow}")
                summary = "\n".join(lines)
                if summary and summary != last_sent_summary.get(user_id, ""):
                    await bot.send_message(
                        user_id,
                        f"🔔 **UPDATE**\n━━━━━━━━━━━━━━━━━━━\n{summary}",
                        parse_mode='Markdown',
                        reply_markup=get_simple_keyboard()
                    )
                    last_sent_summary[user_id] = summary
            await asyncio.sleep(INTERVAL)
        except Exception as e:
            print(f"⚠️ Auto-loop error: {e}")
            await asyncio.sleep(INTERVAL)

def start_auto_send():
    asyncio.run(auto_send_loop())

# ======== اجرای ربات در ترد اصلی (بدون خطای ترد) ========
def run_bot_in_main_thread():
    """اجرای ربات در ترد اصلی (بدون خطای set_wakeup_fd)"""
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
    app.add_handler(CommandHandler("dot", dot))
    app.add_handler(CommandHandler("oil", oil))
    app.add_handler(CommandHandler("brent", brent))
    app.add_handler(CommandHandler("gas", gas))
    app.add_handler(CommandHandler("sugar", sugar))
    app.add_handler(CommandHandler("all", all_status))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CallbackQueryHandler(button_handler))
    print("🤖 Bot polling started...")
    app.run_polling()  # در ترد اصلی اجرا می‌شود

# ======== وب‌سرور Flask (در ترد جداگانه) ========
flask_app = Flask(__name__)

@flask_app.route('/')
def home():
    return "✅ Bot is running!"

@flask_app.route('/health')
def health():
    return "OK"

def run_flask():
    port = int(os.environ.get('PORT', 10000))
    flask_app.run(host='0.0.0.0', port=port)

# ======== اجرای اصلی ========
if __name__ == '__main__':
    init_db()
    
    # وب‌سرور را در یک ترد جداگانه اجرا کن (ربات در ترد اصلی می‌ماند)
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    
    # حلقه خودکار را در یک ترد جداگانه اجرا کن
    auto_thread = threading.Thread(target=start_auto_send, daemon=True)
    auto_thread.start()
    
    # ربات را در ترد اصلی اجرا کن (مهم: اینجا نباید از asyncio استفاده کنی)
    run_bot_in_main_thread()
