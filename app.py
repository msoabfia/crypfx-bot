import os
import asyncio
import time
import sqlite3
import logging
import threading
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Tuple, Any

import requests
import json
import re
from curl_cffi import requests as cffi_requests
from flask import Flask
from telegram import Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler
from telegram.error import TimedOut, NetworkError, Forbidden

# ==================== تنظیمات ====================
CONFIG = {
    "TELEGRAM_TOKEN": os.environ.get("TELEGRAM_TOKEN"),
    "ADMIN_CHAT_ID": os.environ.get("ADMIN_CHAT_ID", "483833953"),
    "INTERVAL": 60,
    "TIMEOUT": 30,
    "DB_PATH": "market_data.db",
    "PRICE_RETENTION_DAYS": 30,
}

if not CONFIG["TELEGRAM_TOKEN"]:
    raise ValueError("TELEGRAM_TOKEN environment variable not set!")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

CATEGORIES = {
    "crypto": {
        "name": "ارزهای دیجیتال",
        "emoji": "💰",
        "symbols": [
            ("btc", "BTC", "₿"),
            ("eth", "ETH", "💎"),
            ("bnb", "BNB", "🟡"),
            ("sol", "SOL", "☀️"),
            ("ltc", "LTC", "⚡"),
            ("bch", "BCH", "🔶"),
            ("xrp", "XRP", "💠"),
            ("trx", "TRX", "🔴"),
            ("doge", "DOGE", "🐕"),
            ("gram", "GRAM", "🔷"),
        ],
    },
    "metals": {
        "name": "فلزات گرانبها",
        "emoji": "🏆",
        "symbols": [
            ("gold", "GOLD", "🏆"),
            ("silver", "SILVER", "🥈"),
        ],
    },
    "energy": {
        "name": "انرژی و نفت",
        "emoji": "⛽",
        "symbols": [
            ("oil", "OIL", "🛢️"),
            ("brent", "BRENT", "🛢️"),
            ("gas", "GAS", "🔥"),
        ],
    },
    "agriculture": {
        "name": "کشاورزی",
        "emoji": "🌾",
        "symbols": [
            ("sugar", "SUGAR", "🍬"),
        ],
    },
}

# ==================== مدیریت دیتابیس ====================
class DatabaseManager:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._init_tables()

    def _init_tables(self):
        with self._get_connection() as conn:
            c = conn.cursor()
            c.execute(
                """CREATE TABLE IF NOT EXISTS prices
                 (symbol TEXT, timestamp TEXT, price REAL, PRIMARY KEY (symbol, timestamp))"""
            )
            c.execute(
                """CREATE TABLE IF NOT EXISTS closing_prices
                 (symbol TEXT, timestamp TEXT, price REAL, PRIMARY KEY (symbol, timestamp))"""
            )
            c.execute(
                """CREATE TABLE IF NOT EXISTS user_selections
                 (user_id INTEGER, symbol TEXT, PRIMARY KEY (user_id, symbol))"""
            )
            c.execute(
                """CREATE TABLE IF NOT EXISTS user_settings
                 (user_id INTEGER PRIMARY KEY, auto_send INTEGER DEFAULT 0)"""
            )
            conn.commit()

    def _get_connection(self):
        return sqlite3.connect(self.db_path)

    def execute_query(self, query: str, params: tuple = ()):
        with self._get_connection() as conn:
            c = conn.cursor()
            c.execute(query, params)
            conn.commit()
            return c

    def fetch_one(self, query: str, params: tuple = ()):
        with self._get_connection() as conn:
            c = conn.cursor()
            c.execute(query, params)
            return c.fetchone()

    def fetch_all(self, query: str, params: tuple = ()):
        with self._get_connection() as conn:
            c = conn.cursor()
            c.execute(query, params)
            return c.fetchall()


db = DatabaseManager(CONFIG["DB_PATH"])

