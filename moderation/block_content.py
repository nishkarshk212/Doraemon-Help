import logging
from telegram import Update, ChatPermissions
from telegram.constants import ParseMode
from telegram.ext import ContextTypes, CommandHandler, MessageHandler, filters
from core.config import OWNER_ID, send_bot_response, log_to_channel
from managers.settings_manager_mongo import get_chat_settings
from managers.moderation_manager_mongo import add_warn, reset_warns
from managers.user_manager_mongo import is_user_admin, get_user_id
from moderation.moderation import can_user_ban
from managers.block_content_manager_mongo import add_blocked_content, remove_blocked_content, is_content_blocked, get_blocked_content, clear_all_blocked_content
import re

def extract_pack_name(text):
    """Extract sticker pack name from link or text."""
    if not text:
        return None
    
    # Handle t.me/addstickers/PackName
    if "t.me/addstickers/" in text:
        return text.split("t.me/addstickers/")[1].split()[0].split("?")[0]
    
    # Handle telegram.me/addstickers/PackName
    if "telegram.me/addstickers/" in text:
        return text.split("telegram.me/addstickers/")[1].split()[0].split("?")[0]
        
    # Handle tg://addstickers?set=PackName
    if "tg://addstickers?set=" in text:
        return text.split("tg://addstickers?set=")[1].split()[0].split("&")[0]
        
    return text.strip()

async def block_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Command to block a specific content (text, media, etc.)."""
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    
    can_ban, error_msg = await can_user_ban(chat_id, user_id, context)
    if not can_ban:
        await send_bot_response(update, context, error_msg or "Only admins with ban permission can use the /block command.")
        return

    # 1. Handle reply to a message (media or text)
    if update.message.reply_to_message:
        reply = update.message.reply_to_message
        
        # Check for media first
        media_file_id = None
        media_type = None
        
        if reply.photo: 
            media_file_id = reply.photo[-1].file_id
            media_type = "photo"
        elif reply.video: 
            media_file_id = reply.video.file_id
            media_type = "video"
        elif reply.animation: 
            media_file_id = reply.animation.file_id
            media_type = "gif"
        elif reply.document: 
            media_file_id = reply.document.file_id
            media_type = "document"
        elif reply.sticker: 
            media_file_id = reply.sticker.file_id
            media_type = "sticker"
        
        if media_file_id:
            if add_blocked_content(chat_id, "media", media_file_id):
                await send_bot_response(update, context, f"✅ Blocked this {media_type} successfully.")
            else:
                await send_bot_response(update, context, f"ℹ️ This {media_type} is already blocked.")
            return
            
        # If no media, check for text
        if reply.text:
            if add_blocked_content(chat_id, "text", reply.text):
                await send_bot_response(update, context, f"✅ Blocked the word: '{reply.text[:50]}...'")
            else:
                await send_bot_response(update, context, "ℹ️ This word is already blocked.")
            return

    # 2. Handle arguments (words or sentences)
    if context.args:
        text_to_block = " ".join(context.args)
        if add_blocked_content(chat_id, "text", text_to_block):
            await send_bot_response(update, context, f"✅ Blocked the word: '{text_to_block}'")
        else:
            await send_bot_response(update, context, "ℹ️ This word is already blocked.")
        return

    await send_bot_response(update, context, "Usage: Reply to a message with /block or use `/block <word>` to block content.")

async def unblock_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Command to unblock content."""
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    
    can_ban, _ = await can_user_ban(chat_id, user_id, context)
    if not can_ban:
        return

    # Handle reply
    if update.message.reply_to_message:
        reply = update.message.reply_to_message
        media_file_id = None
        if reply.photo: media_file_id = reply.photo[-1].file_id
        elif reply.video: media_file_id = reply.video.file_id
        elif reply.animation: media_file_id = reply.animation.file_id
        elif reply.document: media_file_id = reply.document.file_id
        elif reply.sticker: media_file_id = reply.sticker.file_id
        
        if media_file_id:
            if remove_blocked_content(chat_id, "media", media_file_id):
                await send_bot_response(update, context, "✅ Unblocked this media.")
            else:
                await send_bot_response(update, context, "❌ This media is not in the block list.")
            return
            
        if reply.text:
            if remove_blocked_content(chat_id, "text", reply.text):
                await send_bot_response(update, context, f"✅ Unblocked: '{reply.text[:50]}...'")
            else:
                await send_bot_response(update, context, "❌ This word is not in the block list.")
            return

    # Handle arguments
    if context.args:
        text_to_unblock = " ".join(context.args)
        if remove_blocked_content(chat_id, "text", text_to_unblock):
            await send_bot_response(update, context, f"✅ Unblocked: '{text_to_unblock}'")
        else:
            await send_bot_response(update, context, "❌ This word is not in the block list.")
        return

    await send_bot_response(update, context, "Usage: Reply to a message with /unblock or use `/unblock <word>`.")

