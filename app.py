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
CHAT_ID = os.environ.get('CHAT_ID', '483833953')  # مقدار پیش‌فرض برای تست
INTERVAL = int(os.environ.get('INTERVAL', 60))
TIMEOUT = 30
# =========================

logging.basicConfig(level=logging.ERROR)

# ======== لیست نمادها (همان کد شما) ========
SYMBOLS = [
    ('btc', 'BTC', '₿'), ('eth', 'ETH', '💎'), ('bnb', 'BNB', '🟡'),
    ('gram', 'GRAM', '🔷'), ('xrp', 'XRP', '💠'), ('sol', 'SOL', '☀️'),
    ('doge', 'DOGE', '🐕'), ('bch', 'BCH', '🔶'), ('ltc', 'LTC', '⚡'),
    ('trx', 'TRX', '🔴'), ('dot', 'DOT', '🟣'), ('gold', 'GOLD', '🏆'),
    ('silver', 'SILVER', '🥈'), ('oil', 'OIL', '🛢️'), ('brent', 'BRENT', '🛢️'),
    ('gas', 'GAS', '🔥'), ('sugar', 'SUGAR', '🍬')
]

# ======== تمام توابع get_price، is_market_open، fetch_yahoo و ... ========
# (دقیقاً همان کدهایی که در فایل خود دارید، بدون تغییر)
# برای جلوگیری از طولانی شدن، فرض می‌کنیم همه‌ی توابع شما اینجا قرار دارند.

# ======== دیتابیس و توابع مدیریت ========
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
    c.execute('INSERT INTO history (symbol, timestamp, price) VALUES (?, ?, ?)',
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

# ======== توابع ربات (دقیقاً همان کد شما) ========
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

# ======== بقیه دستورات (gold, silver, btc, ...) ========
# (همان کدهای شما، برای اختصار حذف شده، اما باید در فایل نهایی باشد)

# ======== حلقه خودکار (با استفاده از asyncio) ========
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

# ======== اجرای ربات در ترد جداگانه ========
def run_bot_polling():
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
    app.run_polling(signal_handlers=False)  # مهم: برای جلوگیری از خطای ترد

# ======== وب‌سرور Flask ========
flask_app = Flask(__name__)

@flask_app.route('/')
def home():
    return "✅ Bot is running!"

@flask_app.route('/health')
def health():
    return "OK"

# ======== اجرای اصلی ========
if __name__ == '__main__':
    init_db()
    
    # اجرای ربات در ترد جداگانه
    bot_thread = threading.Thread(target=run_bot_polling, daemon=True)
    bot_thread.start()
    
    # اجرای حلقه خودکار در ترد جداگانه
    auto_thread = threading.Thread(target=lambda: asyncio.run(auto_send_loop()), daemon=True)
    auto_thread.start()
    
    # اجرای وب‌سرور
    port = int(os.environ.get('PORT', 10000))
    flask_app.run(host='0.0.0.0', port=port)