# ==================== کش سراسری ====================
price_cache = {"data": {}, "last_update": 0, "lock": threading.Lock()}
sending_active = {}
last_sent_summary = {}
sending_lock = threading.Lock()


def get_all_symbols_list():
    return [key for cat in CATEGORIES.values() for key, _, _ in cat["symbols"]]


# ==================== دریافت قیمت از منابع ====================
def fetch_yahoo(symbol: str) -> Optional[float]:
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1h&range=1d"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
    }
    try:
        resp = cffi_requests.get(url, headers=headers, impersonate="chrome120", timeout=8)
        data = resp.json()
        result = data.get("chart", {}).get("result")
        if result and len(result) > 0:
            meta = result[0].get("meta", {})
            if meta.get("regularMarketPrice") is not None:
                return float(meta["regularMarketPrice"])
            quote = result[0].get("indicators", {}).get("quote", [{}])[0]
            if quote.get("close"):
                for p in reversed(quote["close"]):
                    if p is not None:
                        return float(p)
    except Exception as e:
        logger.error(f"Error fetching {symbol}: {e}")
    return None


def fetch_price_from_source(symbol_key: str) -> Optional[float]:
    if not is_market_open(symbol_key):
        return None

    symbol_map = {
        "gram": "GRAM-USD",
        "btc": "BTC-USD",
        "eth": "ETH-USD",
        "bnb": "BNB-USD",
        "xrp": "XRP-USD",
        "sol": "SOL-USD",
        "doge": "DOGE-USD",
        "bch": "BCH-USD",
        "ltc": "LTC-USD",
        "trx": "TRX-USD",
        "gold": "GC=F",
        "silver": "SI=F",
        "oil": "CL=F",
        "brent": "BZ=F",
        "gas": "NG=F",
    }
    if symbol_key in symbol_map:
        return fetch_yahoo(symbol_map[symbol_key])
    if symbol_key == "sugar":
        price = fetch_yahoo("SB=F")
        return round(price / 100, 4) if price else None
    return None


def is_market_open(symbol_key: str) -> bool:
    now = datetime.now()
    today = now.weekday()
    if symbol_key in ["btc", "eth", "bnb", "gram", "xrp", "sol", "doge", "bch", "ltc", "trx", "dot"]:
        return True
    if today in [5, 6]:
        return False
    if symbol_key == "sugar":
        iran_hour = (now.hour + 3) % 24
        iran_minute = now.minute + 30
        if iran_minute >= 60:
            iran_hour = (iran_hour + 1) % 24
            iran_minute -= 60
        return 12 <= iran_hour < 21 or (iran_hour == 21 and iran_minute <= 30)
    return True


# ==================== توابع دیتابیسی ====================
def save_price(symbol: str, price: float):
    db.execute_query(
        "INSERT INTO prices (symbol, timestamp, price) VALUES (?, ?, ?)",
        (symbol, datetime.now().isoformat(), price),
    )


def save_closing_price(symbol: str, price: float):
    db.execute_query(
        "INSERT OR REPLACE INTO closing_prices (symbol, timestamp, price) VALUES (?, ?, ?)",
        (symbol, datetime.now().isoformat(), price),
    )


def get_closing_price(symbol: str) -> Optional[float]:
    row = db.fetch_one(
        "SELECT price FROM closing_prices WHERE symbol=? ORDER BY timestamp DESC LIMIT 1",
        (symbol,),
    )
    return row[0] if row else None


def get_last_price(symbol: str) -> Optional[float]:
    row = db.fetch_one(
        "SELECT price FROM prices WHERE symbol=? ORDER BY timestamp DESC LIMIT 1",
        (symbol,),
    )
    return row[0] if row else None


def get_price_24h_ago(symbol: str) -> Optional[float]:
    target_time = (datetime.now() - timedelta(hours=24)).isoformat()
    row = db.fetch_one(
        "SELECT price FROM prices WHERE symbol=? AND timestamp <= ? ORDER BY timestamp DESC LIMIT 1",
        (symbol, target_time),
    )
    return row[0] if row else None


