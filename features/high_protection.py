"""
High Protection & Security Suite for Doraemon Help Bot
Features:
- Anti-Raid / Raid Lockdown (Velocity-based mass-join detection & auto-mute/kick)
- Anti-Channel (Sender Chat Shield & auto-ban external channels)
- Anti-Mention (Mass tag / mention raid filter)
- Anti-Duplicate (Repetitive spam flood detection & auto-mute)
- Anti-Zalgo & Invisible Unicode Shield
- Master Shield Mode (/shield on | /shield off | /lockdown)
"""

import time
import html
import hashlib
import logging
import unicodedata
from telegram import Update, ChatPermissions, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode, ChatMemberStatus
from telegram.ext import ContextTypes, CommandHandler, MessageHandler, filters
from managers.settings_manager_mongo import get_chat_settings, update_chat_setting
from managers.user_manager_mongo import is_user_admin
from core.config import OWNER_ID, log_to_channel, send_bot_response, colored_button, to_small_caps

logger = logging.getLogger(__name__)

# In-memory tracking structures
RAID_JOIN_TRACKER = {}      # {chat_id: [timestamps]}
RAID_LOCKDOWN_STATUS = {}   # {chat_id: expiry_timestamp}
DUPLICATE_MSG_TRACKER = {}  # {(chat_id, user_id): [(text_hash, timestamp)]}
ANONYMOUS_ADMIN_ID = 1087968824

def _clean_trackers():
    """Periodic memory cleanup of old tracker keys."""
    now = time.time()
    # Clean join tracker
    for cid in list(RAID_JOIN_TRACKER.keys()):
        RAID_JOIN_TRACKER[cid] = [t for t in RAID_JOIN_TRACKER[cid] if now - t <= 60]
        if not RAID_JOIN_TRACKER[cid]:
            RAID_JOIN_TRACKER.pop(cid, None)
    # Clean duplicate msg tracker
    for key in list(DUPLICATE_MSG_TRACKER.keys()):
        DUPLICATE_MSG_TRACKER[key] = [item for item in DUPLICATE_MSG_TRACKER[key] if now - item[1] <= 30]
        if not DUPLICATE_MSG_TRACKER[key]:
            DUPLICATE_MSG_TRACKER.pop(key, None)

async def clean_protection_trackers_job(context: ContextTypes.DEFAULT_TYPE = None):
    """JobQueue async callback for periodic memory cleanup."""
    _clean_trackers()

