import logging
import re
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import ContextTypes, CommandHandler, MessageHandler, filters, CallbackQueryHandler
from managers.filters_manager_mongo import get_chat_filters, add_chat_filter, remove_chat_filter, remove_all_chat_filters
from managers.user_manager_mongo import is_user_admin
from moderation.moderation import can_user_ban
from core.config import (
    colored_button,
    clean_button_url,
    parse_markdown_buttons,
    normalize_buttons,
    build_colored_keyboard,
    extract_buttons_from_markup,
)

def extract_trigger_and_reply(command_text: str):
    """Extract filter trigger (handles quotes and single words) and reply text."""
    # Strip command prefix: /filter or /filter@BotName
    content = re.sub(r'^/filter(?:@\w+)?\s*', '', command_text, flags=re.IGNORECASE).strip()
    if not content:
        return None, ""
        
    # Check if trigger is wrapped in quotes
    if content.startswith('"'):
        end_idx = content.find('"', 1)
        if end_idx != -1:
            trigger = content[1:end_idx].strip()
            reply = content[end_idx + 1:].strip()
            return trigger.lower(), reply
    elif content.startswith("'"):
        end_idx = content.find("'", 1)
        if end_idx != -1:
            trigger = content[1:end_idx].strip()
            reply = content[end_idx + 1:].strip()
            return trigger.lower(), reply
            
    # Unquoted: split by first whitespace
    parts = content.split(None, 1)
    if len(parts) == 1:
        return parts[0].strip().lower(), ""
    return parts[0].strip().lower(), parts[1].strip()