def clean_old_prices(days: int = None):
    if days is None:
        days = CONFIG["PRICE_RETENTION_DAYS"]
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()
    db.execute_query("DELETE FROM prices WHERE timestamp < ?", (cutoff,))
    db.execute_query("DELETE FROM closing_prices WHERE timestamp < ?", (cutoff,))


def save_auto_send_status(user_id: int, status: bool):
    db.execute_query(
        "INSERT OR REPLACE INTO user_settings (user_id, auto_send) VALUES (?, ?)",
        (user_id, 1 if status else 0),
    )


def get_all_auto_send_users() -> List[int]:
    rows = db.fetch_all("SELECT user_id FROM user_settings WHERE auto_send = 1")
    return [row[0] for row in rows]


def get_user_selections(user_id: int) -> List[str]:
    rows = db.fetch_all("SELECT symbol FROM user_selections WHERE user_id=?", (user_id,))
    return [row[0] for row in rows]


def save_user_selection(user_id: int, symbol: str):
    db.execute_query(
        "INSERT OR IGNORE INTO user_selections (user_id, symbol) VALUES (?, ?)",
        (user_id, symbol),
    )


def remove_user_selection(user_id: int, symbol: str):
    db.execute_query(
        "DELETE FROM user_selections WHERE user_id=? AND symbol=?",
        (user_id, symbol),
    )


def clear_user_selections(user_id: int):
    db.execute_query("DELETE FROM user_selections WHERE user_id=?", (user_id,))


def select_all_symbols(user_id: int):
    db.execute_query("DELETE FROM user_selections WHERE user_id=?", (user_id,))
    for cat in CATEGORIES.values():
        for key, _, _ in cat["symbols"]:
            db.execute_query(
                "INSERT OR IGNORE INTO user_selections (user_id, symbol) VALUES (?, ?)",
                (user_id, key),
            )


# ==================== کش قیمت ====================
def refresh_price_cache():
    with price_cache["lock"]:
        now = time.time()
        if now - price_cache["last_update"] < CONFIG["INTERVAL"]:
            return
        logger.info(f"🔄 به‌روزرسانی کش قیمت‌ها در {datetime.now().isoformat()}")
        new_data = {}
        for symbol in get_all_symbols_list():
            new_price = fetch_price_from_source(symbol)
            old_24h = get_price_24h_ago(symbol)
            if new_price is not None:
                new_data[symbol] = {"new": new_price, "old_24h": old_24h}
                save_price(symbol, new_price)
                save_closing_price(symbol, new_price)
            else:
                closing_price = get_closing_price(symbol)
                if closing_price is not None:
                    new_data[symbol] = {"new": closing_price, "old_24h": old_24h}
                else:
                    last = get_last_price(symbol)
                    if last is not None:
                        new_data[symbol] = {"new": last, "old_24h": old_24h}
        price_cache["data"] = new_data
        price_cache["last_update"] = now
        clean_old_prices()


def get_cached_price_with_24h(symbol: str) -> Tuple[Optional[float], Optional[float]]:
    refresh_price_cache()
    with price_cache["lock"]:
        data = price_cache["data"].get(symbol)
        return (data.get("new"), data.get("old_24h")) if data else (None, None)


# ==================== ابزارهای نمایش ====================
def format_price(price: float, symbol_key: str) -> str:
    if price is None:
        return "⛔ در دسترس نیست"
    if symbol_key == "gram":
        return f"{price:,.4f}"
    if price < 0.001:
        return f"{price:.4e}"
    if price < 1:
        return f"{price:.6f}"
    return f"{price:,.2f}"


def format_change(change: Optional[float]) -> str:
    if change is None:
        return ""
    if abs(change) < 0.0001:
        return "➖ بدون تغییر"
    return f"📈 {change:+.2f}%" if change > 0 else f"📉 {change:+.2f}%"


