import os
import io
import re
import json
import sqlite3
import logging
import secrets
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

import requests
import qrcode
from flask import Flask, request, abort

# -------------------- Configuration --------------------
BOT_TOKEN = os.getenv("8911623254:AAG1QIF5QcJisvwcIanoyW1INfqoKW4NWM8", "").strip()
ADMIN_ID_RAW = os.getenv("8250691009", "").strip()
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()
TRC20_ADDRESS = os.getenv("TRC20_ADDRESS", "").strip()
BEP20_ADDRESS = os.getenv("BEP20_ADDRESS", "").strip()
DB_PATH = os.getenv("DB_PATH", "nexora.sqlite3")
DEFAULT_RATE = Decimal(os.getenv("DEFAULT_RATE", "114"))
DEFAULT_MIN = Decimal(os.getenv("DEFAULT_MIN", "25"))
DEFAULT_MAX = Decimal(os.getenv("DEFAULT_MAX", "500"))

if not BOT_TOKEN:
    raise RuntimeError("8911623254:AAG1QIF5QcJisvwcIanoyW1INfqoKW4NWM8")
if not ADMIN_ID_RAW.isdigit():
    raise RuntimeError("8250691009")
if not WEBHOOK_SECRET or not re.fullmatch(r"[A-Za-z0-9_-]{16,}", WEBHOOK_SECRET):
    raise RuntimeError("WEBHOOK_SECRET must be at least 16 letters/numbers/_/- characters")

ADMIN_ID = int(ADMIN_ID_RAW)
API = f"https://api.telegram.org/bot{BOT_TOKEN}"
FILE_API = f"https://api.telegram.org/file/bot{BOT_TOKEN}"
app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("nexora")

# -------------------- Telegram helpers --------------------
def tg(method, data=None, files=None, timeout=30):
    response = requests.post(f"{API}/{method}", data=data or {}, files=files, timeout=timeout)
    response.raise_for_status()
    result = response.json()
    if not result.get("ok"):
        raise RuntimeError(f"Telegram API error: {result}")
    return result.get("result")

def send_message(chat_id, text, reply_markup=None):
    data = {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    if reply_markup:
        data["reply_markup"] = json.dumps(reply_markup)
    return tg("sendMessage", data)

def send_photo(chat_id, photo, caption=None, reply_markup=None):
    data = {"chat_id": chat_id}
    if caption:
        data["caption"] = caption[:1024]
        data["parse_mode"] = "HTML"
    if reply_markup:
        data["reply_markup"] = json.dumps(reply_markup)
    if isinstance(photo, bytes):
        return tg("sendPhoto", data, files={"photo": ("deposit-qr.png", photo, "image/png")})
    data["photo"] = photo
    return tg("sendPhoto", data)

def answer_callback(callback_id, text=None):
    data = {"callback_query_id": callback_id}
    if text:
        data["text"] = text[:180]
    try:
        tg("answerCallbackQuery", data)
    except Exception:
        log.exception("answerCallbackQuery failed")

def is_admin(user_id):
    return int(user_id or 0) == ADMIN_ID

def money(d):
    return f"{d:,.2f}".rstrip("0").rstrip(".")

def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

# -------------------- Database --------------------
def db():
    conn = sqlite3.connect(DB_PATH, timeout=20)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with db() as con:
        con.execute("""CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY, value TEXT NOT NULL
        )""")
        con.execute("""CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY, upi TEXT, created_at TEXT NOT NULL
        )""")
        con.execute("""CREATE TABLE IF NOT EXISTS sessions (
            user_id INTEGER PRIMARY KEY, step TEXT NOT NULL, data TEXT NOT NULL DEFAULT '{}'
        )""")
        con.execute("""CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL, username TEXT, upi TEXT NOT NULL,
            amount TEXT NOT NULL, network TEXT NOT NULL, wallet TEXT NOT NULL,
            txid TEXT NOT NULL UNIQUE, screenshot_file_id TEXT,
            rate TEXT NOT NULL, inr_estimate TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'PENDING',
            admin_note TEXT DEFAULT '', created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )""")
    set_default("rate", str(DEFAULT_RATE))
    set_default("min_amount", str(DEFAULT_MIN))
    set_default("max_amount", str(DEFAULT_MAX))
    set_default("welcome", "💎 <b>Welcome to Nexora Exchange</b>\n\nYour USDT sell request will be manually reviewed by our admin.\n\n💵 Rate: ₹{rate} per USDT\n📉 Minimum: {min} USDT\n📈 Maximum: {max} USDT\n\nChoose an option below to continue.")
    set_default("sell_message", "💰 <b>SELL USDT</b>\n\n💵 Current rate: ₹{rate} per USDT\n📉 Minimum: {min} USDT\n📈 Maximum: {max} USDT\n\nBefore continuing, add your payout UPI ID. Use only details you control.")
    set_default("banner_file_id", "")
    set_default("support", "Please contact the bot administrator for support.")
    set_default("wallet_TRC20", TRC20_ADDRESS)
    set_default("wallet_BEP20", BEP20_ADDRESS)

def get_setting(key, default=""):
    with db() as con:
        row = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default

def set_setting(key, value):
    with db() as con:
        con.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))

