from telegram import Update, MessageEntity, InlineKeyboardButton, InlineKeyboardMarkup, ReactionTypeEmoji
from telegram.ext import ContextTypes, CommandHandler, CallbackQueryHandler, MessageHandler, MessageReactionHandler, filters
from telegram.constants import ParseMode
import html
from datetime import datetime, timedelta, timezone
import logging
import copy
import random
import asyncio
import re
import httpx

from core.config import OWNER_ID, send_bot_response, colored_button
from managers.settings_manager_mongo import get_chat_settings, update_chat_setting
from managers.user_manager_mongo import is_user_admin, cache_user, can_user_configure_settings, get_user_id
from moderation.moderation import can_user_ban, can_user_mute

# DEFAULT_SETTINGS for blocking (will be merged with MongoDB settings)
DEFAULT_BLOCKING_SETTINGS = {
    "blocking_enabled": True,
    "block_stickers": False,
    "block_premium_sticker": False,
    "block_link": False,
    "block_embed_link": False,
    "block_media": False,
    "block_documents": False,
    "block_forward": False,
    "block_channel_post": False,
    "block_command": False,
    "block_contact": False,
    "block_location": False,
    "block_voice": False,
    "block_audio": False,
    "block_text": False,
    "block_video_note": False,
    "block_poll": False,
    "block_dice": False,
    "block_game": False,
    "block_reactions": False,
    "msg_length_min": 0,
    "msg_length_max": 2000,
    "msg_length_delete": False,
    "msg_length_penalty": "off",  # off, warn, kick, mute, ban
    "custom_block_list": [],
    "custom_block_media": [],
    "custom_block_stickers": [],
    "user_permissions": {},  # {user_id: {"block_stickers": True, ...}}
    # Clean service settings
    "clean_service_enabled": True,
    "clean_join": False,
    "clean_left": False,
    "clean_title": False,
    "clean_photo": False,
    "clean_voice_start": False,
    "clean_voice_end": False,
    "clean_voice_schedule": False,
    "clean_voice_invite": False,
    "clean_pinned": False,
}

def get_random_premium_emoji():
    """Returns a random premium-looking emoji for UI enhancement."""
    emojis = ["💎", "✨", "🌟", "🛡️", "🔥", "👑", "⚡", "🎯", "🚀", "🌈", "✅"]
    return random.choice(emojis)

async def is_admin_or_creator(context, chat_id, user_id, message=None):
    """Check if user is admin/creator, including anonymous admin support."""
    # Anonymous admin proxy ID (1087968824)
    if user_id == 1087968824:
        logging.info(f"[BLOCKING] Detected anonymous admin message (user_id=1087968824)")
        return True
    
    # Also check if message has sender_chat (indicates anonymous admin or channel)
    if message and getattr(message, 'sender_chat', None):
        if message.sender_chat.id == chat_id:
            logging.info(f"[BLOCKING] Detected anonymous admin via sender_chat")
            return True
    
    return await is_user_admin(chat_id, user_id, context)

def is_emergency_active(settings, chat_id=None):
    """Checks if emergency mode is currently active based on time and settings."""
    if not settings.get("emergency_enabled", False):
        return False
    
    import pytz
    from datetime import datetime
    
    timezone_str = settings.get("nightmode_timezone", "Asia/Kolkata")
    try:
        tz = pytz.timezone(timezone_str)
    except Exception:
        tz = pytz.timezone("Asia/Kolkata")
        
    now = datetime.now(tz)
    current_time = now.strftime("%H:%M")
    start_time = settings.get("emergency_start_time", "00:00")
    end_time = settings.get("emergency_end_time", "23:59")
    mode = settings.get("emergency_mode", "daily")
    
    # Simple time comparison
    is_in_time_range = False
    if start_time <= end_time:
        is_in_time_range = start_time <= current_time <= end_time
    else:
        # Over midnight
        is_in_time_range = current_time >= start_time or current_time <= end_time
        
    # If mode is "today" and time range has passed for the day
    if mode == "today" and not is_in_time_range and chat_id:
        # If current time is past end_time, disable it
        # Note: This is a bit simplistic for "today" over midnight, but works for same-day
        if start_time <= end_time and current_time > end_time:
            from managers.settings_manager_mongo import update_chat_setting
            update_chat_setting(chat_id, "emergency_enabled", False)
            return False

    return is_in_time_range

async def send_blocking_notification(update: Update, context: ContextTypes.DEFAULT_TYPE, settings, default_text):
    """Sends a notification when content is blocked, respects settings."""
    chat_id = update.effective_chat.id
    msg = update.effective_message
    
    custom_text = settings.get("blocking_custom_text")
    if custom_text:
        # Simple formatting
        user = update.effective_user
        text = custom_text.replace("{NAME}", user.first_name).replace("{ID}", str(user.id))
    else:
        text = default_text
        
    try:
        sent_msg = await msg.reply_text(text, parse_mode='HTML')
        
        # Check if we should delete this notification
        if settings.get("blocking_delete_notifications", True):
            timer = settings.get("blocking_notification_timer", 30)
            
            async def delete_notification():
                await asyncio.sleep(timer)
                try:
                    await sent_msg.delete()
                except:
                    pass
                    
            asyncio.create_task(delete_notification())
            
    except Exception as e:
        logging.error(f"Error sending blocking notification: {e}")