def generate_price_message(selections: List[str]) -> str:
    lines = []
    for cat_key, cat in CATEGORIES.items():
        cat_selected = [s for s in cat["symbols"] if s[0] in selections]
        if not cat_selected:
            continue
        lines.append(f"{cat['emoji']} {cat['name']}:")
        for key, name, emoji in cat_selected:
            new_price, old_24h = get_cached_price_with_24h(key)
            if new_price is None:
                if not is_market_open(key):
                    last = get_last_price(key)
                    if last is not None:
                        lines.append(f"{emoji} {name} : {format_price(last, key)} 🔒 بازار بسته")
                    else:
                        lines.append(f"{emoji} {name} : 🔒 بازار بسته")
                else:
                    lines.append(f"{emoji} {name} : ⛔ در دسترس نیست")
                continue
            change = None
            if old_24h and old_24h > 0:
                change = ((new_price - old_24h) / old_24h) * 100
            lines.append(f"{emoji} {name} : {format_price(new_price, key)} {format_change(change)}")
        lines.append("")
    return "\n".join(lines) if lines else "هیچ نمادی انتخاب نشده است."


def get_all_symbols():
    return [s for cat in CATEGORIES.values() for s in cat["symbols"]]


def build_inline_keyboard(buttons: List[Dict]) -> InlineKeyboardMarkup:
    keyboard = []
    for row in buttons:
        keyboard.append([InlineKeyboardButton(row["text"], callback_data=row["callback"])])
    return InlineKeyboardMarkup(keyboard)


# ==================== توابع تلگرام ====================
async def send_message(chat_id: int, text: str, parse_mode: str = "Markdown", reply_markup=None):
    bot = Bot(token=CONFIG["TELEGRAM_TOKEN"])
    await bot.send_message(chat_id=chat_id, text=text, parse_mode=parse_mode, reply_markup=reply_markup)


async def show_main_menu(chat_id: int, user_id: int, query=None):
    text = "📊 **به ربات قیمت‌های لحظه‌ای خوش آمدید!**\n\nلطفاً یک دسته را انتخاب کنید:"
    buttons = [
        {"text": f"{cat['emoji']} {cat['name']}", "callback": f"cat_{cat_key}"}
        for cat_key, cat in CATEGORIES.items()
    ] + [
        {"text": "📊 نمایش همه", "callback": "show_all"},
        {"text": "📋 وضعیت دیتابیس", "callback": "status"},
        {"text": "⚙️ ویرایش نمادها", "callback": "show_all"},
    ]
    reply_markup = InlineKeyboardMarkup([[InlineKeyboardButton(b["text"], callback_data=b["callback"]) for b in buttons]])
    if query:
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=reply_markup)
    else:
        await send_message(chat_id, text, reply_markup=reply_markup)


async def show_category_symbols(chat_id: int, user_id: int, category_key: str, query=None):
    cat = CATEGORIES[category_key]
    selections = get_user_selections(user_id)
    selected_text = "\n".join([f"{emoji} {name}" for key, name, emoji in cat["symbols"] if key in selections]) or "هیچ نمادی انتخاب نشده است."
    text = f"📊 **{cat['emoji']} {cat['name']}**\n\n✅ روی هر نماد کلیک کنید تا انتخاب/لغو شود.\nبعد از انتخاب، روی **شروع ارسال** کلیک کنید.\n\n**انتخاب‌شده:**\n{selected_text}"

    keyboard = []
    for key, name, emoji in cat["symbols"]:
        checked = "✅ " if key in selections else ""
        keyboard.append([InlineKeyboardButton(f"{checked}{emoji} {name}", callback_data=f"toggle_{key}")])
    keyboard.extend([
        [InlineKeyboardButton("🔙 بازگشت به دسته‌ها", callback_data="back_categories")],
        [InlineKeyboardButton("📊 انتخاب همه", callback_data=f"select_all_cat_{category_key}")],
        [InlineKeyboardButton("🚀 شروع ارسال", callback_data="start_sending")],
        [InlineKeyboardButton("🛑 توقف ارسال", callback_data="stop_sending")],
        [InlineKeyboardButton("🗑️ پاک کردن همه انتخاب‌ها", callback_data="clear_all")],
    ])
    reply_markup = InlineKeyboardMarkup(keyboard)
    if query:
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=reply_markup)
    else:
        await send_message(chat_id, text, reply_markup=reply_markup)


