import os, asyncio, time, sqlite3, requests, re, json, logging, threading
from datetime import datetime, timedelta
from curl_cffi import requests as cffi_requests
from flask import Flask
from telegram import Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler
from telegram.error import Forbidden

os.environ['TZ'] = 'UTC'
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN')
if not TELEGRAM_TOKEN: raise ValueError("TELEGRAM_TOKEN is not set!")
INTERVAL, TIMEOUT, DB_PATH = 60, 30, "market_data.db"
logging.basicConfig(level=logging.INFO)

CATEGORIES = {
    'crypto': {'name': 'ارزهای دیجیتال', 'emoji': '💰', 'symbols': [
        ('btc','BTC','₿'), ('eth','ETH','💎'), ('bnb','BNB','🟡'), ('sol','SOL','☀️'),
        ('ltc','LTC','⚡'), ('bch','BCH','🔶'), ('xrp','XRP','💠'), ('trx','TRX','🔴'),
        ('doge','DOGE','🐕'), ('gram','GRAM','🔷')]},
    'metals': {'name': 'فلزات گرانبها', 'emoji': '🏆', 'symbols': [
        ('gold','GOLD','🏆'), ('silver','SILVER','🥈')]},
    'energy': {'name': 'انرژی و نفت', 'emoji': '⛽', 'symbols': [
        ('oil','OIL','🛢️'), ('brent','BRENT','🛢️'), ('gas','GAS','🔥')]},
    'agriculture': {'name': 'کشاورزی', 'emoji': '🌾', 'symbols': [
        ('sugar','SUGAR','🍬')]}
}

# =============== کش‌ها و قفل‌ها ===============
price_cache = {'data': {}, 'last_update': 0, 'lock': threading.Lock()}
holiday_cache = {'date': '', 'is_holiday': False, 'lock': threading.Lock()}
sending_active, last_sent_summary, sending_lock = {}, {}, threading.Lock()
SYMBOLS = [s[0] for cat in CATEGORIES.values() for s in cat['symbols']]

# =============== دیتابیس ===============
def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute('CREATE TABLE IF NOT EXISTS prices (symbol TEXT, timestamp TEXT, price REAL, PRIMARY KEY (symbol, timestamp))')
        c.execute('CREATE TABLE IF NOT EXISTS closing_prices (symbol TEXT, timestamp TEXT, price REAL, PRIMARY KEY (symbol, timestamp))')
        c.execute('CREATE TABLE IF NOT EXISTS user_selections (user_id INTEGER, symbol TEXT, PRIMARY KEY (user_id, symbol))')
        c.execute('CREATE TABLE IF NOT EXISTS auto_send_settings (user_id INTEGER PRIMARY KEY, active INTEGER)')
        conn.commit()
    print("✅ DB initialized.")

def save_price(s, p):   conn = sqlite3.connect(DB_PATH); c=conn.cursor(); c.execute('INSERT INTO prices VALUES (?,?,?)', (s, datetime.now().isoformat(), p)); conn.commit(); conn.close()
def get_last_price(s):  conn=sqlite3.connect(DB_PATH); r=conn.cursor().execute('SELECT price FROM prices WHERE symbol=? ORDER BY timestamp DESC LIMIT 1',(s,)).fetchone(); conn.close(); return r[0] if r else None
def get_price_24h(s):   conn=sqlite3.connect(DB_PATH); r=conn.cursor().execute('SELECT price FROM prices WHERE symbol=? AND timestamp <= ? ORDER BY timestamp DESC LIMIT 1', (s, (datetime.now()-timedelta(hours=24)).isoformat())).fetchone(); conn.close(); return r[0] if r else None
def clean_old():        conn=sqlite3.connect(DB_PATH); c=conn.cursor(); c.execute('DELETE FROM prices WHERE timestamp < ?', ((datetime.now()-timedelta(days=30)).isoformat(),)); c.execute('DELETE FROM closing_prices WHERE timestamp < ?', ((datetime.now()-timedelta(days=30)).isoformat(),)); conn.commit(); conn.close()
def load_statuses():    conn=sqlite3.connect(DB_PATH); rows=conn.cursor().execute('SELECT user_id FROM auto_send_settings WHERE active=1').fetchall(); conn.close(); [sending_active.__setitem__(uid[0], True) for uid in rows]; print(f"📂 {len(rows)} active users loaded.")
def save_status(uid,a): conn=sqlite3.connect(DB_PATH); conn.cursor().execute('INSERT OR REPLACE INTO auto_send_settings VALUES (?,?)',(uid,1 if a else 0)); conn.commit(); conn.close()