def set_default(key, value):
    with db() as con:
        con.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)", (key, str(value)))

def get_user(user_id):
    with db() as con:
        row = con.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
    return row

def save_user(user_id, upi=None):
    with db() as con:
        con.execute("""INSERT INTO users(user_id,upi,created_at) VALUES(?,?,?)
          ON CONFLICT(user_id) DO UPDATE SET upi=COALESCE(excluded.upi,users.upi)""",
          (user_id, upi, now_iso()))

def get_session(user_id):
    with db() as con:
        row = con.execute("SELECT * FROM sessions WHERE user_id=?", (user_id,)).fetchone()
    if not row:
        return None, {}
    try:
        return row["step"], json.loads(row["data"] or "{}")
    except Exception:
        return row["step"], {}

def set_session(user_id, step, data=None):
    with db() as con:
        con.execute("""INSERT INTO sessions(user_id,step,data) VALUES(?,?,?)
          ON CONFLICT(user_id) DO UPDATE SET step=excluded.step,data=excluded.data""",
          (user_id, step, json.dumps(data or {})))

def clear_session(user_id):
    with db() as con:
        con.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))

def get_order(order_id):
    with db() as con:
        return con.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()

# -------------------- UI --------------------
def main_keyboard():
    return {"inline_keyboard": [
        [{"text": "💰 Sell USDT", "callback_data": "sell"}],
        [{"text": "🏦 Add Bank / UPI ID", "callback_data": "add_upi"}],
        [{"text": "📋 My Orders", "callback_data": "my_orders"},
         {"text": "💬 Support", "callback_data": "support"}]
    ]}

def network_keyboard():
    return {"inline_keyboard": [
        [{"text": "TRC20", "callback_data": "network:TRC20"},
         {"text": "BEP20", "callback_data": "network:BEP20"}],
        [{"text": "⬅️ Main Menu", "callback_data": "home"}]
    ]}

def confirm_keyboard():
    return {"inline_keyboard": [
        [{"text": "📤 Submit Transaction", "callback_data": "submit_tx"}],
        [{"text": "⬅️ Main Menu", "callback_data": "home"}]
    ]}

def home(chat_id, user_id):
    rate = get_setting("rate", "114")
    minimum = get_setting("min_amount", "25")
    maximum = get_setting("max_amount", "500")
    template = get_setting("welcome")
    text = template.replace("{rate}", rate).replace("{min}", minimum).replace("{max}", maximum)
    banner = get_setting("banner_file_id")
    if banner:
        try:
            send_photo(chat_id, banner, caption=text, reply_markup=main_keyboard())
            return
        except Exception:
            log.exception("Could not send welcome banner")
    send_message(chat_id, text, main_keyboard())

def start_sell(chat_id, user_id):
    rate = get_setting("rate", "114")
    minimum = get_setting("min_amount", "25")
    maximum = get_setting("max_amount", "500")
    text = get_setting("sell_message").replace("{rate}", rate).replace("{min}", minimum).replace("{max}", maximum)
    user = get_user(user_id)
    if not user or not user["upi"]:
        send_message(chat_id, text + "\n\n🏦 Please add your UPI ID first.", {
            "inline_keyboard": [[{"text": "🏦 Add UPI ID", "callback_data": "add_upi"}],
                                [{"text": "⬅️ Main Menu", "callback_data": "home"}]]
        })
        return
    set_session(user_id, "amount", {})
    send_message(chat_id, text + "\n\n✍️ Enter the amount of USDT you want to sell (25–500).")

