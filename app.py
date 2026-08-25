import os
os.environ["TZ"] = "UTC"

import asyncio
import time
import sqlite3
import threading
import logging

from datetime import datetime
from curl_cffi import requests as cffi_requests
from flask import Flask

from telegram import Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    CallbackQueryHandler,
)

# ============================================================
# CONFIG
# ============================================================

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
if not TELEGRAM_TOKEN:
    raise ValueError("TELEGRAM_TOKEN environment variable not set!")

CHAT_ID = os.environ.get("CHAT_ID", "483833953")
INTERVAL = 60
HTTP_TIMEOUT = 10
TELEGRAM_TIMEOUT = 30
DB_PATH = "market_data.db"
NOBITEX_STATS_URL = "https://api.nobitex.ir/market/stats"

logging.basicConfig(
    level=logging.ERROR,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

# ============================================================
# CATEGORIES
# ============================================================

CATEGORIES = {
    "fiat": {
        "name": "واحد پولی(تومان)",
        "emoji": "💳",
        "symbols": [
            ("usdt", "USDT", "💵"),
            ("aed", "AED", "🇦🇪"),
        ],
    },
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

# ============================================================
# CENTRAL PRICE CACHE
# ============================================================

price_cache = {}
price_cache_lock = threading.Lock()

def set_cached_price(symbol, price):
    if price is None:
        return
    with price_cache_lock:
        price_cache[symbol] = {
            "price": price,
            "timestamp": time.time(),
        }

def get_cached_price(symbol):
    with price_cache_lock:
        item = price_cache.get(symbol)
        if not item:
            return None
        return item["price"]

def get_cached_timestamp(symbol):
    with price_cache_lock:
        item = price_cache.get(symbol)
        if not item:
            return None
        return item["timestamp"]

# ============================================================
# NOBITEX - USDT
# ============================================================

def fetch_usdt_from_nobitex():
    try:
        response = cffi_requests.get(
            NOBITEX_STATS_URL,
            params={
                "srcCurrency": "usdt",
                "dstCurrency": "rls",
            },
            impersonate="chrome120",
            timeout=HTTP_TIMEOUT,
        )
        if response.status_code != 200:
            print(f"⚠️ Nobitex HTTP {response.status_code}")
            return None

        data = response.json()
        stats = data.get("stats", {})
        market = stats.get("usdt-rls")  # دقیقاً همان کلید اصلی شما
        if not market:
            print("⚠️ Nobitex: usdt-rls not found")
            return None

        latest = market.get("latest")
        if latest is None:
            print("⚠️ Nobitex: latest not found")
            return None

        price_irr = float(latest)
        if price_irr <= 0:
            return None

        price_toman = price_irr / 10
        return int(round(price_toman))

    except Exception as e:
        print(f"⚠️ Nobitex USDT error: {e}")
        return None

# ============================================================
# YAHOO FINANCE
# ============================================================

def fetch_yahoo(symbol):
    url = (
        "https://query1.finance.yahoo.com/v8/finance/chart/"
        f"{symbol}?interval=1h&range=1d"
    )
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 "
            "(KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        )
    }

    try:
        response = cffi_requests.get(
            url,
            headers=headers,
            impersonate="chrome120",
            timeout=HTTP_TIMEOUT,
        )
        if response.status_code != 200:
            print(f"⚠️ Yahoo {symbol}: HTTP {response.status_code}")
            return None

        data = response.json()
        chart = data.get("chart", {})
        results = chart.get("result")
        if not results:
            return None

        result = results[0]
        meta = result.get("meta", {})
        regular_price = meta.get("regularMarketPrice")
        if regular_price is not None:
            return float(regular_price)

        indicators = result.get("indicators", {})
        quotes = indicators.get("quote", [])
        if quotes:
            closes = quotes[0].get("close", [])
            for price in reversed(closes):
                if price is not None:
                    return float(price)

        return None

    except Exception as e:
        print(f"⚠️ Yahoo {symbol} error: {e}")
        return None

# ============================================================
# PRICE SOURCES
# ============================================================

YAHOO_SYMBOLS = {
    "btc": "BTC-USD",
    "eth": "ETH-USD",
    "bnb": "BNB-USD",
    "sol": "SOL-USD",
    "ltc": "LTC-USD",
    "bch": "BCH-USD",
    "xrp": "XRP-USD",
    "trx": "TRX-USD",
    "doge": "DOGE-USD",
    "gram": "GRAM-USD",
    "gold": "GC=F",
    "silver": "SI=F",
    "oil": "CL=F",
    "brent": "BZ=F",
    "gas": "NG=F",
    "sugar": "SB=F",
}

# ============================================================
# FETCH ALL PRICES
# ============================================================

def collect_all_prices():
    print("🔄 شروع دریافت قیمت‌ها...")

    usdt_price = fetch_usdt_from_nobitex()
    if usdt_price is not None:
        set_cached_price("usdt", usdt_price)
        save_price("usdt", usdt_price)
        print(f"💵 USDT: {usdt_price:,} تومان")
    else:
        print("⚠️ USDT آپدیت نشد؛ آخرین مقدار Cache حفظ شد.")

    if usdt_price is not None:
        aed_price = int(usdt_price / 3.67)
        set_cached_price("aed", aed_price)
        save_price("aed", aed_price)
        print(f"🇦🇪 AED: {aed_price:,} تومان")

    for symbol_key, yahoo_symbol in YAHOO_SYMBOLS.items():
        price = fetch_yahoo(yahoo_symbol)
        if price is None:
            print(f"⚠️ {symbol_key} آپدیت نشد.")
            continue
        if symbol_key == "sugar":
            price = round(price / 100, 4)
        set_cached_price(symbol_key, price)
        save_price(symbol_key, price)
        print(f"✅ {symbol_key}: {price}")

    print("✅ دریافت قیمت‌ها تمام شد.")

# ============================================================
# PRICE COLLECTOR THREAD
# ============================================================

def price_collector_loop():
    print("📡 Price Collector شروع شد.")
    while True:
        start_time = time.time()
        try:
            collect_all_prices()
        except Exception as e:
            print(f"❌ Price Collector error: {e}")
        elapsed = time.time() - start_time
        sleep_time = max(1, INTERVAL - elapsed)
        time.sleep(sleep_time)

# ============================================================
# MARKET STATUS
# ============================================================

def is_market_open(symbol_key):
    now = datetime.now()
    today = now.weekday()

    if symbol_key in [
        "btc", "eth", "bnb", "gram", "xrp", "sol", "doge", "bch", "ltc", "trx", "dot", "usdt", "aed"
    ]:
        return True

    if today == 6:
        return False

    if symbol_key == "sugar":
        iran_hour = (now.hour + 3) % 24
        iran_minute = now.minute + 30
        if iran_minute >= 60:
            iran_hour = (iran_hour + 1) % 24
            iran_minute -= 60
        if (
            iran_hour >= 12
            and (iran_hour < 21 or (iran_hour == 21 and iran_minute <= 30))
        ):
            return True
        return False

    return True

# ============================================================
# DATABASE
# ============================================================

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS prices
        (
            symbol TEXT,
            timestamp TEXT,
            price REAL,
            PRIMARY KEY (symbol, timestamp)
        )
        """
    )
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS history
        (
            symbol TEXT,
            timestamp TEXT,
            price REAL
        )
        """
    )
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS user_selections
        (
            user_id INTEGER,
            symbol TEXT,
            PRIMARY KEY (user_id, symbol)
        )
        """
    )
    conn.commit()
    conn.close()

def save_price(symbol, price):
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        timestamp = datetime.now().isoformat()
        c.execute(
            "INSERT INTO prices (symbol, timestamp, price) VALUES (?, ?, ?)",
            (symbol, timestamp, price)
        )
        c.execute(
            "INSERT INTO history (symbol, timestamp, price) VALUES (?, ?, ?)",
            (symbol, timestamp, price)
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"⚠️ DB save error: {e}")

def get_last_price(symbol):
    try:
        cached = get_cached_price(symbol)
        if cached is not None:
            return cached

        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute(
            "SELECT price FROM prices WHERE symbol=? ORDER BY timestamp DESC LIMIT 1",
            (symbol,)
        )
        row = c.fetchone()
        conn.close()
        return row[0] if row else None

    except Exception as e:
        print(f"⚠️ DB read error: {e}")
        return None

# ============================================================
# USER SELECTIONS
# ============================================================

def get_user_selections(user_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT symbol FROM user_selections WHERE user_id=?", (user_id,))
    rows = c.fetchall()
    conn.close()
    return [row[0] for row in rows]

def save_user_selection(user_id, symbol):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "INSERT OR IGNORE INTO user_selections (user_id, symbol) VALUES (?, ?)",
        (user_id, symbol)
    )
    conn.commit()
    conn.close()

def remove_user_selection(user_id, symbol):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "DELETE FROM user_selections WHERE user_id=? AND symbol=?",
        (user_id, symbol)
    )
    conn.commit()
    conn.close()

def clear_user_selections(user_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM user_selections WHERE user_id=?", (user_id,))
    conn.commit()
    conn.close()

def select_all_symbols(user_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM user_selections WHERE user_id=?", (user_id,))
    for category in CATEGORIES.values():
        for key, _, _ in category["symbols"]:
            c.execute(
                "INSERT OR IGNORE INTO user_selections (user_id, symbol) VALUES (?, ?)",
                (user_id, key)
            )
    conn.commit()
    conn.close()

# ============================================================
# SENDING STATE
# ============================================================

sending_active = {}
last_sent_summary = {}

# ============================================================
# SYMBOL HELPERS
# ============================================================

def get_all_symbols():
    result = []
    for category in CATEGORIES.values():
        result.extend(category["symbols"])
    return result

# ============================================================
# TELEGRAM SEND
# ============================================================

async def send_message(chat_id, text, parse_mode="Markdown", reply_markup=None):
    bot = Bot(token=TELEGRAM_TOKEN)
    await bot.send_message(
        chat_id=chat_id,
        text=text,
        parse_mode=parse_mode,
        reply_markup=reply_markup
    )

# ============================================================
# FORMAT PRICE
# ============================================================

def format_price(price, symbol_key):
    if price is None:
        return "⛔ در دسترس نیست"
    if symbol_key in ["usdt", "aed"]:
        return f"{price:,.0f}"
    if symbol_key == "gram":
        return f"{price:,.4f}"
    if price < 0.001:
        return f"{price:.4e}"
    if price < 1:
        return f"{price:.6f}"
    return f"{price:,.2f}"

def format_change(change):
    if change is None:
        return ""
    if abs(change) < 0.001:
        return "➖ بدون تغییر"
    if change > 0:
        return f"📈 {change:+.2f}%"
    return f"📉 {change:+.2f}%"

# ============================================================
# PRICE MESSAGE
# ============================================================

def generate_price_message(selections):
    lines = []

    for cat_key, cat in CATEGORIES.items():
        selected_symbols = [s for s in cat["symbols"] if s[0] in selections]
        if not selected_symbols:
            continue

        lines.append(f"{cat['emoji']} {cat['name']}:")

        for key, name, emoji in selected_symbols:
            new_price = get_cached_price(key)
            if new_price is None:
                new_price = get_last_price(key)

            if new_price is None:
                lines.append(f"{emoji} {name} : ⛔ در دسترس نیست")
                continue

            formatted = format_price(new_price, key)
            old_price = get_previous_price(key)
            change = None
            if old_price is not None and old_price > 0:
                change = ((new_price - old_price) / old_price) * 100

            change_text = format_change(change)
            if change_text:
                lines.append(f"{emoji} {name} : {formatted} {change_text}")
            else:
                lines.append(f"{emoji} {name} : {formatted}")

        lines.append("")

    if not lines:
        return "هیچ نمادی انتخاب نشده است."

    return "\n".join(lines)

# ============================================================
# PREVIOUS PRICE
# ============================================================

def get_previous_price(symbol):
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute(
            "SELECT price FROM prices WHERE symbol=? ORDER BY timestamp DESC LIMIT 2",
            (symbol,)
        )
        rows = c.fetchall()
        conn.close()
        if len(rows) >= 2:
            return rows[1][0]
        return None
    except Exception:
        return None

# ============================================================
# MAIN MENU
# ============================================================

async def show_main_menu(chat_id, user_id):
    text = (
        "📊 **به ربات قیمت‌های لحظه‌ای خوش آمدید!**\n\n"
        "لطفاً یک دسته را انتخاب کنید:"
    )
    keyboard = []
    for cat_key, cat in CATEGORIES.items():
        keyboard.append([
            InlineKeyboardButton(
                f"{cat['emoji']} {cat['name']}",
                callback_data=f"cat_{cat_key}"
            )
        ])
    keyboard.append([
        InlineKeyboardButton("📊 نمایش همه", callback_data="show_all")
    ])
    keyboard.append([
        InlineKeyboardButton("📋 وضعیت دیتابیس", callback_data="status")
    ])

    await send_message(
        chat_id,
        text,
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

# ============================================================
# CATEGORY MENU
# ============================================================

async def show_category_symbols(chat_id, user_id, category_key):
    cat = CATEGORIES[category_key]
    selections = get_user_selections(user_id)

    text = (
        f"📊 **{cat['emoji']} {cat['name']}**\n\n"
        "✅ روی هر نماد کلیک کنید تا انتخاب/لغو شود.\n"
        "بعد از انتخاب، روی **شروع ارسال** کلیک کنید.\n\n"
        "**انتخاب‌شده:**\n"
    )

    selected = []
    for key, name, emoji in cat["symbols"]:
        if key in selections:
            selected.append(f"{emoji} {name}")

    text += "\n".join(selected) if selected else "هیچ نمادی انتخاب نشده است."

    keyboard = []
    for key, name, emoji in cat["symbols"]:
        checked = "✅ " if key in selections else ""
        keyboard.append([
            InlineKeyboardButton(
                f"{checked}{emoji} {name}",
                callback_data=f"toggle_{key}"
            )
        ])

    keyboard.extend([
        [
            InlineKeyboardButton(
                "🔙 بازگشت به دسته‌ها",
                callback_data="back_categories"
            )
        ],
        [
            InlineKeyboardButton(
                "📊 انتخاب همه",
                callback_data=f"select_all_cat_{category_key}"
            )
        ],
        [
            InlineKeyboardButton(
                "🚀 شروع ارسال",
                callback_data="start_sending"
            )
        ],
        [
            InlineKeyboardButton(
                "🛑 توقف ارسال",
                callback_data="stop_sending"
            )
        ],
        [
            InlineKeyboardButton(
                "🗑️ پاک کردن همه انتخاب‌ها",
                callback_data="clear_all"
            )
        ],
    ])

    await send_message(
        chat_id,
        text,
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

# ============================================================
# ALL SYMBOLS MENU
# ============================================================

async def show_all_symbols(chat_id, user_id):
    selections = get_user_selections(user_id)

    text = (
        "📊 **همه نمادها**\n\n"
        "✅ روی هر نماد کلیک کنید تا انتخاب/لغو شود.\n"
        "بعد از انتخاب، روی **شروع ارسال** کلیک کنید.\n\n"
        "**انتخاب‌شده:**\n"
    )

    selected = []
    for key, name, emoji in get_all_symbols():
        if key in selections:
            selected.append(f"{emoji} {name}")

    text += "\n".join(selected) if selected else "هیچ نمادی انتخاب نشده است."

    keyboard = []
    for key, name, emoji in get_all_symbols():
        checked = "✅ " if key in selections else ""
        keyboard.append([
            InlineKeyboardButton(
                f"{checked}{emoji} {name}",
                callback_data=f"toggle_{key}"
            )
        ])

    keyboard.extend([
        [
            InlineKeyboardButton(
                "🔙 بازگشت به دسته‌ها",
                callback_data="back_categories"
            )
        ],
        [
            InlineKeyboardButton(
                "📊 انتخاب همه",
                callback_data="select_all"
            )
        ],
        [
            InlineKeyboardButton(
                "🚀 شروع ارسال",
                callback_data="start_sending"
            )
        ],
        [
            InlineKeyboardButton(
                "🛑 توقف ارسال",
                callback_data="stop_sending"
            )
        ],
        [
            InlineKeyboardButton(
                "🗑️ پاک کردن همه انتخاب‌ها",
                callback_data="clear_all"
            )
        ],
    ])

    await send_message(
        chat_id,
        text,
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

# ============================================================
# START
# ============================================================

async def start(update, context):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    await show_main_menu(chat_id, user_id)

# ============================================================
# CALLBACK HANDLER
# ============================================================

async def button_handler(update, context):
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    chat_id = query.message.chat.id
    data = query.data

    if data == "back_categories":
        await show_main_menu(chat_id, user_id)
        return

    if data == "status":
        await status_db(chat_id)
        return

    if data == "clear_all":
        clear_user_selections(user_id)
        await query.edit_message_text("🗑️ همه انتخاب‌ها پاک شد.")
        await show_main_menu(chat_id, user_id)
        return

    if data == "select_all":
        select_all_symbols(user_id)
        await query.edit_message_text("📊 همه نمادها انتخاب شدند.")
        await show_all_symbols(chat_id, user_id)
        return

    if data == "show_all":
        await show_all_symbols(chat_id, user_id)
        return

    if data.startswith("cat_"):
        category_key = data[len("cat_"):]
        await show_category_symbols(chat_id, user_id, category_key)
        return

    if data.startswith("select_all_cat_"):
        category_key = data[len("select_all_cat_"):]
        cat = CATEGORIES[category_key]
        for key, _, _ in cat["symbols"]:
            save_user_selection(user_id, key)
        await query.edit_message_text(f"📊 همه نمادهای {cat['name']} انتخاب شدند.")
        await show_category_symbols(chat_id, user_id, category_key)
        return

    if data == "start_sending":
        selections = get_user_selections(user_id)
        if not selections:
            await query.edit_message_text(
                "⚠️ حداقل یک نماد انتخاب کنید.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔙 بازگشت", callback_data="back_categories")]
                ])
            )
            return

        sending_active[user_id] = True
        last_sent_summary[user_id] = ""
        await query.edit_message_text(
            "🚀 **ارسال خودکار شروع شد!**\nهر ۱ دقیقه قیمت‌ها به‌روزرسانی می‌شوند.",
            parse_mode="Markdown"
        )
        return

    if data == "stop_sending":
        sending_active[user_id] = False
        await query.edit_message_text(
            "🛑 **ارسال خودکار متوقف شد.**",
            parse_mode="Markdown"
        )
        return

    if data.startswith("toggle_"):
        symbol = data[len("toggle_"):]
        selections = get_user_selections(user_id)
        if symbol in selections:
            remove_user_selection(user_id, symbol)
        else:
            save_user_selection(user_id, symbol)
        await show_all_symbols(chat_id, user_id)
        return

# ============================================================
# SINGLE STATUS
# ============================================================

async def status_single(update, symbol_key, name, emoji):
    chat_id = update.effective_chat.id

    price = get_cached_price(symbol_key)
    if price is None:
        price = get_last_price(symbol_key)

    if price is None:
        await send_message(
            chat_id,
            f"{emoji} **{name}**\n⛔ قیمت در دسترس نیست.",
            parse_mode="Markdown"
        )
        return

    formatted = format_price(price, symbol_key)
    previous = get_previous_price(symbol_key)
    change = None
    if previous is not None and previous > 0:
        change = ((price - previous) / previous) * 100

    change_text = format_change(change)
    text = f"{emoji} **{name}**\n💰 {formatted}"
    if change_text:
        text += f"\n{change_text}"

    await send_message(chat_id, text, parse_mode="Markdown")

# ============================================================
# COMMANDS
# ============================================================

async def gold(update, context):
    await status_single(update, "gold", "GOLD", "🏆")

async def silver(update, context):
    await status_single(update, "silver", "SILVER", "🥈")

async def btc(update, context):
    await status_single(update, "btc", "BTC", "₿")

async def eth(update, context):
    await status_single(update, "eth", "ETH", "💎")

async def bnb(update, context):
    await status_single(update, "bnb", "BNB", "🟡")

async def gram(update, context):
    await status_single(update, "gram", "GRAM", "🔷")

async def xrp(update, context):
    await status_single(update, "xrp", "XRP", "💠")

async def sol(update, context):
    await status_single(update, "sol", "SOL", "☀️")

async def doge(update, context):
    await status_single(update, "doge", "DOGE", "🐕")

async def bch(update, context):
    await status_single(update, "bch", "BCH", "🔶")

async def ltc(update, context):
    await status_single(update, "ltc", "LTC", "⚡")

async def trx(update, context):
    await status_single(update, "trx", "TRX", "🔴")

async def usdt(update, context):
    await status_single(update, "usdt", "USDT", "💵")

async def aed(update, context):
    await status_single(update, "aed", "AED", "🇦🇪")

async def oil(update, context):
    await status_single(update, "oil", "OIL", "🛢️")

async def brent(update, context):
    await status_single(update, "brent", "BRENT", "🛢️")

async def gas(update, context):
    await status_single(update, "gas", "GAS", "🔥")

async def sugar(update, context):
    await status_single(update, "sugar", "SUGAR", "🍬")

# ============================================================
# ALL STATUS
# ============================================================

async def all_status(update, context):
    chat_id = update.effective_chat.id
    selections = get_user_selections(update.effective_user.id)

    if not selections:
        await send_message(
            chat_id,
            "⚠️ هیچ نمادی انتخاب نشده است.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 بازگشت به منو", callback_data="back_categories")]
            ])
        )
        return

    message = generate_price_message(selections)
    await send_message(
        chat_id,
        f"📊 **قیمت‌های لحظه‌ای**\n━━━━━━━━━━━━━━━━━━━\n{message}",
        parse_mode="Markdown"
    )

# ============================================================
# DATABASE STATUS
# ============================================================

async def status_cmd(update, context):
    await status_db(update.effective_chat.id)

async def status_db(chat_id):
    report = "📊 **وضعیت قیمت‌ها**\n\n"

    for key, name, emoji in get_all_symbols():
        price = get_cached_price(key)
        if price is None:
            price = get_last_price(key)
        if price is None:
            value = "ندارد"
        else:
            value = format_price(price, key)
        report += f"{emoji} {name}: {value}\n"

    usdt_timestamp = get_cached_timestamp("usdt")
    if usdt_timestamp:
        age = int(time.time() - usdt_timestamp)
        report += f"\n🕐 آخرین آپدیت USDT: {age} ثانیه قبل"

    await send_message(chat_id, report, parse_mode="Markdown")

# ============================================================
# HELP
# ============================================================

async def help_command(update, context):
    chat_id = update.effective_chat.id
    await send_message(
        chat_id,
        "📋 **دستورات:**\n\n"
        "/start - منوی اصلی\n"
        "/all - نمایش قیمت‌های انتخاب‌شده\n"
        "/status - وضعیت قیمت‌ها\n"
        "/usdt - قیمت تتر\n"
        "/aed - قیمت درهم\n"
        "/btc - قیمت بیت‌کوین\n"
        "/eth - قیمت اتریوم"
    )

# ============================================================
# AUTO SEND
# ============================================================

async def auto_send_loop():
    bot = Bot(token=TELEGRAM_TOKEN)

    while True:
        try:
            active_users = list(sending_active.keys())
            for user_id in active_users:
                if not sending_active.get(user_id, False):
                    continue

                selections = get_user_selections(user_id)
                if not selections:
                    sending_active[user_id] = False
                    continue

                message = generate_price_message(selections)
                if not message:
                    continue

                if message == last_sent_summary.get(user_id, ""):
                    continue

                try:
                    await bot.send_message(
                        chat_id=user_id,
                        text=f"🔔 **به‌روزرسانی قیمت‌ها**\n━━━━━━━━━━━━━━━━━━━\n{message}",
                        parse_mode="Markdown"
                    )
                    last_sent_summary[user_id] = message
                except Exception as e:
                    print(f"⚠️ Telegram error {user_id}: {e}")

            await asyncio.sleep(INTERVAL)

        except Exception as e:
            print(f"⚠️ Auto send error: {e}")
            await asyncio.sleep(INTERVAL)

def start_auto_send():
    asyncio.run(auto_send_loop())

# ============================================================
# TELEGRAM BOT
# ============================================================

def run_bot_in_main_thread():
    app = (
        Application
        .builder()
        .token(TELEGRAM_TOKEN)
        .connect_timeout(TELEGRAM_TIMEOUT)
        .read_timeout(TELEGRAM_TIMEOUT)
        .build()
    )

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
    app.add_handler(CommandHandler("usdt", usdt))
    app.add_handler(CommandHandler("aed", aed))
    app.add_handler(CommandHandler("oil", oil))
    app.add_handler(CommandHandler("brent", brent))
    app.add_handler(CommandHandler("gas", gas))
    app.add_handler(CommandHandler("sugar", sugar))
    app.add_handler(CommandHandler("all", all_status))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CallbackQueryHandler(button_handler))

    print("🤖 Telegram Bot شروع شد.")
    app.run_polling()

# ============================================================
# FLASK
# ============================================================

flask_app = Flask(__name__)

@flask_app.route("/")
def home():
    return "✅ ربات در حال اجراست!"

@flask_app.route("/health")
def health():
    return "OK"

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    flask_app.run(host="0.0.0.0", port=port)

# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":
    init_db()

    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()

    price_thread = threading.Thread(target=price_collector_loop, daemon=True)
    price_thread.start()

    auto_thread = threading.Thread(target=start_auto_send, daemon=True)
    auto_thread.start()

    run_bot_in_main_thread()
