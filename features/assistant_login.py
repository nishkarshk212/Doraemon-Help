import os
import html
import logging
import asyncio
import datetime
from typing import Dict, Any

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, CommandHandler, MessageHandler, filters
from telegram.constants import ParseMode

from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import (
    SessionPasswordNeededError,
    PhoneCodeInvalidError,
    PhoneCodeExpiredError,
    PasswordHashInvalidError,
    AuthKeyInvalidError,
    PhoneNumberInvalidError,
    FloodWaitError
)

from core.config import API_ID, API_HASH, OWNER_ID, colored_button, to_small_caps
from moderation.sudo import is_sudo
from core.database import get_collection, COLLECTIONS

logger = logging.getLogger(__name__)

# In-memory stores for active login state
# user_id -> {"step": str, "phone": str, "phone_code_hash": str, "created_at": datetime}
login_states: Dict[int, Dict[str, Any]] = {}

# user_id -> TelegramClient
temp_login_clients: Dict[int, TelegramClient] = {}


def _get_assistant_collection():
    """Returns MongoDB assistant collection."""
    try:
        col_name = COLLECTIONS.get("assistant", "assistant_settings")
        return get_collection(col_name)
    except Exception as e:
        logger.error(f"Error accessing assistant collection: {e}")
        return None


def get_assistant_session() -> Dict[str, Any]:
    """Retrieve the currently stored assistant session from MongoDB."""
    try:
        col = _get_assistant_collection()
        if col is not None:
            doc = col.find_one({"_id": "active_assistant"})
            if doc:
                return doc
    except Exception as e:
        logger.error(f"Error getting assistant session: {e}")
    return {}


def save_assistant_session(string_session: str, me_info=None) -> bool:
    """Save assistant string session and account details to MongoDB and .env."""
    try:
        col = _get_assistant_collection()
        doc = {
            "_id": "active_assistant",
            "string_session": string_session,
            "updated_at": datetime.datetime.utcnow()
        }
        if me_info:
            doc.update({
                "user_id": getattr(me_info, "id", None),
                "first_name": getattr(me_info, "first_name", ""),
                "last_name": getattr(me_info, "last_name", ""),
                "username": getattr(me_info, "username", None),
                "phone": getattr(me_info, "phone", None),
            })
        if col is not None:
            col.replace_one({"_id": "active_assistant"}, doc, upsert=True)
            logger.info("✅ Assistant session saved to MongoDB successfully.")
    except Exception as e:
        logger.error(f"Failed to save assistant session to MongoDB: {e}")

    # Best-effort .env update for persistent container rebuilds
    try:
        env_path = os.path.join(os.getcwd(), ".env")
        if os.path.exists(env_path) and os.access(env_path, os.W_OK):
            with open(env_path, "r", encoding="utf-8") as f:
                lines = f.readlines()
            new_lines = []
            found = False
            for line in lines:
                if line.startswith("STRING_SESSION="):
                    new_lines.append(f"STRING_SESSION='{string_session}'\n")
                    found = True
                else:
                    new_lines.append(line)
            if not found:
                new_lines.append(f"STRING_SESSION='{string_session}'\n")
            with open(env_path, "w", encoding="utf-8") as f:
                f.writelines(new_lines)
            logger.info("✅ Assistant session saved to .env file.")
    except Exception as e:
        logger.warning(f"Could not update .env with assistant session: {e}")

    return True


def delete_assistant_session() -> bool:
    """Delete the active assistant session from MongoDB."""
    try:
        col = _get_assistant_collection()
        if col is not None:
            col.delete_one({"_id": "active_assistant"})
            logger.info("🗑 Assistant session deleted from MongoDB.")
            return True
    except Exception as e:
        logger.error(f"Failed to delete assistant session: {e}")
    return False


async def clean_temp_client(user_id: int):
    """Disconnect and clean up any temporary client for this user."""
    client = temp_login_clients.pop(user_id, None)
    if client:
        try:
            await client.disconnect()
        except Exception:
            pass
    login_states.pop(user_id, None)