def show_deposit(chat_id, user_id, network):
    user = get_user(user_id)
    wallet = get_setting("wallet_" + network, "").strip()
    if not wallet:
        send_message(chat_id, f"⚠️ The {network} wallet is not configured yet. Please contact support.")
        clear_session(user_id)
        return
    step, data = get_session(user_id)
    amount = data.get("amount")
    if not amount:
        send_message(chat_id, "Your amount was not found. Please start again using Sell USDT.")
        clear_session(user_id)
        return
    data["network"] = network
    data["wallet"] = wallet
    set_session(user_id, "txid", data)
    qr = qrcode.QRCode(box_size=8, border=3)
    qr.add_data(wallet)
    qr.make(fit=True)
    image = qr.make_image(fill_color="black", back_color="white")
    bio = io.BytesIO()
    image.save(bio, format="PNG")
    bio.seek(0)
    caption = (f"📥 <b>USDT DEPOSIT DETAILS</b>\n\n"
               f"💎 Asset: USDT\n🌐 Network: <b>{network}</b>\n"
               f"💰 Amount: <b>{amount} USDT</b>\n\n"
               f"<b>Wallet Address</b>\n<code>{wallet}</code>\n\n"
               "⚠️ Send only USDT on the selected network. Verify the address carefully. "
               "Wrong-network transfers may be lost.\n\n"
               "After sending, tap Submit Transaction and provide your TxID and screenshot.")
    send_photo(chat_id, bio.getvalue(), caption, confirm_keyboard())

def send_admin_order(order_id):
    order = get_order(order_id)
    if not order:
        return
    text = (f"🆕 <b>NEW USDT SELL REQUEST #{order['id']}</b>\n\n"
            f"👤 User ID: <code>{order['user_id']}</code>\n"
            f"Username: @{order['username'] or 'not set'}\n"
            f"🏦 UPI ID: <code>{order['upi']}</code>\n"
            f"💰 Amount: <b>{order['amount']} USDT</b>\n"
            f"🌐 Network: <b>{order['network']}</b>\n"
            f"📍 Wallet: <code>{order['wallet']}</code>\n"
            f"🧾 TxID: <code>{order['txid']}</code>\n"
            f"💵 Rate snapshot: ₹{order['rate']} / USDT\n"
            f"💸 Estimated payout: ₹{order['inr_estimate']}\n"
            f"📌 Status: <b>{order['status']}</b>\n"
            f"🕒 Created: {order['created_at']}")
    send_message(ADMIN_ID, text, {"inline_keyboard": [
        [{"text": "✅ Approve", "callback_data": f"admin:approve:{order_id}"},
         {"text": "❌ Reject", "callback_data": f"admin:reject:{order_id}"}]
    ]})
    if order["screenshot_file_id"]:
        send_photo(ADMIN_ID, order["screenshot_file_id"], f"📷 Screenshot for order #{order_id}")

