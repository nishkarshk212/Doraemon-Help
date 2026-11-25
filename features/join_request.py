import logging
import time
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ChatJoinRequestHandler, CommandHandler, CallbackQueryHandler
from telegram.constants import ParseMode
from managers.settings_manager_mongo import get_chat_settings, update_chat_setting
from managers.user_manager_mongo import is_user_admin
from core.config import colored_button, log_to_channel, send_bot_response

# In-memory tracking of approved join requests: (chat_id, user_id) -> timestamp
_APPROVED_JOIN_REQUESTS: dict[tuple[int, int], float] = {}

def record_approved_join_request(chat_id: int, user_id: int):
    """Record that a user's join request was approved so captcha can be bypassed upon joining."""
    now = time.time()
    # Clean up entries older than 30 minutes
    to_delete = [k for k, t in _APPROVED_JOIN_REQUESTS.items() if now - t > 1800]
    for k in to_delete:
        _APPROVED_JOIN_REQUESTS.pop(k, None)
    _APPROVED_JOIN_REQUESTS[(chat_id, user_id)] = now
    logging.info(f"Recorded approved join request for user {user_id} in chat {chat_id}")

def is_recent_approved_join_request(chat_id: int, user_id: int, max_age_seconds: int = 1800) -> bool:
    """Check if user joined via an approved join request within the last max_age_seconds."""
    now = time.time()
    ts = _APPROVED_JOIN_REQUESTS.get((chat_id, user_id))
    if ts and (now - ts <= max_age_seconds):
        return True
    return False


def _is_dm_blocked_error(err: str) -> bool:
    """Return True if the error indicates the bot cannot initiate a DM with the user.

    Covers: Forbidden, can't initiate conversation, chat not found,
    Bad Request variants, user is deleted/deactivated, etc.
    """
    if not err:
        return False
    low = err.lower()
    markers = [
        "forbidden",
        "can't initiate",
        "cannot initiate",
        "initiate conversation",
        "chat not found",
        "chat_id is empty",
        "user is deactivated",
        "user is deleted",
        "user not found",
        "peer_id invalid",
        "bot can't send",
        "bot cannot initiate",
        "have no access",
        "not started the bot",
        "blocked",
    ]
    return any(m in low for m in markers)

async def _send_dm_multi_try(context, target_user_id, text, parse_mode, group_chat, user_obj=None, reply_markup=None):
    """Try to deliver a DM to a user via Bot API only.

    Limitation (Telegram rule): Bot API can only DM users who have already
    pressed /start on the bot, OR within 10 minutes after their join request
    is approved. Users who have not started the bot will not receive
    pending notifications (the message is silently skipped).

    Note: Telethon (user-account) is intentionally NOT used here so messages
    come only from the bot, and no group fallback is used either.

    Returns: (method_used: str | None, error_log: list[str])
    """
    errors = []
    group_title = getattr(group_chat, "title", "the group") if group_chat else "the group"

    # --- Attempt 1: Bot API DM only (no user-account delivery) ---
    try:
        await context.bot.send_message(
            chat_id=target_user_id,
            text=text,
            parse_mode=parse_mode,
            reply_markup=reply_markup,
        )
        logging.info(f"✅ DM to user {target_user_id} via Bot API (group: {group_title})")
        return "bot_api", errors
    except Exception as e1:
        err1 = str(e1)
        errors.append(f"Bot API: {err1}")
        logging.info(f"ℹ️  Bot API DM attempt failed for user {target_user_id}: {err1[:120]}")

    return None, errors

