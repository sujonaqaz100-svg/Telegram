# -*- coding: utf-8 -*-
"""
Vanilla Exchange Telegram Bot - Render Free Tier Ready
"""
import asyncio, logging, os, random, threading, time
from datetime import datetime, timedelta
from io import BytesIO

import aiohttp, psycopg2, qrcode, requests
from psycopg2.extras import RealDictCursor
from flask import Flask, request, Response
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters,
)

# ============================================================
#  ⚙️  CONFIG  —  নিচের মানগুলো Render-এ Environment Variable দিয়ে দিবেন
# ============================================================
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "")  # যেমন https://your-app.onrender.com
PORT = int(os.environ.get("PORT", 10000))
DATABASE_URL = os.environ.get("DATABASE_URL", "")

ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "YourAdmin")
CHECKER_USERNAME = os.environ.get("CHECKER_USERNAME", "YourChecker")
REFUND_USERNAME = os.environ.get("REFUND_USERNAME", "YourRefund")
SUPPORT_USERNAME = os.environ.get("SUPPORT_USERNAME", "Vanilacardprepaid")

PURCHASE_RATE = 0.36
CARDS_PER_PAGE = 10
DEPOSIT_MIN = 15
DEPOSIT_EXPIRE_MIN = 30

SOL_ADDRESSES = [
    "6h3TxBcLNTqth4Cy7ho2ZRUWEE913V2DGSMfVzbeuZkN",
    "G5EEUtKbPjgkrEEi1tmvDsYFK9FrTcmikF2RTEdm5ZL2",
    "2GKnf4AxXTo1KARKTQ8HwktbdhJRQr9RLvKMhzBHtELx",
    "HeVPqfDWg5KpXdYesqzu1B6p9SPPF2Pdw3WiHePPChTC",
]
LTC_ADDRESSES = [
    "ltc1q8u5y45yayhsezjzh9y02xh75h0ek0ja4xhfmqu",
    "ltc1qks038fn9asrksx6ufja8x2dhhz3f9fkrzkqxuj",
    "ltc1qgxv834ummxxzvee8q8pf03qj9lnltkflkr3zpn",
    "ltc1qv3yjnclkq6538snvp9yd0h5wdw2mnmjzdns40r",
    "ltc1q7mvj3syqnwjlf0sstxxzj3ee7ktuaxd3867247",
]
BTC_ADDRESSES = [
    "bc1qxy2kgdygjrsqtzq2n0yrf2493p83kkfjhx0wlh",
    "bc1qar0srrr7xfkvy5l643lydnw9re59gtzzwf5mdq",
]

FILTER_BINS = {
    "joker": ["461126", "533985"],
    "amex": ["377935", "375163", "373778"],
    "vanilla": ["411810", "409758", "520356", "525362", "484718", "545660"],
    "cardbalance": ["428313", "432465", "457824"],
    "walmart": ["485246"],
    "giftcardmall": ["451129", "403446", "435880", "511332"],
}

# ============================================================
#  🗄️  DATABASE (PostgreSQL)
# ============================================================
def db():
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)