def process_update(update):
    if "callback_query" in update:
        cb = update["callback_query"]
        from_user = cb.get("from", {})
        user_id = int(from_user.get("id", 0))
        chat_id = int(cb.get("message", {}).get("chat", {}).get("id", user_id))
        data = cb.get("data", "")
        answer_callback(cb.get("id"))
        if data.startswith("admin:"):
            if not is_admin(user_id):
                send_message(chat_id, "⛔ Admin only.")
                return
            _, action, oid = data.split(":", 2)
            admin_order_action(chat_id, action, int(oid))
            return
        if data == "home":
            clear_session(user_id)
            home(chat_id, user_id)
        elif data == "sell":
            start_sell(chat_id, user_id)
        elif data == "add_upi":
            set_session(user_id, "upi", {})
            send_message(chat_id, "🏦 <b>Add UPI ID</b>\n\nPlease enter your UPI ID (example: name@bank). Never send your UPI PIN or OTP.")
        elif data == "my_orders":
            show_my_orders(chat_id, user_id)
        elif data == "support":
            send_message(chat_id, get_setting("support", "Please contact the administrator."))
        elif data.startswith("network:"):
            network = data.split(":", 1)[1]
            if network not in ("TRC20", "BEP20"):
                send_message(chat_id, "Invalid network.")
                return
            step, session_data = get_session(user_id)
            if step != "network":
                send_message(chat_id, "Please start again from Sell USDT.")
                return
            show_deposit(chat_id, user_id, network)
        elif data == "submit_tx":
            step, session_data = get_session(user_id)
            if step != "txid":
                send_message(chat_id, "Please start again from Sell USDT.")
                return
            set_session(user_id, "txid_input", session_data)
            send_message(chat_id, "🧾 <b>Submit Transaction</b>\n\nPlease send your transaction hash (TxID) as a text message.")
        return

    msg = update.get("message")
    if not msg:
        return
    from_user = msg.get("from", {})
    user_id = int(from_user.get("id", 0))
    chat_id = int(msg.get("chat", {}).get("id", user_id))
    text = (msg.get("text") or "").strip()
    photo_list = msg.get("photo") or []
    caption = (msg.get("caption") or "").strip()

    # Admin commands
    if is_admin(user_id) and (text.startswith("/") or caption.startswith("/")):
        command_text = text or caption
        handle_admin_command(chat_id, command_text, msg, photo_list)
        return

    if text == "/start" or text == "/menu":
        clear_session(user_id)
        save_user(user_id)
        home(chat_id, user_id)
        return
    if text == "/cancel":
        clear_session(user_id)
        send_message(chat_id, "Cancelled. Use /start to open the menu.", main_keyboard())
        return
    if text == "/help":
        send_message(chat_id, "Use the buttons to sell USDT, add your UPI ID, or check your orders. Admin manually reviews all transactions.")
        return

    step, data = get_session(user_id)
    if step == "upi":
        if not text or len(text) > 100 or not re.fullmatch(r"[A-Za-z0-9._-]{2,256}@[A-Za-z0-9.-]{2,100}", text):
            send_message(chat_id, "That UPI ID format doesn't look right. Please enter a valid UPI ID (example: name@bank).")
            return
        save_user(user_id, text)
        clear_session(user_id)
        send_message(chat_id, "✅ <b>UPI ID saved.</b>\n\nYou can now tap Sell USDT to continue.", main_keyboard())
        return

    if step == "amount":
        try:
            amount = Decimal(text)
        except (InvalidOperation, TypeError):
            send_message(chat_id, "Please enter a valid number, for example: 25 or 50.")
            return
        minimum = Decimal(get_setting("min_amount", "25"))
        maximum = Decimal(get_setting("max_amount", "500"))
        if amount < minimum or amount > maximum or amount <= 0:
            send_message(chat_id, f"⚠️ <b>SELLING LIMIT NOTICE</b>\n\nMinimum: {minimum} USDT\nMaximum: {maximum} USDT\n\nPlease enter an amount within these limits.")
            return
        data["amount"] = str(amount.normalize())
        set_session(user_id, "network", data)
        send_message(chat_id, "🌐 <b>Select USDT Network</b>\n\nChoose the exact network you will use to send USDT. Do not send on another network.", network_keyboard())
        return

    if step == "txid_input":
        if not text or len(text) < 8 or len(text) > 200:
            send_message(chat_id, "Please send a valid-looking transaction hash as text.")
            return
        data["txid"] = text
        set_session(user_id, "screenshot", data)
        send_message(chat_id, "📷 Now send the transaction screenshot as a photo. The screenshot and TxID will be checked manually.")
        return

    if step == "screenshot":
        if not photo_list:
            send_message(chat_id, "Please send the screenshot using Telegram's photo attachment, not as text.")
            return
        user = get_user(user_id)
        if not user or not user["upi"]:
            send_message(chat_id, "Your UPI ID is missing. Please add it and start again.")
            clear_session(user_id)
            return
        amount = Decimal(data["amount"])
        rate = Decimal(get_setting("rate", "114"))
        inr = amount * rate
        network = data["network"]
        wallet = data["wallet"]
        txid = data["txid"]
        photo_file_id = photo_list[-1]["file_id"]
        username = from_user.get("username", "")
        try:
            with db() as con:
                cur = con.execute("""INSERT INTO orders
                  (user_id,username,upi,amount,network,wallet,txid,screenshot_file_id,rate,inr_estimate,status,created_at,updated_at)
                  VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (user_id, username, user["upi"], str(amount.normalize()), network, wallet, txid,
                   photo_file_id, str(rate), str(inr.quantize(Decimal("0.01"))), "PENDING", now_iso(), now_iso()))
                order_id = cur.lastrowid
        except sqlite3.IntegrityError:
            send_message(chat_id, "⚠️ This TxID has already been submitted. Please check it or contact support.")
            return
        clear_session(user_id)
        send_message(chat_id, f"✅ <b>Request submitted!</b>\n\nOrder ID: <code>#{order_id}</code>\nStatus: <b>PENDING REVIEW</b>\n\nYour transaction will be manually checked. Submission does not guarantee approval or payout.", main_keyboard())
        send_admin_order(order_id)
        return

    # If user sends a photo without being asked
    if photo_list:
        send_message(chat_id, "Use /start and follow the Sell USDT steps. Submit a screenshot only when the bot asks for it.")
    elif text and not text.startswith("/"):
        send_message(chat_id, "Please use the menu buttons below.", main_keyboard())

def show_my_orders(chat_id, user_id):
    with db() as con:
        rows = con.execute("SELECT id,amount,network,status,created_at FROM orders WHERE user_id=? ORDER BY id DESC LIMIT 10", (user_id,)).fetchall()
    if not rows:
        send_message(chat_id, "📋 You don't have any sell orders yet.", main_keyboard())
        return
    lines = ["📋 <b>Your Recent Orders</b>"]
    for row in rows:
        lines.append(f"#{row['id']} • {row['amount']} USDT • {row['network']} • <b>{row['status']}</b> • {row['created_at'][:10]}")
    send_message(chat_id, "\n".join(lines), main_keyboard())

def admin_order_action(chat_id, action, order_id, note=""):
    order = get_order(order_id)
    if not order:
        send_message(chat_id, f"Order #{order_id} not found.")
        return
    if action == "approve":
        if order["status"] != "PENDING":
            send_message(chat_id, f"Order #{order_id} is {order['status']}; only PENDING orders can be approved.")
            return
        new_status = "APPROVED"
    elif action == "reject":
        if order["status"] in ("PAID", "REJECTED"):
            send_message(chat_id, f"Order #{order_id} is already {order['status']}.")
            return
        new_status = "REJECTED"
    elif action == "paid":
        if order["status"] != "APPROVED":
            send_message(chat_id, f"Order #{order_id} must be APPROVED before marking PAID.")
            return
        new_status = "PAID"
    else:
        send_message(chat_id, "Unknown action.")
        return
    with db() as con:
        con.execute("UPDATE orders SET status=?,admin_note=?,updated_at=? WHERE id=?", (new_status, note, now_iso(), order_id))
    send_message(chat_id, f"✅ Order #{order_id} updated to <b>{new_status}</b>.")
    try:
        send_message(order["user_id"], f"📋 <b>Order Update</b>\n\nOrder: #{order_id}\nStatus: <b>{new_status}</b>\n{('Note: ' + note) if note else ''}\n\n" +
                     ("Payout was marked as completed by admin." if new_status == "PAID" else "Please wait for further updates or contact support."))
    except Exception:
        log.exception("Could not notify user")

def handle_admin_command(chat_id, command_text, msg, photos):
    parts = command_text.split()
    command = parts[0].split("@")[0].lower()
    args = parts[1:]
    if command == "/admin":
        send_message(chat_id, (
            "👑 <b>Nexora Admin Commands</b>\n\n"
            "/setrate 114\n/setlimits 25 500\n"
            "/setwallet BEP20 YOUR_ADDRESS\n/setwallet TRC20 YOUR_ADDRESS\n"
            "/setmessage Your message here\n"
            "/setwelcome Your welcome message here\n"
            "/setimage welcome (send as photo caption)\n"
            "/setsupport Contact support text\n"
            "/orders\n/approve ORDER_ID\n/reject ORDER_ID [reason]\n/paid ORDER_ID\n"
            "/cancelsetup"
        ))
        return
    if command == "/setrate":
        if len(args) != 1:
            send_message(chat_id, "Usage: /setrate 114")
            return
        try:
            rate = Decimal(args[0])
            if rate <= 0 or rate > 1000000:
                raise ValueError()
        except Exception:
            send_message(chat_id, "Enter a valid positive rate, e.g. /setrate 114")
            return
        set_setting("rate", str(rate.normalize()))
        send_message(chat_id, f"✅ Sell rate set to ₹{rate} per USDT.")
        return
    if command == "/setlimits":
        if len(args) != 2:
            send_message(chat_id, "Usage: /setlimits 25 500")
            return
        try:
            mn, mx = Decimal(args[0]), Decimal(args[1])
            if mn <= 0 or mx < mn:
                raise ValueError()
        except Exception:
            send_message(chat_id, "Enter valid limits, e.g. /setlimits 25 500")
            return
        set_setting("min_amount", str(mn.normalize()))
        set_setting("max_amount", str(mx.normalize()))
        send_message(chat_id, f"✅ Limits updated: minimum {mn} USDT, maximum {mx} USDT.")
        return
    if command == "/setwallet":
        if len(args) != 2 or args[0].upper() not in ("TRC20", "BEP20"):
            send_message(chat_id, "Usage: /setwallet BEP20 YOUR_WALLET_ADDRESS")
            return
        network, address = args[0].upper(), args[1]
        if len(address) < 20 or len(address) > 120:
            send_message(chat_id, "That address length looks unusual. Check it carefully before saving.")
            return
        set_setting("wallet_" + network, address)
        send_message(chat_id, f"✅ {network} wallet address saved. Verify it before accepting real transfers.")
        return
    if command in ("/setmessage", "/setwelcome", "/setsupport"):
        value = command_text.partition(" ")[2].strip()
        if not value:
            send_message(chat_id, f"Usage: {command} Your text here")
            return
        key = {"/setmessage": "sell_message", "/setwelcome": "welcome", "/setsupport": "support"}[command]
        set_setting(key, value)
        send_message(chat_id, f"✅ {key.replace('_',' ').title()} text updated. You can use {{rate}}, {{min}}, and {{max}} placeholders.")
        return
    if command == "/setimage":
        if not photos:
            send_message(chat_id, "Send a photo with caption: /setimage welcome")
            return
        if args and args[0].lower() == "welcome":
            set_setting("banner_file_id", photos[-1]["file_id"])
            send_message(chat_id, "✅ Welcome banner image saved.")
        else:
            send_message(chat_id, "Usage: send a photo with caption /setimage welcome")
        return
    if command == "/cancelsetup":
        send_message(chat_id, "No multi-step admin setup is running. Admin settings are changed with commands.")
        return
    if command == "/orders":
        with db() as con:
            rows = con.execute("SELECT id,user_id,amount,network,status,created_at FROM orders ORDER BY id DESC LIMIT 20").fetchall()
        if not rows:
            send_message(chat_id, "No orders yet.")
            return
        lines = ["📋 <b>Recent Orders</b>"]
        for row in rows:
            lines.append(f"#{row['id']} • User <code>{row['user_id']}</code> • {row['amount']} USDT • {row['network']} • <b>{row['status']}</b>")
        send_message(chat_id, "\n".join(lines))
        return
    if command in ("/approve", "/reject", "/paid"):
        if not args or not args[0].isdigit():
            send_message(chat_id, f"Usage: {command} ORDER_ID")
            return
        order_id = int(args[0])
        note = " ".join(args[1:]) if len(args) > 1 else ""
        action = command[1:]
        admin_order_action(chat_id, action, order_id, note)
        return
    send_message(chat_id, "Unknown admin command. Send /admin to see available commands.")

@app.get("/")
def health():
    return "Nexora Exchange bot is running.", 200

@app.post("/webhook/<secret>")
def webhook(secret):
    if not secrets.compare_digest(secret, WEBHOOK_SECRET):
        abort(403)
    update = request.get_json(silent=True)
    if not update:
        return "ok", 200
    try:
        process_update(update)
    except Exception:
        log.exception("Update processing failed")
    return "ok", 200

@app.get("/health")
def health_json():
    return {"status": "ok"}, 200

init_db()