# =============== دریافت قیمت با Retry و لاگ ===============
def fetch_yahoo_with_retry(sym, retries=3):
    for attempt in range(retries):
        try:
            print(f"🔄 Fetching {sym} (attempt {attempt+1}/{retries})...")
            resp = cffi_requests.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?interval=1h&range=1d",
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"},
                impersonate="chrome120", timeout=8)
            if resp.status_code != 200:
                print(f"⚠️ Yahoo returned {resp.status_code} for {sym}")
                continue
            data = resp.json()
            res = data['chart']['result'][0]
            if 'meta' in res and res['meta'].get('regularMarketPrice') is not None:
                price = float(res['meta']['regularMarketPrice'])
                print(f"✅ {sym}: {price}")
                return price
            for p in reversed(res['indicators']['quote'][0]['close']):
                if p is not None:
                    print(f"✅ {sym}: {p}")
                    return float(p)
            print(f"⚠️ No price found for {sym}")
        except Exception as e:
            print(f"❌ Error fetching {sym}: {e}")
            if attempt < retries - 1:
                print(f"⏳ Retrying in 2 seconds...")
                time.sleep(2)
    print(f"❌ Failed to fetch {sym} after {retries} attempts")
    return None

def fetch_yahoo(sym):
    return fetch_yahoo_with_retry(sym, retries=3)

def is_holiday():
    with holiday_cache['lock']:
        today = datetime.now().strftime('%Y-%m-%d')
        if holiday_cache['date'] == today: return holiday_cache['is_holiday']
        api_key = os.environ.get('CALENDARIFIC_API_KEY')
        if not api_key: holiday_cache['date'], holiday_cache['is_holiday'] = today, False; return False
        try:
            r = requests.get(f"https://calendarific.com/api/v2/holidays?api_key={api_key}&country=US&year={datetime.now().year}&day={today}", timeout=5)
            if r.status_code == 200 and r.json().get('meta', {}).get('code') == 200:
                h = r.json().get('response', {}).get('holidays', [])
                if h: print(f"📅 Holiday: {h[0].get('name')}"); holiday_cache['date'], holiday_cache['is_holiday'] = today, True; return True
        except Exception as e: print(f"⚠️ Holiday check error: {e}")
        holiday_cache['date'], holiday_cache['is_holiday'] = today, False; return False

def is_market_open(sym):
    if sym in ['btc','eth','bnb','gram','xrp','sol','doge','bch','ltc','trx','dot']: return True
    if datetime.now().weekday() in [5,6] or is_holiday(): return False
    if sym == 'sugar':
        h = (datetime.now().hour + 3) % 24; m = datetime.now().minute + 30
        if m >= 60: h, m = (h+1)%24, m-60
        return 12 <= h < 21 or (h == 21 and m <= 30)
    return True

def fetch_price(sym):
    if not is_market_open(sym):
        print(f"⏸️ Market closed for {sym}")
        return None
    mapping = {'gram':'GRAM-USD','btc':'BTC-USD','eth':'ETH-USD','bnb':'BNB-USD','xrp':'XRP-USD','sol':'SOL-USD','doge':'DOGE-USD','bch':'BCH-USD','ltc':'LTC-USD','trx':'TRX-USD','gold':'XAUT-USD','silver':'SI=F','oil':'CL=F','brent':'BZ=F','gas':'NG=F','sugar':'SB=F'}
    price = fetch_yahoo(mapping[sym])
    return round(price/100, 4) if sym == 'sugar' and price else price

