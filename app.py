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
# =========================

logging.basicConfig(level=logging.ERROR)

# ======== لیست نمادها ========
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

# ======== دریافت قیمت (برای تست) ========
def get_price(symbol_key):
    return 123.45

# ======== توابع ربات ========
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    await show_menu(chat_id, user_id, context)

async def show_menu(chat_id, user_id, context):
    text = "📊 **SELECT SYMBOLS**\n\nClick to select/deselect.\nAfter selection, click **🚀 START**.\n\n**SELECTED:**\n"
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
    keyboard.append([InlineKeyboardButton("🚀 START", callback_data="start_sending")])
    keyboard.append([InlineKeyboardButton("🗑️ CLEAR ALL", callback_data="clear_all")])
    reply_markup = InlineKeyboardMarkup(keyboard)
    await context.bot.send_message(chat_id, text, parse_mode='Markdown', reply_markup=reply_markup)

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    chat_id = query.message.chat.id
    data = query.data
    
    if data == "clear_all":
        clear_user_selections(user_id)
        await query.edit_message_text("🗑️ All cleared.")
        await show_menu(chat_id, user_id, context)
        return
    
    if data == "start_sending":
        selections = get_user_selections(user_id)
        if not selections:
            await query.edit_message_text("⚠️ Select at least one symbol.")
            return
        await query.edit_message_text("🚀 **Auto-send started!**\nUpdates every 1 minute.")
        return
    
    if data.startswith("toggle_"):
        symbol = data.replace("toggle_", "")
        selections = get_user_selections(user_id)
        if symbol in selections:
            remove_user_selection(user_id, symbol)
        else:
            save_user_selection(user_id, symbol)
        await show_menu(chat_id, user_id, context)
        return

# ======== وب‌سرور ========
flask_app = Flask(__name__)

@flask_app.route('/')
def home():
    return "✅ Bot is running!"

# ======== اجرای اصلی ========
async def main():
    init_db()
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_handler))
    
    print("🤖 Bot started...")
    await app.initialize()
    await app.start()
    await app.updater.start_polling()
    
    import threading
    def run_flask():
        port = int(os.environ.get('PORT', 10000))
        flask_app.run(host='0.0.0.0', port=port)
    
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    
    while True:
        await asyncio.sleep(1)

if __name__ == "__main__":
    asyncio.run(main())
