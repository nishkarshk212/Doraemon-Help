import os
from dotenv import load_dotenv
import logging

from features.font import to_small_caps

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
LOG_CHANNEL_ID = os.getenv("LOG_CHANNEL_ID")
OWNER_ID = int(os.getenv("OWNER_ID", 8519966775))

START_IMG = os.getenv("START_IMG", "https://i.ibb.co/xSd6kWdj/Group-Help-Bot-Shield-Branding.png").split()

async def log_to_channel(context, message):
    """Logs a message to the specified log channel (non-blocking async dispatch)."""
    if not LOG_CHANNEL_ID:
        return

    async def _dispatch_log():
        try:
            await context.bot.send_message(chat_id=LOG_CHANNEL_ID, text=f"🔔 #LOG\n\n{message}")
        except Exception as e:
            logging.error(f"Failed to log to channel: {e}")

    try:
        import asyncio
        loop = asyncio.get_event_loop()
        if loop.is_running():
            asyncio.create_task(_dispatch_log())
        else:
            await _dispatch_log()
    except Exception:
        try:
            await _dispatch_log()
        except Exception:
            pass

async def delete_admin_command(update, context):
    """Helper function to delete an admin's command message."""
    if update.message and update.message.text and update.message.text.startswith('/'):
        from managers.user_manager_mongo import is_user_admin
        chat_id = update.effective_chat.id
        user_id = update.effective_user.id
        
        # Don't delete if it's a private chat
        if update.effective_chat.type == "private":
            return
            
        # Check if user is admin/owner
        if await is_user_admin(chat_id, user_id, context):
            try:
                # We use a job to delete it slightly later to ensure the command 
                # handler has received the message and started processing
                if context.job_queue:
                    context.job_queue.run_once(
                        delete_message_job,
                        1, # 1 second delay
                        data={"chat_id": chat_id, "message_id": update.message.message_id}
                    )
            except Exception:
                pass

async def send_bot_response(update, context, text, **kwargs):
    """Sends a bot response formatted with small caps and schedules deletion in 30s."""
    logging.info(f"[RESPONSE] Sending response to chat {update.effective_chat.id if update.effective_chat else 'Unknown'}")
    # Convert text to small caps
    formatted_text = to_small_caps(text)
    logging.info(f"[RESPONSE] Formatted text: {formatted_text[:50]}...")
    
    # Send message - Try replying first, fallback to normal message if original is gone
    try:
        msg = await update.message.reply_text(formatted_text, **kwargs)
        logging.info(f"[RESPONSE] Sent via reply_text")
    except Exception as e:
        # If reply fails (e.g. original message deleted), send to chat normally
        logging.info(f"[RESPONSE] reply_text failed: {e}, falling back to send_message")
        msg = await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=formatted_text,
            **kwargs
        )
        logging.info(f"[RESPONSE] Sent via send_message")
    
    # Handle command deletion if enabled for admins (after sending response)
    await delete_admin_command(update, context)
    
    # Schedule deletion in 30 seconds (only in groups, never in private DMs)
    if update.effective_chat and update.effective_chat.type != "private" and context.job_queue and msg:
        context.job_queue.run_once(
            delete_message_job,
            30,
            data={"chat_id": msg.chat_id, "message_id": msg.message_id}
        )
    return msg

async def send_bot_media(update, context, video=None, photo=None, caption="", **kwargs):
    """Sends a bot media response with formatted caption and robust text fallback."""
    formatted_caption = to_small_caps(caption)
    msg = None
    
    # Send media - Try replying first, fallback to direct send, fallback to plain text
    try:
        if video:
            msg = await update.message.reply_video(video, caption=formatted_caption, **kwargs)
        elif photo:
            msg = await update.message.reply_photo(photo, caption=formatted_caption, **kwargs)
    except Exception as e1:
        try:
            if video:
                msg = await context.bot.send_video(chat_id=update.effective_chat.id, video=video, caption=formatted_caption, **kwargs)
            elif photo:
                msg = await context.bot.send_photo(chat_id=update.effective_chat.id, photo=photo, caption=formatted_caption, **kwargs)
        except Exception as e2:
            logging.error(f"Failed to send media ({e1}, {e2}). Falling back to text response.")
            # Media failed (e.g. image URL error or timeout) -> Fallback to text message
            text_kwargs = {k: v for k, v in kwargs.items() if k in ['reply_markup', 'parse_mode', 'disable_web_page_preview']}
            try:
                msg = await update.message.reply_text(formatted_caption, **text_kwargs)
            except Exception:
                msg = await context.bot.send_message(chat_id=update.effective_chat.id, text=formatted_caption, **text_kwargs)

    # Handle command deletion if enabled for admins (after sending response)
    await delete_admin_command(update, context)

    # Schedule deletion in 30 seconds (only in groups, never in private DMs)
    if update.effective_chat and update.effective_chat.type != "private" and context.job_queue and msg:
        context.job_queue.run_once(
            delete_message_job,
            30,
            data={"chat_id": msg.chat_id, "message_id": msg.message_id}
        )
    return msg

async def edit_bot_response(query, context, text, **kwargs):
    """Edits a bot message with small caps formatting."""
    formatted_text = to_small_caps(text)
    try:
        await query.edit_message_text(formatted_text, **kwargs)
    except Exception as e:
        logging.error(f"[edit_bot_response] Error: {e}", exc_info=True)

async def delete_message_job(context):
    """Job function to delete a message."""
    data = context.job.data
    try:
        await context.bot.delete_message(chat_id=data["chat_id"], message_id=data["message_id"])
    except Exception:
        pass

def colored_button(text, color="default"):
    """Add color prefix to button text for background styling.
    
    Args:
        text: Button text
        color: 'green', 'red', 'blue', 'default'
    
    Returns:
        Text with color prefix
    """
    if color == "green":
        return f"#g {text}"
    elif color == "red":
        return f"#r {text}"
    elif color == "blue":
        return f"#p {text}"
    return text

# --- Monkey-patch telegram.InlineKeyboardButton to support native background styling ---
try:
    import telegram
    _original_inline_init = telegram.InlineKeyboardButton.__init__

    def _patched_inline_init(self, text, *args, **kwargs):
        style = None
        # Parse text for color tags
        if text.startswith("#g ") or text == "#g":
            style = "success"
            text = text[3:] if text.startswith("#g ") else ""
        elif text.startswith("#r ") or text == "#r":
            style = "danger"
            text = text[3:] if text.startswith("#r ") else ""
        elif text.startswith("#p ") or text == "#p":
            style = "primary"
            text = text[3:] if text.startswith("#p ") else ""
            
        if style:
            api_kwargs = kwargs.get('api_kwargs', {})
            api_kwargs['style'] = style
            kwargs['api_kwargs'] = api_kwargs
            
        _original_inline_init(self, text, *args, **kwargs)

    telegram.InlineKeyboardButton.__init__ = _patched_inline_init
except ImportError:
    pass
