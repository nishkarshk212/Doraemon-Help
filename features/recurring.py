import datetime
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import ContextTypes, CallbackQueryHandler, MessageHandler, filters
from managers.settings_manager_mongo import get_chat_settings, update_chat_setting
from core.config import (
    colored_button,
    send_bot_response,
    edit_bot_response,
    clean_button_url,
    parse_markdown_buttons,
    parse_line_buttons,
    normalize_buttons,
    build_colored_keyboard,
    format_buttons_for_display,
)

async def get_recurring_keyboard(settings):
    """Generate keyboard for the main recurring messages view."""
    recurring_messages = settings.get("recurring_messages", [])
    keyboard = []
    
    # "Add message" button (limit to 5 slots)
    if len(recurring_messages) < 5:
        keyboard.append([InlineKeyboardButton(colored_button("➕ Add message", "blue"), callback_data="set_recurring_add")])
    
    for msg in recurring_messages:
        msg_id = msg['id']
        is_active = msg.get('active', False)
        active_status = "✅ Active" if is_active else "❌ Inactive"
        active_color = "green" if is_active else "red"
        
        # Row with [Slot Button] [Active/Inactive Toggle] [Delete Button]
        keyboard.append([
            InlineKeyboardButton(colored_button(f"🗯 {msg_id}", "blue"), callback_data=f"set_recurring_config_text_{msg_id}"),
            InlineKeyboardButton(colored_button(f"{active_status}", active_color), callback_data=f"set_recurring_toggle_active_{msg_id}"),
            InlineKeyboardButton(colored_button("🗑", "red"), callback_data=f"set_recurring_delete_{msg_id}")
        ])
        
    keyboard.append([InlineKeyboardButton(colored_button("🔙 Back", "default"), callback_data="set_view_main")])
    return InlineKeyboardMarkup(keyboard)

