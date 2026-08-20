import os
os.environ['TZ'] = 'UTC'
import requests
from flask import Flask, request
from telegram import Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Dispatcher, CommandHandler, CallbackQueryHandler, ContextTypes
import logging
import sqlite3
from datetime import datetime
import asyncio

# ======== تنظیمات ========
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN')
if not TELEGRAM_TOKEN:
    raise ValueError("TELEGRAM_TOKEN environment variable not set!")
WEBHOOK_URL = os.environ.get('RENDER_EXTERNAL_URL', 'https://crypfx-bot-1.onrender.com')
# =========================

logging.basicConfig(level=logging.ERROR)

app = Flask(__name__)
bot = Bot(token=TELEGRAM_TOKEN)

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

# ======== لیست نمادها ========
SYMBOLS = [
    ('btc', 'BTC', '₿'), ('eth', 'ETH', '💎'), ('bnb', 'BNB', '🟡'),
    ('gram', 'GRAM', '🔷'), ('xrp', 'XRP', '💠'), ('sol', 'SOL', '☀️'),
    ('doge', 'DOGE', '🐕'), ('bch', 'BCH', '🔶'), ('ltc', 'LTC', '⚡'),
    ('trx', 'TRX', '🔴'), ('dot', 'DOT', '🟣'), ('gold', 'GOLD', '🏆'),
    ('silver', 'SILVER', '🥈'), ('oil', 'OIL', '🛢️'), ('brent', 'BRENT', '🛢️'),
    ('gas', 'GAS', '🔥'), ('sugar', 'SUGAR', '🍬')
]

# ======== دریافت قیمت ========
def get_price(symbol_key):
    # اینجا فقط یک نمونه ساده از دریافت قیمت قراره
    # برای مثال، یک عدد ثابت برمی‌گردونیم تا ربات تست بشه
    return 123.45
    # (شما می‌تونید کد کامل دریافت قیمت از یاهو رو اینجا قرار بدید)

# ======== توابع ربات ========
async def start(update, context):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    await show_menu(chat_id, user_id)

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
    keyboard = []
    for key, name, emoji in SYMBOLS:
        checked = "✅ " if key in selections else ""
        keyboard.append([InlineKeyboardButton(f"{checked}{emoji} {name}", callback_data=f"toggle_{key}")])
    keyboard.append([InlineKeyboardButton("📊 SHOW ALL", callback_data="select_all")])
    keyboard.append([InlineKeyboardButton("🚀 START", callback_data="start_sending")])
    keyboard.append([InlineKeyboardButton("🛑 STOP", callback_data="stop_sending")])
    keyboard.append([InlineKeyboardButton("🗑️ CLEAR ALL", callback_data="clear_all")])
    keyboard.append([InlineKeyboardButton("📋 DATABASE", callback_data="status")])
    reply_markup = InlineKeyboardMarkup(keyboard)
    await bot.send_message(chat_id, text, parse_mode='Markdown', reply_markup=reply_markup)

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
        await query.edit_message_text("🗑️ All selections cleared.")
        await show_menu(chat_id, user_id)
        return
    if data == "select_all":
        select_all_symbols(user_id)
        await query.edit_message_text("📊 **SHOW ALL activated!**")
        return
    if data == "start_sending":
        selections = get_user_selections(user_id)
        if not selections:
            await query.edit_message_text("⚠️ Select at least one symbol.")
            return
        await query.edit_message_text("🚀 **Auto-send started!**\nUpdates every 1 minute.")
        return
    if data == "stop_sending":
        await query.edit_message_text("🛑 **Auto-send stopped.**")
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

async def status_db(chat_id):
    report = "📊 **DATABASE STATUS**\n"
    for key, name, emoji in SYMBOLS:
        price = get_last_price(key)
        report += f"🔹 {name}: {price if price else 'N/A'}\n"
    await bot.send_message(chat_id, report)

# ======== Webhook ========
@app.route('/', methods=['GET'])
def index():
    return "✅ Bot is running!"

@app.route('/webhook', methods=['POST'])
def webhook():
    update = Update.de_json(request.get_json(), bot)
    dispatcher.process_update(update)
    return "ok"

# ======== تنظیم Webhook ========
def set_webhook():
    url = f"{WEBHOOK_URL}/webhook"
    bot.delete_webhook()
    bot.set_webhook(url=url)
    print(f"✅ Webhook set to {url}")

# ======== اجرا ========
if __name__ == '__main__':
    init_db()
    dispatcher = Dispatcher(bot, None, use_context=True)
    dispatcher.add_handler(CommandHandler("start", start))
    dispatcher.add_handler(CallbackQueryHandler(button_handler))
    set_webhook()
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port)
