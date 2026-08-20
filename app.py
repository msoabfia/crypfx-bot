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
# (توابع fetch_yahoo, fetch_twelve, fetch_coingecko, get_price, is_market_open را اینجا قرار دهید)
# برای جلوگیری از طولانی شدن، فرض می‌کنیم این توابع در کد شما موجود هستند.

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

# ======== توابع ربات ========
sending_active = {}
last_sent_summary = {}

async def send_message(chat_id, text, parse_mode='Markdown', reply_markup=None):
    bot = Bot(token=TELEGRAM_TOKEN)
    await bot.send_message(chat_id=chat_id, text=text, parse_mode=parse_mode, reply_markup=reply_markup)

async def start(update, context):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    await show_menu(chat_id, user_id)

async def show_menu(chat_id, user_id):
    # (کد منوی شما)
    await send_message(chat_id, "📊 SELECT SYMBOLS", reply_markup=InlineKeyboardMarkup([]))

# ======== اجرای اصلی ========
def run_bot_polling():
    """اجرای ربات با روش Polling (بدون آرگومان signal_handlers)"""
    app = Application.builder().token(TELEGRAM_TOKEN).connect_timeout(TIMEOUT).read_timeout(TIMEOUT).build()
    app.add_handler(CommandHandler("start", start))
    # ... بقیه دستورات را اینجا اضافه کنید ...
    print("🤖 Bot polling started...")
    app.run_polling()  # ← بدون signal_handlers

# ======== وب‌سرور Flask ========
flask_app = Flask(__name__)

@flask_app.route('/')
def home():
    return "✅ Bot is running!"

@flask_app.route('/health')
def health():
    return "OK"

# ======== اجرای همزمان ========
if __name__ == '__main__':
    init_db()
    
    # اجرای ربات در ترد جداگانه
    bot_thread = threading.Thread(target=run_bot_polling, daemon=True)
    bot_thread.start()
    
    # اجرای وب‌سرور در ترد اصلی
    port = int(os.environ.get('PORT', 10000))
    flask_app.run(host='0.0.0.0', port=port)
