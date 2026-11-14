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

# --- Button Color Maps & Markdown Parsing Utilities ---
import re

BUTTON_COLOR_MAP = {
    "#g": "green", "#green": "green", "#success": "green", "green": "green", "success": "green",
    "#r": "red", "#red": "red", "#danger": "red", "red": "red", "danger": "red",
    "#p": "blue", "#primary": "blue", "#blue": "blue", "#b": "blue", "blue": "blue", "primary": "blue",
    "#default": "default", "default": "default"
}

def clean_button_url(url: str) -> str:
    """Clean and validate a button URL, auto-prefixing https:// if scheme is missing."""
    if not url:
        return ""
    url = url.strip()
    if url.startswith("buttonurl://"):
        url = url[len("buttonurl://"):]
    elif url.startswith("buttonurl:"):
        url = url[len("buttonurl:"):]
    if not (url.startswith("http://") or url.startswith("https://") or url.startswith("tg://")):
        url = "https://" + url
    return url

def parse_markdown_buttons(text: str):
    """Parse inline button definitions from markdown text and strip them from the content.
    
    Supports:
    - [Button Text](buttonurl:url) and [Button Text](buttonurl:url:same)
    - [Button Text | url] and [Button Text | url | same]
    - [#g Button Text | url] or [Button Text | url | green]
    - [#r Button Text | url] or [Button Text | url | red]
    - [#p Button Text | url] or [Button Text | url | blue]
    
    Returns:
        Tuple[str, List[List[dict]]]: (cleaned_text, buttons_matrix)
    """
    if not text:
        return text, []

    pattern = re.compile(
        r'\[(?P<label>[^\]]+?)\]\((?:buttonurl:/*)?(?P<url_btn>[^\s\)]+?)\)'
        r'|'
        r'\[(?P<pipe_content>[^\]]+?\|[^\]]+?)\]',
        re.DOTALL
    )

    buttons = []
    last_end = 0
    text_pieces = []

    lines = text.splitlines(keepends=True)
    line_offsets = []
    curr_off = 0
    for l in lines:
        line_offsets.append(curr_off)
        curr_off += len(l)

    def get_line_num(pos):
        for idx, off in enumerate(line_offsets):
            if pos < off:
                return max(0, idx - 1)
        return len(line_offsets) - 1

    last_line_num = -1
    matches = list(pattern.finditer(text))

    for m in matches:
        start, end = m.span()
        text_pieces.append(text[last_end:start])
        last_end = end

        line_num = get_line_num(start)
        btn_label = None
        btn_url = None
        btn_color = "default"
        same_row = False

        if m.group('label') and m.group('url_btn'):
            raw_label = m.group('label').strip()
            raw_url = m.group('url_btn').strip()
            if raw_url.endswith(':same'):
                raw_url = raw_url[:-5].strip()
                same_row = True

            if '|' in raw_label:
                parts = [p.strip() for p in raw_label.split('|')]
                raw_label = parts[0]
                for p in parts[1:]:
                    p_low = p.lower()
                    if p_low in BUTTON_COLOR_MAP:
                        btn_color = BUTTON_COLOR_MAP[p_low]
                    elif p_low in ['same', ':same']:
                        same_row = True

            tag_match = re.match(r'^(#g|#r|#p|#b|#success|#green|#danger|#red|#primary|#blue|#default)\s+', raw_label, re.IGNORECASE)
            if tag_match:
                tag = tag_match.group(1).lower()
                btn_color = BUTTON_COLOR_MAP.get(tag, "default")
                raw_label = raw_label[tag_match.end():].strip()

            btn_label = raw_label
            btn_url = clean_button_url(raw_url)

        elif m.group('pipe_content'):
            pipe_str = m.group('pipe_content').strip()
            parts = [p.strip() for p in pipe_str.split('|')]
            if len(parts) >= 2:
                raw_label = parts[0]
                raw_url = parts[1]
                for extra in parts[2:]:
                    extra_low = extra.lower()
                    if extra_low in [':same', 'same']:
                        same_row = True
                    elif extra_low in BUTTON_COLOR_MAP:
                        btn_color = BUTTON_COLOR_MAP[extra_low]

                tag_match = re.match(r'^(#g|#r|#p|#b|#success|#green|#danger|#red|#primary|#blue|#default)\s+', raw_label, re.IGNORECASE)
                if tag_match:
                    tag = tag_match.group(1).lower()
                    btn_color = BUTTON_COLOR_MAP.get(tag, "default")
                    raw_label = raw_label[tag_match.end():].strip()

                if raw_url.endswith(':same'):
                    raw_url = raw_url[:-5].strip()
                    same_row = True

                btn_label = raw_label
                btn_url = clean_button_url(raw_url)

        if btn_label and btn_url:
            button_obj = {'text': btn_label, 'url': btn_url, 'color': btn_color}
            if (same_row or line_num == last_line_num) and buttons:
                buttons[-1].append(button_obj)
            else:
                buttons.append([button_obj])
            last_line_num = line_num

    text_pieces.append(text[last_end:])
    cleaned_text = ''.join(text_pieces).strip()
    return cleaned_text, buttons