async def get_group_link(group_chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> str | None:
    """Helper to retrieve or export invite link for a group."""
    try:
        group_chat = await context.bot.get_chat(group_chat_id)
        if getattr(group_chat, 'username', None):
            return f"https://t.me/{group_chat.username}"
        elif getattr(group_chat, 'invite_link', None):
            return group_chat.invite_link
        else:
            try:
                return await context.bot.export_chat_invite_link(group_chat_id)
            except Exception as e:
                logging.warning(f"Could not export invite link for group {group_chat_id}: {e}")
                raw_id = abs(group_chat_id)
                stripped_id = raw_id - 1000000000000 if raw_id > 1000000000000 else raw_id
                return f"https://t.me/c/{stripped_id}/1"
    except Exception as e:
        logging.warning(f"Could not get group link for {group_chat_id}: {e}")
        return None

async def send_approval_dm(target_user_id: int, user_obj, group_chat, context: ContextTypes.DEFAULT_TYPE):
    """Sends join request approval confirmation message to user's private chat DM with direct group link."""
    try:
        group_title_html = (group_chat.title or "the group").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        group_chat_id = getattr(group_chat, 'id', None)
        
        group_url = await get_group_link(group_chat_id, context) if group_chat_id else None

        if group_url:
            group_mention = f"<a href=\"{group_url}\">{group_title_html}</a>"
        else:
            group_mention = f"<b>{group_title_html}</b>"

        approval_text = (
            f"<tg-emoji emoji-id=\"6242354082741230982\">🐺</tg-emoji>  ʏᴏᴜʀ ʀᴇQᴜᴇꜱᴛ ʙᴇ ᴀᴘᴘʀᴏᴠᴇᴅ ʙʏ ɢʀᴏᴜᴘ ᴀᴅᴍɪɴ\n"
            f"<tg-emoji emoji-id=\"5280555367221197240\">😽</tg-emoji>  ᴡᴇʟᴄᴏᴍᴇ ᴛᴏ ᴏᴜʀ {group_mention}\n"
            f"<tg-emoji emoji-id=\"5364125616801073577\">✈️</tg-emoji>  ᴇɴJᴏʏ ʏᴏᴜʀ ꜱᴛᴀʏ!"
        )

        reply_markup = None
        if group_url:
            reply_markup = InlineKeyboardMarkup([
                [InlineKeyboardButton(colored_button("🚀 Enter Group", "blue"), url=group_url)]
            ])

        method, errs = await _send_dm_multi_try(
            context, target_user_id, approval_text, ParseMode.HTML, group_chat, user_obj, reply_markup=reply_markup
        )

        if method is None:
            logging.error(
                f"❌ ALL delivery methods failed for approval DM to user {target_user_id}:\n  - "
                + "\n  - ".join(errs)
            )
        else:
            logging.info(f"🎟️  Approval DM delivered via [{method}] to user {target_user_id}")

    except Exception as ex:
        logging.error(f"Error building approval DM for user {target_user_id}: {ex}", exc_info=True)

async def handle_chat_join_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles incoming chat join requests for a group."""
    join_req = update.chat_join_request
    if not join_req:
        return

    chat = join_req.chat
    user = join_req.from_user
    chat_id = chat.id
    user_id = user.id

    settings = get_chat_settings(chat_id)
    # Check if join requests feature is enabled
    is_enabled = settings.get("join_request_enabled", True)
    mode = settings.get("join_request_mode", "manual").lower()

    if not is_enabled or mode in ["off", "disabled"]:
        logging.info(f"Join Request from user {user_id} in chat {chat_id} skipped (feature disabled/off)")
        return

    logging.info(f"Join Request from user {user_id} ({user.first_name}) in chat {chat_id} (mode: {mode})")

    if mode == "accept":
        try:
            record_approved_join_request(chat_id, user_id)
            await context.bot.approve_chat_join_request(chat_id=chat_id, user_id=user_id)
            logging.info(f"Auto-approved join request for user {user_id} in chat {chat_id}")
            await log_to_channel(context, f"✅ <b>Auto-Approved Join Request</b>\nUser: {user.mention_html()} (<code>{user_id}</code>)\nGroup: <b>{chat.title}</b>")
            await send_approval_dm(user_id, user, chat, context)
        except Exception as e:
            logging.error(f"Error approving join request for {user_id}: {e}", exc_info=True)

    elif mode == "decline":
        try:
            await context.bot.decline_chat_join_request(chat_id=chat_id, user_id=user_id)
            logging.info(f"Auto-declined join request for user {user_id} in chat {chat_id}")
            await log_to_channel(context, f"❌ <b>Auto-Declined Join Request</b>\nUser: {user.mention_html()} (<code>{user_id}</code>)\nGroup: <b>{chat.title}</b>")
        except Exception as e:
            logging.error(f"Error declining join request for {user_id}: {e}", exc_info=True)

    elif mode == "captcha":
        from features.captcha import start_join_request_captcha
        await start_join_request_captcha(join_req, context)
        return

    else:
        # --- MANUAL MODE ---
        # Send approval card in the group for admins
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(colored_button("✅ Accept", "blue"), callback_data=f"joinreq_acc_{chat_id}_{user_id}"),
                InlineKeyboardButton(colored_button("❌ Decline", "blue"), callback_data=f"joinreq_dec_{chat_id}_{user_id}")
            ]
        ])
        msg_text = (
            f"<tg-emoji emoji-id=\"6242354082741230982\">🐺</tg-emoji>  <b>New Join Request</b>\n\n"
            f"<tg-emoji emoji-id=\"5280555367221197240\">😽</tg-emoji>  <b>User:</b> {user.mention_html()} (<code>{user_id}</code>)\n"
            f"<tg-emoji emoji-id=\"5364125616801073577\">✈️</tg-emoji>  <b>Group:</b> {chat.title}\n\n"
            f"Admins, please accept or decline this request."
        )
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=msg_text,
                reply_markup=keyboard,
                parse_mode=ParseMode.HTML
            )
        except Exception as e:
            logging.error(f"Error sending manual join request alert in chat {chat_id}: {e}", exc_info=True)

        # --- Send Pending notification to user ---
        group_title_html = (chat.title or "the group").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        group_mention = f"@{chat.username}" if getattr(chat, 'username', None) else f"<b>{group_title_html}</b>"
        pending_text = (
            f"<tg-emoji emoji-id=\"6242354082741230982\">🐺</tg-emoji>  ᴘʟᴇᴀꜱᴇ ᴋᴇᴇᴘ ᴘᴀᴛɪᴇɴᴄᴇ.\n"
            f"<tg-emoji emoji-id=\"5280555367221197240\">😽</tg-emoji>  ʏᴏᴜʀ ʀᴇQᴜᴇꜱᴛ ɪꜱ ᴀᴘᴘʀᴏᴠᴇᴅ ꜱᴏᴏɴ ʙʏ ᴀᴅᴍɪɴ ᴏꜰ {group_mention}\n"
            f"<tg-emoji emoji-id=\"5364125616801073577\">✈️</tg-emoji>  ᴡᴇ'ʟʟ ɴᴏᴛɪꜰʏ ʏᴏᴜ ᴏɴᴄᴇ ᴀᴘᴘʀᴏᴠᴇᴅ!"
        )
        method, errs = await _send_dm_multi_try(
            context, user_id, pending_text, ParseMode.HTML, chat, user
        )
        if method is None:
            logging.warning(
                f"⚠️  ALL delivery methods failed for PENDING DM to user {user_id}:\n  - "
                + "\n  - ".join(errs)
            )
        else:
            logging.info(f"⏳ Pending DM delivered via [{method}] to user {user_id} for chat {chat_id}")


async def join_request_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Command /joinreq to configure group join request mode and enable/disable toggle."""
    if not update.effective_chat or not update.effective_user:
        return

    chat_id = update.effective_chat.id
    user_id = update.effective_user.id

    if update.effective_chat.type == "private":
        await update.message.reply_text("This command can only be used in groups.")
        return

    if not await is_user_admin(chat_id, user_id, context):
        await update.message.reply_text("❌ Admin only command.")
        return

    args = context.args
    settings = get_chat_settings(chat_id)
    is_enabled = settings.get("join_request_enabled", True)
    current_mode = settings.get("join_request_mode", "manual").lower()

    if args:
        sub = args[0].lower()
        if sub in ["enable", "on", "active"]:
            update_chat_setting(chat_id, "join_request_enabled", True)
            await send_bot_response(update, context, "✅ Join Request handling is now <b>ENABLED</b>.", parse_mode=ParseMode.HTML)
            return
        elif sub in ["disable", "off", "deactivate"]:
            update_chat_setting(chat_id, "join_request_enabled", False)
            await send_bot_response(update, context, "❌ Join Request handling is now <b>DISABLED</b>.", parse_mode=ParseMode.HTML)
            return
        elif sub in ["accept", "autoaccept", "approve"]:
            update_chat_setting(chat_id, "join_request_enabled", True)
            update_chat_setting(chat_id, "join_request_mode", "accept")
            await send_bot_response(update, context, "✅ Join request mode set to: Auto-Accept (Approves automatically & sends DM welcome).")
            return
        elif sub in ["decline", "autodecline", "reject"]:
            update_chat_setting(chat_id, "join_request_enabled", True)
            update_chat_setting(chat_id, "join_request_mode", "decline")
            await send_bot_response(update, context, "❌ Join request mode set to: Auto-Decline (Declines all join requests).")
            return
        elif sub in ["captcha", "bot", "verify"]:
            update_chat_setting(chat_id, "join_request_enabled", True)
            update_chat_setting(chat_id, "join_request_mode", "captcha")
            await send_bot_response(update, context, "🤖 Join request mode set to: <b>Captcha in PM</b> (DMs captcha to user; auto-approves once solved).", parse_mode=ParseMode.HTML)
            return
        elif sub in ["manual", "ask"]:
            update_chat_setting(chat_id, "join_request_enabled", True)
            update_chat_setting(chat_id, "join_request_mode", "manual")
            await send_bot_response(update, context, "⚙️ Join request mode set to: Manual (Sends approval cards for admins).")
            return

    status_btn_text = f"Join Requests: {'✅ Enabled' if is_enabled else '❌ Disabled'}"
    accept_label = "✅ Auto-Accept" + (" ✓" if current_mode == "accept" else "")
    decline_label = "❌ Auto-Decline" + (" ✓" if current_mode == "decline" else "")
    captcha_label = "🤖 Captcha in PM" + (" ✓" if current_mode == "captcha" else "")
    manual_label = "⚙️ Manual Approval" + (" ✓" if current_mode == "manual" else "")
    off_label = "🚫 Turn Off" + (" ✓" if not is_enabled or current_mode == "off" else "")

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(colored_button(status_btn_text, "blue"), callback_data="joinreq_toggle_enable")
        ],
        [
            InlineKeyboardButton(colored_button(accept_label, "blue"), callback_data="joinreq_set_accept"),
            InlineKeyboardButton(colored_button(decline_label, "blue"), callback_data="joinreq_set_decline"),
        ],
        [
            InlineKeyboardButton(colored_button(captcha_label, "blue"), callback_data="joinreq_set_captcha"),
            InlineKeyboardButton(colored_button(manual_label, "blue"), callback_data="joinreq_set_manual"),
        ],
        [
            InlineKeyboardButton(colored_button(off_label, "blue"), callback_data="joinreq_set_off")
        ]
    ])

    status_text = "ENABLED" if is_enabled else "DISABLED"
    text = (
        f"⚙️ <b>Join Request Settings</b>\n\n"
        f"Status: <b>{status_text}</b>\n"
        f"Current Mode: <b>{current_mode.upper()}</b>\n\n"
        f"Choose how the bot should handle join requests for this group:\n"
        f"• <b>Enable/Disable:</b> Toggle entire join request processing on or off.\n"
        f"• <b>Auto-Accept:</b> Automatically approves join requests and sends welcome DM.\n"
        f"• <b>Auto-Decline:</b> Automatically declines join requests.\n"
        f"• <b>Captcha in PM:</b> DMs verification captcha to user; approves automatically once solved.\n"
        f"• <b>Manual:</b> Prompts admins in group with Accept/Decline buttons.\n"
        f"• <b>Turn Off:</b> Ignore incoming join requests."
    )

    await update.message.reply_text(text, reply_markup=keyboard, parse_mode=ParseMode.HTML)