# =============== کش قیمت‌ها با لاگ بیشتر ===============
def refresh_cache():
    with price_cache['lock']:
        now = time.time()
        if now - price_cache['last_update'] < INTERVAL:
            print(f"⏩ Cache is fresh (last update {price_cache['last_update']})")
            return
        print(f"🔄 Updating price cache at {datetime.now().isoformat()}")
        data = {}
        for sym in SYMBOLS:
            old = get_price_24h(sym)
            new = fetch_price(sym)
            if new is not None:
                data[sym] = {'new': new, 'old': old}
                save_price(sym, new)
                print(f"✅ Updated {sym}: {new}")
            else:
                last = get_last_price(sym)
                if last is not None:
                    data[sym] = {'new': last, 'old': old}
                    print(f"⚠️ Using last price for {sym}: {last}")
                else:
                    print(f"❌ No price available for {sym}")
        price_cache['data'], price_cache['last_update'] = data, time.time()
        clean_old()

def get_price(sym):
    refresh_cache()
    with price_cache['lock']:
        d = price_cache['data'].get(sym)
        return (d['new'], d['old']) if d else (None, None)

# =============== پیام‌ها و کیبورد ===============
def fmt_price(p, s): return "⛔ در دسترس نیست" if p is None else f"{p:,.4f}" if s=='gram' else f"{p:,.2f}" if p>=1 else f"{p:.6f}"
def fmt_change(c): return "" if c is None else "➖ بدون تغییر" if abs(c)<0.0001 else f"📈 {c:+.2f}%" if c>0 else f"📉 {c:+.2f}%"

def gen_msg(selections):
    lines = []
    for cat in CATEGORIES.values():
        selected = [s for s in cat['symbols'] if s[0] in selections]
        if not selected: continue
        lines.append(f"{cat['emoji']} {cat['name']}:")
        for sym, name, emoji in selected:
            new, old = get_price(sym)
            if new is None:
                if not is_market_open(sym):
                    last = get_last_price(sym)
                    lines.append(f"{emoji} {name} : {fmt_price(last, sym)} 🔒 بازار بسته" if last else f"{emoji} {name} : 🔒 بازار بسته")
                else: lines.append(f"{emoji} {name} : ⛔ در دسترس نیست")
                continue
            change = ((new-old)/old*100) if old and old>0 else None
            lines.append(f"{emoji} {name} : {fmt_price(new, sym)} {fmt_change(change)}")
        lines.append("")
    return "\n".join(lines) or "هیچ نمادی انتخاب نشده است."

def make_keyboard(selections, all_symbols):
    kb = [[InlineKeyboardButton(f"{'✅ ' if sym in selections else ''}{emoji} {name}", callback_data=f"toggle_{sym}")] for sym, name, emoji in all_symbols]
    kb.extend([[InlineKeyboardButton("🔙 بازگشت به دسته‌ها", callback_data="back_categories")],
               [InlineKeyboardButton("📊 انتخاب همه", callback_data="select_all")],
               [InlineKeyboardButton("🚀 شروع ارسال", callback_data="start_sending")],
               [InlineKeyboardButton("🛑 توقف ارسال", callback_data="stop_sending")],
               [InlineKeyboardButton("🗑️ پاک کردن همه", callback_data="clear_all")]])
    return InlineKeyboardMarkup(kb)

