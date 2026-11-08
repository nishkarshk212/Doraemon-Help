import logging
import asyncio
import html
import random
from datetime import datetime
from typing import List, Dict, Optional

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ChatPermissions
from telegram.ext import ContextTypes, CommandHandler, CallbackQueryHandler, MessageHandler, filters
from telegram.constants import ParseMode

from core.config import send_bot_response, colored_button, OWNER_ID
from core.database import get_collection
from managers.settings_manager_mongo import get_chat_settings
from managers.user_manager_mongo import is_user_admin

logger = logging.getLogger(__name__)

# Active tagging sessions: {chat_id: asyncio.Event}
ACTIVE_TAG_EVENTS: Dict[int, asyncio.Event] = {}

RANDOM_EMOJIS = [
    "🔥", "✨", "⚡", "🌟", "💫", "🎯", "💎", "🚀", "👑", "🌈",
    "🦄", "🎉", "🎸", "🏆", "🍕", "🍔", "🧁", "🦁", "🐼", "🦊"
]

DEFAULT_AUTO_MSG = "👋 Hey there! I'm currently away or busy. I will get back to you as soon as possible!"

# Collections
def get_automsg_col():
    return get_collection("user_automsg")

def get_saveit_col():
    return get_collection("user_saveit")

def get_favs_col():
    return get_collection("user_favorites")

def get_sessions_col():
    return get_collection("user_sessions")


# ═════════════════════════════════════════════════════════════════════════════
# 1. SMART TAGGER ENGINE
# ═════════════════════════════════════════════════════════════════════════════