async def list_blocked_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Lists all blocked content in the chat."""
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    
    can_ban, _ = await can_user_ban(chat_id, user_id, context)
    if not can_ban:
        return

    blocked = get_blocked_content(chat_id)
    text_list = blocked.get("text", [])
    pack_list = blocked.get("stickerpack", [])
    
    if not text_list and not pack_list:
        await send_bot_response(update, context, "No content is blocked in this group.")
        return
        
    response = "🚫 **Blocked Content List:**\n\n"
    
    if text_list:
        response += "**Words:**\n"
        for i, item in enumerate(text_list, 1):
            response += f"{i}. `{item}`\n"
        response += "\n"
        
    if pack_list:
        response += "**Sticker Packs:**\n"
        for i, item in enumerate(pack_list, 1):
            response += f"{i}. <code>{item}</code>\n"
    
    await send_bot_response(update, context, response, parse_mode=ParseMode.HTML)

async def unblock_all_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Command to unblock all content in the chat."""
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    
    can_ban, _ = await can_user_ban(chat_id, user_id, context)
    if not can_ban:
        return

    if clear_all_blocked_content(chat_id):
        await send_bot_response(update, context, "✅ All blocked words and media have been cleared.")
    else:
        await send_bot_response(update, context, "❌ No blocked content found to clear.")

async def block_pack_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Command to block a specific sticker pack."""
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    
    can_ban, error_msg = await can_user_ban(chat_id, user_id, context)
    if not can_ban:
        await send_bot_response(update, context, error_msg or "Only admins with ban permission can use the /blockpack command.")
        return

    # Handle reply to a sticker
    if update.message.reply_to_message and update.message.reply_to_message.sticker:
        sticker = update.message.reply_to_message.sticker
        if not sticker.set_name:
            await send_bot_response(update, context, "❌ This sticker does not belong to a pack.")
            return
            
        if add_blocked_content(chat_id, "stickerpack", sticker.set_name):
            await send_bot_response(update, context, f"✅ Blocked sticker pack: <code>{sticker.set_name}</code>", parse_mode=ParseMode.HTML)
        else:
            await send_bot_response(update, context, f"ℹ️ Sticker pack <code>{sticker.set_name}</code> is already blocked.", parse_mode=ParseMode.HTML)
        return

    # Handle arguments (set_name or link)
    if context.args:
        set_name = extract_pack_name(context.args[0])
        if add_blocked_content(chat_id, "stickerpack", set_name):
            await send_bot_response(update, context, f"✅ Blocked sticker pack: <code>{set_name}</code>", parse_mode=ParseMode.HTML)
        else:
            await send_bot_response(update, context, f"ℹ️ Sticker pack <code>{set_name}</code> is already blocked.", parse_mode=ParseMode.HTML)
        return

    await send_bot_response(update, context, "Usage: Reply to a sticker with /blockpack or use `/blockpack <link_or_name>`.")

async def unblock_pack_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Command to unblock a specific sticker pack."""
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    
    can_ban, _ = await can_user_ban(chat_id, user_id, context)
    if not can_ban:
        return

    # Handle reply to a sticker
    if update.message.reply_to_message and update.message.reply_to_message.sticker:
        sticker = update.message.reply_to_message.sticker
        if sticker.set_name:
            if remove_blocked_content(chat_id, "stickerpack", sticker.set_name):
                await send_bot_response(update, context, f"✅ Unblocked sticker pack: <code>{sticker.set_name}</code>", parse_mode=ParseMode.HTML)
            else:
                await send_bot_response(update, context, f"❌ Sticker pack <code>{sticker.set_name}</code> is not blocked.", parse_mode=ParseMode.HTML)
            return

    # Handle arguments
    if context.args:
        set_name = extract_pack_name(context.args[0])
        if remove_blocked_content(chat_id, "stickerpack", set_name):
            await send_bot_response(update, context, f"✅ Unblocked sticker pack: <code>{set_name}</code>", parse_mode=ParseMode.HTML)
        else:
            await send_bot_response(update, context, f"❌ Sticker pack <code>{set_name}</code> is not blocked.", parse_mode=ParseMode.HTML)
        return

    await send_bot_response(update, context, "Usage: Reply to a sticker with /unblockpack or use `/unblockpack <link_or_name>`.")