async def show_all_symbols(chat_id: int, user_id: int, query=None):
    selections = get_user_selections(user_id)
    selected_text = "\n".join([f"{emoji} {name}" for key, name, emoji in get_all_symbols() if key in selections]) or "هیچ نمادی انتخاب نشده است."
    text = f"📊 **همه نمادها**\n\n✅ روی هر نماد کلیک کنید تا انتخاب/لغو شود.\nبعد از انتخاب، روی **شروع ارسال** کلیک کنید.\n\n**انتخاب‌شده:**\n{selected_text}"

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
    reply_markup = InlineKeyboardMarkup(keyboard)
    if query:
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=reply_markup)
    else:
        await send_message(chat_id, text, reply_markup=reply_markup)


async def start(update: Update, context):
    await show_main_menu(update.effective_chat.id, update.effective_user.id)


async def button_handler(update: Update, context):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    chat_id = query.message.chat.id
    data = query.data

    if data == "back_categories":
        await show_main_menu(chat_id, user_id, query)
    elif data == "status":
        report = "📊 **وضعیت دیتابیس**\n"
        for key, name, emoji in get_all_symbols():
            price = get_last_price(key)
            report += f"🔹 {name}: {price if price else 'ندارد'}\n"
        await query.edit_message_text(report, parse_mode="Markdown")
    elif data == "clear_all":
        clear_user_selections(user_id)
        await query.edit_message_text("🗑️ همه انتخاب‌ها پاک شد.")
        await show_main_menu(chat_id, user_id)
    elif data == "select_all":
        select_all_symbols(user_id)
        await query.edit_message_text("📊 همه نمادها انتخاب شدند.")
        await show_all_symbols(chat_id, user_id, query)
    elif data == "show_all":
        await show_all_symbols(chat_id, user_id, query)
    elif data.startswith("cat_"):
        await show_category_symbols(chat_id, user_id, data.replace("cat_", ""), query)
    elif data.startswith("select_all_cat_"):
        cat_key = data.replace("select_all_cat_", "")
        for key, _, _ in CATEGORIES[cat_key]["symbols"]:
            save_user_selection(user_id, key)
        await query.edit_message_text(f"📊 همه نمادهای {CATEGORIES[cat_key]['name']} انتخاب شدند.")
        await show_category_symbols(chat_id, user_id, cat_key, query)
    elif data == "start_sending":
        selections = get_user_selections(user_id)
        if not selections:
            await query.edit_message_text("⚠️ حداقل یک نماد انتخاب کنید.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 بازگشت", callback_data="back_categories")]]))
            return
        with sending_lock:
            sending_active[user_id] = True
            last_sent_summary[user_id] = ""
        save_auto_send_status(user_id, True)
        await query.edit_message_text("🚀 **ارسال خودکار شروع شد!**\nهر ۱ دقیقه قیمت‌های انتخاب‌شده ارسال می‌شود.", parse_mode="Markdown")
    elif data == "stop_sending":
        with sending_lock:
            sending_active[user_id] = False
        save_auto_send_status(user_id, False)
        await query.edit_message_text("🛑 **ارسال خودکار متوقف شد.**", parse_mode="Markdown")
    elif data.startswith("toggle_"):
        symbol = data.replace("toggle_", "")
        selections = get_user_selections(user_id)
        if symbol in selections:
            remove_user_selection(user_id, symbol)
        else:
            save_user_selection(user_id, symbol)
        await show_all_symbols(chat_id, user_id, query)