# =============== هندلرهای تلگرام ===============
def get_user_sels(uid): conn=sqlite3.connect(DB_PATH); r=conn.cursor().execute('SELECT symbol FROM user_selections WHERE user_id=?',(uid,)).fetchall(); conn.close(); return [x[0] for x in r]
def save_user_sels(uid, sym): conn=sqlite3.connect(DB_PATH); conn.cursor().execute('INSERT OR IGNORE INTO user_selections VALUES (?,?)',(uid,sym)); conn.commit(); conn.close()
def remove_user_sels(uid, sym): conn=sqlite3.connect(DB_PATH); conn.cursor().execute('DELETE FROM user_selections WHERE user_id=? AND symbol=?',(uid,sym)); conn.commit(); conn.close()
def clear_user_sels(uid): conn=sqlite3.connect(DB_PATH); conn.cursor().execute('DELETE FROM user_selections WHERE user_id=?',(uid,)); conn.commit(); conn.close()
def select_all(uid): conn=sqlite3.connect(DB_PATH); c=conn.cursor(); c.execute('DELETE FROM user_selections WHERE user_id=?',(uid,)); [c.execute('INSERT OR IGNORE INTO user_selections VALUES (?,?)',(uid,s[0])) for cat in CATEGORIES.values() for s in cat['symbols']]; conn.commit(); conn.close()

def get_all_symbols(): return [(s[0], s[1], s[2]) for cat in CATEGORIES.values() for s in cat['symbols']]

async def send(chat_id, text, reply_markup=None):
    await Bot(TELEGRAM_TOKEN).send_message(chat_id=chat_id, text=text, parse_mode='Markdown', reply_markup=reply_markup)

async def show_main(chat_id, uid, query=None):
    kb = [[InlineKeyboardButton(f"{cat['emoji']} {cat['name']}", callback_data=f"cat_{k}")] for k,cat in CATEGORIES.items()]
    kb.append([InlineKeyboardButton("📊 نمایش همه", callback_data="show_all")])
    kb.append([InlineKeyboardButton("📋 وضعیت دیتابیس", callback_data="status")])
    text = "📊 **به ربات قیمت‌های لحظه‌ای خوش آمدید!**\nلطفاً یک دسته را انتخاب کنید:"
    if query: await query.edit_message_text(text, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(kb))
    else: await send(chat_id, text, InlineKeyboardMarkup(kb))

async def show_cat(chat_id, uid, cat_key, query=None):
    cat = CATEGORIES[cat_key]; sels = get_user_sels(uid)
    text = f"📊 **{cat['emoji']} {cat['name']}**\n✅ روی هر نماد کلیک کنید.\nبعد از انتخاب روی **شروع ارسال** کلیک کنید.\n\n**انتخاب‌شده:**\n"
    selected = [f"{e} {n}" for k,n,e in cat['symbols'] if k in sels]
    text += "\n".join(selected) if selected else "هیچ نمادی انتخاب نشده است."
    kb = [[InlineKeyboardButton(f"{'✅ ' if k in sels else ''}{e} {n}", callback_data=f"toggle_{k}")] for k,n,e in cat['symbols']]
    kb.extend([[InlineKeyboardButton("🔙 بازگشت به دسته‌ها", callback_data="back_categories")],
               [InlineKeyboardButton("📊 انتخاب همه", callback_data=f"select_all_cat_{cat_key}")],
               [InlineKeyboardButton("🚀 شروع ارسال", callback_data="start_sending")],
               [InlineKeyboardButton("🛑 توقف ارسال", callback_data="stop_sending")],
               [InlineKeyboardButton("🗑️ پاک کردن همه انتخاب‌ها", callback_data="clear_all")]])
    if query: await query.edit_message_text(text, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(kb))
    else: await send(chat_id, text, InlineKeyboardMarkup(kb))

async def show_all(chat_id, uid, query=None):
    sels = get_user_sels(uid); all_syms = get_all_symbols()
    text = "📊 **همه نمادها**\n✅ روی هر نماد کلیک کنید.\nبعد از انتخاب روی **شروع ارسال** کلیک کنید.\n\n**انتخاب‌شده:**\n"
    selected = [f"{emoji} {name}" for _, name, emoji in all_syms if _ in sels]
    text += "\n".join(selected) if selected else "هیچ نمادی انتخاب نشده است."
    kb = make_keyboard(sels, all_syms)
    if query: await query.edit_message_text(text, parse_mode='Markdown', reply_markup=kb)
    else: await send(chat_id, text, kb)