async def check_blocked_content_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """MessageHandler to check every message for blocked content."""
    if not update.message or not update.effective_chat:
        return
        
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    
    # Check if content is blocked
    is_blocked, reason = is_content_blocked(chat_id, update.message)
    
    if is_blocked:
        # Respect freed users: if this is a sticker and the user is exempt from
        # sticker blocking, do not delete or penalize them.
        if update.message.sticker:
            settings = get_chat_settings(chat_id)
            user_perms = settings.get("user_permissions", {}).get(str(user_id), {})
            if user_perms.get("block_stickers", False):
                logging.info(f"[BLOCK] Sticker blocked ({reason}) but user {user_id} is FREED, skipping")
                return

        # 1. Always delete the message first
        try:
            await update.message.delete()
        except Exception as e:
            logging.error(f"Failed to delete blocked content: {e}")

        # 2. Skip penalties for admins and owner
        if user_id == OWNER_ID or await is_user_admin(chat_id, user_id, context):
            logging.info(f"Blocked content deleted from admin/owner {user_id} in {chat_id}. Skipping penalties.")
            return

        # 3. Apply penalties for regular members
        settings = get_chat_settings(chat_id)
        limit = settings.get("block_warn_limit", 3)
        penalty = settings.get("block_warn_penalty", "warn") # warn, mute, ban, kick
        
        user_name = update.effective_user.first_name
        
        if penalty == "warn":
            from handlers.blocking_handler import send_blocking_notification
            await send_blocking_notification(
                update, context, settings,
                f"⚠️ User <code>{user_id}</code>, that content is blocked in this group. Please avoid using it."
            )
        elif penalty == "mute":
            try:
                await context.bot.restrict_chat_member(chat_id, user_id, permissions=ChatPermissions(can_send_messages=False))
                await context.bot.send_message(chat_id, f"🔇 User <code>{user_id}</code> has been muted for using blocked content.", parse_mode=ParseMode.HTML)
            except Exception as e:
                logging.error(f"Failed to mute user: {e}")
        elif penalty == "ban":
            try:
                await context.bot.ban_chat_member(chat_id, user_id)
                await context.bot.send_message(chat_id, f"🚫 User <code>{user_id}</code> has been banned for using blocked content.", parse_mode=ParseMode.HTML)
            except Exception as e:
                logging.error(f"Failed to ban user: {e}")
        elif penalty == "kick":
            try:
                await context.bot.ban_chat_member(chat_id, user_id)
                await context.bot.unban_chat_member(chat_id, user_id)
                await context.bot.send_message(chat_id, f"👞 User <code>{user_id}</code> has been kicked for using blocked content.", parse_mode=ParseMode.HTML)
            except Exception as e:
                logging.error(f"Failed to kick user: {e}")
        return

    # Check NSFW content using integrated NSFW Safeguard engine
    settings = get_chat_settings(chat_id)
    if settings.get("nsfw_filter_enabled", False):
        from features.nsfw_panel import process_nsfw_moderation
        handled = await process_nsfw_moderation(update, context)
        if handled:
            return

    # Check message length (admins/owner are exempt from length limits)
    if user_id == OWNER_ID or await is_user_admin(chat_id, user_id, context):
        return

    msg_content = (update.message.text or "") + (update.message.caption or "")
    if msg_content:
        limit = settings.get("msg_length_limit", 0)
        max_l = settings.get("msg_length_max", 0)
        effective_limit = limit if limit > 0 else (max_l if (settings.get("msg_length_delete", False) and max_l > 0) or (max_l > 0 and max_l != 2000) else 0)
        if effective_limit > 0 and len(msg_content) > effective_limit:
            try:
                await update.message.delete()
                from handlers.blocking_handler import send_blocking_notification
                await send_blocking_notification(
                    update, context, settings,
                    f"📏 <b>Message too long!</b>\n\n"
                    f"User <code>{user_id}</code>, your message exceeded the allowed limit of <b>{effective_limit}</b> characters "
                    f"({len(msg_content)}/{effective_limit})."
                )
            except Exception as e:
                logging.error(f"Failed to delete message exceeding length limit: {e}")
            return

def get_block_content_handlers():
    return [
        CommandHandler("block", block_command),
        CommandHandler("unblock", unblock_command),
        CommandHandler("blockpack", block_pack_command),
        CommandHandler("unblockpack", unblock_pack_command),
        CommandHandler("unblockall", unblock_all_command),
        CommandHandler("listblock", list_blocked_command),
        # MessageHandler to catch all messages (text and media)
        MessageHandler(filters.ALL & ~filters.COMMAND & filters.ChatType.GROUPS, check_blocked_content_handler)
    ]
