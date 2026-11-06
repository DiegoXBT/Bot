import sqlite3
import time
import hmac
import hashlib
import base64
import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)

# ==========================================
# 1. CONFIGURATION
# ==========================================
TELEGRAM_BOT_TOKEN = "9172866637:AAFqKf2nlhC9duEsdMKWU3Nfijvl52tsNoI"

PRIVATE_GROUP_ID = -1004458012645

MIN_BALANCE_USD = 100.0
WEEX_REFERRAL_LINK = "https://www.weex.com/register?vipCode=g2gb"
WEEX_BASE_URL = "https://api-spot.weex.com"

WEEX_API_KEY = "weex_6db66868a879b11b2349555c5fb199b5"
WEEX_API_SECRET = "09b300a853231901ae435e64866731f63565a07025645b3c94ffd4a17ae4fa2f"
WEEX_PASSPHRASE = "hAYhdPLds2Hm6KjCgVJhujgmjs4htj4v"

# ==========================================
# 2. LOCAL DATABASE (Prevents UID sharing)
# ==========================================
conn = sqlite3.connect("moonshow_users.db", check_same_thread=False)
cursor = conn.cursor()
cursor.execute("""
    CREATE TABLE IF NOT EXISTS verified_users (
        uid TEXT PRIMARY KEY,
        telegram_id INTEGER UNIQUE,
        verified_at INTEGER
    )
""")
conn.commit()


# ==========================================
# 3. WEEX API CONNECTION & BALANCE CHECK (SAFE PARSING)
# ==========================================
def get_weex_server_timestamp() -> str:
    """Fetches WEEX's exact server time to prevent clock drift errors."""
    try:
        r = requests.get(f"{WEEX_BASE_URL}/api/v3/time", timeout=5)
        if r.status_code == 200 and r.text.strip():
            data = r.json()
            srv_time = data.get("serverTime") or data.get("data", {}).get("serverTime")
            if srv_time:
                return str(srv_time)
    except Exception:
        pass
    return str(int(time.time() * 1000))


def weex_get_request(request_path: str, query_string: str) -> tuple[int, dict | list | None]:
    """Signs and sends a GET request to WEEX Partner API V3."""
    timestamp = get_weex_server_timestamp()
    full_query = f"?{query_string}" if query_string else ""
    message = f"{timestamp}GET{request_path}{full_query}"

    mac = hmac.new(
        WEEX_API_SECRET.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256
    )
    signature = base64.b64encode(mac.digest()).decode("utf-8")

    headers = {
        "ACCESS-KEY": WEEX_API_KEY.strip(),
        "ACCESS-SIGN": signature,
        "ACCESS-PASSPHRASE": WEEX_PASSPHRASE.strip(),
        "ACCESS-TIMESTAMP": timestamp,
        "Content-Type": "application/json",
        "locale": "en-US",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }

    url = f"{WEEX_BASE_URL}{request_path}{full_query}"
    response = requests.get(url, headers=headers, timeout=10)

    print(f"\n--- WEEX API CALL: {request_path}{full_query} ---")
    print(f"HTTP Status: {response.status_code}")
    print(f"Raw Response Body: {repr(response.text[:500])}")

    if not response.text or not response.text.strip():
        return response.status_code, None

    try:
        return response.status_code, response.json()
    except Exception:
        return response.status_code, {"raw_error": response.text[:200]}


def safe_float(val) -> float:
    """Safely converts WEEX response fields to float, handling '--', None, or empty strings."""
    if val is None:
        return 0.0
    if isinstance(val, (int, float)):
        return float(val)
    val_str = str(val).strip()
    if not val_str or val_str in ("--", "N/A", "null", "None"):
        return 0.0
    try:
        return float(val_str)
    except ValueError:
        return 0.0