async def start(update, ctx): await show_main(update.effective_chat.id, update.effective_user.id)

async def button(update, ctx):
    q = update.callback_query; await q.answer(); uid = q.from_user.id; cid = q.message.chat.id; data = q.data
    if data == "back_categories": await show_main(cid, uid, q); return
    if data == "status":
        report = "📊 **وضعیت دیتابیس**\n" + "\n".join([f"🔹 {n}: {get_last_price(k) or 'ندارد'}" for k,n,_ in get_all_symbols()])
        await q.edit_message_text(report, parse_mode='Markdown'); return
    if data == "clear_all": clear_user_sels(uid); await q.edit_message_text("🗑️ همه انتخاب‌ها پاک شد."); await show_main(cid, uid); return
    if data == "select_all": select_all(uid); await q.edit_message_text("📊 همه نمادها انتخاب شدند."); await show_all(cid, uid, q); return
    if data == "show_all": await show_all(cid, uid, q); return
    if data.startswith("cat_"): await show_cat(cid, uid, data[4:], q); return
    if data.startswith("select_all_cat_"):
        for k,_,_ in CATEGORIES[data[15:]].get('symbols', []): save_user_sels(uid, k)
        await q.edit_message_text(f"📊 همه نمادهای {CATEGORIES[data[15:]]['name']} انتخاب شدند."); await show_cat(cid, uid, data[15:], q); return
    if data == "start_sending":
        if not get_user_sels(uid): await q.edit_message_text("⚠️ حداقل یک نماد انتخاب کنید.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 بازگشت", callback_data="back_categories")]])); return
        with sending_lock: sending_active[uid], last_sent_summary[uid] = True, ""
        save_status(uid, True); await q.edit_message_text("🚀 **ارسال خودکار شروع شد!**\nهر ۱ دقیقه قیمت‌ها ارسال می‌شود.", parse_mode='Markdown'); return
    if data == "stop_sending":
        with sending_lock: sending_active[uid] = False
        save_status(uid, False); await q.edit_message_text("🛑 **ارسال خودکار متوقف شد.**", parse_mode='Markdown'); return
    if data.startswith("toggle_"):
        sym = data[7:]; sels = get_user_sels(uid)
        (save_user_sels if sym not in sels else remove_user_sels)(uid, sym)
        await show_all(cid, uid, q)

async def single(update, sym, name, emoji):
    cid = update.effective_chat.id; new, old = get_price(sym)
    if new is None:
        last = get_last_price(sym)
        if last is not None: await send(cid, f"{emoji} **{name}**\n💰 {fmt_price(last, sym)}" + (" 🔒 بازار بسته" if not is_market_open(sym) else ""))
        else: await send(cid, f"{emoji} **{name}**\n🔒 بازار بسته" if not is_market_open(sym) else f"{emoji} {name}: ⛔ در دسترس نیست.")
        return
    change = ((new-old)/old*100) if old and old>0 else None
    await send(cid, f"{emoji} **{name}**\n💰 {fmt_price(new, sym)}\n{fmt_change(change)}")

async def all_status(update, ctx):
    cid = update.effective_chat.id; sels = get_user_sels(update.effective_user.id)
    if not sels: await send(cid, "⚠️ هیچ نمادی انتخاب نشده است.", InlineKeyboardMarkup([[InlineKeyboardButton("🔙 بازگشت به منو", callback_data="back_categories")]])); return
    await send(cid, f"📊 **قیمت‌های لحظه‌ای**\n━━━━━━━━━━━━━━━━━━━\n{gen_msg(sels)}")

async def help_cmd(update, ctx): await send(update.effective_chat.id, "📋 **دستورات:**\n/start - منوی اصلی\n/all - نمایش قیمت‌ها\n/status - وضعیت دیتابیس")