async def handle_anti_raid_join(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Detect mass join attacks and enforce immediate raid lockdown."""
    chat = update.effective_chat
    if not chat or chat.type not in ("group", "supergroup"):
        return

    chat_id = chat.id
    new_members = update.message.new_chat_members if update.message else []
    if not new_members:
        return

    settings = get_chat_settings(chat_id)
    anti_raid_enabled = settings.get("anti_raid_enabled", True) or settings.get("shield_mode_enabled", False)
    if not anti_raid_enabled:
        return

    now = time.time()
    threshold = settings.get("anti_raid_threshold", 5)
    window = settings.get("anti_raid_window", 10)
    action = settings.get("anti_raid_action", "mute")
    mute_time = settings.get("anti_raid_mute_time", 900)

    # Check if chat is already under raid lockdown
    is_locked = RAID_LOCKDOWN_STATUS.get(chat_id, 0) > now

    if is_locked:
        # Enforce lockdown penalty on incoming members
        for user in new_members:
            if user.is_bot:
                continue
            try:
                if action == "mute":
                    await context.bot.restrict_chat_member(
                        chat_id, user.id,
                        permissions=ChatPermissions(can_send_messages=False),
                        until_date=int(RA_lock := RAID_LOCKDOWN_STATUS[chat_id])
                    )
                elif action == "kick":
                    await context.bot.ban_chat_member(chat_id, user.id)
                    await context.bot.unban_chat_member(chat_id, user.id)
                elif action == "ban":
                    await context.bot.ban_chat_member(chat_id, user.id)
            except Exception as e:
                logger.error(f"[ANTI-RAID] Failed to enforce lockdown on user {user.id}: {e}")
        return

    # Track joins
    if chat_id not in RAID_JOIN_TRACKER:
        RAID_JOIN_TRACKER[chat_id] = []

    for _ in new_members:
        RAID_JOIN_TRACKER[chat_id].append(now)

    # Filter window
    RAID_JOIN_TRACKER[chat_id] = [t for t in RAID_JOIN_TRACKER[chat_id] if now - t <= window]

    if len(RAID_JOIN_TRACKER[chat_id]) >= threshold:
        # RAID TRIGGERED!
        expiry = now + mute_time
        RAID_LOCKDOWN_STATUS[chat_id] = expiry
        RAID_JOIN_TRACKER[chat_id].clear()

        # Punish current batch of joiners
        for user in new_members:
            if user.is_bot:
                continue
            try:
                if action == "mute":
                    await context.bot.restrict_chat_member(
                        chat_id, user.id,
                        permissions=ChatPermissions(can_send_messages=False),
                        until_date=int(expiry)
                    )
                elif action == "kick":
                    await context.bot.ban_chat_member(chat_id, user.id)
                    await context.bot.unban_chat_member(chat_id, user.id)
                elif action == "ban":
                    await context.bot.ban_chat_member(chat_id, user.id)
            except Exception as e:
                logger.error(f"[ANTI-RAID] Failed initial raid action on {user.id}: {e}")

        # Broadcast alert to group
        alert_text = (
            "🚨 <b>─── ᴀɴᴛɪ-ʀᴀɪᴅ ꜱʜɪᴇʟᴅ ᴀᴄᴛɪᴠᴀᴛᴇᴅ ───</b> 🚨\n\n"
            f"⚠️ <b>Mass Join Attack Detected:</b> <code>{threshold}+ accounts</code> joined in {window}s!\n"
            f"🔒 <b>Raid Lockdown Active:</b> All new joiners are automatically <b>{action.upper()}D</b> for 15 minutes.\n"
            "🛡️ <i>Admins can disable with</i> <code>/antiraid off</code> <i>or configure via</i> <code>/protection</code>."
        )
        try:
            await context.bot.send_message(chat_id, alert_text, parse_mode=ParseMode.HTML)
        except Exception:
            pass

        # Log event
        await log_to_channel(
            context,
            f"🚨 <b>Anti-Raid Activated</b> in chat <code>{chat_id}</code> ({chat.title})\n"
            f"Action: <b>{action.upper()}</b> | Duration: 15 min | Threshold: {threshold} in {window}s"
        )

async def handle_anti_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Detect and block messages sent as external Telegram Channels."""
    msg = update.effective_message
    if not msg or not msg.sender_chat:
        return

    chat = update.effective_chat
    if not chat or chat.type not in ("group", "supergroup"):
        return

    chat_id = chat.id
    sender_chat = msg.sender_chat

    # Anonymous admin sending as group itself -> allowed
    if sender_chat.id == chat_id:
        return

    # Linked discussion channel forwards -> allowed
    linked_id = getattr(chat, 'linked_chat_id', None)
    if linked_id and sender_chat.id == linked_id:
        return

    settings = get_chat_settings(chat_id)
    enabled = settings.get("anti_channel_enabled", False) or settings.get("shield_mode_enabled", False)
    if not enabled:
        return

    action = settings.get("anti_channel_action", "delete_ban")

    try:
        # 1. Delete channel message
        await msg.delete()
        # 2. Ban sender channel
        if "ban" in action:
            await context.bot.ban_chat_sender_chat(chat_id, sender_chat.id)

        channel_title = html.escape(sender_chat.title or "Channel")
        warn_msg = await context.bot.send_message(
            chat_id,
            f"🚫 <b>ᴀɴᴛɪ-ᴄʜᴀɴɴᴇʟ:</b> Messages from channel <b>{channel_title}</b> are blocked and banned.",
            parse_mode=ParseMode.HTML
        )
        # Auto-delete notice in 15 seconds
        if context.job_queue:
            context.job_queue.run_once(
                lambda ctx: warn_msg.delete(),
                15,
                chat_id=chat_id
            )
    except Exception as e:
        logger.error(f"[ANTI-CHANNEL] Failed to restrict sender_chat {sender_chat.id} in {chat_id}: {e}")

def _is_zalgo(text: str) -> bool:
    """Check if text contains excessive zero-width chars or zalgo stacked diacritics."""
    if not text:
        return False
    # Check zero-width characters
    zw_chars = sum(1 for c in text if c in ('\u200b', '\u200c', '\u200d', '\ufeff', '\u200e', '\u200f'))
    if zw_chars >= 8:
        return True
    # Check combining diacritics (Mn category)
    combining = sum(1 for c in text if unicodedata.category(c) == 'Mn')
    if len(text) > 10 and (combining / len(text)) > 0.35:
        return True
    return False

async def handle_high_protection_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Process mention spam, duplicate spam, and zalgo protection."""
    msg = update.effective_message
    if not msg:
        return

    chat = update.effective_chat
    if not chat or chat.type not in ("group", "supergroup"):
        return

    chat_id = chat.id
    user = update.effective_user
    if not user or user.is_bot:
        return

    user_id = user.id
    if user_id == OWNER_ID or user_id == ANONYMOUS_ADMIN_ID:
        return

    # Fast admin check (cached)
    if await is_user_admin(chat_id, user_id, context):
        return

    settings = get_chat_settings(chat_id)
    text = msg.text or msg.caption or ""
    now = time.time()

    # 1. Anti-Zalgo & Invisible Unicode Shield
    if settings.get("anti_zalgo_enabled", True) or settings.get("shield_mode_enabled", False):
        if _is_zalgo(text):
            try:
                await msg.delete()
                return
            except Exception:
                pass

    # 2. Mass-Mention Spam Protection
    if settings.get("mention_spam_enabled", True) or settings.get("shield_mode_enabled", False):
        limit = settings.get("mention_spam_limit", 5)
        entities = (msg.entities or ()) + (msg.caption_entities or ())
        mentions_count = sum(1 for e in entities if e.type in ("mention", "text_mention"))
        # Also count raw @mentions if text has plain mentions
        mentions_count = max(mentions_count, text.count("@"))

        if mentions_count >= limit:
            try:
                await msg.delete()
                action = settings.get("mention_spam_action", "delete_warn")
                user_mention = user.mention_html()
                if "mute" in action:
                    await context.bot.restrict_chat_member(
                        chat_id, user_id,
                        permissions=ChatPermissions(can_send_messages=False),
                        until_date=int(now + 600)
                    )
                    await context.bot.send_message(
                        chat_id,
                        f"🔇 {user_mention} has been muted for 10 minutes for mass-mention spam ({mentions_count} tags).",
                        parse_mode=ParseMode.HTML
                    )
                else:
                    await context.bot.send_message(
                        chat_id,
                        f"⚠️ {user_mention}, mass-mention spam is prohibited (Limit: {limit} mentions).",
                        parse_mode=ParseMode.HTML
                    )
                return
            except Exception as e:
                logger.error(f"[ANTI-MENTION] Action failed for user {user_id}: {e}")

    # 3. Duplicate Message Flood Protection
    if (settings.get("duplicate_spam_enabled", True) or settings.get("shield_mode_enabled", False)) and len(text.strip()) > 4:
        clean_norm = "".join(text.split()).lower()
        text_hash = hashlib.md5(clean_norm.encode("utf-8", "ignore")).hexdigest()
        limit = settings.get("duplicate_spam_limit", 3)
        window = settings.get("duplicate_spam_window", 10)

        key = (chat_id, user_id)
        if key not in DUPLICATE_MSG_TRACKER:
            DUPLICATE_MSG_TRACKER[key] = []

        DUPLICATE_MSG_TRACKER[key].append((text_hash, now))
        # Filter window
        DUPLICATE_MSG_TRACKER[key] = [item for item in DUPLICATE_MSG_TRACKER[key] if now - item[1] <= window]

        matches = sum(1 for item in DUPLICATE_MSG_TRACKER[key] if item[0] == text_hash)
        if matches >= limit:
            try:
                await msg.delete()
                DUPLICATE_MSG_TRACKER[key].clear()
                # Mute user for 5 minutes
                await context.bot.restrict_chat_member(
                    chat_id, user_id,
                    permissions=ChatPermissions(can_send_messages=False),
                    until_date=int(now + 300)
                )
                user_mention = user.mention_html()
                await context.bot.send_message(
                    chat_id,
                    f"🔇 {user_mention} has been muted for 5 minutes for repetitive duplicate message spam.",
                    parse_mode=ParseMode.HTML
                )
                return
            except Exception as e:
                logger.error(f"[DUPLICATE-SPAM] Action failed for {user_id}: {e}")

# ─────────────────────────────────────────────────────────────
# COMMANDS: /shield, /lockdown, /antiraid, /antichannel, /antimention
# ─────────────────────────────────────────────────────────────

async def shield_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """One-click master shield mode toggle."""
    chat = update.effective_chat
    user = update.effective_user
    if not chat or chat.type not in ("group", "supergroup") or not user:
        return

    chat_id = chat.id
    if not await is_user_admin(chat_id, user.id, context) and user.id != OWNER_ID:
        await send_bot_response(update, context, "Only group administrators can toggle Shield Mode.")
        return

    settings = get_chat_settings(chat_id)
    current_shield = settings.get("shield_mode_enabled", False)

    # Determine desired action
    sub = (context.args[0].lower() if context.args else None)
    if sub in ("on", "enable", "activate", "true"):
        turn_on = True
    elif sub in ("off", "disable", "deactivate", "false"):
        turn_on = False
    else:
        turn_on = not current_shield

    if turn_on:
        # Backup current settings before enabling full shield
        backup = {
            "anti_raid_enabled": settings.get("anti_raid_enabled", True),
            "anti_channel_enabled": settings.get("anti_channel_enabled", False),
            "bot_protection_enabled": settings.get("bot_protection_enabled", False),
            "link_spam_protection_enabled": settings.get("link_spam_protection_enabled", False),
            "forward_protection_enabled": settings.get("forward_protection_enabled", False),
            "antiflood_enabled": settings.get("antiflood_enabled", False),
            "nsfw_filter_enabled": settings.get("nsfw_filter_enabled", False),
            "clean_service_enabled": settings.get("clean_service_enabled", True),
        }
        update_chat_setting(chat_id, "shield_backup_settings", backup)
        update_chat_setting(chat_id, "shield_mode_enabled", True)
        update_chat_setting(chat_id, "anti_raid_enabled", True)
        update_chat_setting(chat_id, "anti_channel_enabled", True)
        update_chat_setting(chat_id, "bot_protection_enabled", True)
        update_chat_setting(chat_id, "link_spam_protection_enabled", True)
        update_chat_setting(chat_id, "forward_protection_enabled", True)
        update_chat_setting(chat_id, "antiflood_enabled", True)
        update_chat_setting(chat_id, "nsfw_filter_enabled", True)
        update_chat_setting(chat_id, "clean_service_enabled", True)

        card = (
            "🛡️ <b>─── [ ᴍᴀxɪᴍᴜᴍ ꜱʜɪᴇʟᴅ ʟᴏᴄᴋᴅᴏᴡɴ ] ───</b> 🛡️\n\n"
            f"<b>Group:</b> {html.escape(chat.title or 'This Group')}\n"
            "<b>Status:</b> 🟢 <b>HIGH PROTECTION ACTIVE</b>\n\n"
            "🔒 <b>Anti-Raid:</b> <code>ENABLED</code> (Velocity Mass-Join Shield)\n"
            "📢 <b>Anti-Channel:</b> <code>ENABLED</code> (Bans External Channel Senders)\n"
            "🤖 <b>Bot Protection:</b> <code>ENABLED</code> (Auto-Kicks Rogue Bots)\n"
            "🔗 <b>Link Protection:</b> <code>ENABLED</code> (Deletes URL & Invite Spam)\n"
            "🔄 <b>Forward Shield:</b> <code>ENABLED</code> (Blocks Channel Forwards)\n"
            "🌊 <b>Anti-Flood:</b> <code>ENABLED</code> (Strict 4 msgs / 3s)\n"
            "👥 <b>Anti-Mention:</b> <code>ENABLED</code> (Limits Mass Tags)\n"
            "🔞 <b>NSFW Filter:</b> <code>ENABLED</code> (AI Vision & Text Filter)\n\n"
            "<i>Your group is now fortified against raids and spam.</i>\n"
            "Turn off anytime with <code>/shield off</code>."
        )
    else:
        # Restore backup if available
        backup = settings.get("shield_backup_settings") or {}
        update_chat_setting(chat_id, "shield_mode_enabled", False)
        if backup:
            for k, v in backup.items():
                update_chat_setting(chat_id, k, v)
        update_chat_setting(chat_id, "shield_backup_settings", None)

        card = (
            "🛡️ <b>─── [ ꜱʜɪᴇʟᴅ ᴍᴏᴅᴇ ᴅᴇᴀᴄᴛɪᴠᴀᴛᴇᴅ ] ───</b> 🛡️\n\n"
            f"<b>Group:</b> {html.escape(chat.title or 'This Group')}\n"
            "<b>Status:</b> ⚪ <b>CUSTOM SETTINGS RESTORED</b>\n\n"
            "Group security has returned to your standard configuration."
        )

    btn = InlineKeyboardMarkup([[
        InlineKeyboardButton(colored_button("⚙️ Protection Settings", "blue"), callback_data="set_view_protection")
    ]])
    await update.message.reply_text(card, reply_markup=btn, parse_mode=ParseMode.HTML)

async def antiraid_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Toggle Anti-Raid velocity join protection."""
    chat = update.effective_chat
    user = update.effective_user
    if not chat or chat.type not in ("group", "supergroup") or not user:
        return
    chat_id = chat.id
    if not await is_user_admin(chat_id, user.id, context) and user.id != OWNER_ID:
        await send_bot_response(update, context, "Only group administrators can configure Anti-Raid.")
        return

    sub = context.args[0].lower() if context.args else None
    settings = get_chat_settings(chat_id)
    current = settings.get("anti_raid_enabled", True)

    if sub in ("on", "enable", "true"):
        new_val = True
    elif sub in ("off", "disable", "false"):
        new_val = False
    else:
        new_val = not current

    update_chat_setting(chat_id, "anti_raid_enabled", new_val)
    status_str = "🟢 <b>ENABLED</b>" if new_val else "🔴 <b>DISABLED</b>"
    text = (
        f"🚨 <b>Anti-Raid Protection:</b> {status_str}\n\n"
        f"Threshold: <code>{settings.get('anti_raid_threshold', 5)} joins / {settings.get('anti_raid_window', 10)}s</code>\n"
        f"Action: <b>{settings.get('anti_raid_action', 'mute').upper()}</b>\n"
        f"Auto-Mute Duration: 15 minutes."
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)

async def antichannel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Toggle Anti-Channel external sender protection."""
    chat = update.effective_chat
    user = update.effective_user
    if not chat or chat.type not in ("group", "supergroup") or not user:
        return
    chat_id = chat.id
    if not await is_user_admin(chat_id, user.id, context) and user.id != OWNER_ID:
        await send_bot_response(update, context, "Only group administrators can configure Anti-Channel.")
        return

    sub = context.args[0].lower() if context.args else None
    settings = get_chat_settings(chat_id)
    current = settings.get("anti_channel_enabled", False)

    if sub in ("on", "enable", "true"):
        new_val = True
    elif sub in ("off", "disable", "false"):
        new_val = False
    else:
        new_val = not current

    update_chat_setting(chat_id, "anti_channel_enabled", new_val)
    status_str = "🟢 <b>ENABLED</b>" if new_val else "🔴 <b>DISABLED</b>"
    text = (
        f"📢 <b>Anti-Channel Protection:</b> {status_str}\n\n"
        f"External channels sending messages in this group will be deleted and auto-banned."
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)

def get_high_protection_handlers():
    """Returns all high protection and security suite handlers."""
    return [
        # Anti-Raid Join Handler (runs in join update group)
        MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, handle_anti_raid_join),
        # Anti-Channel Handler (runs on group messages)
        MessageHandler(filters.ChatType.GROUPS & ~filters.COMMAND, handle_anti_channel),
        # Message Protection (Mentions, Duplicates, Zalgo)
        MessageHandler(filters.ChatType.GROUPS & ~filters.COMMAND, handle_high_protection_message),
        # Commands
        CommandHandler(["shield", "lockdown"], shield_command),
        CommandHandler(["antiraid", "raidmode"], antiraid_command),
        CommandHandler(["antichannel", "blockchannel"], antichannel_command),
    ]