def init_db():
    con = db(); cur = con.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id BIGINT PRIMARY KEY, username TEXT, full_name TEXT,
        balance DOUBLE PRECISION DEFAULT 0, total_deposit DOUBLE PRECISION DEFAULT 0,
        last_deposit TEXT, purchase_count INT DEFAULT 0, usd_spent DOUBLE PRECISION DEFAULT 0,
        last_cards TEXT, invited INT DEFAULT 0, referred_by BIGINT,
        ref_earnings DOUBLE PRECISION DEFAULT 0, vendor INT DEFAULT 0, relist INT DEFAULT 0,
        created_at TEXT
    );""")
    cur.execute("""
    CREATE TABLE IF NOT EXISTS cards (
        id SERIAL PRIMARY KEY, bin TEXT, last4 TEXT, full_data TEXT,
        balance DOUBLE PRECISION, card_type TEXT, sold INT DEFAULT 0,
        sold_at TEXT, purchased_by BIGINT
    );""")
    cur.execute("""
    CREATE TABLE IF NOT EXISTS deposits (
        id SERIAL PRIMARY KEY, user_id BIGINT, amount DOUBLE PRECISION, coin TEXT,
        address TEXT, charge_id TEXT, status TEXT DEFAULT 'pending',
        created_at TEXT, expires_at TEXT
    );""")
    con.commit(); con.close()

def get_or_create_user(uid, username, full_name, referred_by=None):
    con = db(); cur = con.cursor()
    cur.execute("SELECT * FROM users WHERE id=%s", (uid,))
    row = cur.fetchone()
    if not row:
        cur.execute("INSERT INTO users (id,username,full_name,referred_by,created_at) VALUES (%s,%s,%s,%s,%s)",
                    (uid, username, full_name, referred_by, datetime.utcnow().isoformat()))
        if referred_by:
            cur.execute("UPDATE users SET invited=invited+1 WHERE id=%s", (referred_by,))
        con.commit()
        cur.execute("SELECT * FROM users WHERE id=%s", (uid,)); row = cur.fetchone()
    con.close(); return dict(row)

def update_user(uid, **kwargs):
    if not kwargs: return
    cols = ", ".join(f"{k}=%s" for k in kwargs)
    con = db(); con.execute(f"UPDATE users SET {cols} WHERE id=%s", (*kwargs.values(), uid))
    con.commit(); con.close()

def get_cards(page=1, per_page=CARDS_PER_PAGE, bins=None, percent=None, max_balance=None):
    con = db(); cur = con.cursor(); offset = (page-1)*per_page
    q = "SELECT * FROM cards WHERE 1=1"; params = []
    if bins:
        q += f" AND bin IN ({','.join(['%s']*len(bins))})"; params.extend(bins)
    if max_balance:
        q += " AND balance <= %s"; params.append(max_balance)
    q += " ORDER BY id ASC LIMIT %s OFFSET %s"; params.extend([per_page, offset])
    cur.execute(q, params); rows = [dict(r) for r in cur.fetchall()]; con.close()
    if percent:
        keep = max(1, int(len(rows)*percent/100))
        rows = random.sample(rows, keep) if len(rows) >= keep else rows
    return rows

def get_cards_count(bins=None):
    con = db(); cur = con.cursor()
    if bins:
        cur.execute(f"SELECT COUNT(*) FROM cards WHERE bin IN ({','.join(['%s']*len(bins))})", bins)
    else:
        cur.execute("SELECT COUNT(*) FROM cards")
    n = cur.fetchone()["count"]; con.close(); return n

def get_card(card_id):
    con = db(); cur = con.cursor()
    cur.execute("SELECT * FROM cards WHERE id=%s", (card_id,)); row = cur.fetchone()
    con.close(); return dict(row) if row else None

def mark_card_sold(card_id, buyer_id=None):
    con = db(); con.execute("UPDATE cards SET sold=1, sold_at=%s, purchased_by=%s WHERE id=%s",
                            (datetime.utcnow().isoformat(), buyer_id, card_id))
    con.commit(); con.close()

def auto_out_of_stock():
    cutoff = (datetime.utcnow()-timedelta(hours=1)).isoformat()
    con = db(); con.execute("UPDATE cards SET sold=1 WHERE sold=0 AND sold_at IS NOT NULL AND sold_at < %s", (cutoff,))
    con.commit(); con.close()

def is_registered_card(card_id):
    return random.random() < 0.75

def seed_demo_cards():
    con = db(); cur = con.cursor()
    cur.execute("SELECT COUNT(*) FROM cards")
    if cur.fetchone()["count"] > 0: con.close(); return
    bins = ["411810","409758","520356","525362","484718","545660","428313","432465",
            "457824","485246","451129","403446","435880","511332","461126","533985",
            "377935","375163","373778"]
    for i in range(50):
        b = random.choice(bins); last4 = f"{random.randint(0,9999):04d}"
        bal = round(random.uniform(5,180),2)
        full = f"{b}{random.randint(100000000000,999999999999)}|{random.randint(1,12):02d}|{random.randint(25,30)}|{random.randint(100,999)}"
        cur.execute("INSERT INTO cards (bin,last4,full_data,balance,card_type) VALUES (%s,%s,%s,%s,%s)",
                    (b,last4,full,bal,"vanilla"))
    con.commit(); con.close()

# ============================================================
#  🛠️  HELPERS
# ============================================================
def utc_now_str(): return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")

async def get_live_rate(coin):
    ids = {"BTC":"bitcoin","LTC":"litecoin","SOL":"solana"}
    url = f"https://api.coingecko.com/api/v3/simple/price?ids={ids[coin]}&vs_currencies=usd"
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, timeout=10) as r:
                data = await r.json()
        return float(data[ids[coin]]["usd"])
    except Exception:
        return {"BTC":65000.0,"LTC":71.17,"SOL":150.0}.get(coin,1.0)

def make_qr(text):
    img = qrcode.make(text); bio = BytesIO(); img.save(bio, format="PNG"); bio.seek(0)
    return bio

def pick_address(coin):
    return random.choice({"BTC":BTC_ADDRESSES,"LTC":LTC_ADDRESSES,"SOL":SOL_ADDRESSES}[coin])

def build_main_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎫 Stock", callback_data="stock"),
         InlineKeyboardButton("📞 Contact Admin", url=f"https://t.me/{ADMIN_USERNAME}")],
        [InlineKeyboardButton("Card chaker🔎", url=f"https://t.me/{CHECKER_USERNAME}"),
         InlineKeyboardButton("Refund Support 📱", url=f"https://t.me/{REFUND_USERNAME}")],
    ])

# ============================================================
#  🚀  HANDLERS
# ============================================================
async def start(update, context):
    user = update.effective_user
    referred_by = None
    if context.args and context.args[0].startswith("ref_"):
        try:
            referred_by = int(context.args[0].replace("ref_",""))
            if referred_by == user.id: referred_by = None
        except ValueError: pass
    get_or_create_user(user.id, user.username or "", user.full_name or "", referred_by)
    text = ("⚡️Welcome Sujon to Vanila Exchange! ⚡️\n\n"
            "Sell, Buy, and strike deals in seconds!!\n"
            "All transactions are secure and transparent.\n"
            "All types of cards are available here at best rates. Current rate is 35%")
    await update.message.reply_text(text, reply_markup=build_main_menu())

async def stock(update, context):
    await update.callback_query.answer(); await render_listings(update, context, page=1)

async def listings_cmd(update, context): await render_listings(update, context, page=1)
async def cents_listing_cmd(update, context): await render_listings(update, context, page=1)

async def render_listings(update, context, page=1, bins=None, percent=None, max_balance=None, edit=True):
    cards = get_cards(page=page, bins=bins, percent=percent, max_balance=max_balance)
    total = get_cards_count(bins=bins)
    total_pages = max(1, (total+CARDS_PER_PAGE-1)//CARDS_PER_PAGE)
    keyboard = []
    for idx, card in enumerate(cards, start=(page-1)*CARDS_PER_PAGE+1):
        card_btn = InlineKeyboardButton(f"{idx}. {card['bin']}****{card['last4']} | ${card['balance']:.2f}", callback_data="noop")
        buy_btn = InlineKeyboardButton("⚠️OUT OF STOCK", callback_data="noop") if card["sold"] else InlineKeyboardButton("🛒Purchase", callback_data=f"buy_{card['id']}")
        keyboard.append([card_btn, buy_btn])
    keyboard.append([InlineKeyboardButton("💰Deposit", callback_data="open_deposit"),
                     InlineKeyboardButton("Refresh🔂", callback_data=f"page_{page}"),
                     InlineKeyboardButton("🔍Filters", callback_data="open_filters")])
    nav = []
    if page > 1: nav.append(InlineKeyboardButton("⬅️ Back", callback_data=f"page_{page-1}"))
    nav.append(InlineKeyboardButton(f"{page}/{total_pages}", callback_data="noop"))
    if page < total_pages: nav.append(InlineKeyboardButton("Next ➡️", callback_data=f"page_{page+1}"))
    if nav: keyboard.append(nav)
    text = f"📋 Card Listings — Page {page}\n📦 Total: {total} cards"
    markup = InlineKeyboardMarkup(keyboard)
    if update.callback_query and edit:
        try: await update.callback_query.edit_message_text(text, reply_markup=markup)
        except Exception: await update.callback_query.message.reply_text(text, reply_markup=markup)
    else:
        await update.message.reply_text(text, reply_markup=markup)

async def page_cb(update, context):
    q = update.callback_query; await q.answer("🔄 Refreshed")
    page = int(q.data.split("_")[1]); await render_listings(update, context, page=page)

async def noop(update, context): await update.callback_query.answer()

async def open_filters(update, context):
    q = update.callback_query; await q.answer()
    keyboard = [
        [InlineKeyboardButton("🔓Unregistered", callback_data="f_unregistered"), InlineKeyboardButton("🔐Registered", callback_data="f_registered")],
        [InlineKeyboardButton("⚪Vanilla", callback_data="f_vanilla"), InlineKeyboardButton("💠CardBalance", callback_data="f_cardbalance")],
        [InlineKeyboardButton("☀️Walmart", callback_data="f_walmart"), InlineKeyboardButton("🛍️ GiftCardMall", callback_data="f_giftcardmall")],
        [InlineKeyboardButton("🎭Joker", callback_data="f_joker"), InlineKeyboardButton("🟦AMEX", callback_data="f_amex")],
        [InlineKeyboardButton("🧹Clear Filters", callback_data="f_clear")],
    ]
    await q.edit_message_text("🔍 Filter Cards", reply_markup=InlineKeyboardMarkup(keyboard))

async def apply_filter(update, context):
    q = update.callback_query; key = q.data.replace("f_",""); await q.answer()
    if key == "clear": await render_listings(update, context, page=1); return
    if key == "unregistered": await render_listings(update, context, page=1, percent=20, max_balance=200); return
    if key == "registered": await render_listings(update, context, page=1, percent=75); return
    if key in FILTER_BINS: await render_listings(update, context, page=1, bins=FILTER_BINS[key]); return

async def buy_card(update, context):
    q = update.callback_query; card_id = int(q.data.split("_")[1]); card = get_card(card_id)
    if not card or card["sold"]: await q.answer("❌ Card no longer available", show_alert=True); return
    await q.answer()
    total_cost = round(card["balance"]*PURCHASE_RATE,2)
    reg_status = "Registration" if is_registered_card(card_id) else "Unregistered"
    text = (f"(n) - Listing Information\n\nCard: `{card['bin']}****{card['last4']}`\n"
            f"Balance: ${card['balance']:.2f}\nPurchase Rate: 36%\nTotal Cost: ${total_cost:.2f}\n"
            f"Registration Status: {reg_status}\nCard Status: Fresh\nClick Confirm to proceed with purchase")
    keyboard = [[InlineKeyboardButton("⛔ Cencel", callback_data="cancel_purchase"),
                 InlineKeyboardButton("✅ Confirm", callback_data=f"confirm_{card_id}")]]
    await q.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

async def cancel_purchase(update, context):
    q = update.callback_query; await q.answer(); await q.edit_message_text("⛔ Purchase cancelled.")

async def confirm_purchase(update, context):
    q = update.callback_query; card_id = int(q.data.split("_")[1]); await q.answer()
    card = get_card(card_id)
    if not card or card["sold"]: await q.edit_message_text("❌ Card sold out."); return
    user = get_or_create_user(q.from_user.id, q.from_user.username or "", q.from_user.full_name or "")
    total_cost = round(card["balance"]*PURCHASE_RATE,2)
    await q.edit_message_text("💰 Verifying your balance..."); await asyncio.sleep(2)
    if user["balance"] < total_cost:
        await q.edit_message_text(f"❌ Insufficient balance. Please deposit funds and try again.\nOR Please contact with Admin: @{ADMIN_USERNAME}"); return
    new_balance = user["balance"]-total_cost
    update_user(user["id"], balance=new_balance, purchase_count=user["purchase_count"]+1,
                usd_spent=user["usd_spent"]+total_cost, last_cards=f"{card['bin']}****{card['last4']}")
    mark_card_sold(card_id, buyer_id=user["id"])
    await q.edit_message_text(f"✅ Purchase successful!\n\nCard: `{card['full_data']}`\nCost: ${total_cost:.2f}\nNew Balance: ${new_balance:.4f}", parse_mode="Markdown")

async def profile_cmd(update, context):
    user = update.effective_user
    db_user = get_or_create_user(user.id, user.username or "", user.full_name or "")
    ref_link = f"https://t.me/{(await context.bot.get_me()).username}?start=ref_{user.id}"
    text = (f"Vanila Exchange PROFILE\n\n👤 {user.full_name}\n🧠 It is impossible to love and to be wise.\n💬 By: Francis Bacon\n\n"
            f"🆔 User ID: `{user.id}`\n🔹 Username: @{user.username or 'N/A'}\n💰 USD Balance: `{db_user['balance']:.10f}`\n\n"
            f"📥 Deposits\n• Total USD: {db_user['total_deposit']:.4f}\n• Last: {db_user['last_deposit'] or 'Never'}\n\n"
            f"🛒 Purchases\n• Count: {db_user['purchase_count']}\n• USD Spent: ${db_user['usd_spent']:.2f}\n"
            f"• Last Cards:\n  • {db_user['last_cards'] or 'No cards purchased yet.'}\n\n"
            f"👥 Referrals\n• Invited: {db_user['invited']}\n• Referred By: {db_user['referred_by'] or '—'}\n• Your link: `{ref_link}`\n\n"
            f"🛠 Permissions\n• Vendor: {'✅' if db_user['vendor'] else '❌'}\n• Re-list: {'✅' if db_user['relist'] else '❌'}\n\n"
            f"Last updated: {utc_now_str()}")
    keyboard = [[InlineKeyboardButton("🔗 Copy Referral Link", callback_data=f"copyref_{user.id}")]]
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

async def copy_ref(update, context):
    q = update.callback_query; uid = int(q.data.split("_")[1])
    link = f"https://t.me/{(await context.bot.get_me()).username}?start=ref_{uid}"
    await q.answer(f"🔗 {link}", show_alert=True)

async def balance_cmd(update, context):
    user = update.effective_user
    db_user = get_or_create_user(user.id, user.username or "", user.full_name or "")
    text = (f"Name : {user.full_name}\nID : `{user.id}`\nYour balance USD : `{db_user['balance']:.6f}`\n"
            f"Balance last Update : {utc_now_str()}")
    keyboard = [[InlineKeyboardButton("💰Deposit", callback_data="open_deposit")]]
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

async def withdraw_cmd(update, context):
    user = update.effective_user
    db_user = get_or_create_user(user.id, user.username or "", user.full_name or "")
    text = (f"💎 Account Details\n👤 Name: {user.full_name}\n🆔 ID: `{user.id}`\n💰 Balance: `{db_user['balance']:.5f}` USD")
    keyboard = [[InlineKeyboardButton("✅Confirm", callback_data="withdraw_confirm")]]
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

async def withdraw_confirm(update, context):
    q = update.callback_query; await q.answer(); await q.edit_message_text("⚙️Checking Balance.....")
    await asyncio.sleep(3); await q.edit_message_text("⚠️ Your balance is currently empty. Deposit to start withdrawing!")

async def deposit_cmd(update, context):
    text = ("🏦 Vanila Exchange Deposit\n―――――――――――――――――――\n🪙 Accepted coins\nLTC BTC SOL\n―――――――――――――――――――\n"
            "✅ Minimum $15\n🕐 Expires : 30 Minutes\n⚡ Confirms ~1–2 min\n―――――――――――――――――――\n"
            "⚠️ Send the exact amount to the correct address.\nWrong amount or address = loss of funds.\n👇 Select amount:")
    keyboard = [[InlineKeyboardButton("$30", callback_data="amt_30"), InlineKeyboardButton("$50", callback_data="amt_50")],
                [InlineKeyboardButton("$100", callback_data="amt_100"), InlineKeyboardButton("$500", callback_data="amt_500")],
                [InlineKeyboardButton("✏️ Custom Amount", callback_data="amt_custom")]]
    if update.callback_query:
        await update.callback_query.answer(); await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

async def amount_cb(update, context):
    q = update.callback_query; await q.answer(); data = q.data
    if data == "amt_custom":
        context.user_data["awaiting_custom"] = True
        await q.edit_message_text("✏️ Custom Deposit Amount\n\nEnter amount in USD (Minimum $15)\n\nThen choose your coin — address generates instantly."); return
    amount = float(data.replace("amt_","")); await show_coin_options(update, context, amount)

async def handle_custom_amount(update, context):
    if not context.user_data.get("awaiting_custom"): return
    txt = update.message.text.strip().replace("$","")
    try: amount = float(txt)
    except ValueError: return await update.message.reply_text("❌ Invalid amount. Enter a number (e.g. 25).")
    if amount < DEPOSIT_MIN: return await update.message.reply_text(f"❌ Minimum deposit is ${DEPOSIT_MIN}\nTry again with a larger amount.")
    context.user_data["awaiting_custom"] = False
    await show_coin_options(update, context, amount, from_message=True)

async def show_coin_options(update, context, amount, from_message=False):
    text = f"🏦 DEPOSIT ${amount:g}\n\n🪙 Choose payment coin:\n\n🪙 LTC — Litecoin\n🟣 SOL — Solana\n₿ BTC — Bitcoin"
    keyboard = [[InlineKeyboardButton("₿ BTC", callback_data=f"coin_BTC_{amount}"),
                 InlineKeyboardButton("🟣 SOL", callback_data=f"coin_SOL_{amount}"),
                 InlineKeyboardButton("🪙 LTC", callback_data=f"coin_LTC_{amount}")]]
    markup = InlineKeyboardMarkup(keyboard)
    if from_message: await update.message.reply_text(text, reply_markup=markup)
    else: await update.callback_query.edit_message_text(text, reply_markup=markup)

async def coin_selected(update, context):
    q = update.callback_query; await q.answer("⏳ Generating invoice...")
    _, coin, amount_s = q.data.split("_"); amount = float(amount_s)
    address = pick_address(coin); rate = await get_live_rate(coin)
    send_amount = round(amount/rate, 8); charge_id = q.from_user.id
    valid_till = (datetime.utcnow()+timedelta(minutes=DEPOSIT_EXPIRE_MIN)).strftime("%Y-%m-%d %H:%M:%S UTC")
    order_no = random.randint(726268,999999)
    caption = (f"Send **{coin}** to the address shown below:\n\n📸 Scan the QR code or copy the address to proceed with payment.\n\n"
               f"🏦 **{coin} Address**: `{address}`\n💎 Currency: {coin}\nDeposit Amount: **${amount:g}**\n"
               f"Rate used: 1 {coin} ≈ ${rate:,.2f}\n💸 Send Exactly: `{send_amount} {coin}`\n"
               f"⚠️ Only send {coin} assets to this address. Other assets will be lost forever.\n"
               f"Charge ID: `{charge_id}`\nValid till: {valid_till}\n\nMore details:\nPayment ID: `{charge_id}`\nOrder number: {order_no}\n\n"
               f"1. Make sure you deposit the exact value to get the funds.\n2. Do not deposit two times to this same address.\n"
               f"3. Deposit within {DEPOSIT_EXPIRE_MIN} Minutes.\n4. If you sent money, wait for confirmations.\n"
               f"5. Balance auto-credited within 2 minutes.\n\nAny issue, contact @{SUPPORT_USERNAME} with your charge ID.")
    keyboard = [[InlineKeyboardButton("✆Contract", url=f"https://t.me/{SUPPORT_USERNAME}")]]
    qr_bio = make_qr(address)
    try:
        msg = await q.edit_message_media(media=InputMediaPhoto(qr_bio, caption=caption, parse_mode="Markdown"),
                                          reply_markup=InlineKeyboardMarkup(keyboard))
    except Exception:
        msg = await q.message.reply_photo(photo=qr_bio, caption=caption, parse_mode="Markdown",
                                           reply_markup=InlineKeyboardMarkup(keyboard))
    con = db(); con.execute("INSERT INTO deposits (user_id,amount,coin,address,charge_id,created_at,expires_at) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                            (charge_id, amount, coin, address, str(charge_id), datetime.utcnow().isoformat(),
                             (datetime.utcnow()+timedelta(minutes=DEPOSIT_EXPIRE_MIN)).isoformat()))
    con.commit(); con.close()
    context.job_queue.run_once(expire_deposit_job, when=(DEPOSIT_EXPIRE_MIN+1)*60,
                               data={"chat_id": q.from_user.id, "msg_id": msg.message_id, "coin": coin})

async def expire_deposit_job(context):
    d = context.job.data
    try: await context.bot.delete_message(d["chat_id"], d["msg_id"])
    except Exception: pass
    await context.bot.send_message(d["chat_id"], f"⚠️ Your {d['coin']} deposit session has expired! ⌛\nPlease send /deposit to continue. ✨")

async def ref_cmd(update, context):
    user = update.effective_user
    db_user = get_or_create_user(user.id, user.username or "", user.full_name or "")
    link = f"https://t.me/{(await context.bot.get_me()).username}?start=ref_{user.id}"
    text = ("🎉 EXCLUSIVE REFERRAL PROGRAM\n\nShare the excitement with your friends! For every deposit your active "
            "referrals make, you will automatically earn a 5% commission.\n\n"
            f"🔗 Your unique link: `{link}`\n\n📊 Your Stats\n• Total Referrals: {db_user['invited']}\n"
            f"• Earnings: ${db_user['ref_earnings']:.2f}\n\n📌 Program Rules\n"
            "Earn on First Activity: Your bonus is unlocked after your referral makes their first transaction.\n"
            "Original Accounts Only: Self-referrals are not eligible for rewards.\n"
            "Account Integrity: Any fake or fraudulent referrals will lead to a permanent ban.")
    await update.message.reply_text(text, parse_mode="Markdown")

async def help_cmd(update, context):
    await update.message.reply_text(f"If you need help, please contact @{ADMIN_USERNAME}")

async def refund_rules_cmd(update, context):
    text = ("⚡️⚡️⚡️ VERY IMPORTANT ⚡️⚡️⚡️\n💳 Vanila Exchange – Refund Policy 💳\n\n"
            "✅✅✅ CARD REFUND REQUIREMENTS ✅✅✅\n1️⃣ Refund requests must be submitted within 30 minutes of purchase.\n"
            "2️⃣ Refunds are accepted ONLY if the card is stolen or partially used.\n3️⃣ You must have a valid Telegram username set.\n\n"
            f"💬 Official Refund Support: @{REFUND_USERNAME}\n\n❌❌❌ AUTOMATIC REFUND REJECTIONS ❌❌❌\n"
            "🚫 No refund for ReListed cards\n🚫 No refund for cards used with Google / Google Pay\n"
            "🚫 No Telegram username = Auto rejection\n\n⚠️ IMPORTANT NOTICE: All cards are checked immediately before delivery\n"
            f"📩 Need help? Contact support: @{ADMIN_USERNAME}")
    keyboard = [[InlineKeyboardButton("Back⬅️", callback_data="stock")]]
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

async def auto_oos_job(context):
    try: auto_out_of_stock()
    except Exception as e: logging.warning(f"auto_oos error: {e}")

# ============================================================
#  🌐  FLASK WEBHOOK SERVER
# ============================================================
flask_app = Flask(__name__)
ptb_app = None

@flask_app.route(f"/{BOT_TOKEN}", methods=["POST"])
def webhook():
    if ptb_app is None: return Response("Bot not ready", status=503)
    update = Update.de_json(request.get_json(force=True), ptb_app.bot)
    ptb_app.update_queue.put_nowait(update)
    return Response("OK", status=200)

@flask_app.route("/", methods=["GET"])
@flask_app.route("/health", methods=["GET"])
def health(): return "✅ Vanilla Exchange Bot is running", 200

def keep_alive():
    while True:
        time.sleep(300)
        try: requests.get(WEBHOOK_URL, timeout=10)
        except Exception: pass

async def set_webhook():
    await ptb_app.bot.set_webhook(url=f"{WEBHOOK_URL}/{BOT_TOKEN}", drop_pending_updates=True)

# ============================================================
#  🎬  MAIN
# ============================================================
def main():
    global ptb_app
    logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
    init_db(); seed_demo_cards()
    ptb_app = ApplicationBuilder().token(BOT_TOKEN).build()

    ptb_app.add_handler(CommandHandler("start", start))
    ptb_app.add_handler(CommandHandler("listings", listings_cmd))
    ptb_app.add_handler(CommandHandler("cents_listing", cents_listing_cmd))
    ptb_app.add_handler(CommandHandler("profile", profile_cmd))
    ptb_app.add_handler(CommandHandler("balance", balance_cmd))
    ptb_app.add_handler(CommandHandler("withdraw", withdraw_cmd))
    ptb_app.add_handler(CommandHandler("deposit", deposit_cmd))
    ptb_app.add_handler(CommandHandler("ref", ref_cmd))
    ptb_app.add_handler(CommandHandler("help", help_cmd))
    ptb_app.add_handler(CommandHandler("refund_rules", refund_rules_cmd))

    ptb_app.add_handler(CallbackQueryHandler(noop, pattern=r"^noop$"))
    ptb_app.add_handler(CallbackQueryHandler(stock, pattern=r"^stock$"))
    ptb_app.add_handler(CallbackQueryHandler(page_cb, pattern=r"^page_\d+$"))
    ptb_app.add_handler(CallbackQueryHandler(open_filters, pattern=r"^open_filters$"))
    ptb_app.add_handler(CallbackQueryHandler(apply_filter, pattern=r"^f_"))
    ptb_app.add_handler(CallbackQueryHandler(buy_card, pattern=r"^buy_\d+$"))
    ptb_app.add_handler(CallbackQueryHandler(cancel_purchase, pattern=r"^cancel_purchase$"))
    ptb_app.add_handler(CallbackQueryHandler(confirm_purchase, pattern=r"^confirm_\d+$"))
    ptb_app.add_handler(CallbackQueryHandler(copy_ref, pattern=r"^copyref_"))
    ptb_app.add_handler(CallbackQueryHandler(withdraw_confirm, pattern=r"^withdraw_confirm$"))
    ptb_app.add_handler(CallbackQueryHandler(deposit_cmd, pattern=r"^open_deposit$"))
    ptb_app.add_handler(CallbackQueryHandler(amount_cb, pattern=r"^amt_"))
    ptb_app.add_handler(CallbackQueryHandler(coin_selected, pattern=r"^coin_"))

    ptb_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_custom_amount))
    ptb_app.job_queue.run_repeating(auto_oos_job, interval=60, first=10)

    asyncio.run(set_webhook())
    threading.Thread(target=keep_alive, daemon=True).start()
    flask_app.run(host="0.0.0.0", port=PORT)

if __name__ == "__main__":
    main()