# =============== حلقه ارسال خودکار ===============
async def auto_loop():
    bot = Bot(TELEGRAM_TOKEN); last_clean = time.time(); print("🔄 Auto-send loop started.")
    load_statuses()
    while True:
        try:
            print("⏳ New cycle...")
            refresh_cache()
            with sending_lock:
                for uid in list(sending_active.keys()):
                    if not sending_active[uid]: continue
                    sels = get_user_sels(uid)
                    if not sels: sending_active[uid] = False; save_status(uid, False); continue
                    msg = gen_msg(sels)
                    if msg and msg != last_sent_summary.get(uid, ""):
                        print(f"📤 Sending to {uid}...")
                        try:
                            await bot.send_message(uid, f"🔔 **به‌روزرسانی قیمت‌ها**\n━━━━━━━━━━━━━━━━━━━\n{msg}",
                                parse_mode='Markdown', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⚙️ ویرایش نمادها", callback_data="show_all")]]))
                            last_sent_summary[uid] = msg
                        except Forbidden:
                            print(f"🚫 User {uid} blocked bot."); sending_active[uid] = False; save_status(uid, False); clear_user_sels(uid)
                        except Exception as e: print(f"⚠️ Send error to {uid}: {e}")
            if time.time() - last_clean > 600: last_clean = time.time()
            await asyncio.sleep(INTERVAL)
        except Exception as e:
            print(f"❌ Loop error: {e}")
            await asyncio.sleep(INTERVAL)

def start_loop(): asyncio.run(auto_loop())

# =============== Flask Web Service ===============
flask_app = Flask(__name__)

@flask_app.route('/')
def home():
    return "✅ ربات در حال اجراست!", 200

@flask_app.route('/health')
def health():
    return "OK", 200

def run_flask():
    port = int(os.environ.get('PORT', 10000))
    flask_app.run(host='0.0.0.0', port=port)

# =============== راه‌اندازی ===============
async def run_bot():
    app = Application.builder().token(TELEGRAM_TOKEN).connect_timeout(TIMEOUT).read_timeout(TIMEOUT).build()
    for cmd in [('start',start), ('help',help_cmd), ('gold',lambda u,c: single(u,'gold','GOLD','🏆')), 
                ('silver',lambda u,c: single(u,'silver','SILVER','🥈')), ('btc',lambda u,c: single(u,'btc','BTC','₿')),
                ('eth',lambda u,c: single(u,'eth','ETH','💎')), ('bnb',lambda u,c: single(u,'bnb','BNB','🟡')),
                ('gram',lambda u,c: single(u,'gram','GRAM','🔷')), ('xrp',lambda u,c: single(u,'xrp','XRP','💠')),
                ('sol',lambda u,c: single(u,'sol','SOL','☀️')), ('doge',lambda u,c: single(u,'doge','DOGE','🐕')),
                ('bch',lambda u,c: single(u,'bch','BCH','🔶')), ('ltc',lambda u,c: single(u,'ltc','LTC','⚡')),
                ('trx',lambda u,c: single(u,'trx','TRX','🔴')), ('oil',lambda u,c: single(u,'oil','OIL','🛢️')),
                ('brent',lambda u,c: single(u,'brent','BRENT','🛢️')), ('gas',lambda u,c: single(u,'gas','GAS','🔥')),
                ('sugar',lambda u,c: single(u,'sugar','SUGAR','🍬')), ('all',all_status), ('status',lambda u,c: None)]:
        app.add_handler(CommandHandler(cmd[0], cmd[1]))
    app.add_handler(CallbackQueryHandler(button))
    await app.bot.delete_webhook(); print("✅ Webhook cleared.")
    await app.initialize(); await app.start(); await app.updater.start_polling()
    print("🤖 Bot running...")
    try:
        while True:
            await asyncio.sleep(60)
    except KeyboardInterrupt:
        print("🛑 Stopping...")
    finally:
        await app.stop(); await app.shutdown()

def main():
    init_db()
    threading.Thread(target=run_flask, daemon=True).start()
    threading.Thread(target=start_loop, daemon=True).start()
    asyncio.run(run_bot())

if __name__ == '__main__': main()