def verify_moonshow_uid(uid: str) -> tuple[bool, str]:
    """
    Verifies that a WEEX UID exists and has at least $100 USD in balance.
    """
    try:
        asset_info = {}
        found_account = False

        # Attempt 1: Check via Partner Asset Endpoint (/api/v3/agency/getAssert)
        status, asset_data = weex_get_request("/api/v3/agency/getAssert", f"userId={uid}")
        if asset_data and isinstance(asset_data, dict):
            raw_data = asset_data.get("data", asset_data)
            if isinstance(raw_data, list) and raw_data:
                asset_info = raw_data[0]
                found_account = True
            elif isinstance(raw_data, dict) and raw_data:
                asset_info = raw_data
                found_account = True

        # Attempt 2: Check via channel trade asset endpoint if needed
        if not found_account:
            status, trade_data = weex_get_request("/api/v3/rebate/affiliate/getChannelUserTradeAndAsset", f"uid={uid}")
            if trade_data and isinstance(trade_data, dict):
                raw_data = trade_data.get("data", trade_data)
                if isinstance(raw_data, list) and raw_data:
                    asset_info = raw_data[0]
                    found_account = True
                elif isinstance(raw_data, dict) and raw_data:
                    asset_info = raw_data
                    found_account = True

        # Extract balances safely using safe_float() to completely avoid '--' conversion crashes
        avail_balance = safe_float(asset_info.get("availableBalance"))
        contract_usdt = safe_float(asset_info.get("contractTotalUsdt"))
        spot_usdt = safe_float(asset_info.get("spotTotalUsdt"))
        total_deposit = safe_float(asset_info.get("depositTotalAmount") or asset_info.get("rechargeAmount"))
        total_asset = safe_float(asset_info.get("totalAsset") or asset_info.get("totalBalance") or asset_info.get("balance"))

        current_balance = max(avail_balance + contract_usdt + spot_usdt, total_deposit, total_asset)

        if current_balance < MIN_BALANCE_USD:
            return (
                False,
                f"You don't meet the requirements to join the group. Your account must have net assets of at least {MIN_BALANCE_USD:.2f} to use the BOT for joining. (Current balance: {current_balance:.2f})."
            )

        return True, f"Balance verified (${current_balance:.2f} USD)!"

    except Exception as e:
        return False, f"Error connecting to WEEX API: {str(e)}"


# ==========================================
# 4. TELEGRAM BOT HANDLERS & BUTTON MENUS
# ==========================================
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sends the initial menu matching your screenshot when user types /start."""
    keyboard = [
        [InlineKeyboardButton("ALREADY WEEX MEMBER", callback_data="already_member")],
        [InlineKeyboardButton("SIGNUP & GET BONUS", url=WEEX_REFERRAL_LINK)]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    welcome_text = (
        "How to Join :\n\n"
        "1. Sign up on WEEX to start trading.\n"
        "2. Deposit $100 into your account.\n"
        "3. Enter your unique UID.\n"
        "4. Once all steps are completed, you’ll receive an invite link to join."
    )

    if update.message:
        await update.message.reply_text(welcome_text, parse_mode="Markdown", reply_markup=reply_markup)
    elif update.callback_query:
        await update.callback_query.message.reply_text(welcome_text, parse_mode="Markdown", reply_markup=reply_markup)


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles clicks on the inline buttons."""
    query = update.callback_query
    await query.answer()

    if query.data == "already_member":
        context.user_data["waiting_for_uid"] = True
        await query.message.reply_text("Please send your Weex UID:")


async def get_group_id_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Helper command: Type /getid inside your group to find PRIVATE_GROUP_ID."""
    chat_id = update.effective_chat.id
    chat_title = update.effective_chat.title or "Private Chat"
    await update.message.reply_text(
        f"Chat Name: {chat_title}\n"
        f"Chat ID: `{chat_id}`\n\n",
        parse_mode="Markdown"
    )


async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles text messages (specifically looking for the WEEX UID in DMs)."""
    if update.effective_chat.type != "private":
        return

    text = update.message.text.strip()

    if not context.user_data.get("waiting_for_uid"):
        await update.message.reply_text("Please press /start to begin the verification process.")
        return

    if not text.isdigit():
        await update.message.reply_text(" Please send a valid numeric Weex UID.")
        return

    uid = text
    telegram_id = update.effective_user.id

    context.user_data["waiting_for_uid"] = False

    cursor.execute("SELECT telegram_id FROM verified_users WHERE uid = ?", (uid,))
    row = cursor.fetchone()
    if row:
        await update.message.reply_text(" This Weex UID has already been used to claim an invite link.")
        return

    status_msg = await update.message.reply_text(" Checking your Weex UID and balance...")

    is_valid, result_msg = verify_moonshow_uid(uid)
    if not is_valid:
        await status_msg.edit_text(f"{result_msg}")
        return

    try:
        invite = await context.bot.create_chat_invite_link(
            chat_id=PRIVATE_GROUP_ID,
            member_limit=1,
            expire_date=int(time.time()) + 3600,
            name=f"MoonShow_{uid}"
        )

        cursor.execute(
            "INSERT INTO verified_users (uid, telegram_id, verified_at) VALUES (?, ?, ?)",
            (uid, telegram_id, int(time.time()))
        )
        conn.commit()

        await status_msg.edit_text(
            f"Welcome to join our group! {invite.invite_link}"
        )
    except Exception as e:
        await status_msg.edit_text(
            f"Your account is verified, but I failed to create the invite link.\n"
        )


def main():
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("getid", get_group_id_command))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message))

    print("The Moon Show WEEX Bot is LIVE! Press Ctrl+C to stop.")
    app.run_polling()


if __name__ == "__main__":
    main()
