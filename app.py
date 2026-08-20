import os
os.environ['TZ'] = 'UTC'
import asyncio
import time
import sqlite3
import requests
from datetime import datetime
from flask import Flask
from telegram import Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
import logging

# ======== تنظیمات ========
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN')
if not TELEGRAM_TOKEN:
    raise ValueError("TELEGRAM_TOKEN environment variable not set!")
INTERVAL = 60
TIMEOUT = 30
# =========================

logging.basicConfig(level=logging.ERROR)

# ======== لیست نمادها (برای تست با یک نماد ساده) ========
SYMBOLS = [
    ('btc', 'BTC', '₿'),
    ('eth', 'ETH', '💎'),
]

# ======== دیتابیس ========
DB_PATH = "market_data.db"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS prices
                 (symbol TEXT, timestamp TEXT, price REAL, PRIMARY KEY (symbol, timestamp))''')
    c.execute('''CREATE TABLE IF NOT EXISTS user_selections
                 (user_id INTEGER, symbol TEXT, PRIMARY KEY (user_id, symbol))''')
    conn.commit()
    conn.close()

def get_last_price(symbol):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT price FROM prices WHERE symbol=? ORDER BY timestamp DESC LIMIT 1', (symbol,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else None

def save_price(symbol, price):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('INSERT INTO prices (symbol, timestamp, price) VALUES (?, ?, ?)',
              (symbol, datetime.now().isoformat(), price))
    conn.commit()
    conn.close()

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

# ======== دریافت قیمت (ساده برای تست) ========
def get_price(symbol_key):
    # برای تست، یک عدد ثابت برمی‌گردانیم
    return 123.45

# ======== توابع ربات ========
async def start(update, context):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    await show_menu(chat_id, user_id)

async def show_menu(chat_id, user_id):
    text = "📊 **SELECT SYMBOLS**\n\n"
    text += "✅ Click to select/deselect.\n"
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
    
    await context.bot.send_message(chat_id, text, parse_mode='Markdown', reply_markup=reply_markup)

async def button_handler(update, context):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    chat_id = query.message.chat.id
    data = query.data
    
    if data == "status":
        report = "📊 **DATABASE STATUS**\n"
        for key, name, emoji in SYMBOLS:
            price = get_last_price(key)
            report += f"🔹 {name}: {price if price else 'N/A'}\n"
        await context.bot.send_message(chat_id, report)
        return
    
    if data == "clear_all":
        clear_user_selections(user_id)
        await query.edit_message_text("🗑️ All selections cleared.")
        await show_menu(chat_id, user_id)
        return
    
    if data == "select_all":
        select_all_symbols(user_id)
        await query.edit_message_text("📊 All symbols selected!")
        await show_menu(chat_id, user_id)
        return
    
    if data == "start_sending":
        selections = get_user_selections(user_id)
        if not selections:
            await query.edit_message_text("⚠️ Select at least one symbol.")
            return
        await query.edit_message_text("🚀 **Auto-send started!**\nUpdates every 1 minute.")
        # در اینجا می‌توانید حلقه ارسال خودکار را شروع کنید
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

# ======== وب‌سرور Flask برای Render ========
flask_app = Flask(__name__)

@flask_app.route('/')
def home():
    return "✅ Bot is running!"

# ======== اجرای اصلی (بدون ترد، در همین جا) ========
async def main():
    init_db()
    app = Application.builder().token(TELEGRAM_TOKEN).connect_timeout(TIMEOUT).read_timeout(TIMEOUT).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_handler))
    
    print("🤖 Bot started...")
    await app.initialize()
    await app.start()
    await app.updater.start_polling()
    
    # Flask را در یک ترد جداگانه اجرا می‌کنیم (اما ربات در main thread اجرا می‌شود)
    import threading
    def run_flask():
        port = int(os.environ.get('PORT', 10000))
        flask_app.run(host='0.0.0.0', port=port)
    
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    
    # منتظر می‌مانیم تا ربات کار کند
    while True:
        await asyncio.sleep(1)

if __name__ == "__main__":
    asyncio.run(main())