async def status_single(update: Update, symbol_key: str, name: str, emoji: str):
    chat_id = update.effective_chat.id
    new_price, old_24h = get_cached_price_with_24h(symbol_key)
    if new_price is None:
        last = get_last_price(symbol_key)
        if last is not None:
            formatted = format_price(last, symbol_key)
            if not is_market_open(symbol_key):
                await send_message(chat_id, f"{emoji} **{name}**\n💰 {formatted}\n🔒 بازار بسته")
            else:
                await send_message(chat_id, f"{emoji} **{name}**\n💰 {formatted}")
        else:
            if not is_market_open(symbol_key):
                await send_message(chat_id, f"{emoji} **{name}**\n🔒 بازار بسته")
            else:
                await send_message(chat_id, f"{emoji} {name}: ⛔ در دسترس نیست.")
        return
    formatted = format_price(new_price, symbol_key)
    change = None
    if old_24h and old_24h > 0:
        change = ((new_price - old_24h) / old_24h) * 100
    await send_message(chat_id, f"{emoji} **{name}**\n💰 {formatted} {format_change(change)}")


# تعریف دستورات (با استفاده از دیکشنری)
COMMANDS = {
    "gold": ("gold", "GOLD", "🏆"),
    "silver": ("silver", "SILVER", "🥈"),
    "btc": ("btc", "BTC", "₿"),
    "eth": ("eth", "ETH", "💎"),
    "bnb": ("bnb", "BNB", "🟡"),
    "gram": ("gram", "GRAM", "🔷"),
    "xrp": ("xrp", "XRP", "💠"),
    "sol": ("sol", "SOL", "☀️"),
    "doge": ("doge", "DOGE", "🐕"),
    "bch": ("bch", "BCH", "🔶"),
    "ltc": ("ltc", "LTC", "⚡"),
    "trx": ("trx", "TRX", "🔴"),
    "oil": ("oil", "OIL", "🛢️"),
    "brent": ("brent", "BRENT", "🛢️"),
    "gas": ("gas", "GAS", "🔥"),
    "sugar": ("sugar", "SUGAR", "🍬"),
}


async def all_status(update: Update, context):
    selections = get_user_selections(update.effective_user.id)
    if not selections:
        await send_message(update.effective_chat.id, "⚠️ هیچ نمادی انتخاب نشده است. لطفاً ابتدا نمادهای مورد نظر را انتخاب کنید.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 بازگشت به منو", callback_data="back_categories")]]))
        return
    await send_message(update.effective_chat.id, f"📊 **قیمت‌های لحظه‌ای**\n━━━━━━━━━━━━━━━━━━━\n{generate_price_message(selections)}")


async def help_command(update: Update, context):
    await send_message(update.effective_chat.id,
        "📋 **دستورات:**\n/start - منوی اصلی\n/all - نمایش قیمت‌های انتخاب‌شده\n/status - وضعیت دیتابیس")


# ==================== حلقه خودکار ====================
async def auto_send_loop():
    bot = Bot(token=CONFIG["TELEGRAM_TOKEN"])
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
                    save_auto_send_status(user_id, False)
                    continue
                message = generate_price_message(selections)
                if message:
                    with sending_lock:
                        last_msg = last_sent_summary.get(user_id, "")
                    if message != last_msg:
                        reply_markup = InlineKeyboardMarkup([[InlineKeyboardButton("⚙️ ویرایش نمادها", callback_data="show_all")]])
                        try:
                            await bot.send_message(
                                user_id,
                                f"🔔 **به‌روزرسانی قیمت‌ها**\n━━━━━━━━━━━━━━━━━━━\n{message}",
                                parse_mode="Markdown",
                                reply_markup=reply_markup,
                            )
                            with sending_lock:
                                last_sent_summary[user_id] = message
                        except Forbidden:
                            logger.warning(f"🚫 کاربر {user_id} ربات را بلاک/حذف کرده است.")
                            with sending_lock:
                                sending_active[user_id] = False
                            save_auto_send_status(user_id, False)
                            clear_user_selections(user_id)
                        except Exception as e:
                            logger.error(f"⚠️ خطا در ارسال به {user_id}: {e}")
            if time.time() - last_cleanup > 600:
                clean_inactive_users()
                last_cleanup = time.time()
            await asyncio.sleep(CONFIG["INTERVAL"])
        except Exception as e:
            logger.error(f"⚠️ خطا در حلقه خودکار: {e}")
            await asyncio.sleep(CONFIG["INTERVAL"])