async def send_recurring_payload(bot, chat_id: int, msg: dict) -> bool:
    """Deliver a recurring message with its text, media, and colored inline buttons."""
    text = msg.get('text')
    media = msg.get('media')
    media_type = msg.get('media_type')
    buttons = msg.get('buttons', [])
    
    clean_text = text
    reply_markup = None
    if buttons:
        reply_markup = build_colored_keyboard(buttons)
    elif text and ("[" in text and ("|" in text or "buttonurl:" in text)):
        clean_text, parsed_btns = parse_markdown_buttons(text)
        if parsed_btns:
            reply_markup = build_colored_keyboard(parsed_btns)
            
    if not clean_text and not media:
        return False
        
    try:
        if media:
            if media_type == 'photo':
                await bot.send_photo(chat_id=chat_id, photo=media, caption=clean_text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
            elif media_type == 'video':
                await bot.send_video(chat_id=chat_id, video=media, caption=clean_text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
            elif media_type == 'animation':
                await bot.send_animation(chat_id=chat_id, animation=media, caption=clean_text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
        else:
            await bot.send_message(chat_id=chat_id, text=clean_text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
        return True
    except Exception as e:
        err_str = str(e).lower()
        if "can't parse entities" in err_str or "entity" in err_str:
            # Fallback retry without HTML parse_mode
            if media:
                if media_type == 'photo':
                    await bot.send_photo(chat_id=chat_id, photo=media, caption=clean_text, reply_markup=reply_markup)
                elif media_type == 'video':
                    await bot.send_video(chat_id=chat_id, video=media, caption=clean_text, reply_markup=reply_markup)
                elif media_type == 'animation':
                    await bot.send_animation(chat_id=chat_id, animation=media, caption=clean_text, reply_markup=reply_markup)
            else:
                await bot.send_message(chat_id=chat_id, text=clean_text, reply_markup=reply_markup)
            return True
        raise e

async def recurring_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    chat_id = context.user_data.get('settings_chat_id', update.effective_chat.id)
    settings = get_chat_settings(chat_id)
    recurring_messages = settings.get("recurring_messages", [])
    
    if data == "set_view_recurring" or data.startswith("set_recurring_toggle_") or data.startswith("set_recurring_delete_") or data == "set_recurring_add":
        # Handle additions
        if data == "set_recurring_add":
            new_id = 1
            if recurring_messages:
                new_id = max(m['id'] for m in recurring_messages) + 1
            
            new_msg = {
                "id": new_id,
                "active": False,
                "type": "time",
                "interval": 1440,
                "message_interval": 100,
                "text": None,
                "media": None,
                "media_type": None,
                "buttons": [],
                "last_sent_at": None,
                "current_message_count": 0
            }
            recurring_messages.append(new_msg)
            update_chat_setting(chat_id, "recurring_messages", recurring_messages)
            settings = get_chat_settings(chat_id)
            await query.answer("New message slot added!")
            
        # Handle deletions
        elif data.startswith("set_recurring_delete_"):
            msg_id = int(data.split("_")[-1])
            recurring_messages = [m for m in recurring_messages if m['id'] != msg_id]
            update_chat_setting(chat_id, "recurring_messages", recurring_messages)
            settings = get_chat_settings(chat_id)
            await query.answer("Message slot deleted!")

        # Handle toggles
        elif data.startswith("set_recurring_toggle_active_"):
            msg_id = int(data.split("_")[-1])
            for msg in recurring_messages:
                if msg['id'] == msg_id:
                    msg['active'] = not msg.get('active', False)
                    break
            update_chat_setting(chat_id, "recurring_messages", recurring_messages)
            settings = get_chat_settings(chat_id)
            await query.answer("Status updated!")

        # Build message text
        text = "🕑 <b>Recurring messages</b>\n"
        text += "From this menu you can set messages that will be sent repeatedly to the group every few minutes/hours or every few messages.\n\n"
        text += f"<b>Current time:</b> {datetime.datetime.now().strftime('%d %b %Y, %H:%M')}\n\n"
        
        if not recurring_messages:
            text += "<i>No recurring messages set.</i>"
        else:
            for msg in recurring_messages:
                msg_id = msg['id']
                active_icon = "✅" if msg.get('active', False) else "❌"
                id_emoji = f"{msg_id}️⃣" if msg_id <= 10 else f"{msg_id}"
                
                text += f"🗯 {id_emoji} • <b>Active</b> {active_icon}\n"
                
                # Last sent time
                last_sent = msg.get('last_sent_at')
                if last_sent:
                    if isinstance(last_sent, str):
                        try:
                            last_sent = datetime.datetime.fromisoformat(last_sent)
                            time_str = last_sent.strftime("%H:%M")
                        except Exception:
                            time_str = "N/A"
                    else:
                        time_str = last_sent.strftime("%H:%M")
                else:
                    time_str = "Never"
                
                text += f" ├ <i>Time: {time_str}</i>\n"
                
                # Interval
                msg_type = msg.get('type', 'time')
                if msg_type == 'time':
                    interval = msg.get('interval', 1440)
                    if interval >= 1440:
                        interval_str = f"{interval // 60} hours" if interval % 1440 == 0 else f"{interval} minutes"
                    elif interval >= 60:
                        interval_str = f"{interval // 60} hours"
                    else:
                        interval_str = f"{interval} minutes"
                    text += f" ├ <i>Every {interval_str}</i>\n"
                else:
                    interval = msg.get('message_interval', 100)
                    text += f" ├ <i>Every {interval} messages</i>\n"
                
                # Message content status
                msg_text = msg.get('text')
                msg_media = msg.get('media')
                btns = msg.get('buttons', [])
                btn_count = sum(len(r) for r in normalize_buttons(btns))
                btn_str = f" • 🔘 <i>{btn_count} btn(s)</i>" if btn_count > 0 else ""
                
                if msg_text or msg_media:
                    content_preview = msg_text[:20] + "..." if msg_text and len(msg_text) > 20 else (msg_text or "Media only")
                    text += f" └ <i>Message: {content_preview}{btn_str}</i>\n"
                else:
                    text += f" └ <i>Message is not set.{btn_str}</i>\n"
                text += "\n"

        await edit_bot_response(
            query, context,
            text,
            reply_markup=await get_recurring_keyboard(settings),
            parse_mode='HTML'
        )
        return

    # Configuration Sub-menu for a specific message
    if data.startswith("set_recurring_config_text_"):
        msg_id = int(data.split("_")[-1])
        context.user_data['config_recurring_id'] = msg_id
        
        msg = next((m for m in recurring_messages if m['id'] == msg_id), None)
        if not msg:
            await query.answer("Message slot not found!", show_alert=True)
            return
            
        msg_type = msg.get('type', 'time')
        type_label = "🕒 Time" if msg_type == 'time' else "💬 Messages"
        btns = msg.get('buttons', [])
        btn_count = sum(len(r) for r in normalize_buttons(btns))
        btn_label = f"🔡 URL Buttons ({btn_count})" if btn_count > 0 else "🔡 URL Buttons"
        
        keyboard = [
            [InlineKeyboardButton(colored_button(f"Type: {type_label}", "blue"), callback_data=f"set_recurring_toggle_type_{msg_id}")],
            [InlineKeyboardButton(colored_button("⚙️ Set Interval", "blue"), callback_data=f"set_recurring_config_interval_{msg_id}")],
            [InlineKeyboardButton(colored_button("📝 Set Message Text/Media", "blue"), callback_data=f"set_recurring_prompt_text_{msg_id}")],
            [
                InlineKeyboardButton(colored_button(btn_label, "green" if btn_count > 0 else "blue"), callback_data=f"set_recurring_buttons_{msg_id}"),
                InlineKeyboardButton(colored_button("👀 Preview", "blue"), callback_data=f"set_recurring_preview_{msg_id}")
            ],
            [InlineKeyboardButton(colored_button("🔙 Back to List", "default"), callback_data="set_view_recurring")]
        ]
        
        await edit_bot_response(
            query, context,
            f"⚙️ <b>Configuring Recurring Message {msg_id}</b>\n\nChoose what you want to change:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='HTML'
        )
        await query.answer()
        return

    # Interval configuration prompt
    if data.startswith("set_recurring_config_interval_"):
        msg_id = int(data.split("_")[-1])
        context.user_data['config_recurring_id'] = msg_id
        context.user_data['config_state'] = 'awaiting_recurring_interval'
        
        msg = next((m for m in recurring_messages if m['id'] == msg_id), None)
        msg_type = msg.get('type', 'time') if msg else 'time'
        
        if msg_type == 'time':
            instruction = "Please enter the interval in minutes (e.g., 60 for 1 hour, 1440 for 1 day):"
        else:
            instruction = "Please enter the message interval (e.g., 100 for every 100 messages):"
            
        await edit_bot_response(
            query, context,
            f"⚙️ <b>Configuring Interval for Message {msg_id}</b>\n\n{instruction}",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(colored_button("Cancel", "default"), callback_data=f"set_recurring_config_text_{msg_id}")]]),
            parse_mode='HTML'
        )
        await query.answer()
        return

    # Toggle Type (Time vs Messages)
    if data.startswith("set_recurring_toggle_type_"):
        msg_id = int(data.split("_")[-1])
        for msg in recurring_messages:
            if msg['id'] == msg_id:
                msg['type'] = 'messages' if msg.get('type', 'time') == 'time' else 'time'
                break
        update_chat_setting(chat_id, "recurring_messages", recurring_messages)
        query.data = f"set_recurring_config_text_{msg_id}"
        await recurring_callback(update, context)
        return

    # Prompt for text/media
    if data.startswith("set_recurring_prompt_text_"):
        msg_id = int(data.split("_")[-1])
        context.user_data['config_recurring_id'] = msg_id
        context.user_data['config_state'] = 'awaiting_recurring_text'
        
        prompt_body = (
            f"📝 <b>Configuring Text for Message {msg_id}</b>\n\n"
            f"Please send the message you want to be sent repeatedly. You can include media (photo/video/GIF).\n\n"
            f"💡 <i>Tip: You can include inline colored buttons right in your message text:</i>\n"
            f"<code>[#g Visit Channel | https://t.me/channel]</code>\n"
            f"<code>[#r Rules | https://t.me/rules] [#p Support | https://t.me/support :same]</code>"
        )
        
        await edit_bot_response(
            query, context,
            prompt_body,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(colored_button("Cancel", "default"), callback_data=f"set_recurring_config_text_{msg_id}")]]),
            parse_mode='HTML'
        )
        await query.answer()
        return

    # Buttons menu for recurring message
    if data.startswith("set_recurring_buttons_"):
        msg_id = int(data.split("_")[-1])
        context.user_data['config_recurring_id'] = msg_id
        
        msg = next((m for m in recurring_messages if m['id'] == msg_id), None)
        btns = msg.get('buttons', []) if msg else []
        btns_display = format_buttons_for_display(btns)
        
        btn_keyboard = [
            [InlineKeyboardButton(colored_button("➕ Set / Add Buttons", "green"), callback_data=f"set_recurring_btn_prompt_{msg_id}")],
            [InlineKeyboardButton(colored_button("🗑️ Clear Buttons", "red"), callback_data=f"set_recurring_btn_clear_{msg_id}")],
            [InlineKeyboardButton(colored_button("👀 Preview Message", "blue"), callback_data=f"set_recurring_preview_{msg_id}")],
            [InlineKeyboardButton(colored_button("🔙 Back to Message", "default"), callback_data=f"set_recurring_config_text_{msg_id}")]
        ]
        
        btn_text = (
            f"🔘 <b>Recurring Message {msg_id} - Inline Buttons</b>\n\n"
            f"Attach inline buttons styled with custom background colors:\n"
            f"• <b>Green:</b> <code>#g Button Text | https://t.me/link</code>\n"
            f"• <b>Red:</b> <code>#r Button Text | https://t.me/link</code>\n"
            f"• <b>Blue:</b> <code>#p Button Text | https://t.me/link</code>\n"
            f"• <b>Same row:</b> Add <code>:same</code> or <code>| same</code>\n\n"
            f"<b>Current Buttons:</b>\n{btns_display}"
        )
        
        await edit_bot_response(
            query, context,
            btn_text,
            reply_markup=InlineKeyboardMarkup(btn_keyboard),
            parse_mode='HTML'
        )
        await query.answer()
        return

    # Prompt for buttons input
    if data.startswith("set_recurring_btn_prompt_"):
        msg_id = int(data.split("_")[-1])
        context.user_data['config_recurring_id'] = msg_id
        context.user_data['config_state'] = 'awaiting_recurring_buttons'
        
        prompt_text = (
            f"🔡 <b>Send Buttons for Message {msg_id}</b>\n\n"
            f"Send the buttons in one of the following formats:\n\n"
            f"<b>Format 1 (Pipes with color tags):</b>\n"
            f"<code>[#g Join Channel | https://t.me/channel]</code>\n"
            f"<code>[#r Rules | https://t.me/rules] [#p Support | https://t.me/support :same]</code>\n\n"
            f"<b>Format 2 (Line by line):</b>\n"
            f"<code>#g Channel | https://t.me/channel</code>\n"
            f"<code>#r Community | https://t.me/group | same</code>\n"
            f"<code>Website | https://google.com | blue</code>\n\n"
            f"Send <code>/cancel</code> to abort."
        )
        
        await edit_bot_response(
            query, context,
            prompt_text,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(colored_button("Cancel", "default"), callback_data=f"set_recurring_buttons_{msg_id}")]]),
            parse_mode='HTML'
        )
        await query.answer()
        return

    # Clear buttons for this recurring message
    if data.startswith("set_recurring_btn_clear_"):
        msg_id = int(data.split("_")[-1])
        for m in recurring_messages:
            if m['id'] == msg_id:
                m['buttons'] = []
                break
        update_chat_setting(chat_id, "recurring_messages", recurring_messages)
        await query.answer("All buttons cleared for this message!")
        query.data = f"set_recurring_buttons_{msg_id}"
        await recurring_callback(update, context)
        return

    # Preview recurring message
    if data.startswith("set_recurring_preview_"):
        msg_id = int(data.split("_")[-1])
        msg = next((m for m in recurring_messages if m['id'] == msg_id), None)
        if not msg:
            await query.answer("Message not found!", show_alert=True)
            return
            
        try:
            success = await send_recurring_payload(context.bot, update.effective_chat.id, msg)
            if success:
                await query.answer("👀 Preview sent above!", show_alert=False)
            else:
                await query.answer("❌ Message text or media has not been set yet!", show_alert=True)
        except Exception as e:
            logging.error(f"[recurring_preview] Error sending preview: {e}")
            await query.answer(f"❌ Error sending preview: {e}", show_alert=True)
        return