async def check_emergency_notifications(chat_id, context, settings):
    """Checks if emergency mode state changed and sends start/end notifications."""
    is_active = is_emergency_active(settings, chat_id)
    
    # Store the last known state in context to detect transitions
    last_state = context.chat_data.get(f"emergency_last_state_{chat_id}", False)
    
    if is_active and not last_state:
        # Emergency mode JUST started
        blocking_types = []
        if settings.get("emergency_block_text"): blocking_types.append("📝 Text")
        if settings.get("emergency_block_stickers"): blocking_types.append("🖼️ Stickers")
        if settings.get("emergency_block_media"): blocking_types.append("📁 Media")
        if settings.get("emergency_block_links"): blocking_types.append("🔗 Links")
        if settings.get("emergency_block_premium"): blocking_types.append("💎 Premium Stickers")
        if settings.get("emergency_block_contact"): blocking_types.append("👤 Contact")
        if settings.get("emergency_block_location"): blocking_types.append("📍 Location")
        if settings.get("emergency_block_voice"): blocking_types.append("🎤 Voice")
        if settings.get("emergency_block_audio"): blocking_types.append("🎵 Audio")
        if settings.get("emergency_block_forward"): blocking_types.append("🔄 Forward")
        if settings.get("emergency_block_poll"): blocking_types.append("📊 Poll")
        
        apply_on = settings.get("emergency_apply_on", "members").title()
        end_time = settings.get("emergency_end_time", "23:59")
        
        types_str = "\n".join(blocking_types) if blocking_types else "All Content"
        
        notif_text = (
            f"🚨 <b>Emergency Mode Activated!</b> 🚨\n\n"
            f"ᴛʜᴇ ꜰᴏʟʟᴏᴡɪɴɢ ᴄᴏɴᴛᴇɴᴛ ɪꜱ ɴᴏᴡ ʀᴇꜱᴛʀɪᴄᴛᴇᴅ:\n"
            f"{types_str}\n\n"
            f"👤 <b>ᴀᴘᴘʟʏ ᴏɴ:</b> {apply_on}\n"
            f"🕒 <b>ᴇɴᴅꜱ ᴀᴛ:</b> {end_time}\n\n"
            f"<i>ᴛʜɪꜱ ɪꜱ ᴀ ꜱᴄʜᴇᴅᴜʟᴇᴅ ꜱᴇᴄᴜʀɪᴛʏ ᴘᴇʀɪᴏᴅ.</i>"
        )
        
        try:
            sent_msg = await context.bot.send_message(chat_id, notif_text, parse_mode='HTML')
            # Auto-pin the emergency notification
            try:
                await context.bot.pin_chat_message(chat_id, sent_msg.message_id, disable_notification=False)
                # Store message_id to unpin later
                context.chat_data[f"emergency_notif_msg_id_{chat_id}"] = sent_msg.message_id
            except Exception as pin_e:
                logging.error(f"Error pinning emergency notification: {pin_e}")
        except Exception as e:
            logging.error(f"Error sending emergency start notification: {e}")
            
    elif not is_active and last_state:
        # Emergency mode JUST ended
        notif_text = (
            f"✅ <b>Emergency Mode Ended!</b> ✅\n\n"
            f"ᴀʟʟ ꜱᴄʜᴇᴅᴜʟᴇᴅ ʀᴇꜱᴛʀɪᴄᴛɪᴏɴꜱ ʜᴀᴠᴇ ʙᴇᴇɴ ʟɪꜰᴛᴇᴅ. "
            f"ᴛʜᴇ ɢʀᴏᴜᴘ ɪꜱ ɴᴏᴡ ʙᴀᴄᴋ ᴛᴏ ꜱᴛᴀɴᴅᴀʀᴅ ᴏᴘᴇʀᴀᴛɪᴏɴꜱ."
        )
        try:
            # Unpin the start notification if we have the ID
            msg_id = context.chat_data.get(f"emergency_notif_msg_id_{chat_id}")
            if msg_id:
                try:
                    await context.bot.unpin_chat_message(chat_id, msg_id)
                    del context.chat_data[f"emergency_notif_msg_id_{chat_id}"]
                except Exception as unpin_e:
                    logging.error(f"Error unpinning emergency notification: {unpin_e}")
            
            await context.bot.send_message(chat_id, notif_text, parse_mode='HTML')
        except Exception as e:
            logging.error(f"Error sending emergency end notification: {e}")
            
    # Update last known state
    context.chat_data[f"emergency_last_state_{chat_id}"] = is_active