def clean_inactive_users():
    with sending_lock:
        inactive_users = [uid for uid, active in sending_active.items() if not active]
        for uid in inactive_users:
            del sending_active[uid]
            if uid in last_sent_summary:
                del last_sent_summary[uid]
            save_auto_send_status(uid, False)
        if inactive_users:
            logger.info(f"🧹 {len(inactive_users)} کاربر غیرفعال پاک شدند.")


def start_auto_send():
    asyncio.run(auto_send_loop())


# ==================== راه‌اندازی ربات ====================
def run_bot_in_main_thread():
    # بازیابی کاربران فعال
    try:
        active_users = get_all_auto_send_users()
        for user_id in active_users:
            sending_active[user_id] = True
            last_sent_summary[user_id] = ""
        if active_users:
            logger.info(f"🔄 {len(active_users)} کاربر با ارسال خودکار فعال بازیابی شدند.")
    except Exception as e:
        logger.error(f"⚠️ خطا در بازیابی وضعیت کاربران: {e}")

    # ارسال پیام آپدیت به مدیر
    try:
        bot = Bot(token=CONFIG["TELEGRAM_TOKEN"])
        asyncio.run(
            bot.send_message(
                chat_id=CONFIG["ADMIN_CHAT_ID"],
                text="✅ **آپدیت ربات با موفقیت انجام شد!**\n"
                     f"🔄 {len(get_all_auto_send_users())} کاربر با ارسال خودکار فعال بازیابی شدند.\n"
                     "ربات دوباره راه‌اندازی شد و آماده‌ی کار است.",
                parse_mode="Markdown",
            )
        )
        logger.info(f"📨 پیام آپدیت به مدیر ({CONFIG['ADMIN_CHAT_ID']}) ارسال شد.")
    except Exception as e:
        logger.error(f"⚠️ خطا در ارسال پیام آپدیت: {e}")

    # اجرای ربات
    app = Application.builder().token(CONFIG["TELEGRAM_TOKEN"]).connect_timeout(CONFIG["TIMEOUT"]).read_timeout(CONFIG["TIMEOUT"]).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("all", all_status))
    app.add_handler(CommandHandler("status", status_single_helper))

    for cmd, (key, name, emoji) in COMMANDS.items():
        async def handler(update, context, key=key, name=name, emoji=emoji):
            await status_single(update, key, name, emoji)
        app.add_handler(CommandHandler(cmd, handler))

    app.add_handler(CallbackQueryHandler(button_handler))
    logger.info("🤖 ربات در حال اجرا...")
    app.run_polling()


async def status_single_helper(update: Update, context):
    # برای دستور /status که وضعیت کامل را نشان می‌دهد
    await all_status(update, context)


# ==================== وب سرویس Flask ====================
flask_app = Flask(__name__)


@flask_app.route("/")
def home():
    return "✅ ربات در حال اجراست!"


@flask_app.route("/health")
def health():
    return "OK", 200


def run_flask():
    port = int(os.environ.get("PORT", 10000))
    flask_app.run(host="0.0.0.0", port=port)


# ==================== اجرای اصلی ====================
if __name__ == "__main__":
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()

    auto_thread = threading.Thread(target=start_auto_send, daemon=True)
    auto_thread.start()

    run_bot_in_main_thread()