def parse_line_buttons(text: str):
    """Parse buttons defined line-by-line (e.g. Label | URL | color | same)."""
    buttons = []
    lines = [l.strip() for l in text.strip().splitlines() if l.strip()]
    for line in lines:
        if '|' in line:
            parts = [p.strip() for p in line.split('|')]
            if len(parts) >= 2:
                raw_label = parts[0]
                raw_url = parts[1]
                btn_color = "default"
                same_row = False
                for extra in parts[2:]:
                    extra_low = extra.lower()
                    if extra_low in [':same', 'same']:
                        same_row = True
                    elif extra_low in BUTTON_COLOR_MAP:
                        btn_color = BUTTON_COLOR_MAP[extra_low]

                tag_match = re.match(r'^(#g|#r|#p|#b|#success|#green|#danger|#red|#primary|#blue|#default)\s+', raw_label, re.IGNORECASE)
                if tag_match:
                    tag = tag_match.group(1).lower()
                    btn_color = BUTTON_COLOR_MAP.get(tag, "default")
                    raw_label = raw_label[tag_match.end():].strip()

                btn_url = clean_button_url(raw_url)
                btn_obj = {'text': raw_label, 'url': btn_url, 'color': btn_color}
                if same_row and buttons:
                    buttons[-1].append(btn_obj)
                else:
                    buttons.append([btn_obj])
    return buttons

def normalize_buttons(buttons):
    """Normalize any button representation (matrix, flat list, or single dict) into List[List[dict]]."""
    if not buttons:
        return []
    if isinstance(buttons, dict):
        buttons = [buttons]
    if isinstance(buttons, list) and buttons:
        if isinstance(buttons[0], list):
            res = []
            for row in buttons:
                res_row = []
                for b in row:
                    if isinstance(b, dict):
                        text = b.get('text') or b.get('label') or ''
                        url = b.get('url') or ''
                        color = b.get('color') or 'default'
                        cb = b.get('callback_data')
                        if text and (url or cb):
                            res_row.append({'text': text, 'url': url, 'color': color, 'callback_data': cb})
                if res_row:
                    res.append(res_row)
            return res
        elif isinstance(buttons[0], dict):
            res = []
            for b in buttons:
                text = b.get('text') or b.get('label') or ''
                url = b.get('url') or ''
                color = b.get('color') or 'default'
                cb = b.get('callback_data')
                if text and (url or cb):
                    res.append([{'text': text, 'url': url, 'color': color, 'callback_data': cb}])
            return res
    return []

def build_colored_keyboard(buttons):
    """Build an InlineKeyboardMarkup from button definitions, applying colored_button styles."""
    matrix = normalize_buttons(buttons)
    if not matrix:
        return None
    try:
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        keyboard = []
        for row in matrix:
            row_buttons = []
            for btn in row:
                text = btn.get('text', '')
                color = btn.get('color', 'default')
                url = btn.get('url')
                cb = btn.get('callback_data')
                if not text:
                    continue
                styled_text = colored_button(text, color)
                if url:
                    row_buttons.append(InlineKeyboardButton(text=styled_text, url=url))
                elif cb:
                    row_buttons.append(InlineKeyboardButton(text=styled_text, callback_data=cb))
            if row_buttons:
                keyboard.append(row_buttons)
        if keyboard:
            return InlineKeyboardMarkup(keyboard)
    except Exception as e:
        logging.error(f"[build_colored_keyboard] Error building keyboard: {e}")
    return None

def format_buttons_for_display(buttons):
    """Format buttons into a human-readable display string with color badges."""
    matrix = normalize_buttons(buttons)
    if not matrix:
        return "<i>No buttons configured.</i>"
    color_icons = {"green": "🟢", "red": "🔴", "blue": "🔵", "default": "⚪"}
    lines = []
    for r_idx, row in enumerate(matrix):
        row_str = []
        for b in row:
            icon = color_icons.get(b.get('color', 'default'), "⚪")
            row_str.append(f"{icon} [{b.get('text', '')}]({b.get('url', '')})")
        lines.append(f"<b>Row {r_idx + 1}:</b> " + " | ".join(row_str))
    return "\n".join(lines)

def extract_buttons_from_markup(reply_markup):
    """Extract matrix of normalized button dicts from an InlineKeyboardMarkup."""
    if not reply_markup or not hasattr(reply_markup, 'inline_keyboard'):
        return []
    matrix = []
    for row in reply_markup.inline_keyboard:
        row_btns = []
        for b in row:
            text = b.text or ""
            url = b.url or ""
            cb = getattr(b, 'callback_data', None)
            color = "default"
            api_kwargs = getattr(b, 'api_kwargs', None) or {}
            if 'style' in api_kwargs:
                st = api_kwargs['style']
                if st == 'success': color = 'green'
                elif st == 'danger': color = 'red'
                elif st == 'primary': color = 'blue'
            tag_match = re.match(r'^(#g|#r|#p|#b|#success|#green|#danger|#red|#primary|#blue|#default)\s+', text, re.IGNORECASE)
            if tag_match:
                tag = tag_match.group(1).lower()
                color = BUTTON_COLOR_MAP.get(tag, "default")
                text = text[tag_match.end():].strip()
            if text and (url or cb):
                row_btns.append({'text': text, 'url': url, 'color': color, 'callback_data': cb})
        if row_btns:
            matrix.append(row_btns)
    return matrix

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
            if api_kwargs is None:
                api_kwargs = {}
            else:
                api_kwargs = dict(api_kwargs)
            api_kwargs['style'] = style
            kwargs['api_kwargs'] = api_kwargs
            
        _original_inline_init(self, text, *args, **kwargs)

    telegram.InlineKeyboardButton.__init__ = _patched_inline_init
except ImportError:
    pass