async def login_session_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Directly link an assistant using a Telethon string session.
    Usage: /login <string_session>
    """
    if not update.effective_chat or update.effective_chat.type != "private":
        return await update.message.reply_text("⚠️ <b>For security, assistant login can only be done in private chat (DM).</b>", parse_mode=ParseMode.HTML)

    user_id = update.effective_user.id
    if not is_sudo(user_id):
        return await update.message.reply_text("❌ <b>Only bot owner and authorized sudo users can manage the assistant.</b>", parse_mode=ParseMode.HTML)

    if not API_ID or not API_HASH:
        return await update.message.reply_text("❌ <b>Error:</b> <code>API_ID</code> or <code>API_HASH</code> is missing from bot configuration.", parse_mode=ParseMode.HTML)

    args = context.args or []
    if not args:
        return await update.message.reply_text(
            "📱 <b><u>ASSISTANT LOGIN USAGE</u></b>\n\n"
            "• <code>/login &lt;telethon_string_session&gt;</code>\n"
            "  <i>Directly link an existing Telethon string session.</i>\n\n"
            "• <code>/login_phone</code>\n"
            "  <i>Interactive login using Phone Number + OTP + 2FA password.</i>\n\n"
            "• <code>/assistant</code>\n"
            "  <i>Check current connected assistant details and status.</i>\n\n"
            "• <code>/logout_assistant</code>\n"
            "  <i>Disconnect and wipe assistant account.</i>",
            parse_mode=ParseMode.HTML
        )

    session_str = " ".join(args).strip().strip("'\"")
    status_msg = await update.message.reply_text("🔄 <i>Verifying Telethon string session...</i>", parse_mode=ParseMode.HTML)

    try:
        session_obj = StringSession(session_str)
    except Exception as e:
        return await status_msg.edit_text(f"❌ <b>Invalid session string format:</b> <code>{e}</code>", parse_mode=ParseMode.HTML)

    test_client = None
    try:
        test_client = TelegramClient(session_obj, API_ID, API_HASH)
        await test_client.connect()

        if not await test_client.is_user_authorized():
            await test_client.disconnect()
            return await status_msg.edit_text("❌ <b>Failed:</b> Session string is invalid or has expired.", parse_mode=ParseMode.HTML)

        me = await test_client.get_me()
        await test_client.disconnect()

        # Persist session to MongoDB and .env
        save_assistant_session(session_str, me)

        # Hot-reload voice chat assistant
        from features.voice_chat import reload_telethon_client
        ok, res = await reload_telethon_client(session_str)

        name = html.escape(getattr(me, "first_name", "") + (" " + me.last_name if getattr(me, "last_name", None) else ""))
        username = f"@{me.username}" if me.username else "<i>None</i>"
        phone = f"+{me.phone}" if getattr(me, "phone", None) else "<i>Hidden</i>"
        vc_status = "🟢 Active & Monitoring" if ok else f"⚠️ Start issue: {res}"

        await status_msg.edit_text(
            "🎉 <b><u>ASSISTANT CONNECTED SUCCESSFULLY</u></b>\n\n"
            f"👤 <b>Account:</b> <code>{name}</code>\n"
            f"🆔 <b>User ID:</b> <code>{me.id}</code>\n"
            f"🔗 <b>Username:</b> {username}\n"
            f"📱 <b>Phone:</b> <code>{phone}</code>\n\n"
            f"🎙 <b>Voice Chat Engine:</b> <code>{vc_status}</code>\n\n"
            "✅ <i>Assistant is now active for voice chat monitoring, /vcbanall, /vcmuteall, and invitations!</i>",
            parse_mode=ParseMode.HTML
        )
    except AuthKeyInvalidError:
        await status_msg.edit_text("❌ <b>Failed:</b> Telegram rejected this session (AuthKeyInvalid). Please generate a fresh session string.", parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.error(f"Login error for user {user_id}: {e}", exc_info=True)
        await status_msg.edit_text(f"❌ <b>Error verifying session:</b> <code>{e}</code>", parse_mode=ParseMode.HTML)
    finally:
        if test_client and test_client.is_connected():
            try:
                await test_client.disconnect()
            except Exception:
                pass


async def login_phone_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Start interactive phone login wizard.
    Prompts for phone number -> OTP -> 2FA.
    """
    if not update.effective_chat or update.effective_chat.type != "private":
        return await update.message.reply_text("⚠️ <b>For security, assistant login can only be done in private chat (DM).</b>", parse_mode=ParseMode.HTML)

    user_id = update.effective_user.id
    if not is_sudo(user_id):
        return await update.message.reply_text("❌ <b>Only bot owner and authorized sudo users can manage the assistant.</b>", parse_mode=ParseMode.HTML)

    if not API_ID or not API_HASH:
        return await update.message.reply_text("❌ <b>Error:</b> <code>API_ID</code> or <code>API_HASH</code> is missing from bot configuration.", parse_mode=ParseMode.HTML)

    await clean_temp_client(user_id)

    login_states[user_id] = {
        "step": "WAITING_PHONE",
        "phone": None,
        "phone_code_hash": None,
        "created_at": datetime.datetime.now()
    }

    await update.message.reply_text(
        "📱 <b><u>ASSISTANT PHONE LOGIN WIZARD</u></b>\n\n"
        "Please send your Telegram account <b>phone number</b> with the country code.\n\n"
        "💡 <b>Examples:</b>\n"
        "• <code>+919876543210</code> (India)\n"
        "• <code>+12345678900</code> (US/Canada)\n\n"
        "<i>Send /cancel_login anytime to abort.</i>",
        parse_mode=ParseMode.HTML
    )