async def handle_blocking(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles all blocking logic for messages."""
    if not update.effective_chat or update.effective_chat.type == "private":
        return False

    chat_id = update.effective_chat.id
    settings = get_chat_settings(chat_id)
    
    # DEBUG: Log what get_chat_settings returns
    has_user_perms = 'user_permissions' in settings
    user_perms_count = len(settings.get('user_permissions', {}))
    logging.info(f"[DEBUG] get_chat_settings returned: has_user_permissions={has_user_perms}, count={user_perms_count}")
    
    # Merge with defaults
    for key, value in DEFAULT_BLOCKING_SETTINGS.items():
        if key not in settings:
            settings[key] = value
    
    # Check if blocking is enabled
    if not settings.get("blocking_enabled", True):
        return False
    
    msg = update.effective_message
    if not msg:
        return False

    # Check age of message (1 month = 30 days)
    # Only ignore if the message is older than 30 days
    message_date = msg.date
    if message_date:
        now = datetime.now(timezone.utc)
        if (now - message_date).days > 30:
            logging.info(f"[BLOCKING] Ignoring old message (ID: {msg.message_id}, Date: {message_date})")
            return False

    logging.info(f"[BLOCKING] Checking message in chat {chat_id}")

    should_delete = False
    penalty_reason = ""
    is_length_violation = False
    length_limit_val = 0
    actual_length = 0
    
    # Check Message Length
    if msg.text or msg.caption:
        content = (msg.text or "") + (msg.caption or "")
        length = len(content)
        actual_length = length
        
        limit = settings.get("msg_length_limit", 0)
        max_l = settings.get("msg_length_max", 0)
        
        # Determine effective maximum limit:
        # Priority: msg_length_limit > 0 (set by /config or /maxlength)
        # Or msg_length_delete is True and max_l > 0
        # Or max_l > 0 and max_l != 2000 (explicitly customized)
        if limit > 0:
            effective_max = limit
        elif settings.get("msg_length_delete", False) and max_l > 0:
            effective_max = max_l
        elif max_l > 0 and max_l != 2000:
            effective_max = max_l
        else:
            effective_max = 0
            
        min_l = settings.get("msg_length_min", 0)
        
        logging.info(f"[BLOCKING] Message length check: length={length}, min={min_l}, effective_max={effective_max}")
        
        if effective_max > 0 and length > effective_max:
            logging.info(f"[BLOCKING] Message length {length} exceeds maximum limit {effective_max}")
            is_admin = await is_admin_or_creator(context, chat_id, update.effective_user.id if update.effective_user else 0, msg)
            if not is_admin:
                should_delete = True
                is_length_violation = True
                length_limit_val = effective_max
        elif min_l > 0 and length < min_l:
            logging.info(f"[BLOCKING] Message length {length} is below minimum {min_l}")
            is_admin = await is_admin_or_creator(context, chat_id, update.effective_user.id if update.effective_user else 0, msg)
            if not is_admin:
                should_delete = True
                is_length_violation = True
                length_limit_val = min_l

    # Check if user is freed from specific blocking
    if not update.effective_user:
        return False
        
    user_id = update.effective_user.id
    user_perms = settings.get("user_permissions", {}).get(str(user_id), {})
    
    # Debug logging
    logging.info(f"[BLOCKING] User {user_id}, user_perms={user_perms}")

    # Check for Emergency Mode
    emergency_active = is_emergency_active(settings, chat_id)
    apply_on = settings.get("emergency_apply_on", "members") # everyone, admins, members
    
    if emergency_active:
        logging.info(f"[BLOCKING] Emergency mode is ACTIVE, apply_on={apply_on}")

    # Helper to check if user is freed from a specific block
    async def is_user_freed(block_key, emergency_type=None):
        # Check if user is admin/creator/owner
        is_admin = await is_admin_or_creator(context, chat_id, user_id, msg)
        is_owner = user_id == OWNER_ID
        
        # Check for specific high-level permissions (Ban or Change Info)
        is_high_level_admin = False
        if is_admin and not is_owner:
            try:
                # For anonymous admins, we can't check specific rights easily, 
                # so we treat them as high-level to be safe
                if user_id == 1087968824:
                    is_high_level_admin = True
                else:
                    member = await context.bot.get_chat_member(chat_id, user_id)
                    is_high_level_admin = member.can_restrict_members or member.can_change_info
            except: pass

        # If emergency is active and this type is blocked in emergency
        if emergency_active and emergency_type:
            if settings.get(f"emergency_block_{emergency_type}"):
                if apply_on == "everyone":
                    # Everyone is blocked, including owner
                    return False
                if apply_on == "admins":
                    # Apply on admins ONLY if they are NOT high-level admins
                    # If user is admin AND NOT high-level AND NOT owner -> Blocked
                    if is_admin and not is_high_level_admin and not is_owner:
                        return False
                    return True # Members, High-level admins, and Owner are freed
                if apply_on == "members":
                    # ONLY members are blocked
                    if not is_admin and not is_owner:
                        return False
                    return True # Admins and Owner are freed
        
        # In user_permissions, True means freed/allowed, False/missing means blocked
        freed = user_perms.get(block_key, False)
        
        # Admins are always freed from standard blocks
        if is_admin:
            return True
            
        logging.info(f"[BLOCKING] is_user_freed({block_key}) = {freed}")
        return freed

    # Check for basic types
    msg = update.effective_message
    if not msg:
        return False
        
    # Check for Emergency notification transition
    await check_emergency_notifications(chat_id, context, settings)
    
    if msg.sticker:
        is_premium = bool(msg.sticker.premium_animation or msg.sticker.custom_emoji_id)
        
        logging.info(f"[BLOCKING] Sticker detected, block_stickers={settings.get('block_stickers')}, is_premium={is_premium}")
        
        if (settings.get("block_stickers") or (emergency_active and settings.get("emergency_block_stickers"))) and not await is_user_freed("block_stickers", "stickers"):
            should_delete = True
        elif is_premium and (settings.get("block_premium_sticker") or (emergency_active and settings.get("emergency_block_premium"))) and not await is_user_freed("block_premium_sticker", "premium"):
            should_delete = True
    
    # Check for custom emojis and links in entities
    entities = list(msg.entities or []) + list(msg.caption_entities or [])
    for entity in entities:
        if entity.type == MessageEntity.CUSTOM_EMOJI:
            if (settings.get("block_premium_sticker") or (emergency_active and settings.get("emergency_block_premium"))) and not await is_user_freed("block_premium_sticker", "premium"):
                should_delete = True
        elif entity.type == MessageEntity.URL:
            if (settings.get("block_link") or (emergency_active and settings.get("emergency_block_links"))) and not await is_user_freed("block_link", "links"):
                should_delete = True
        elif entity.type == MessageEntity.TEXT_LINK:
            if (settings.get("block_embed_link") or (emergency_active and settings.get("emergency_block_links"))) and not await is_user_freed("block_embed_link", "links"):
                should_delete = True

    if msg.photo or msg.video or msg.animation:
        if (settings.get("block_media") or (emergency_active and settings.get("emergency_block_media"))) and not await is_user_freed("block_media", "media"):
            should_delete = True
            
    if msg.document:
        if settings.get("block_documents") and not await is_user_freed("block_documents"):
            should_delete = True
            
    # Check for forwarded messages
    is_forwarded = (hasattr(msg, 'forward_origin') and msg.forward_origin is not None) or \
                   (hasattr(msg, 'forward_from') and msg.forward_from is not None)
    
    # Check if message is from a channel
    is_channel_post = False
    if getattr(msg, 'sender_chat', None) and msg.sender_chat.type == "channel":
        is_channel_post = True
    elif getattr(msg, 'forward_origin', None) and getattr(msg.forward_origin, 'chat', None) and msg.forward_origin.chat.type == "channel":
        is_channel_post = True
    elif getattr(msg, 'forward_from_chat', None) and msg.forward_from_chat.type == "channel":
        is_channel_post = True
    
    # Block channel posts
    if is_channel_post and settings.get("block_channel_post") and not await is_user_freed("block_channel_post"):
        should_delete = True
    
    # Block forwarded messages (non-channel)
    if is_forwarded and not is_channel_post:
        if (settings.get("block_forward") or (emergency_active and settings.get("emergency_block_forward"))) and not await is_user_freed("block_forward", "forward"):
            should_delete = True
            
    # Check for commands (block_command)
    if msg.text and settings.get("block_command") and not await is_user_freed("block_command"):
        is_command = msg.text.startswith(('/', '!', '.', '#'))
        
        if is_command:
            # Allow essential bot commands
            allowed_cmds = ("/start", "/settings", "/free", "/help", "/rules", "/me", "/info", "/link", "/report")
            is_allowed = any(msg.text.startswith(cmd) for cmd in allowed_cmds)
            
            if not is_allowed:
                should_delete = True
            
    if msg.contact and (settings.get("block_contact") or (emergency_active and settings.get("emergency_block_contact"))) and not await is_user_freed("block_contact", "contact"):
        should_delete = True
        
    if msg.location and (settings.get("block_location") or (emergency_active and settings.get("emergency_block_location"))) and not await is_user_freed("block_location", "location"):
        should_delete = True
        
    if msg.voice and (settings.get("block_voice") or (emergency_active and settings.get("emergency_block_voice"))) and not await is_user_freed("block_voice", "voice"):
        should_delete = True

    # Block plain text messages (including captions)
    if (msg.text or msg.caption) and (settings.get("block_text") or (emergency_active and settings.get("emergency_block_text"))) and not await is_user_freed("block_text", "text"):
        content = msg.text or msg.caption
        is_command = content.startswith(('/', '!', '.', '#'))
        if not is_command:
            should_delete = True

    # Block music/audio files
    if msg.audio and (settings.get("block_audio") or (emergency_active and settings.get("emergency_block_audio"))) and not await is_user_freed("block_audio", "audio"):
        should_delete = True
        
    if msg.video_note and settings.get("block_video_note") and not await is_user_freed("block_video_note"):
        should_delete = True
        
    if msg.poll and (settings.get("block_poll") or (emergency_active and settings.get("emergency_block_poll"))) and not await is_user_freed("block_poll", "poll"):
        should_delete = True

    if msg.dice and settings.get("block_dice") and not await is_user_freed("block_dice"):
        should_delete = True

    if msg.game and settings.get("block_game") and not await is_user_freed("block_game"):
        should_delete = True

    # Custom Block List (text or caption) - applies to EVERYONE including owner
    content_to_check = (msg.text or "") + (msg.caption or "")
    custom_block_matched = False
    if content_to_check and settings.get("custom_block_list"):
        for blocked_word in settings["custom_block_list"]:
            if blocked_word.lower() in content_to_check.lower():
                should_delete = True
                custom_block_matched = True
                break
    
    # Custom Block Media
    if not should_delete and settings.get("custom_block_media"):
        custom_block_media = settings.get("custom_block_media", [])
        current_file_id = None
        
        if msg.photo:
            current_file_id = msg.photo[-1].file_id
        elif msg.video:
            current_file_id = msg.video.file_id
        elif msg.document:
            current_file_id = msg.document.file_id
        elif msg.audio:
            current_file_id = msg.audio.file_id
        elif msg.voice:
            current_file_id = msg.voice.file_id
        elif msg.video_note:
            current_file_id = msg.video_note.file_id
        
        if current_file_id:
            for blocked_media in custom_block_media:
                if blocked_media.get("file_id") == current_file_id:
                    should_delete = True
                    custom_block_matched = True
                    break
    
    # Custom Block Stickers
    if not should_delete and settings.get("custom_block_stickers"):
        custom_block_stickers = settings.get("custom_block_stickers", [])
        
        if msg.sticker and msg.sticker.file_id in custom_block_stickers:
            should_delete = True
            custom_block_matched = True

    # Custom Block Sticker Packs
    if not should_delete:
        from managers.block_content_manager_mongo import get_blocked_content
        blocked = get_blocked_content(chat_id)
        if msg.sticker and msg.sticker.set_name:
            logging.info(f"[BLOCKING] Checking sticker set: {msg.sticker.set_name} against {blocked.get('stickerpack', [])}")
            if msg.sticker.set_name in blocked.get("stickerpack", []):
                # Respect freed users: if the user is exempt from sticker blocking, do not delete
                if await is_user_freed("block_stickers", "stickers"):
                    logging.info(f"[BLOCKING] Sticker set {msg.sticker.set_name} is blocked but user {user_id} is FREED, skipping")
                else:
                    logging.info(f"[BLOCKING] Sticker set {msg.sticker.set_name} IS BLOCKED")
                    should_delete = True
                    custom_block_matched = True

    if should_delete:
        try:
            # For custom block list OR Emergency Mode, follow the 'apply_on' logic
            # For other standard blocking rules, admins/creator are usually exempt
            if not custom_block_matched and not emergency_active:
                is_admin = await is_admin_or_creator(context, chat_id, update.effective_user.id, msg)
                if is_admin:
                    return False
            
            # Send notification for blocked content
            is_forwarded_msg = (hasattr(msg, 'forward_origin') and msg.forward_origin is not None) or \
                              (hasattr(msg, 'forward_from') and msg.forward_from is not None)
            
            if is_length_violation:
                user_id_str = update.effective_user.id if update.effective_user else 0
                await send_blocking_notification(
                    update, context, settings,
                    f"📏 <b>Message too long!</b>\n\n"
                    f"User <code>{user_id_str}</code>, your message exceeded the character limit of <b>{length_limit_val}</b> characters "
                    f"(length: <b>{actual_length}</b>)."
                )
            elif msg.audio and settings.get("block_audio"):
                await send_blocking_notification(
                    update, context, settings,
                    f"🎵 <b>Music files are not allowed in this group.</b>\n\n"
                    f"<i>Your audio file has been automatically deleted.</i>"
                )
            elif is_forwarded_msg and settings.get("block_forward"):
                await send_blocking_notification(
                    update, context, settings,
                    f"⚠️ <b>Forwarded messages are not allowed in this group.</b>\n\n"
                    f"<i>Your forwarded message has been automatically deleted.</i>"
                )
            else:
                # Generic notification for other blocked content
                await send_blocking_notification(
                    update, context, settings,
                    f"⚠️ <b>Content blocked!</b>\n\n"
                    f"<i>Your message has been automatically deleted due to group rules.</i>"
                )
                
            await msg.delete()
            return True
        except Exception as e:
            logging.error(f"Error deleting blocked message: {e}")
            
    return False

async def handle_clean_service(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles cleaning service messages."""
    if not update.effective_chat or update.effective_chat.type == "private":
        return False

    chat_id = update.effective_chat.id
    settings = get_chat_settings(chat_id)
    
    # Merge with defaults
    for key, value in DEFAULT_BLOCKING_SETTINGS.items():
        if key not in settings:
            settings[key] = value
    
    if not settings.get("clean_service_enabled", True):
        return False
    
    msg = update.effective_message
    if not msg:
        return False

    should_delete = False

    if msg.new_chat_members and settings.get("clean_join"):
        should_delete = True
    elif msg.left_chat_member and settings.get("clean_left"):
        should_delete = True
    elif msg.new_chat_title and settings.get("clean_title"):
        should_delete = True
    elif (msg.new_chat_photo or msg.delete_chat_photo) and settings.get("clean_photo"):
        should_delete = True
    elif (getattr(msg, 'video_chat_started', None) or getattr(msg, 'voice_chat_started', None)) and settings.get("clean_voice_start"):
        should_delete = True
    elif (getattr(msg, 'video_chat_ended', None) or getattr(msg, 'voice_chat_ended', None)) and settings.get("clean_voice_end"):
        should_delete = True
    elif (getattr(msg, 'video_chat_scheduled', None) or getattr(msg, 'voice_chat_scheduled', None)) and settings.get("clean_voice_schedule"):
        should_delete = True
    elif (getattr(msg, 'video_chat_participants_invited', None) or getattr(msg, 'voice_chat_participants_invited', None)) and settings.get("clean_voice_invite"):
        should_delete = True
    elif msg.pinned_message and settings.get("clean_pinned"):
        should_delete = True

    if should_delete:
        try:
            await msg.delete()
            return True
        except Exception as e:
            logging.error(f"Error deleting service message: {e}")
            
    return False

async def block_text_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Command to toggle text message blocking."""
    if not update.effective_chat or update.effective_chat.type == "private":
        return
        
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    
    if not await can_user_ban(chat_id, user_id, context):
        await send_bot_response(update, context, "Only admins with ban permission can use this command.")
        return
        
    settings = get_chat_settings(chat_id)
    current = settings.get("block_text", False)
    new_status = not current
    
    update_chat_setting(chat_id, "block_text", new_status)
    
    status_text = "ENABLED ✅" if new_status else "DISABLED ❌"
    await send_bot_response(update, context, f"🚫 <b>Block Text Messages:</b> {status_text}\n\nAll non-command text messages will now be {'deleted' if new_status else 'allowed'}.")

async def block_reactions_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Command to toggle reaction blocking."""
    if not update.effective_chat or update.effective_chat.type == "private":
        return
        
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    
    if not await can_user_ban(chat_id, user_id, context):
        await send_bot_response(update, context, "Only admins with ban permission can use this command.")
        return
        
    settings = get_chat_settings(chat_id)
    current = settings.get("block_reactions", False)
    new_status = not current
    
    update_chat_setting(chat_id, "block_reactions", new_status)
    
    status_text = "ENABLED ✅" if new_status else "DISABLED ❌"
    await send_bot_response(update, context, f"⚡ <b>Block Reactions:</b> {status_text}\n\nAll reactions from non-admins will now be {'deleted immediately' if new_status else 'allowed'}.")

async def maxlength_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Command to check or set message maximum character limit."""
    if not update.effective_chat or update.effective_chat.type == "private":
        return
        
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id if update.effective_user else 0
    
    settings = get_chat_settings(chat_id)
    current_limit = settings.get("msg_length_limit", 0)
    if current_limit <= 0 and settings.get("msg_length_delete", False):
        current_limit = settings.get("msg_length_max", 0)
        
    if not context.args:
        status_text = f"<b>{current_limit} characters</b>" if current_limit > 0 else "<b>Disabled (Unlimited)</b>"
        await send_bot_response(
            update, context,
            f"📏 <b>Message Length Limit:</b> {status_text}\n\n"
            f"• To set limit: <code>/maxlength &lt;number&gt;</code> (e.g. <code>/maxlength 200</code>)\n"
            f"• To disable: <code>/maxlength off</code>\n\n"
            f"<i>You can also configure this interactively in /config &gt; Message Length.</i>"
        )
        return
        
    if not await can_user_ban(chat_id, user_id, context):
        await send_bot_response(update, context, "Only admins with ban permission can change message length limits.")
        return
        
    arg = context.args[0].lower().strip()
    if arg in ["off", "disable", "none", "0"]:
        update_chat_setting(chat_id, "msg_length_limit", 0)
        update_chat_setting(chat_id, "msg_length_max", 0)
        update_chat_setting(chat_id, "msg_length_delete", False)
        await send_bot_response(update, context, "✅ <b>Message length limit disabled.</b> All message lengths are now permitted.")
        return
        
    if arg.isdigit():
        val = int(arg)
        if val < 5:
            await send_bot_response(update, context, "⚠️ Message length limit must be at least 5 characters.")
            return
        update_chat_setting(chat_id, "msg_length_limit", val)
        update_chat_setting(chat_id, "msg_length_max", val)
        update_chat_setting(chat_id, "msg_length_delete", True)
        await send_bot_response(update, context, f"✅ <b>Message length limit set to {val} characters.</b> Messages exceeding this limit will be automatically deleted.")
        return
        
    await send_bot_response(update, context, "⚠️ Invalid input. Usage: <code>/maxlength 200</code> or <code>/maxlength off</code>")

async def free_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Command to exempt a user from specific blocking rules."""
    if not update.effective_chat:
        return
        
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id if update.effective_user else 0
    
    # Handle anonymous admins (sender_chat is the group)
    if not user_id and update.effective_message and update.effective_message.sender_chat:
        if update.effective_message.sender_chat.id == chat_id:
            user_id = 1087968824
            
    logging.info(f"[FREE] Processing command for chat {chat_id}, user {user_id}")
    
    # Only admins with specific permissions can use this command
    if not await can_user_mute(chat_id, user_id, context):
        await send_bot_response(update, context, "Only admins with 'Mute Users' permission can use the /free command.")
        return
    
    # Resolve the target user
    target_user_id, user_name = await get_user_id(update, context)
    # Escape user-controlled display name so names containing <, > or & (e.g. "<3")
    # cannot break the HTML parse mode (fixes "Can't parse entities" crashes).
    user_name = html.escape(user_name or "User")
    
    if not target_user_id:
        await send_bot_response(update, context, 
            "Usage:\n"
            "• Reply to a user's message with /free\n"
            "• /free <user_id> - e.g., /free 123456789\n"
            "• /free @username - e.g., /free @exampleuser\n\n"
            "This will exempt them from all blocking rules.")
        return
    
    # Check if target is an admin
    if await is_user_admin(chat_id, target_user_id, context):
        await send_bot_response(update, context, "❌ I cannot exempt another administrator as they are already freed by default.")
        return
    
    # Get current settings
    settings = get_chat_settings(chat_id)
    user_permissions = settings.get("user_permissions", {})
    
    # Check if user is already freed
    user_id_str = str(target_user_id)
    already_freed = user_id_str in user_permissions
    
    # Define labels for permissions
    blocking_labels = {
        "block_stickers": "🎫 Stickers",
        "block_premium_sticker": "✨ Premium",
        "block_bio_link": "🔗 Bio Link",
    }
    
    if already_freed:
        # Get existing exemptions
        exemptions = user_permissions[user_id_str]
        
        # List which ones are freed (True)
        freed_list = []
        for key, label in blocking_labels.items():
            if exemptions.get(key, False):
                freed_list.append(f"• {label} ✅")
        
        random_emoji = get_random_premium_emoji()
        if freed_list:
            freed_text = "\n".join(freed_list)
            message_text = (
                f"{random_emoji} <b>{user_name}</b> (<code>{target_user_id}</code>) ɪꜱ ᴀʟʀᴇᴀᴅʏ ꜰʀᴇᴇᴅ!\n\n"
                f"<b>📊 ᴄᴜʀʀᴇɴᴛ ᴇxᴇᴍᴘᴛɪᴏɴꜱ:</b>\n{freed_text}\n\n"
                f"💡 ʏᴏᴜ ᴄᴀɴ ꜱᴛɪʟʟ ᴍᴀɴᴀɢᴇ ᴛʜᴇɪʀ ʙʟᴏᴄᴋɪɴɢ ᴘᴇʀᴍɪꜱꜱɪᴏɴꜱ ʙᴇʟᴏᴡ:"
            )
        else:
            message_text = (
                f"{random_emoji} <b>{user_name}</b> (<code>{target_user_id}</code>) ʜᴀꜱ ᴀ ꜰʀᴇᴇ ʀᴇᴄᴏʀᴅ ʙᴜᴛ ɴᴏ ᴀᴄᴛɪᴠᴇ ᴇxᴇᴍᴘᴛɪᴏɴꜱ.\n\n"
                f"💡 ꜱᴇʟᴇᴄᴛ ꜰᴇᴀᴛᴜʀᴇꜱ ᴛᴏ ᴇxᴇᴍᴘᴛ ᴛʜᴇᴍ ꜰʀᴏᴍ ʙʟᴏᴄᴋɪɴɢ:"
            )
    else:
        # New entry - start with no exemptions
        random_emoji = get_random_premium_emoji()
        message_text = (
            f"{random_emoji} ꜱᴇʟᴇᴄᴛ ʙʟᴏᴄᴋɪɴɢ ᴇxᴇᴍᴘᴛɪᴏɴꜱ ꜰᴏʀ <b>{user_name}</b> (<code>{target_user_id}</code>):\n\n"
            f"ᴛᴏɢɢʟᴇ ꜰᴇᴀᴛᴜʀᴇꜱ ᴛᴏ ᴀʟʟᴏᴡ ᴛʜᴇᴍ ᴛᴏ ꜱᴇɴᴅ ᴄᴏɴᴛᴇɴᴛ:"
        )
    
    # Create keyboard with permission button
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(colored_button("🛡 Permissions", "blue"), callback_data=f"free_perms_{chat_id}_{target_user_id}")]
    ])
    
    await send_bot_response(update, context, message_text, reply_markup=keyboard, parse_mode="HTML")