async def set_filter(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Set a new filter for the chat with optional inline colored buttons."""
    if not update.message:
        return
    
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    
    # Check permissions (only admins with Ban permission)
    can_ban, error_msg = await can_user_ban(chat_id, user_id, context)
    if not can_ban:
        await update.message.reply_text(error_msg or "Only admins with 'Ban Users' permission can use the /filter command.")
        return

    text_content = update.message.text or ""
    reply = update.message.reply_to_message
    
    trigger, reply_text = extract_trigger_and_reply(text_content)
    
    if not trigger:
        usage = (
            "📌 <b>Filter Usage:</b>\n\n"
            "• <code>/filter &lt;trigger&gt; &lt;reply&gt;</code>\n"
            "• <code>/filter \"multi word trigger\" &lt;reply&gt;</code>\n"
            "• Or reply to a message with <code>/filter &lt;trigger&gt;</code>\n\n"
            "🎨 <b>Inline Colored Buttons:</b>\n"
            "Add inline buttons right in your filter reply:\n"
            "<code>[#g Join Channel | https://t.me/channel]</code>\n"
            "<code>[#r Rules | https://t.me/rules] [#p Support | https://t.me/support :same]</code>"
        )
        await update.message.reply_text(usage, parse_mode=ParseMode.HTML)
        return

    try:
        content_data = {}
        buttons = []

        # Parse buttons from reply_text if provided in the command
        cmd_clean_text = ""
        cmd_buttons = []
        if reply_text:
            cmd_clean_text, cmd_buttons = parse_markdown_buttons(reply_text)

        if reply:
            # Handle reply to a media/text message
            if reply.text:
                raw_text = reply.text_html or reply.text
                clean_text, r_btns = parse_markdown_buttons(raw_text)
                buttons = cmd_buttons if cmd_buttons else r_btns
                if not buttons and reply.reply_markup:
                    buttons = extract_buttons_from_markup(reply.reply_markup)
                final_text = cmd_clean_text if cmd_clean_text else clean_text
                content_data = {"type": "text", "content": final_text, "buttons": buttons, "is_html": True}
                
            elif reply.sticker:
                buttons = cmd_buttons
                if not buttons and reply.reply_markup:
                    buttons = extract_buttons_from_markup(reply.reply_markup)
                content_data = {"type": "sticker", "content": reply.sticker.file_id, "buttons": buttons}
                
            elif reply.photo:
                raw_cap = reply.caption_html or reply.caption or ""
                clean_cap, r_btns = parse_markdown_buttons(raw_cap)
                buttons = cmd_buttons if cmd_buttons else r_btns
                if not buttons and reply.reply_markup:
                    buttons = extract_buttons_from_markup(reply.reply_markup)
                final_cap = cmd_clean_text if cmd_clean_text else clean_cap
                content_data = {"type": "photo", "content": reply.photo[-1].file_id, "caption": final_cap, "buttons": buttons, "is_html": True}
                
            elif reply.video:
                raw_cap = reply.caption_html or reply.caption or ""
                clean_cap, r_btns = parse_markdown_buttons(raw_cap)
                buttons = cmd_buttons if cmd_buttons else r_btns
                if not buttons and reply.reply_markup:
                    buttons = extract_buttons_from_markup(reply.reply_markup)
                final_cap = cmd_clean_text if cmd_clean_text else clean_cap
                content_data = {"type": "video", "content": reply.video.file_id, "caption": final_cap, "buttons": buttons, "is_html": True}
                
            elif reply.document:
                raw_cap = reply.caption_html or reply.caption or ""
                clean_cap, r_btns = parse_markdown_buttons(raw_cap)
                buttons = cmd_buttons if cmd_buttons else r_btns
                if not buttons and reply.reply_markup:
                    buttons = extract_buttons_from_markup(reply.reply_markup)
                final_cap = cmd_clean_text if cmd_clean_text else clean_cap
                content_data = {"type": "document", "content": reply.document.file_id, "caption": final_cap, "buttons": buttons, "is_html": True}
                
            elif reply.animation:
                raw_cap = reply.caption_html or reply.caption or ""
                clean_cap, r_btns = parse_markdown_buttons(raw_cap)
                buttons = cmd_buttons if cmd_buttons else r_btns
                if not buttons and reply.reply_markup:
                    buttons = extract_buttons_from_markup(reply.reply_markup)
                final_cap = cmd_clean_text if cmd_clean_text else clean_cap
                content_data = {"type": "animation", "content": reply.animation.file_id, "caption": final_cap, "buttons": buttons, "is_html": True}
                
            elif reply.voice:
                buttons = cmd_buttons
                if not buttons and reply.reply_markup:
                    buttons = extract_buttons_from_markup(reply.reply_markup)
                content_data = {"type": "voice", "content": reply.voice.file_id, "buttons": buttons}
            else:
                await update.message.reply_text("❌ This type of content is not supported for filters.")
                return
        else:
            # Standalone command with text
            if not reply_text:
                await update.message.reply_text("❌ Please provide reply content or reply to a message with <code>/filter &lt;trigger&gt;</code>.", parse_mode=ParseMode.HTML)
                return
            content_data = {"type": "text", "content": cmd_clean_text, "buttons": cmd_buttons, "is_html": True}
            buttons = cmd_buttons

        add_chat_filter(chat_id, trigger, content_data)
        
        btn_count = sum(len(r) for r in normalize_buttons(buttons))
        btn_info = f" with <b>{btn_count} inline colored button(s)</b>" if btn_count > 0 else ""
        await update.message.reply_text(
            f"✅ Filter for <code>{trigger}</code> has been set{btn_info}!",
            parse_mode=ParseMode.HTML
        )

    except Exception as e:
        logging.error(f"[set_filter] Error setting filter: {e}", exc_info=True)
        await update.message.reply_text(f"❌ Error setting filter: {e}")

async def list_filters(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List all filters in the current chat."""
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    
    can_ban, _ = await can_user_ban(chat_id, user_id, context)
    if not can_ban:
        await update.message.reply_text("Only admins with 'Ban Users' permission can use this command.")
        return

    chat_filters = get_chat_filters(chat_id)
    if not chat_filters:
        await update.message.reply_text("No filters active in this chat.")
        return
    
    msg = "🔍 <b>Active Filters in this chat:</b>\n\n"
    for trigger, data in chat_filters.items():
        if isinstance(data, dict):
            btns = data.get("buttons", [])
            btn_count = sum(len(r) for r in normalize_buttons(btns))
            btn_badge = f" <i>(🔘 {btn_count} btn)</i>" if btn_count > 0 else ""
            msg += f"• <code>{trigger}</code>{btn_badge}\n"
        else:
            msg += f"• <code>{trigger}</code>\n"
    
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton(colored_button("❌ Close", "default"), callback_data="close_filter")]])
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML, reply_markup=keyboard)