async def cancel_login_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel any active assistant login wizard."""
    user_id = update.effective_user.id
    had_state = user_id in login_states or user_id in temp_login_clients
    await clean_temp_client(user_id)

    if had_state:
        await update.message.reply_text("🛑 <b>Login process has been cancelled.</b>", parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text("ℹ️ No active login process to cancel.", parse_mode=ParseMode.HTML)


async def assistant_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    View connected assistant details and voice chat status.
    """
    user_id = update.effective_user.id
    if not is_sudo(user_id):
        return await update.message.reply_text("❌ <b>Only bot owner and authorized sudo users can view assistant details.</b>", parse_mode=ParseMode.HTML)

    from features.voice_chat import telethon_client, call_to_chat

    is_running = telethon_client and telethon_client.is_connected()
    stored = get_assistant_session()

    me = None
    if is_running:
        try:
            me = await telethon_client.get_me()
        except Exception:
            pass

    if not me and not stored.get("string_session") and not os.getenv("STRING_SESSION"):
        return await update.message.reply_text(
            "⚠️ <b>[ No Assistant Connected! ]</b>\n\n"
            "Use <code>/login_phone</code> or <code>/login &lt;string_session&gt;</code> to link an assistant account.",
            parse_mode=ParseMode.HTML
        )

    name = "Unknown"
    acc_id = "Unknown"
    username = "<i>None</i>"
    phone = "<i>Hidden</i>"

    if me:
        name = html.escape(getattr(me, "first_name", "") + (" " + me.last_name if getattr(me, "last_name", None) else ""))
        acc_id = me.id
        username = f"@{me.username}" if me.username else "<i>None</i>"
        phone = f"+{me.phone}" if getattr(me, "phone", None) else "<i>Hidden</i>"
    elif stored:
        name = html.escape(stored.get("first_name", "Unknown") + (" " + stored.get("last_name", "") if stored.get("last_name") else ""))
        acc_id = stored.get("user_id", "Stored in DB")
        username = f"@{stored.get('username')}" if stored.get("username") else "<i>None</i>"
        phone = f"+{stored.get('phone')}" if stored.get("phone") else "<i>Hidden</i>"

    status_badge = "🟢 <b>Running & Connected</b>" if is_running else "🟡 <b>Disconnected (Use /login to reconnect)</b>"
    active_vcs = len(call_to_chat) if call_to_chat else 0

    text = (
        "🤖 <b><u>ASSISTANT ACCOUNT DETAILS</u></b>\n\n"
        f"👤 <b>Name:</b> <code>{name}</code>\n"
        f"🆔 <b>Account ID:</b> <code>{acc_id}</code>\n"
        f"🔗 <b>Username:</b> {username}\n"
        f"📱 <b>Phone:</b> <code>{phone}</code>\n"
        f"⚡ <b>Status:</b> {status_badge}\n"
        f"🎙 <b>Active Voice Chats Tracked:</b> <code>{active_vcs}</code>\n\n"
        "💡 <b>Available Assistant Commands:</b>\n"
        "• <code>/login &lt;string_session&gt;</code> — Switch/link string session\n"
        "• <code>/login_phone</code> — Interactive phone login wizard\n"
        "• <code>/logout_assistant</code> — Disconnect & remove session\n"
        "• <code>/vcbanall</code> — Ban active VC users in a group\n"
        "• <code>/vcmuteall</code> — Mute active VC users in a group\n"
        "• <code>/vcunmuteall</code> — Unmute active VC users in a group"
    )

    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def logout_assistant_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Disconnect and remove assistant session from the bot.
    """
    user_id = update.effective_user.id
    if not is_sudo(user_id):
        return await update.message.reply_text("❌ <b>Only bot owner and authorized sudo users can log out the assistant.</b>", parse_mode=ParseMode.HTML)

    from features.voice_chat import reload_telethon_client

    # Disconnect live client
    await reload_telethon_client("")

    # Delete from MongoDB
    delete_assistant_session()

    await update.message.reply_text(
        "🚪 <b>Assistant Logged Out Successfully!</b>\n\n"
        "The assistant session has been removed from database and memory.\n"
        "You can link a new assistant anytime using <code>/login</code> or <code>/login_phone</code>.",
        parse_mode=ParseMode.HTML
    )


class InLoginStateFilter(filters.MessageFilter):
    """Filter that matches ONLY if the sending user is actively in a login wizard."""
    def filter(self, message):
        if not message or not message.from_user:
            return False
        return message.from_user.id in login_states


in_login_state_filter = InLoginStateFilter()


async def interactive_login_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handles interactive steps: phone number, OTP code, and 2FA password.
    """
    if not update.message or not update.message.text:
        return

    user_id = update.effective_user.id
    if user_id not in login_states:
        return

    state = login_states[user_id]
    step = state.get("step")
    text = update.message.text.strip()

    # Timeout check: 10 minutes
    if (datetime.datetime.now() - state.get("created_at", datetime.datetime.now())).total_seconds() > 600:
        await clean_temp_client(user_id)
        return await update.message.reply_text("⌛ <b>Login session timed out.</b> Please restart with <code>/login_phone</code>.", parse_mode=ParseMode.HTML)

    # STEP 1: Phone Number
    if step == "WAITING_PHONE":
        phone = text.replace(" ", "").replace("-", "")
        if not (phone.startswith("+") or phone.isdigit()) or len(phone) < 8:
            return await update.message.reply_text(
                "❌ <b>Invalid phone format.</b>\n"
                "Please enter a valid phone number with country code, e.g. <code>+919876543210</code>\n\n"
                "<i>Send /cancel_login to abort.</i>",
                parse_mode=ParseMode.HTML
            )

        status_msg = await update.message.reply_text("⏳ <i>Connecting to Telegram and requesting login code...</i>", parse_mode=ParseMode.HTML)

        try:
            temp_client = TelegramClient(StringSession(), API_ID, API_HASH)
            await temp_client.connect()

            send_res = await temp_client.send_code_request(phone)
            temp_login_clients[user_id] = temp_client

            login_states[user_id].update({
                "step": "WAITING_OTP",
                "phone": phone,
                "phone_code_hash": send_res.phone_code_hash,
                "created_at": datetime.datetime.now()
            })

            await status_msg.edit_text(
                f"📨 <b>Authentication Code Sent!</b>\n\n"
                f"Telegram sent an OTP code to your account (<code>{phone}</code>).\n\n"
                "💡 <b>How to send:</b>\n"
                "• Send the code directly, e.g. <code>12345</code>\n"
                "• Or separate with spaces if Telegram flags it: <code>1 2 3 4 5</code>\n\n"
                "<i>Send /cancel_login to abort.</i>",
                parse_mode=ParseMode.HTML
            )
        except PhoneNumberInvalidError:
            await clean_temp_client(user_id)
            await status_msg.edit_text("❌ <b>The phone number is invalid.</b> Please restart with <code>/login_phone</code>.", parse_mode=ParseMode.HTML)
        except FloodWaitError as fwe:
            await clean_temp_client(user_id)
            await status_msg.edit_text(f"⚠️ <b>Flood wait from Telegram:</b> Please wait {fwe.seconds} seconds before trying again.", parse_mode=ParseMode.HTML)
        except Exception as e:
            logger.error(f"Error sending code to {phone}: {e}", exc_info=True)
            await clean_temp_client(user_id)
            await status_msg.edit_text(f"❌ <b>Error requesting code:</b> <code>{e}</code>", parse_mode=ParseMode.HTML)

    # STEP 2: OTP Code
    elif step == "WAITING_OTP":
        otp_code = text.replace(" ", "").replace("-", "")
        if not otp_code.isdigit():
            return await update.message.reply_text(
                "❌ <b>Code must contain only digits.</b>\n"
                "Example: <code>1 2 3 4 5</code> or <code>12345</code>\n\n"
                "<i>Send /cancel_login to abort.</i>",
                parse_mode=ParseMode.HTML
            )

        temp_client = temp_login_clients.get(user_id)
        if not temp_client:
            await clean_temp_client(user_id)
            return await update.message.reply_text("⚠️ <b>Login session expired.</b> Please restart with <code>/login_phone</code>.", parse_mode=ParseMode.HTML)

        status_msg = await update.message.reply_text("⏳ <i>Verifying authentication code...</i>", parse_mode=ParseMode.HTML)

        try:
            await temp_client.sign_in(
                phone=state["phone"],
                code=otp_code,
                phone_code_hash=state["phone_code_hash"]
            )

            # Successfully signed in without 2FA
            session_str = str(temp_client.session.save())
            me = await temp_client.get_me()
            await clean_temp_client(user_id)

            # Save session
            save_assistant_session(session_str, me)

            # Hot-reload assistant
            from features.voice_chat import reload_telethon_client
            ok, res = await reload_telethon_client(session_str)

            name = html.escape(getattr(me, "first_name", "") + (" " + me.last_name if getattr(me, "last_name", None) else ""))
            username = f"@{me.username}" if me.username else "<i>None</i>"
            phone = f"+{me.phone}" if getattr(me, "phone", None) else "<i>Hidden</i>"
            vc_status = "🟢 Active & Monitoring" if ok else f"⚠️ Start issue: {res}"

            await status_msg.edit_text(
                "🎉 <b><u>ASSISTANT LOGGED IN SUCCESSFULLY!</u></b>\n\n"
                f"👤 <b>Account:</b> <code>{name}</code>\n"
                f"🆔 <b>User ID:</b> <code>{me.id}</code>\n"
                f"🔗 <b>Username:</b> {username}\n"
                f"📱 <b>Phone:</b> <code>{phone}</code>\n\n"
                f"🎙 <b>VC Monitor:</b> <code>{vc_status}</code>\n\n"
                "📋 <b>Your Telethon String Session (Backup):</b>\n"
                f"<code>{session_str}</code>\n\n"
                "✅ <i>Assistant is now connected and ready for /vcbanall and /vcmuteall!</i>",
                parse_mode=ParseMode.HTML
            )

        except SessionPasswordNeededError:
            login_states[user_id]["step"] = "WAITING_2FA"
            await status_msg.edit_text(
                "🔐 <b><u>TWO-STEP VERIFICATION (2FA) DETECTED</u></b>\n\n"
                "Your account is protected with a Telegram Cloud Password.\n"
                "Please enter your <b>2FA Password</b> to complete the login.\n\n"
                "<i>Send /cancel_login to abort.</i>",
                parse_mode=ParseMode.HTML
            )
        except (PhoneCodeInvalidError, PhoneCodeExpiredError) as pe:
            await status_msg.edit_text(
                f"❌ <b>Invalid or Expired Code:</b> <code>{pe}</code>\n\n"
                "Please check the code and try again, or send /cancel_login to restart.",
                parse_mode=ParseMode.HTML
            )
        except Exception as e:
            logger.error(f"Error checking OTP for user {user_id}: {e}", exc_info=True)
            await clean_temp_client(user_id)
            await status_msg.edit_text(f"❌ <b>Login error:</b> <code>{e}</code>", parse_mode=ParseMode.HTML)

    # STEP 3: 2FA Password
    elif step == "WAITING_2FA":
        password = text
        temp_client = temp_login_clients.get(user_id)
        if not temp_client:
            await clean_temp_client(user_id)
            return await update.message.reply_text("⚠️ <b>Login session expired.</b> Please restart with <code>/login_phone</code>.", parse_mode=ParseMode.HTML)

        status_msg = await update.message.reply_text("⏳ <i>Verifying 2FA password...</i>", parse_mode=ParseMode.HTML)

        try:
            await temp_client.sign_in(password=password)

            # Successfully logged in with 2FA
            session_str = str(temp_client.session.save())
            me = await temp_client.get_me()
            await clean_temp_client(user_id)

            # Save session
            save_assistant_session(session_str, me)

            # Hot-reload assistant
            from features.voice_chat import reload_telethon_client
            ok, res = await reload_telethon_client(session_str)

            name = html.escape(getattr(me, "first_name", "") + (" " + me.last_name if getattr(me, "last_name", None) else ""))
            username = f"@{me.username}" if me.username else "<i>None</i>"
            phone = f"+{me.phone}" if getattr(me, "phone", None) else "<i>Hidden</i>"
            vc_status = "🟢 Active & Monitoring" if ok else f"⚠️ Start issue: {res}"

            await status_msg.edit_text(
                "🎉 <b><u>ASSISTANT LOGGED IN SUCCESSFULLY!</u></b>\n\n"
                f"👤 <b>Account:</b> <code>{name}</code>\n"
                f"🆔 <b>User ID:</b> <code>{me.id}</code>\n"
                f"🔗 <b>Username:</b> {username}\n"
                f"📱 <b>Phone:</b> <code>{phone}</code>\n\n"
                f"🎙 <b>VC Monitor:</b> <code>{vc_status}</code>\n\n"
                "📋 <b>Your Telethon String Session (Backup):</b>\n"
                f"<code>{session_str}</code>\n\n"
                "✅ <i>Assistant is now connected and ready for /vcbanall and /vcmuteall!</i>",
                parse_mode=ParseMode.HTML
            )

        except PasswordHashInvalidError:
            await status_msg.edit_text(
                "❌ <b>Incorrect 2FA Password.</b>\n"
                "Please verify your password and try again, or send /cancel_login to abort.",
                parse_mode=ParseMode.HTML
            )
        except Exception as e:
            logger.error(f"2FA login error for user {user_id}: {e}", exc_info=True)
            await clean_temp_client(user_id)
            await status_msg.edit_text(f"❌ <b>2FA verification error:</b> <code>{e}</code>", parse_mode=ParseMode.HTML)


def get_assistant_handlers():
    """Return all Telegram handlers for assistant management."""
    return [
        CommandHandler(["login", "login_session"], login_session_command),
        CommandHandler(["login_phone", "gen_session", "generate"], login_phone_command),
        CommandHandler("cancel_login", cancel_login_command),
        CommandHandler(["assistant", "userbot", "myassistant"], assistant_command),
        CommandHandler(["logout_assistant", "logout_userbot"], logout_assistant_command),
        MessageHandler(
            filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND & in_login_state_filter,
            interactive_login_message_handler
        ),
    ]