async def unfree_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Command to remove exemptions from a user."""
    if not update.effective_chat:
        return
        
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id if update.effective_user else 0
    
    # Handle anonymous admins (sender_chat is the group)
    if not user_id and update.effective_message and update.effective_message.sender_chat:
        if update.effective_message.sender_chat.id == chat_id:
            user_id = 1087968824
            
    # Only admins with specific permissions can use this command
    if not await can_user_mute(chat_id, user_id, context):
        await send_bot_response(update, context, "Only admins with 'Mute Users' permission can use the /unfree command.")
        return
    
    # Resolve target user
    target_user_id, user_name = await get_user_id(update, context)
    # Escape user-controlled display name so names containing <, > or & cannot
    # break the HTML parse mode (fixes "Can't parse entities" crashes).
    user_name = html.escape(user_name or "User")

    if not target_user_id:
        await send_bot_response(update, context, 
            "Usage:\n"
            "• Reply to a user's message with /unfree\n"
            "• /unfree <user_id>\n"
            "• /unfree @username\n\n"
            "This will remove all blocking exemptions from them.")
        return
    
    # Check if target is an admin
    if await is_user_admin(chat_id, target_user_id, context):
        await send_bot_response(update, context, "❌ This user is an administrator and cannot be restricted by blocking rules.")
        return
    
    # Get current settings
    settings = get_chat_settings(chat_id)
    user_permissions = settings.get("user_permissions", {})
    
    # Remove user from permissions
    user_id_str = str(target_user_id)
    if user_id_str in user_permissions:
        del user_permissions[user_id_str]
        update_chat_setting(chat_id, "user_permissions", user_permissions)
        await send_bot_response(update, context, 
            f"❌ <b>{user_name}</b> is no longer exempt from blocking rules.")
    else:
        await send_bot_response(update, context, 
            f"ℹ️ <b>{user_name}</b> is not exempt from any blocking rules.")

async def list_freed_members(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback to show the list of freed members and their exemptions."""
    query = update.callback_query
    chat_id = context.user_data.get('settings_chat_id', update.effective_chat.id)
    user_id = update.effective_user.id
    
    # Only admins with specific permissions can use this
    if not await can_user_mute(chat_id, user_id, context):
        await query.answer("You don't have permission to view freed members.", show_alert=True)
        return
    
    await query.answer()
    
    settings = get_chat_settings(chat_id)
    user_permissions = settings.get("user_permissions", {})
    
    if not user_permissions:
        text = "📋 <b>ꜰʀᴇᴇᴅ ᴍᴇᴍʙᴇʀꜱ ʟɪꜱᴛ</b>\n\nɴᴏ ᴍᴇᴍʙᴇʀꜱ ᴀʀᴇ ᴄᴜʀʀᴇɴᴛʟʏ ᴇxᴇᴍᴘᴛᴇᴅ ꜰʀᴏᴍ ʙʟᴏᴄᴋɪɴɢ ʀᴜʟᴇꜱ."
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton(colored_button("🔙 Back", "red"), callback_data="set_view_blocking")]])
        await query.edit_message_text(text, reply_markup=keyboard, parse_mode='HTML')
        return

    text = "📋 <b>ꜰʀᴇᴇᴅ ᴍᴇᴍʙᴇʀꜱ ʟɪꜱᴛ</b>\n\nꜱᴇʟᴇᴄᴛ ᴀ ᴍᴇᴍʙᴇʀ ᴛᴏ ᴍᴀɴᴀɢᴇ ᴛʜᴇɪʀ ᴇxᴇᴍᴘᴛɪᴏɴꜱ:\n\n"
    keyboard_buttons = []
    
    # Define labels for permissions (same as in free_command)
    blocking_labels = {
        "block_stickers": "🎫",
        "block_premium_sticker": "✨",
        "block_bio_link": "🔗",
    }

    from managers.user_manager_mongo import resolve_username
    
    for uid_str, perms in user_permissions.items():
        # Get user name from cache or bot
        try:
            uid = int(uid_str)
            user_name = f"User {uid}"
            
            # Try cache first
            cached_id, cached_name = resolve_username(uid_str)
            if cached_name:
                user_name = cached_name
            else:
                # Try bot
                try:
                    chat = await context.bot.get_chat(uid)
                    user_name = chat.first_name or chat.title or user_name
                except:
                    pass
            
            # Escape display name so <, > or & cannot break HTML parse mode
            user_name = html.escape(user_name)
            
            # Count active exemptions
            active_exemptions = [blocking_labels[k] for k, v in perms.items() if v and k in blocking_labels]
            exempt_str = " ".join(active_exemptions) if active_exemptions else "None"
            
            keyboard_buttons.append([InlineKeyboardButton(colored_button(f"👤 {user_name} ({len(active_exemptions)})", "blue"), callback_data=f"free_perms_{chat_id}_{uid}")])
            text += f"• <b>{user_name}</b> (<code>{uid}</code>)\n  └ ᴇxᴇᴍᴘᴛ: {exempt_str}\n\n"
        except:
            continue

    keyboard_buttons.append([InlineKeyboardButton(colored_button("🔙 Back", "red"), callback_data="set_view_blocking")])
    keyboard = InlineKeyboardMarkup(keyboard_buttons)
    
    await query.edit_message_text(text, reply_markup=keyboard, parse_mode='HTML')