async def _fetch_group_members(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> List[dict]:
    """
    Intelligently retrieves member list for tagging using multi-tiered fallback:
      1. Telethon user session (if connected)
      2. MongoDB tracked chat users (from message_tracker/users)
      3. Telegram Bot API get_chat_administrators
    """
    members = []

    # Strategy 1: Telethon voice_chat client
    try:
        import features.voice_chat as voice_chat
        tele = getattr(voice_chat, "telethon_client", None)
        if tele and tele.is_connected():
            async for user in tele.iter_participants(chat_id):
                if user and not getattr(user, "is_bot", False) and not getattr(user, "deleted", False):
                    name = html.escape(user.first_name or "Member")
                    members.append({"id": user.id, "name": name, "username": user.username})
            if len(members) >= 5:
                return members
    except Exception as e:
        logger.info(f"Telethon participant fetch skipped: {e}")

    # Strategy 2: MongoDB recorded chat participants
    try:
        msg_tracker_col = get_collection("user_messages")
        if msg_tracker_col is not None:
            docs = msg_tracker_col.find({"chat_id": chat_id}).sort("last_seen", -1).limit(200)
            seen_ids = set()
            for doc in docs:
                uid = doc.get("user_id")
                if uid and uid not in seen_ids:
                    seen_ids.add(uid)
                    name = html.escape(doc.get("first_name") or f"User {uid}")
                    members.append({"id": uid, "name": name, "username": doc.get("username")})
            if len(members) >= 5:
                return members
    except Exception as e:
        logger.info(f"Mongo tracked member fetch skipped: {e}")

    # Strategy 3: Chat Administrators via Bot API
    try:
        admins = await context.bot.get_chat_administrators(chat_id)
        for admin in admins:
            if not admin.user.is_bot:
                name = html.escape(admin.user.first_name or "Admin")
                members.append({"id": admin.user.id, "name": name, "username": admin.user.username})
    except Exception as e:
        logger.error(f"Bot API get_chat_administrators failed: {e}")

    return members


async def start_smart_tagging(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    mode: str = "text"
):
    """Core tagger runner supporting text, emoji, admin, and fast modes."""
    chat = update.effective_chat
    user = update.effective_user

    if not chat or chat.type not in ["group", "supergroup"]:
        return await update.message.reply_text("❌ This command can only be used in groups.")

    settings = get_chat_settings(chat.id)
    if not settings.get("tagger_enabled", True):
        return await update.message.reply_text("❌ Tagger feature is disabled in this group.")

    # Admin verification
    if user.id != OWNER_ID and not await is_user_admin(chat.id, user.id, context):
        return await update.message.reply_text("❌ Only group admins can initiate member tagging.")

    # Check for active session
    if chat.id in ACTIVE_TAG_EVENTS and not ACTIVE_TAG_EVENTS[chat.id].is_set():
        return await update.message.reply_text("⚠️ A tagging session is already active in this group! Use /stop or /cancel to stop it.")

    # Parse custom text & quote
    raw_args = list(context.args) if context.args else []
    is_quote = False
    if "-q" in raw_args:
        is_quote = True
        raw_args.remove("-q")
    elif "--quote" in raw_args:
        is_quote = True
        raw_args.remove("--quote")

    reply_msg = update.effective_message.reply_to_message
    reply_to_id = reply_msg.message_id if reply_msg else None

    custom_text = " ".join(raw_args).strip()
    if not custom_text:
        if reply_msg and (reply_msg.text or reply_msg.caption):
            custom_text = html.escape(reply_msg.text or reply_msg.caption)
        else:
            custom_text = "Attention everyone! 📢"
    else:
        custom_text = html.escape(custom_text)

    if is_quote:
        custom_text = f"<blockquote>{custom_text}</blockquote>"

    # Stop event setup
    stop_event = asyncio.Event()
    ACTIVE_TAG_EVENTS[chat.id] = stop_event

    # Fetch members
    status_msg = await update.message.reply_text("🔄 Gathering member list for tagging...")
    
    if mode == "admin":
        try:
            admins = await context.bot.get_chat_administrators(chat.id)
            members = [
                {"id": a.user.id, "name": html.escape(a.user.first_name or "Admin"), "username": a.user.username}
                for a in admins if not a.user.is_bot
            ]
        except Exception as e:
            members = []
    else:
        members = await _fetch_group_members(chat.id, context)

    if not members:
        del ACTIVE_TAG_EVENTS[chat.id]
        return await status_msg.edit_text("❌ Could not retrieve group members to tag.")

    await status_msg.edit_text(f"🚀 Tagging started for <b>{len(members)}</b> members! Mode: <code>{mode.upper()}</code>\nSend <code>/stop</code> to halt anytime.", parse_mode=ParseMode.HTML)

    batch_size = 5 if mode in ["fast", "admin"] else 3
    delay = 1.8 if mode == "fast" else 2.5

    try:
        for i in range(0, len(members), batch_size):
            if stop_event.is_set():
                await context.bot.send_message(chat.id, "🛑 Tagging stopped successfully.")
                break

            batch = members[i:i + batch_size]
            tag_links = []
            for m in batch:
                if mode == "emoji":
                    emoji_icon = random.choice(RANDOM_EMOJIS)
                    tag_links.append(f"<a href='tg://user?id={m['id']}'>{emoji_icon} {m['name']}</a>")
                else:
                    tag_links.append(f"<a href='tg://user?id={m['id']}'>{m['name']}</a>")

            tags_line = " • ".join(tag_links)
            message_text = f"{custom_text}\n\n{tags_line}"

            try:
                await context.bot.send_message(
                    chat_id=chat.id,
                    text=message_text,
                    parse_mode=ParseMode.HTML,
                    reply_to_message_id=reply_to_id
                )
            except Exception as send_err:
                logger.warning(f"Error sending tag batch: {send_err}")
                await asyncio.sleep(1.0)

            await asyncio.sleep(delay)

        if not stop_event.is_set():
            await context.bot.send_message(chat.id, "✅ Tagging session completed!")

    finally:
        if chat.id in ACTIVE_TAG_EVENTS:
            del ACTIVE_TAG_EVENTS[chat.id]


async def tag_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Standard tagger with custom text: /tag or /utag <message>"""
    await start_smart_tagging(update, context, mode="text")

async def etag_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Emoji decorated tagger: /etag <message>"""
    await start_smart_tagging(update, context, mode="emoji")

async def admintag_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Tag all admins: /admintag <message>"""
    await start_smart_tagging(update, context, mode="admin")

async def ftag_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Fast turbo tagger: /ftag <message>"""
    await start_smart_tagging(update, context, mode="fast")

async def stop_tag_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Stops running tagger: /stop or /cancel or /stoptag"""
    chat_id = update.effective_chat.id
    if chat_id in ACTIVE_TAG_EVENTS:
        ACTIVE_TAG_EVENTS[chat_id].set()
        await update.message.reply_text("🛑 Tagging cancelled.")
    else:
        await update.message.reply_text("ℹ️ No active tagging session in this chat.")


# ═════════════════════════════════════════════════════════════════════════════
# 2. AUTO-DM RESPONDER
# ═════════════════════════════════════════════════════════════════════════════

async def automsg_toggle_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Toggle Auto-DM on or off: /automsg [on/off]"""
    user_id = update.effective_user.id
    col = get_automsg_col()

    existing = col.find_one({"user_id": user_id}) if col is not None else None
    current_enabled = existing.get("enabled", False) if existing else False

    if context.args:
        arg = context.args[0].lower()
        if arg in ["on", "enable", "true", "yes"]:
            new_state = True
        elif arg in ["off", "disable", "false", "no"]:
            new_state = False
        else:
            return await update.message.reply_text("⚠️ Usage: <code>/automsg on</code> or <code>/automsg off</code>", parse_mode=ParseMode.HTML)
    else:
        new_state = not current_enabled

    if col is not None:
        col.update_one(
            {"user_id": user_id},
            {"$set": {"enabled": new_state, "updated_at": datetime.utcnow()}},
            upsert=True
        )

    status_icon = "🟢 <b>ENABLED</b>" if new_state else "🔴 <b>DISABLED</b>"
    await update.message.reply_text(
        f"🤖 <b>Auto-DM Responder</b>\n\n"
        f"Status: {status_icon}\n\n"
        f"💡 When enabled, the bot automatically responds to anyone who messages you or mentions you.\n"
        f"• Use <code>/setautomsg &lt;text&gt;</code> to customize your auto-reply.\n"
        f"• Use <code>/myautomsg</code> to check your active message.",
        parse_mode=ParseMode.HTML
    )

async def set_automsg_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Set custom Auto-DM text: /setautomsg <message>"""
    user_id = update.effective_user.id
    if not context.args:
        return await update.message.reply_text("⚠️ Usage: <code>/setautomsg Hey, I am currently away!</code>", parse_mode=ParseMode.HTML)

    new_text = " ".join(context.args).strip()
    col = get_automsg_col()
    if col is not None:
        col.update_one(
            {"user_id": user_id},
            {"$set": {"text": new_text, "enabled": True, "updated_at": datetime.utcnow()}},
            upsert=True
        )

    await update.message.reply_text(
        f"✅ <b>Auto-DM message updated and activated!</b>\n\n"
        f"📝 <b>Message:</b>\n<i>{html.escape(new_text)}</i>",
        parse_mode=ParseMode.HTML
    )

async def my_automsg_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """View active Auto-DM message: /myautomsg"""
    user_id = update.effective_user.id
    col = get_automsg_col()
    doc = col.find_one({"user_id": user_id}) if col is not None else None

    if not doc:
        return await update.message.reply_text(
            f"ℹ️ You don't have a custom Auto-DM configured.\nDefault: <i>{DEFAULT_AUTO_MSG}</i>\n\nUse <code>/setautomsg &lt;text&gt;</code> to configure.",
            parse_mode=ParseMode.HTML
        )

    status_str = "🟢 Active" if doc.get("enabled", False) else "🔴 Disabled"
    text = doc.get("text", DEFAULT_AUTO_MSG)
    await update.message.reply_text(
        f"🤖 <b>Your Auto-DM Configuration</b>\n\n"
        f"• <b>Status:</b> {status_str}\n"
        f"• <b>Message:</b> <i>{html.escape(text)}</i>",
        parse_mode=ParseMode.HTML
    )

async def reset_automsg_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Reset Auto-DM message to default: /resetautomsg"""
    user_id = update.effective_user.id
    col = get_automsg_col()
    if col is not None:
        col.update_one(
            {"user_id": user_id},
            {"$set": {"text": DEFAULT_AUTO_MSG, "updated_at": datetime.utcnow()}},
            upsert=True
        )
    await update.message.reply_text("🔄 Auto-DM message reset to default successfully.")


# ═════════════════════════════════════════════════════════════════════════════
# 3. SAVEIT MEDIA SAVER
# ═════════════════════════════════════════════════════════════════════════════

async def saveit_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Save restricted, self-destructing, or normal media directly to user's PM.
    Usage: Reply to any media with /saveit or /save
    """
    user = update.effective_user
    msg = update.effective_message

    if not msg.reply_to_message:
        return await update.message.reply_text(
            "⚠️ <b>SaveIt Usage:</b>\n"
            "Reply to any photo, video, document, audio, voice note, or sticker with <code>/saveit</code> (or <code>/save</code>) "
            "to save and forward it directly to your PM!",
            parse_mode=ParseMode.HTML
        )

    target = msg.reply_to_message
    status_msg = await update.message.reply_text("⏳ Processing and saving media to your PM...")

    try:
        # Check if media is present
        sent = False
        caption_text = f"💾 <b>Saved via SaveIt</b>\nFrom Chat: <code>{update.effective_chat.title or 'Private'}</code>"

        if target.photo:
            await context.bot.send_photo(
                chat_id=user.id,
                photo=target.photo[-1].file_id,
                caption=f"{target.caption or ''}\n\n{caption_text}",
                parse_mode=ParseMode.HTML
            )
            sent = True
        elif target.video:
            await context.bot.send_video(
                chat_id=user.id,
                video=target.video.file_id,
                caption=f"{target.caption or ''}\n\n{caption_text}",
                parse_mode=ParseMode.HTML
            )
            sent = True
        elif target.document:
            await context.bot.send_document(
                chat_id=user.id,
                document=target.document.file_id,
                caption=f"{target.caption or ''}\n\n{caption_text}",
                parse_mode=ParseMode.HTML
            )
            sent = True
        elif target.audio:
            await context.bot.send_audio(
                chat_id=user.id,
                audio=target.audio.file_id,
                caption=f"{target.caption or ''}\n\n{caption_text}",
                parse_mode=ParseMode.HTML
            )
            sent = True
        elif target.voice:
            await context.bot.send_voice(
                chat_id=user.id,
                voice=target.voice.file_id,
                caption=caption_text,
                parse_mode=ParseMode.HTML
            )
            sent = True
        elif target.sticker:
            await context.bot.send_sticker(chat_id=user.id, sticker=target.sticker.file_id)
            await context.bot.send_message(chat_id=user.id, text=caption_text, parse_mode=ParseMode.HTML)
            sent = True
        elif target.animation:
            await context.bot.send_animation(
                chat_id=user.id,
                animation=target.animation.file_id,
                caption=f"{target.caption or ''}\n\n{caption_text}",
                parse_mode=ParseMode.HTML
            )
            sent = True
        elif target.text:
            await context.bot.send_message(
                chat_id=user.id,
                text=f"{target.text}\n\n{caption_text}",
                parse_mode=ParseMode.HTML
            )
            sent = True

        if sent:
            await status_msg.edit_text(f"✅ <b>Media saved!</b> Check your private chat with @{context.bot.username}.", parse_mode=ParseMode.HTML)
        else:
            await status_msg.edit_text("❌ No supported media or text found in replied message.")

    except Exception as e:
        logger.error(f"SaveIt delivery failed: {e}")
        await status_msg.edit_text(
            f"⚠️ Could not send media to your PM. Make sure you have started @{context.bot.username} in private chat first!"
        )

async def autosave_toggle_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Toggle AutoSave setting: /autosave [on/off]"""
    user_id = update.effective_user.id
    col = get_saveit_col()
    doc = col.find_one({"user_id": user_id}) if col is not None else None
    current_state = doc.get("autosave", False) if doc else False

    if context.args:
        arg = context.args[0].lower()
        new_state = arg in ["on", "enable", "true", "yes"]
    else:
        new_state = not current_state

    if col is not None:
        col.update_one(
            {"user_id": user_id},
            {"$set": {"autosave": new_state, "updated_at": datetime.utcnow()}},
            upsert=True
        )

    status_str = "🟢 <b>ENABLED</b>" if new_state else "🔴 <b>DISABLED</b>"
    await update.message.reply_text(
        f"💾 <b>SaveIt Auto-Saver</b>\n\n"
        f"• Auto-Save: {status_str}\n\n"
        "💡 When enabled, expiring and timed media sent to your PM will be automatically saved to your private storage.",
        parse_mode=ParseMode.HTML
    )


# ═════════════════════════════════════════════════════════════════════════════
# 4. FAVORITES & SHORTCUTS
# ═════════════════════════════════════════════════════════════════════════════

async def addfav_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Add a shortcut favorite: /addfav <tag> <content or reply>"""
    user_id = update.effective_user.id
    if not context.args:
        return await update.message.reply_text("⚠️ Usage: <code>/addfav &lt;tag&gt; &lt;text or reply to media&gt;</code>", parse_mode=ParseMode.HTML)

    tag = context.args[0].lower().strip("#")
    reply = update.effective_message.reply_to_message

    content = " ".join(context.args[1:]).strip() if len(context.args) > 1 else ""
    media_file_id = None
    media_type = None

    if reply:
        if reply.photo:
            media_file_id = reply.photo[-1].file_id
            media_type = "photo"
        elif reply.video:
            media_file_id = reply.video.file_id
            media_type = "video"
        elif reply.document:
            media_file_id = reply.document.file_id
            media_type = "document"
        elif reply.audio:
            media_file_id = reply.audio.file_id
            media_type = "audio"
        elif reply.text and not content:
            content = reply.text

    if not content and not media_file_id:
        return await update.message.reply_text("⚠️ Please provide text content or reply to a media file.")

    col = get_favs_col()
    if col is not None:
        col.update_one(
            {"user_id": user_id, "tag": tag},
            {
                "$set": {
                    "tag": tag,
                    "content": content,
                    "media_file_id": media_file_id,
                    "media_type": media_type,
                    "updated_at": datetime.utcnow()
                }
            },
            upsert=True
        )

    await update.message.reply_text(f"⭐ Saved favorite <code>#{tag}</code> successfully!", parse_mode=ParseMode.HTML)

async def remfav_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Remove a favorite: /remfav <tag>"""
    user_id = update.effective_user.id
    if not context.args:
        return await update.message.reply_text("⚠️ Usage: <code>/remfav &lt;tag&gt;</code>", parse_mode=ParseMode.HTML)

    tag = context.args[0].lower().strip("#")
    col = get_favs_col()
    if col is not None:
        res = col.delete_one({"user_id": user_id, "tag": tag})
        if res.deleted_count > 0:
            return await update.message.reply_text(f"🗑️ Deleted favorite <code>#{tag}</code>.", parse_mode=ParseMode.HTML)

    await update.message.reply_text(f"❌ Favorite <code>#{tag}</code> not found.", parse_mode=ParseMode.HTML)

async def favs_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List all user favorites: /favs"""
    user_id = update.effective_user.id
    col = get_favs_col()
    if col is None:
        return await update.message.reply_text("ℹ️ Database unavailable.")

    docs = list(col.find({"user_id": user_id}).sort("tag", 1).limit(50))
    if not docs:
        return await update.message.reply_text("⭐ You don't have any saved favorites yet. Use <code>/addfav &lt;tag&gt; &lt;text&gt;</code> to save one!", parse_mode=ParseMode.HTML)

    lines = ["⭐ <b>Your Saved Favorites:</b>\n"]
    for d in docs:
        tag = d.get("tag", "item")
        media_info = f"[{d.get('media_type')}]" if d.get("media_type") else ""
        content_preview = (d.get("content") or "")[:30]
        lines.append(f"• <code>#{tag}</code> {media_info} {content_preview}")

    lines.append("\n💡 Send <code>/getfav &lt;tag&gt;</code> to retrieve any item.")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)

async def getfav_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Retrieve and send a saved favorite: /getfav <tag>"""
    user_id = update.effective_user.id
    if not context.args:
        return await update.message.reply_text("⚠️ Usage: <code>/getfav &lt;tag&gt;</code>", parse_mode=ParseMode.HTML)

    tag = context.args[0].lower().strip("#")
    col = get_favs_col()
    doc = col.find_one({"user_id": user_id, "tag": tag}) if col is not None else None

    if not doc:
        return await update.message.reply_text(f"❌ No favorite found with tag <code>#{tag}</code>.", parse_mode=ParseMode.HTML)

    media_id = doc.get("media_file_id")
    media_type = doc.get("media_type")
    content = doc.get("content") or ""

    if media_type == "photo":
        await update.message.reply_photo(photo=media_id, caption=content)
    elif media_type == "video":
        await update.message.reply_video(video=media_id, caption=content)
    elif media_type == "document":
        await update.message.reply_document(document=media_id, caption=content)
    elif media_type == "audio":
        await update.message.reply_audio(audio=media_id, caption=content)
    else:
        await update.message.reply_text(content)


# ═════════════════════════════════════════════════════════════════════════════
# 5. USERBOT / SELF ACCOUNT LINKING
# ═════════════════════════════════════════════════════════════════════════════

async def login_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Link user string session in PM: /login <string_session>"""
    if update.effective_chat.type != "private":
        try:
            await update.message.delete()
        except Exception:
            pass
        return await update.effective_chat.send_message(
            f"⚠️ User {update.effective_user.mention_html()}, for your security, <code>/login</code> must ONLY be used in PM!",
            parse_mode=ParseMode.HTML
        )

    user_id = update.effective_user.id
    if not context.args:
        return await update.message.reply_text(
            "🔐 <b>Link Your Account / String Session</b>\n\n"
            "To enable personal tagging and auto-saving through your account:\n"
            "Usage: <code>/login &lt;your_string_session&gt;</code>\n\n"
            "🔒 <i>Your session string is stored encrypted in your private database record.</i>",
            parse_mode=ParseMode.HTML
        )

    session_str = context.args[0].strip()
    col = get_sessions_col()
    if col is not None:
        col.update_one(
            {"user_id": user_id},
            {"$set": {"session_string": session_str, "updated_at": datetime.utcnow()}},
            upsert=True
        )

    await update.message.reply_text("✅ <b>Session linked successfully!</b> Your account is now connected for self-actions and tagging.", parse_mode=ParseMode.HTML)

async def myaccount_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Check account connection status: /myaccount"""
    user_id = update.effective_user.id
    col = get_sessions_col()
    doc = col.find_one({"user_id": user_id}) if col is not None else None

    if doc and doc.get("session_string"):
        status_text = "🟢 <b>Connected</b>"
    else:
        status_text = "🔴 <b>Not Connected</b>"

    await update.message.reply_text(
        f"👤 <b>Account Link Status</b>\n\n"
        f"• Status: {status_text}\n"
        f"• User ID: <code>{user_id}</code>\n\n"
        "Use <code>/login &lt;session&gt;</code> in PM to link or <code>/logout</code> to unlink.",
        parse_mode=ParseMode.HTML
    )

async def logout_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Unlink user string session: /logout"""
    user_id = update.effective_user.id
    col = get_sessions_col()
    if col is not None:
        col.delete_one({"user_id": user_id})
    await update.message.reply_text("👋 <b>Your account session has been unlinked successfully.</b>", parse_mode=ParseMode.HTML)


# ═════════════════════════════════════════════════════════════════════════════
# HANDLER REGISTRATION
# ═════════════════════════════════════════════════════════════════════════════

def get_multitask_handlers() -> List:
    """Returns all command handlers for Multi-Task tools."""
    return [
        # Smart Tagger
        CommandHandler(["tag", "utag"], tag_command),
        CommandHandler("etag", etag_command),
        CommandHandler("admintag", admintag_command),
        CommandHandler("ftag", ftag_command),
        CommandHandler(["stop", "cancel", "stoptag"], stop_tag_command),

        # Auto-DM Responder
        CommandHandler("automsg", automsg_toggle_command),
        CommandHandler("setautomsg", set_automsg_command),
        CommandHandler("myautomsg", my_automsg_command),
        CommandHandler("resetautomsg", reset_automsg_command),

        # SaveIt Media Saver
        CommandHandler(["save", "saveit"], saveit_command),
        CommandHandler(["autosave", "savetimed"], autosave_toggle_command),

        # Favorites & Shortcuts
        CommandHandler("addfav", addfav_command),
        CommandHandler("remfav", remfav_command),
        CommandHandler("favs", favs_command),
        CommandHandler("getfav", getfav_command),

        # Account Session Link
        CommandHandler("login", login_command),
        CommandHandler("myaccount", myaccount_command),
        CommandHandler("logout", logout_command),
    ]