async def handle_recurring_config_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle text input when configuring interval, message content, or buttons."""
    state = context.user_data.get('config_state')
    if not isinstance(state, str) or not state.startswith('awaiting_recurring_'):
        return False
    
    if update.message and update.message.text == "/cancel":
        context.user_data['config_state'] = None
        await update.message.reply_text("❌ Configuration cancelled.")
        return True

    chat_id = context.user_data.get('settings_chat_id')
    if not chat_id:
        return False
        
    msg_id = context.user_data.get('config_recurring_id')
    settings = get_chat_settings(chat_id)
    recurring_messages = settings.get("recurring_messages", [])
    
    target_msg = next((m for m in recurring_messages if m['id'] == msg_id), None)
    if not target_msg:
        return False
        
    if state == 'awaiting_recurring_interval':
        try:
            val = int(update.message.text)
            if val <= 0:
                raise ValueError
            
            if target_msg['type'] == 'time':
                target_msg['interval'] = val
            else:
                target_msg['message_interval'] = val
                
            update_chat_setting(chat_id, "recurring_messages", recurring_messages)
            await send_bot_response(update, context, "✅ Interval updated successfully!")
        except Exception:
            await send_bot_response(update, context, "❌ Invalid input. Please enter a positive number.")
            return True

    elif state == 'awaiting_recurring_buttons':
        text = update.message.text
        if not text:
            await send_bot_response(update, context, "❌ Please send text containing buttons.")
            return True
            
        if "[" in text and ("|" in text or "buttonurl:" in text):
            _, parsed_btns = parse_markdown_buttons(text)
        else:
            parsed_btns = parse_line_buttons(text)
            
        if not parsed_btns:
            await send_bot_response(
                update, context,
                "❌ Could not parse any valid buttons. Please check the format:\n<code>#g Label | https://link.com</code>"
            )
            return True
            
        target_msg['buttons'] = parsed_btns
        update_chat_setting(chat_id, "recurring_messages", recurring_messages)
        btn_count = sum(len(r) for r in parsed_btns)
        await send_bot_response(update, context, f"✅ Successfully saved {btn_count} colored button(s) for Recurring Message {msg_id}!")

    elif state == 'awaiting_recurring_text':
        raw_text = None
        media_id = None
        media_type = None
        
        if update.message.text:
            raw_text = update.message.text
        elif update.message.photo:
            raw_text = update.message.caption
            media_id = update.message.photo[-1].file_id
            media_type = 'photo'
        elif update.message.video:
            raw_text = update.message.caption
            media_id = update.message.video.file_id
            media_type = 'video'
        elif update.message.animation:
            raw_text = update.message.caption
            media_id = update.message.animation.file_id
            media_type = 'animation'
        else:
            await send_bot_response(update, context, "❌ Unsupported message type. Please send text, photo, video, or GIF.")
            return True
            
        # Parse inline buttons if present in text/caption
        if raw_text and ("[" in raw_text and ("|" in raw_text or "buttonurl:" in raw_text)):
            clean_text, parsed_btns = parse_markdown_buttons(raw_text)
            target_msg['text'] = clean_text
            if parsed_btns:
                target_msg['buttons'] = parsed_btns
        else:
            target_msg['text'] = raw_text
            
        target_msg['media'] = media_id
        target_msg['media_type'] = media_type
        update_chat_setting(chat_id, "recurring_messages", recurring_messages)
        await send_bot_response(update, context, "✅ Message content updated successfully!")

    # Reset state and show recurring menu
    context.user_data['config_state'] = None
    settings = get_chat_settings(chat_id)
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="🕑 <b>Recurring Messages</b>\n\nUpdated view:",
        reply_markup=await get_recurring_keyboard(settings),
        parse_mode='HTML'
    )
    return True

async def check_recurring_messages(context: ContextTypes.DEFAULT_TYPE):
    """Background job to send time-based recurring messages."""
    from managers.settings_manager_mongo import get_all_chat_settings, update_chat_setting
    
    all_settings = get_all_chat_settings()
    now = datetime.datetime.now()
    
    for chat_id_str, settings in all_settings.items():
        try:
            chat_id = int(chat_id_str)
        except Exception:
            continue
            
        recurring_messages = settings.get("recurring_messages", [])
        changed = False
        
        for msg in recurring_messages:
            if not msg.get('active', False) or msg.get('type') != 'time':
                continue
                
            last_sent = msg.get('last_sent_at')
            interval_mins = msg.get('interval', 1440)
            
            should_send = False
            if last_sent is None:
                should_send = True
            else:
                if isinstance(last_sent, str):
                    try:
                        last_sent = datetime.datetime.fromisoformat(last_sent)
                    except Exception:
                        last_sent = None
                
                if last_sent is None or (now - last_sent).total_seconds() / 60 >= interval_mins:
                    should_send = True
            
            if should_send:
                try:
                    sent = await send_recurring_payload(context.bot, chat_id, msg)
                    if sent:
                        msg['last_sent_at'] = now.isoformat()
                        changed = True
                except Exception as e:
                    err_str = str(e).lower()
                    if "not enough rights" in err_str or "forbidden" in err_str or "chat not found" in err_str:
                        logging.warning(f"Disabling recurring message in {chat_id} due to permission error: {e}")
                        msg['active'] = False
                        changed = True
                    else:
                        logging.error(f"Error sending recurring message to {chat_id}: {e}")
                    
        if changed:
            update_chat_setting(chat_id, "recurring_messages", recurring_messages)

async def count_recurring_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Message handler to count messages for message-based intervals."""
    if not update.effective_chat or update.effective_chat.type not in ['group', 'supergroup']:
        return
        
    if not update.message:
        return
        
    chat_id = update.effective_chat.id
    settings = get_chat_settings(chat_id)
    recurring_messages = settings.get("recurring_messages", [])
    changed = False
    
    for msg in recurring_messages:
        if not msg.get('active', False) or msg.get('type') != 'messages':
            continue
            
        msg['current_message_count'] = msg.get('current_message_count', 0) + 1
        
        if msg['current_message_count'] >= msg.get('message_interval', 100):
            try:
                sent = await send_recurring_payload(context.bot, chat_id, msg)
                if sent:
                    msg['current_message_count'] = 0
                    changed = True
            except Exception as e:
                logging.error(f"Error sending message-based recurring message to {chat_id}: {e}")
                
    if changed:
        update_chat_setting(chat_id, "recurring_messages", recurring_messages)

def get_recurring_handlers():
    return [
        CallbackQueryHandler(recurring_callback, pattern="^set_(view_recurring|recurring_)"),
        MessageHandler(filters.ALL & ~filters.COMMAND, handle_recurring_config_input)
    ]