async def handle_reaction_blocking(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Detects and warns about unauthorized reactions if blocking is enabled."""
    reaction_update = update.message_reaction
    if not reaction_update:
        return

    chat_id = reaction_update.chat.id
    user = reaction_update.user
    
    logging.info(f"[REACTION] Received reaction update in {chat_id} from {user.id if user else 'Unknown'}")

    # If it's an anonymous reaction or no user, we might not be able to identify
    if not user:
        return

    settings = get_chat_settings(chat_id)
    if not settings.get("blocking_enabled", True) or not settings.get("block_reactions", False):
        return

    logging.info(f"[REACTION] Reaction blocking is ENABLED in {chat_id}")

    # Admins are exempt
    is_admin = await is_user_admin(chat_id, user.id, context)
    if is_admin:
        logging.info(f"[REACTION] User {user.id} is admin, exempt.")
        return

    # Freed users are exempt
    user_perms = settings.get("user_permissions", {}).get(str(user.id), {})
    if user_perms.get("block_reactions", False):
        logging.info(f"[REACTION] User {user.id} is freed, exempt.")
        return

    # If we reached here, the reaction is unauthorized
    logging.info(f"[REACTION] Attempting to delete unauthorized reaction from {user.id} in {chat_id}")
    try:
        # Check if bot has permission to delete messages (required for reaction deletion)
        bot_member = await context.bot.get_chat_member(chat_id, context.bot.id)
        if not (bot_member.status == 'administrator' and getattr(bot_member, 'can_delete_messages', False)):
            logging.warning(f"Bot lacks 'can_delete_messages' in {chat_id}, cannot delete reaction.")
            return

        # 1. Attempt to DELETE the reaction (Bot API 7.0+)
        # We use the built-in library method for speed and efficiency
        try:
            await context.bot.delete_message_reaction(
                chat_id=chat_id,
                message_id=reaction_update.message_id,
                user_id=user.id
            )
            logging.info(f"✅ Successfully deleted reaction from user {user.id} in chat {chat_id}")
        except Exception as e:
            logging.warning(f"Failed to delete reaction via library: {e}")
            # Fallback to direct API if needed (sometimes library methods might have issues with specific versions)
            from core.config import BOT_TOKEN
            async with httpx.AsyncClient() as client:
                url = f"https://api.telegram.org/bot{BOT_TOKEN}/deleteMessageReaction"
                params = {"chat_id": chat_id, "message_id": reaction_update.message_id, "user_id": user.id}
                resp = await client.post(url, json=params)
                if not resp.json().get("ok"):
                    logging.error(f"Fallback direct API also failed: {resp.json().get('description')}")
                    return

    except Exception as e:
        logging.error(f"Error handling reaction blocking in {chat_id}: {e}")

def get_blocking_handlers():
    """Return only the message blocking handler for Group 12."""
    return [
        MessageHandler(filters.ALL & filters.ChatType.GROUPS, handle_message_blocking),
    ]

def get_blocking_command_handlers():
    """Return blocking command and callback handlers for Group 0."""
    return [
        CommandHandler("blocktext", block_text_command),
        CommandHandler("blockreaction", block_reactions_command),
        CommandHandler(["maxlength", "length", "setlength"], maxlength_command),
        CommandHandler("free", free_command),
        CommandHandler("unfree", unfree_command),
        CallbackQueryHandler(free_permission_callback, pattern=r"^free_perms_"),
        CallbackQueryHandler(free_permission_toggle, pattern=r"^free_toggle_"),
        CallbackQueryHandler(free_permission_save, pattern=r"^free_save_"),
        CallbackQueryHandler(list_freed_members, pattern=r"^free_list_members$"),
    ]

async def handle_message_blocking(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Wrapper to handle both blocking and clean service."""
    # Only process actual messages or edits, ignore reactions and other updates
    if not update.message and not update.edited_message:
        return

    # Cache user
    if update.effective_user:
        user = update.effective_user
        cache_user(user.id, user.username, user.first_name)
    
    # Handle blocking
    await handle_blocking(update, context)
    
    # Handle clean service
    await handle_clean_service(update, context)

def get_user_permission_keyboard(chat_id, user_id, settings):
    """Get keyboard for user permission settings."""
    # Convert to string for MongoDB compatibility
    user_id_str = str(user_id)
    user_perms = settings.get("user_permissions", {}).get(user_id_str, {})
    
    logging.info(f"[KEYBOARD] Building keyboard for chat {chat_id}, user {user_id_str}, perms: {user_perms}")
    
    # Blocking options in grid format - shortened for mobile
    # True = FREED/ALLOWED ✅, False = BLOCKED ❌
    blocking_options = [
        ("block_stickers", "🎫 Stickers"),
        ("block_premium_sticker", "✨ Premium"),
        ("block_bio_link", "🔗 Bio Link"),
    ]
    
    keyboard = []
    for i in range(0, len(blocking_options), 2):
        row = []
        key1, label1 = blocking_options[i]
        # True = FREED/ALLOWED ✅, False = BLOCKED ❌
        value1 = user_perms.get(key1, False)
        status1 = "✅" if value1 else "❌"
        color1 = "green" if value1 else "red"
        row.append(InlineKeyboardButton(colored_button(f"{label1} {status1}", color1), callback_data=f"free_toggle_{chat_id}_{user_id}_{key1}"))
        
        if i + 1 < len(blocking_options):
            key2, label2 = blocking_options[i + 1]
            value2 = user_perms.get(key2, False)
            status2 = "✅" if value2 else "❌"
            color2 = "green" if value2 else "red"
            row.append(InlineKeyboardButton(colored_button(f"{label2} {status2}", color2), callback_data=f"free_toggle_{chat_id}_{user_id}_{key2}"))
        
        keyboard.append(row)
    
    # Add save button
    keyboard.append([InlineKeyboardButton(colored_button("💾 Save", "blue"), callback_data=f"free_save_{chat_id}_{user_id}")])
    
    return InlineKeyboardMarkup(keyboard)

async def free_permission_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show permission settings for a freed user."""
    query = update.callback_query
    admin_id = update.effective_user.id if update.effective_user else 0
    
    # Callback data format: free_perms_{chat_id}_{user_id}
    parts = query.data.split("_")
    chat_id = int(parts[2])
    target_user_id = int(parts[3])
    
    # Handle anonymous admins in callbacks
    if admin_id == 1087968824 or (not admin_id and query.message and query.message.sender_chat and query.message.sender_chat.id == chat_id):
        admin_id = 1087968824
    
    # Check permissions
    if not await can_user_mute(chat_id, admin_id, context):
        await query.answer("You don't have permission to manage exemptions.", show_alert=True)
        return

    await query.answer()
    
    settings = get_chat_settings(chat_id)
    
    try:
        chat = await context.bot.get_chat(target_user_id)
        user_name = chat.first_name or "Unknown"
    except:
        user_name = "Unknown User"
    
    # Escape display name so <, > or & cannot break HTML parse mode
    user_name = html.escape(user_name)
    
    message_text = (
        f"🛡 <b>ʙʟᴏᴄᴋɪɴɢ ꜱᴇᴛᴛɪɴɢꜱ 🛡</b>\n\n"
        f"<b>{user_name}</b> (<code>{target_user_id}</code>) ᴡɪʟʟ ʙᴇ ᴇxᴇᴍᴘᴛᴇᴅ ꜰʀᴏᴍ:\n\n"
        f"ᴛᴏɢɢʟᴇ ꜰᴇᴀᴛᴜʀᴇꜱ ᴛᴏ ʙʟᴏᴄᴋ ᴄᴏɴᴛᴇɴᴛ:"
    )
    
    keyboard = get_user_permission_keyboard(chat_id, target_user_id, settings)
    
    try:
        await query.edit_message_text(message_text, reply_markup=keyboard, parse_mode='HTML')
    except:
        await query.message.edit_text(message_text, reply_markup=keyboard, parse_mode='HTML')

async def free_permission_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Toggle a permission for a user and auto-save to database."""
    query = update.callback_query
    admin_id = update.effective_user.id if update.effective_user else 0
    
    # Callback data format: free_toggle_{chat_id}_{user_id}_{perm_key}
    parts = query.data.split("_")
    chat_id = int(parts[2])
    
    # Handle anonymous admins in callbacks
    if admin_id == 1087968824 or (not admin_id and query.message and query.message.sender_chat and query.message.sender_chat.id == chat_id):
        admin_id = 1087968824
        
    user_id = int(parts[3])
    user_id_str = str(user_id)
    # Join all parts after index 3 to get the full permission key
    perm_key = "_".join(parts[4:])
    
    # Check permissions
    if not await can_user_mute(chat_id, admin_id, context):
        await query.answer("You don't have permission to manage exemptions.", show_alert=True)
        return

    await query.answer()
    
    settings = get_chat_settings(chat_id)
    
    logging.info(f"[TOGGLE] Starting toggle for chat {chat_id}, user {user_id_str}, key: {perm_key}")
    
    # Get current permissions from database
    user_permissions = settings.get("user_permissions", {})
    
    # Initialize user permissions if not exists
    if user_id_str not in user_permissions:
        user_permissions[user_id_str] = {}
    
    # Get current value and toggle it
    current_value = user_permissions[user_id_str].get(perm_key, False)
    new_value = not current_value
    user_permissions[user_id_str][perm_key] = new_value
    
    logging.info(f"[TOGGLE] {perm_key} changed from {current_value} to {new_value}")
    
    # AUTO-SAVE to database immediately
    update_chat_setting(chat_id, "user_permissions", user_permissions)
    logging.info(f"[TOGGLE] Auto-saved to database for user {user_id_str} in chat {chat_id}")
    
    # Refresh the keyboard with updated permissions
    import copy
    temp_settings = copy.deepcopy(settings)
    if 'user_permissions' not in temp_settings:
        temp_settings['user_permissions'] = {}
    temp_settings['user_permissions'][user_id_str] = user_permissions[user_id_str]
    
    keyboard = get_user_permission_keyboard(chat_id, user_id, temp_settings)
    
    try:
        await query.edit_message_reply_markup(reply_markup=keyboard)
    except Exception as e:
        if "not modified" not in str(e).lower():
            logging.error(f"[TOGGLE] Error updating keyboard: {e}")

async def free_permission_save(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Save permission changes for a user (now just closes the panel since auto-save is enabled)."""
    query = update.callback_query
    admin_id = update.effective_user.id
    
    # Callback data format: free_save_{chat_id}_{user_id}
    parts = query.data.split("_")
    chat_id = int(parts[2])
    user_id = int(parts[3])
    user_id_str = str(user_id)
    
    # Check permissions
    if not await can_user_mute(chat_id, admin_id, context):
        await query.answer("You don't have permission to manage exemptions.", show_alert=True)
        return
    
    await query.answer("Settings saved!")
    
    # Delete the message after saving
    try:
        await query.message.delete()
    except:
        pass