async def can_user_ban(chat_id: int, user_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Check if user is group creator, bot owner, or admin with can_restrict_members permission."""
    from core.config import OWNER_ID
    if user_id == OWNER_ID:
        return True
    try:
        member = await context.bot.get_chat_member(chat_id, user_id)
        if member.status == "creator":
            return True
        if member.status == "administrator":
            return getattr(member, "can_restrict_members", False) or False
    except Exception as e:
        logging.error(f"Error checking ban permission for user {user_id} in chat {chat_id}: {e}")
    return False


async def join_request_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback query handler for join request buttons."""
    query = update.callback_query
    if not query or not query.data:
        return

    data = query.data
    user_id = query.from_user.id
    chat_id = update.effective_chat.id if update.effective_chat else None

    # Handle enable/disable toggle button
    if data == "joinreq_toggle_enable":
        if chat_id and await can_user_ban(chat_id, user_id, context):
            settings = get_chat_settings(chat_id)
            new_val = not settings.get("join_request_enabled", True)
            update_chat_setting(chat_id, "join_request_enabled", new_val)
            await query.answer(f"Join Request feature set to {'ENABLED' if new_val else 'DISABLED'}!")

            is_enabled = new_val
            current_mode = settings.get("join_request_mode", "manual").lower()

            status_btn_text = f"Join Requests: {'✅ Enabled' if is_enabled else '❌ Disabled'}"
            accept_label = "✅ Auto-Accept" + (" ✓" if current_mode == "accept" else "")
            decline_label = "❌ Auto-Decline" + (" ✓" if current_mode == "decline" else "")
            captcha_label = "🤖 Captcha in PM" + (" ✓" if current_mode == "captcha" else "")
            manual_label = "⚙️ Manual Approval" + (" ✓" if current_mode == "manual" else "")
            off_label = "🚫 Turn Off" + (" ✓" if not is_enabled or current_mode == "off" else "")

            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(colored_button(status_btn_text, "blue"), callback_data="joinreq_toggle_enable")
                ],
                [
                    InlineKeyboardButton(colored_button(accept_label, "blue"), callback_data="joinreq_set_accept"),
                    InlineKeyboardButton(colored_button(decline_label, "blue"), callback_data="joinreq_set_decline"),
                ],
                [
                    InlineKeyboardButton(colored_button(captcha_label, "blue"), callback_data="joinreq_set_captcha"),
                    InlineKeyboardButton(colored_button(manual_label, "blue"), callback_data="joinreq_set_manual"),
                ],
                [
                    InlineKeyboardButton(colored_button(off_label, "blue"), callback_data="joinreq_set_off")
                ]
            ])
            status_text = "ENABLED" if is_enabled else "DISABLED"
            text = (
                f"⚙️ <b>Join Request Settings</b>\n\n"
                f"Status: <b>{status_text}</b>\n"
                f"Current Mode: <b>{current_mode.upper()}</b>\n\n"
                f"Choose how the bot should handle join requests for this group:\n"
                f"• <b>Enable/Disable:</b> Toggle entire join request processing on or off.\n"
                f"• <b>Auto-Accept:</b> Automatically approves join requests.\n"
                f"• <b>Auto-Decline:</b> Automatically declines join requests.\n"
                f"• <b>Captcha in PM:</b> DMs verification captcha to user; approves automatically once solved.\n"
                f"• <b>Manual:</b> Prompts admins in group with Accept/Decline buttons.\n"
                f"• <b>Turn Off:</b> Ignore incoming join requests."
            )
            try:
                await query.edit_message_text(text, reply_markup=keyboard, parse_mode=ParseMode.HTML)
            except Exception:
                pass
        else:
            await query.answer("❌ Only admins with 'Ban Users' permission can change settings.", show_alert=True)
        return

    # Handle settings mode toggles
    if data.startswith("joinreq_set_"):
        new_mode = data.replace("joinreq_set_", "")
        if chat_id and await can_user_ban(chat_id, user_id, context):
            if new_mode == "off":
                update_chat_setting(chat_id, "join_request_enabled", False)
                update_chat_setting(chat_id, "join_request_mode", "off")
            else:
                update_chat_setting(chat_id, "join_request_enabled", True)
                update_chat_setting(chat_id, "join_request_mode", new_mode)

            await query.answer(f"Join request mode updated to {new_mode.upper()}!")

            settings = get_chat_settings(chat_id)
            is_enabled = settings.get("join_request_enabled", True)
            current_mode = settings.get("join_request_mode", "manual").lower()

            status_btn_text = f"Join Requests: {'✅ Enabled' if is_enabled else '❌ Disabled'}"
            accept_label = "✅ Auto-Accept" + (" ✓" if current_mode == "accept" else "")
            decline_label = "❌ Auto-Decline" + (" ✓" if current_mode == "decline" else "")
            captcha_label = "🤖 Captcha in PM" + (" ✓" if current_mode == "captcha" else "")
            manual_label = "⚙️ Manual Approval" + (" ✓" if current_mode == "manual" else "")
            off_label = "🚫 Turn Off" + (" ✓" if not is_enabled or current_mode == "off" else "")

            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(colored_button(status_btn_text, "blue"), callback_data="joinreq_toggle_enable")
                ],
                [
                    InlineKeyboardButton(colored_button(accept_label, "blue"), callback_data="joinreq_set_accept"),
                    InlineKeyboardButton(colored_button(decline_label, "blue"), callback_data="joinreq_set_decline"),
                ],
                [
                    InlineKeyboardButton(colored_button(captcha_label, "blue"), callback_data="joinreq_set_captcha"),
                    InlineKeyboardButton(colored_button(manual_label, "blue"), callback_data="joinreq_set_manual"),
                ],
                [
                    InlineKeyboardButton(colored_button(off_label, "blue"), callback_data="joinreq_set_off")
                ]
            ])
            status_text = "ENABLED" if is_enabled else "DISABLED"
            text = (
                f"⚙️ <b>Join Request Settings</b>\n\n"
                f"Status: <b>{status_text}</b>\n"
                f"Current Mode: <b>{current_mode.upper()}</b>\n\n"
                f"Choose how the bot should handle join requests for this group:\n"
                f"• <b>Enable/Disable:</b> Toggle entire join request processing on or off.\n"
                f"• <b>Auto-Accept:</b> Automatically approves join requests.\n"
                f"• <b>Auto-Decline:</b> Automatically declines join requests.\n"
                f"• <b>Captcha in PM:</b> DMs verification captcha to user; approves automatically once solved.\n"
                f"• <b>Manual:</b> Prompts admins in group with Accept/Decline buttons.\n"
                f"• <b>Turn Off:</b> Ignore incoming join requests."
            )
            try:
                await query.edit_message_text(text, reply_markup=keyboard, parse_mode=ParseMode.HTML)
            except Exception:
                pass
        else:
            await query.answer("❌ Only admins with 'Ban Users' permission can change settings.", show_alert=True)
        return

    # Handle manual accept/decline buttons: joinreq_acc_{target_chat_id}_{target_user_id}
    if data.startswith("joinreq_acc_") or data.startswith("joinreq_dec_"):
        try:
            # Format: joinreq_acc_<chat_id>_<user_id>  (chat_id may be negative like -1003905614604)
            # Use rsplit with maxsplit=2 so the final two numeric segments are always separated cleanly
            prefix_and_action, target_chat_id_str, target_user_id_str = data.rsplit("_", 2)
            action = prefix_and_action.split("_")[-1]  # 'acc' or 'dec'
            target_chat_id = int(target_chat_id_str)
            target_user_id = int(target_user_id_str)
        except Exception as pe:
            await query.answer(f"❌ Invalid button data: {pe}", show_alert=True)
            logging.error(f"Failed to parse joinreq callback data '{data}': {pe}")
            return

        # Acknowledge the click early so Telegram doesn't show "query expired" toast
        try:
            await query.answer()
        except Exception:
            pass

        if not await can_user_ban(target_chat_id, user_id, context):
            try:
                await query.answer("❌ Only admins with 'Ban Users' permission can respond to join requests.", show_alert=True)
            except Exception:
                pass
            return

        if action == "acc":
            # Build user_obj outside the approve try block so we can still send DM even if edit_message_text fails
            user_obj = None
            group_chat = None
            try:
                u_chat = await context.bot.get_chat(target_user_id)
                from telegram import User
                user_obj = User(
                    id=target_user_id,
                    first_name=u_chat.first_name or "User",
                    last_name=u_chat.last_name,
                    username=u_chat.username,
                    is_bot=False
                )
            except Exception as ue:
                logging.warning(f"Could not fetch user chat for {target_user_id} (will use None for DM): {ue}")

            try:
                group_chat = await context.bot.get_chat(target_chat_id)
            except Exception as ge:
                logging.warning(f"Could not fetch group chat for {target_chat_id}: {ge}")

            try:
                record_approved_join_request(target_chat_id, target_user_id)
                await context.bot.approve_chat_join_request(chat_id=target_chat_id, user_id=target_user_id)
                # Try to update the card UI — non-fatal on failure
                try:
                    if user_obj:
                        await query.edit_message_text(
                            f"✅ Approved join request for {user_obj.mention_html()} (<code>{target_user_id}</code>).",
                            parse_mode=ParseMode.HTML
                        )
                    else:
                        await query.edit_message_text(
                            f"✅ Approved join request for user <code>{target_user_id}</code>.",
                            parse_mode=ParseMode.HTML
                        )
                except Exception:
                    pass
            except Exception as e:
                try:
                    await query.answer(f"Failed to approve: {e}", show_alert=True)
                except Exception:
                    pass
                logging.error(f"Approve join request failed for {target_user_id} in {target_chat_id}: {e}", exc_info=True)
                return  # Don't send DM if actual approval failed

            # --- Send Approval DM (outside try blocks — ALWAYS attempted) ---
            if group_chat is not None:
                # If user_obj still None, construct a minimal one (mention_html just needs id for link)
                if user_obj is None:
                    from telegram import User
                    user_obj = User(id=target_user_id, first_name="User", is_bot=False)
                await send_approval_dm(target_user_id, user_obj, group_chat, context)

        elif action == "dec":
            user_obj = None
            try:
                u_chat = await context.bot.get_chat(target_user_id)
                from telegram import User
                user_obj = User(
                    id=target_user_id,
                    first_name=u_chat.first_name or "User",
                    last_name=u_chat.last_name,
                    username=u_chat.username,
                    is_bot=False
                )
            except Exception as ue:
                logging.debug(f"Could not fetch user chat for decline UI: {ue}")

            try:
                await context.bot.decline_chat_join_request(chat_id=target_chat_id, user_id=target_user_id)
                try:
                    if user_obj:
                        await query.edit_message_text(
                            f"❌ Declined join request for {user_obj.mention_html()} (<code>{target_user_id}</code>).",
                            parse_mode=ParseMode.HTML
                        )
                    else:
                        await query.edit_message_text(
                            f"❌ Declined join request for user <code>{target_user_id}</code>.",
                            parse_mode=ParseMode.HTML
                        )
                except Exception:
                    pass
            except Exception as e:
                try:
                    await query.answer(f"Failed to decline: {e}", show_alert=True)
                except Exception:
                    pass
                logging.error(f"Decline join request failed for {target_user_id} in {target_chat_id}: {e}", exc_info=True)


def get_join_request_handlers():
    """Returns handlers for join requests."""
    return [
        ChatJoinRequestHandler(handle_chat_join_request),
        CommandHandler(["joinreq", "joinrequest"], join_request_command),
        CallbackQueryHandler(join_request_callback, pattern=r"^joinreq_")
    ]