async def close_filter(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Close the filters list."""
    query = update.callback_query
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    
    if not await is_user_admin(chat_id, user_id, context):
        await query.answer("Only admins can close this list.", show_alert=True)
        return
        
    try:
        await query.message.delete()
    except Exception:
        pass
    await query.answer()

async def stop_filter(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Stop a specific filter."""
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    
    can_ban, _ = await can_user_ban(chat_id, user_id, context)
    if not can_ban:
        await update.message.reply_text("Only admins with 'Ban Users' permission can use this command.")
        return

    if not context.args:
        await update.message.reply_text("Usage: <code>/stop &lt;trigger&gt;</code>", parse_mode=ParseMode.HTML)
        return
    
    trigger = " ".join(context.args).lower().strip()
    if remove_chat_filter(chat_id, trigger):
        await update.message.reply_text(f"🛑 Stopped filter for <code>{trigger}</code>.", parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text(f"Could not find filter for <code>{trigger}</code>.", parse_mode=ParseMode.HTML)

async def stop_all_filters(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Stop all filters in the chat."""
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    
    can_ban, _ = await can_user_ban(chat_id, user_id, context)
    if not can_ban:
        await update.message.reply_text("Only admins with 'Ban Users' permission can use this command.")
        return

    if remove_all_chat_filters(chat_id):
        await update.message.reply_text("🗑 All filters have been deleted. This cannot be undone.")
    else:
        await update.message.reply_text("No filters found to delete.")

async def _send_safe_filter_reply(send_func, *args, **kwargs):
    """Safely deliver filter reply, falling back if HTML parsing fails."""
    try:
        await send_func(*args, **kwargs)
    except Exception as e:
        err_str = str(e).lower()
        if "can't parse entities" in err_str or "entity" in err_str:
            kwargs.pop("parse_mode", None)
            await send_func(*args, **kwargs)
        else:
            raise e

async def filter_reply_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Check every message for a filter trigger and dispatch reply with colored buttons."""
    if not update.message:
        return
    
    text = (update.message.text or update.message.caption or "").lower()
    if not text:
        return
        
    chat_id = update.effective_chat.id
    chat_filters = get_chat_filters(chat_id)
    if not chat_filters:
        return
    
    for trigger, data in chat_filters.items():
        if not trigger:
            continue
        # Match trigger as whole word or phrase
        pattern = r'(?<!\w)' + re.escape(trigger) + r'(?!\w)'
        if re.search(pattern, text) or trigger in text:
            msg_type = data.get("type", "text") if isinstance(data, dict) else "text"
            content = data.get("content") if isinstance(data, dict) else data
            caption = data.get("caption") if isinstance(data, dict) else None
            buttons = data.get("buttons") if isinstance(data, dict) else None
            
            try:
                reply_markup = None
                if buttons:
                    reply_markup = build_colored_keyboard(buttons)
                elif isinstance(data, dict) and data.get("reply_markup"):
                    reply_markup = InlineKeyboardMarkup.de_json(data["reply_markup"], context.bot)
                    
                # Fallback: if unparsed markdown buttons exist in content/caption
                if not reply_markup:
                    check_text = content or caption or ""
                    if "[" in check_text and ("|" in check_text or "buttonurl:" in check_text):
                        clean_t, parsed_btns = parse_markdown_buttons(check_text)
                        if parsed_btns:
                            reply_markup = build_colored_keyboard(parsed_btns)
                            if content:
                                content = clean_t
                            if caption:
                                caption = clean_t

                parse_mode = ParseMode.HTML
                
                if msg_type == "text":
                    await _send_safe_filter_reply(update.message.reply_text, content, parse_mode=parse_mode, reply_markup=reply_markup)
                elif msg_type == "sticker":
                    await update.message.reply_sticker(content, reply_markup=reply_markup)
                elif msg_type == "photo":
                    await _send_safe_filter_reply(update.message.reply_photo, content, caption=caption, parse_mode=parse_mode, reply_markup=reply_markup)
                elif msg_type == "video":
                    await _send_safe_filter_reply(update.message.reply_video, content, caption=caption, parse_mode=parse_mode, reply_markup=reply_markup)
                elif msg_type == "document":
                    await _send_safe_filter_reply(update.message.reply_document, content, caption=caption, parse_mode=parse_mode, reply_markup=reply_markup)
                elif msg_type == "animation":
                    await _send_safe_filter_reply(update.message.reply_animation, content, caption=caption, parse_mode=parse_mode, reply_markup=reply_markup)
                elif msg_type == "voice":
                    await update.message.reply_voice(content, reply_markup=reply_markup)
                break
            except Exception as e:
                logging.error(f"[filter_reply_handler] Error sending filter reply for '{trigger}': {e}", exc_info=True)

def get_filter_handlers():
    return [
        CommandHandler("filter", set_filter),
        CommandHandler("filters", list_filters),
        CommandHandler("stop", stop_filter),
        CommandHandler("stopall", stop_all_filters),
        CallbackQueryHandler(close_filter, pattern="^close_filter$"),
        MessageHandler(filters.ALL & ~filters.COMMAND & filters.ChatType.GROUPS, filter_reply_handler)
    ]
