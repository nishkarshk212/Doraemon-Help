import logging
import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import ContextTypes, CommandHandler, MessageHandler, filters, ChatMemberHandler
import os
from core.config import OWNER_ID, log_to_channel, colored_button
from managers.settings_manager_mongo import get_chat_settings, update_chat_setting
from managers.user_manager_mongo import cache_user

def format_welcome_message(text, user, chat):
    """Formats the welcome message with dynamic placeholders."""
    now = datetime.datetime.now()
    
    # Placeholders dictionary
    # Use HTML for mentions to preserve formatting if the text is HTML
    mention_html = f'<a href="tg://user?id={user.id}">{(user.first_name or "User").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")}</a>'
    group_title = (chat.title or "this group").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    group_mention = f"@{chat.username}" if getattr(chat, 'username', None) else f"<b>{group_title}</b>"
    first_name = (user.first_name or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    last_name = (user.last_name or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    
    placeholders = {
        "{GROUPMENTION}": group_mention,
        "{groupmention}": group_mention,
        "{member.mention}": mention_html,
        "{MEMBER.MENTION}": mention_html,
        "{chat.title}": group_title,
        "{CHAT.TITLE}": group_title,
        "{member.first_name}": first_name,
        "{member.id}": str(user.id),
        "{title}": group_title,
        "{TITLE}": group_title,
        "{ID}": str(user.id),
        "{NAME}": first_name,
        "{SURNAME}": last_name,
        "{NAMESURNAME}": f"{first_name} {last_name}".strip(),
        "{LANG}": getattr(user, 'language_code', None) or "Unknown",
        "{DATE}": now.strftime("%Y-%m-%d"),
        "{TIME}": now.strftime("%H:%M:%S"),
        "{WEEKDAY}": now.strftime("%A"),
        "{MENTION}": mention_html,
        "{USERNAME}": f"@{user.username}" if user.username else "No Username",
        "{GROUPNAME}": group_title,
        "{RULES}": "/rules" 
    }
    
    formatted_text = text
    for key, value in placeholders.items():
        formatted_text = formatted_text.replace(key, value)
    
    return formatted_text

async def set_welcome(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Set the welcome message text."""
    if not update.message: return
    
    # Check admin or owner
    sender_id = update.effective_user.id
    chat_id = update.effective_chat.id
    
    if sender_id != OWNER_ID:
        member = await context.bot.get_chat_member(chat_id, sender_id)
        if member.status not in ['administrator', 'creator']:
            await update.message.reply_text("Admin only command.")
            return

    # Check if this is a reply to a message
    if update.message.reply_to_message:
        reply = update.message.reply_to_message
        
        # We MUST use text_html to capture premium emojis as <tg-emoji> tags
        if reply.text_html:
            welcome_text = reply.text_html
        elif reply.caption_html:
            welcome_text = reply.caption_html
        else:
            await update.message.reply_text("❌ The replied message doesn't have any text to set as welcome.")
            return
            
        # Also check for media in the replied message
        if reply.photo:
            update_chat_setting(chat_id, "welcome_media", reply.photo[-1].file_id)
            update_chat_setting(chat_id, "welcome_media_type", "photo")
            update_chat_setting(chat_id, "welcome_media_enabled", True)
        elif reply.video:
            update_chat_setting(chat_id, "welcome_media", reply.video.file_id)
            update_chat_setting(chat_id, "welcome_media_type", "video")
            update_chat_setting(chat_id, "welcome_media_enabled", True)
        elif reply.animation:
            update_chat_setting(chat_id, "welcome_media", reply.animation.file_id)
            update_chat_setting(chat_id, "welcome_media_type", "animation")
            update_chat_setting(chat_id, "welcome_media_enabled", True)
            
    else:
        # Fallback to the text after the command
        text = update.message.text_html.split(" ", 1)
        if len(text) < 2:
            await update.message.reply_text("Usage: /setwelcome <text> OR reply to a message with /setwelcome")
            return
        welcome_text = text[1]

    # Save to database
    update_chat_setting(chat_id, "welcome_text", welcome_text)
    await update.message.reply_text("✅ Welcome message updated! Premium Emojis and Quote blocks are now fully supported via HTML tags.")

async def set_welcome_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Set the welcome message photo."""
    if not update.message or not update.message.reply_to_message:
        await update.message.reply_text("Please reply to a photo, video, or animation message with /setphoto to set the welcome media.")
        return

    # Check admin or owner
    sender_id = update.effective_user.id
    chat_id = update.effective_chat.id
    
    if sender_id != OWNER_ID:
        member = await context.bot.get_chat_member(chat_id, sender_id)
        if member.status not in ['administrator', 'creator']:
            await update.message.reply_text("Admin only command.")
            return

    reply = update.message.reply_to_message
    if reply.photo:
        media_id = reply.photo[-1].file_id
        media_type = "photo"
    elif reply.video:
        media_id = reply.video.file_id
        media_type = "video"
    elif reply.animation:
        media_id = reply.animation.file_id
        media_type = "animation"
    else:
        await update.message.reply_text("❌ Please reply to a photo, video, or animation.")
        return

    update_chat_setting(chat_id, "welcome_media", media_id)
    update_chat_setting(chat_id, "welcome_media_type", media_type)
    update_chat_setting(chat_id, "welcome_media_enabled", True)
    await update.message.reply_text(f"✅ Welcome {media_type} updated and enabled!")

async def set_welcome_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Set the welcome message button."""
    if not update.message: return
    
    # Check admin or owner
    sender_id = update.effective_user.id
    chat_id = update.effective_chat.id
    
    if sender_id != OWNER_ID:
        member = await context.bot.get_chat_member(chat_id, sender_id)
        if member.status not in ['administrator', 'creator']:
            await update.message.reply_text("Admin only command.")
            return

    text_parts = update.message.text.split(maxsplit=1)
    if len(text_parts) < 2:
        await update.message.reply_text("Usage: /setbutton #color Button text - http://link.com\nColors: #g, #r, #b, #default")
        return

    input_str = text_parts[1].strip()
    
    parts = input_str.split(" - ", 1)
    if len(parts) != 2:
        await update.message.reply_text("❌ Invalid format. Please use: #color Button text - http://link.com")
        return
        
    button_left = parts[0].strip()
    button_url = parts[1].strip()
    
    import re
    color_tag = "default"
    color_match = re.match(r'^(#g|#r|#p|#b|#o|#y|#pu|#cy|#pk|#go|#success|#green|#danger|#red|#primary|#blue|#default|#orange|#yellow|#purple|#cyan|#pink|#gold)\s+', button_left)
    
    if color_match:
        tag = color_match.group(1)
        button_text = button_left[len(tag):].strip()
        
        tag_map = {
            "#g": "green", "#success": "green", "#green": "green",
            "#r": "red", "#danger": "red", "#red": "red",
            "#p": "blue", "#b": "blue", "#primary": "blue", "#blue": "blue",
            "#o": "orange", "#orange": "orange",
            "#y": "yellow", "#yellow": "yellow",
            "#pu": "purple", "#purple": "purple",
            "#cy": "cyan", "#cyan": "cyan",
            "#pk": "pink", "#pink": "pink",
            "#go": "gold", "#gold": "gold",
            "#default": "default"
        }
        color_tag = tag_map.get(tag, "default")
    else:
        button_text = button_left

    if not button_url.startswith("http"):
        await update.message.reply_text("❌ URL must start with http:// or https://")
        return
        
    settings = get_chat_settings(chat_id)
    welcome_buttons = settings.get("welcome_buttons", [])
    welcome_buttons.append({"text": button_text, "url": button_url, "color": color_tag})
    
    update_chat_setting(chat_id, "welcome_buttons", welcome_buttons)
    update_chat_setting(chat_id, "welcome_button_enabled", True)
    
    await update.message.reply_text(f"✅ Button added to welcome message: {button_text} -> {button_url}")

async def _deliver_welcome_payload(target_id, media_enabled, welcome_media, welcome_media_type, personal_welcome, reply_markup, context):
    """Internal helper to deliver welcome payload with or without media to target_id."""
    if media_enabled and welcome_media:
        try:
            if welcome_media_type == "photo":
                return await context.bot.send_photo(
                    chat_id=target_id,
                    photo=welcome_media,
                    caption=personal_welcome,
                    reply_markup=reply_markup,
                    parse_mode=ParseMode.HTML
                )
            elif welcome_media_type == "video":
                return await context.bot.send_video(
                    chat_id=target_id,
                    video=welcome_media,
                    caption=personal_welcome,
                    reply_markup=reply_markup,
                    parse_mode=ParseMode.HTML
                )
            elif welcome_media_type == "animation":
                return await context.bot.send_animation(
                    chat_id=target_id,
                    animation=welcome_media,
                    caption=personal_welcome,
                    reply_markup=reply_markup,
                    parse_mode=ParseMode.HTML
                )
            else:
                return await context.bot.send_document(
                    chat_id=target_id,
                    document=welcome_media,
                    caption=personal_welcome,
                    reply_markup=reply_markup,
                    parse_mode=ParseMode.HTML
                )
        except Exception as e:
            logging.error(f"Error sending welcome media with HTML to {target_id}: {e}")
            try:
                if welcome_media_type == "photo":
                    return await context.bot.send_photo(chat_id=target_id, photo=welcome_media, caption=personal_welcome, reply_markup=reply_markup)
                elif welcome_media_type == "video":
                    return await context.bot.send_video(chat_id=target_id, video=welcome_media, caption=personal_welcome, reply_markup=reply_markup)
                else:
                    return await context.bot.send_animation(chat_id=target_id, animation=welcome_media, caption=personal_welcome, reply_markup=reply_markup)
            except Exception as e2:
                logging.error(f"Media fallback failed for {target_id}: {e2}. Falling back to plain text.")

    try:
        return await context.bot.send_message(
            chat_id=target_id,
            text=personal_welcome,
            reply_markup=reply_markup,
            parse_mode=ParseMode.HTML
        )
    except Exception as e:
        err_str = str(e)
        if "Forbidden" in err_str or "can't initiate" in err_str or "chat not found" in err_str.lower():
            raise e
        logging.error(f"Error sending welcome message with HTML parse mode to {target_id}: {e}. Retrying plain text.")
        return await context.bot.send_message(
            chat_id=target_id,
            text=personal_welcome,
            reply_markup=reply_markup
        )


async def send_welcome(chat, user, context: ContextTypes.DEFAULT_TYPE):
    """Sends the welcome message to personal DM of newly joined user, falling back to group chat if DM fails."""
    chat_id = chat.id
    settings = get_chat_settings(chat_id)
    
    if not settings.get("welcome_enabled", True):
        return

    welcome_text_raw = settings.get('welcome_text', "Welcome {NAME} to the group!")
    welcome_media = settings.get('welcome_media')
    welcome_media_type = settings.get('welcome_media_type', 'photo')
    welcome_delete_time = settings.get('welcome_delete_time', 60)
    media_enabled = settings.get('welcome_media_enabled', True)
    button_enabled = settings.get('welcome_button_enabled', True)
    welcome_buttons = settings.get('welcome_buttons', [])
    welcome_dm_enabled = settings.get('welcome_dm_enabled', True)

    reply_markup = None
    if button_enabled:
        keyboard = []
        for btn in welcome_buttons:
            if btn.get("text") and btn.get("url"):
                btn_color = btn.get("color", "default")
                from core.config import colored_button
                final_text = colored_button(btn["text"], btn_color)
                keyboard.append([InlineKeyboardButton(final_text, url=btn["url"])])
        
        if keyboard:
            reply_markup = InlineKeyboardMarkup(keyboard)

    # Cache the user
    cache_user(user.id, user.username, user.first_name)
    
    # Format the welcome message
    DEFAULT_PERSONAL_WELCOME = "(\\_/)\n( •.• )\n( >ᴛʜɴQ ꜰᴏʀ ᴊᴏɪɴɪɴɢ\n{GROUPMENTION}"
    welcome_text_html = settings.get('welcome_text', DEFAULT_PERSONAL_WELCOME)
    personal_welcome = format_welcome_message(welcome_text_html, user, chat)
    
    dm_sent = False
    if welcome_dm_enabled and not user.is_bot:
        # Try sending personal welcome DM via Bot API only (assistant/userbot never sends DMs)
        try:
            await _deliver_welcome_payload(user.id, media_enabled, welcome_media, welcome_media_type, personal_welcome, reply_markup, context)
            dm_sent = True
            logging.info(f"✅ Sent personal DM welcome to user {user.id} via Bot API for joining chat {chat_id}")
        except Exception as e:
            err_msg = str(e)
            if "Forbidden" in err_msg or "can't initiate" in err_msg or "chat not found" in err_msg.lower():
                logging.debug(f"User {user.id} hasn't started bot in DM. Skipping DM welcome.")
            else:
                logging.debug(f"DM welcome payload skipped/failed for user {user.id}: {e}")

    # Group welcome markup
    group_markup = reply_markup

    # Clean previous group welcome message if enabled
    msg = None
    if settings.get("welcome_clean_enabled", True):
        last_welcome_id = settings.get('last_welcome_id')
        if last_welcome_id:
            try:
                await context.bot.delete_message(chat_id=chat_id, message_id=last_welcome_id)
            except Exception:
                pass

    try:
        msg = await _deliver_welcome_payload(chat_id, media_enabled, welcome_media, welcome_media_type, personal_welcome, group_markup, context)
    except Exception as e:
        logging.error(f"Error delivering group welcome message: {e}")

    # Save new group welcome message ID for auto-clean
    if msg:
        update_chat_setting(chat_id, "last_welcome_id", msg.message_id)

    # Schedule deletion in group if enabled
    if msg and settings.get("welcome_delete_enabled", True) and welcome_delete_time > 0 and context.job_queue:
        from core.config import delete_message_job
        try:
            context.job_queue.run_once(
                delete_message_job,
                welcome_delete_time,
                data={"chat_id": chat_id, "message_id": msg.message_id}
            )
        except Exception as e:
            logging.error(f"Error scheduling welcome deletion: {e}")

async def set_goodbye(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Set the goodbye message text."""
    if not update.message: return
    
    sender_id = update.effective_user.id
    chat_id = update.effective_chat.id
    
    if sender_id != OWNER_ID:
        member = await context.bot.get_chat_member(chat_id, sender_id)
        if member.status not in ['administrator', 'creator']:
            await update.message.reply_text("Admin only command.")
            return

    if update.message.reply_to_message:
        reply = update.message.reply_to_message
        if reply.text_html:
            goodbye_text = reply.text_html
        elif reply.caption_html:
            goodbye_text = reply.caption_html
        else:
            await update.message.reply_text("❌ The replied message doesn't have any text to set as goodbye.")
            return
    else:
        text = update.message.text_html.split(" ", 1)
        if len(text) < 2:
            await update.message.reply_text("Usage: /setgoodbye <text> OR reply to a message with /setgoodbye")
            return
        goodbye_text = text[1]

    update_chat_setting(chat_id, "goodbye_text", goodbye_text)
    await update.message.reply_text("✅ Goodbye message updated! Dynamic placeholders are supported.")


async def send_goodbye(chat, user, context: ContextTypes.DEFAULT_TYPE):
    """Sends the goodbye message directly to the leaving user's private chat (DM), not in the group."""
    chat_id = chat.id
    settings = get_chat_settings(chat_id)
    
    if not settings.get("goodbye_enabled", True):
        return

    if user.is_bot:
        return

    DEFAULT_GOODBYE = "👋 Goodbye {MENTION}! We're sad to see you leave {GROUPNAME}."
    goodbye_text_html = settings.get('goodbye_text', DEFAULT_GOODBYE)
    formatted_goodbye = format_welcome_message(goodbye_text_html, user, chat)

    # Deliver goodbye message directly to user's private DM via Bot API only (assistant/userbot never sends DMs)
    try:
        await context.bot.send_message(
            chat_id=user.id,
            text=formatted_goodbye,
            parse_mode=ParseMode.HTML
        )
        logging.info(f"✅ Sent goodbye DM to user {user.id} via Bot API for leaving chat {chat_id}")
    except Exception as e:
        err_msg = str(e)
        if "Forbidden" in err_msg or "can't initiate" in err_msg or "chat not found" in err_msg.lower():
            logging.debug(f"User {user.id} hasn't started bot in DM. Skipping goodbye DM.")
        else:
            logging.error(f"Error sending goodbye DM to user {user.id}: {e}")


async def on_new_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send welcome message or trigger captcha when a new member joins via service message (fallback)."""
    if not update.message or not update.message.new_chat_members:
        return
        
    chat = update.effective_chat
    settings = get_chat_settings(chat.id)
    captcha_active = settings.get("captcha_enabled", False)
    actor = update.message.from_user
    
    for member in update.message.new_chat_members:
        if member.is_bot: continue
        try:
            from managers.user_manager_mongo import is_user_admin
            if await is_user_admin(chat.id, member.id, context):
                continue
            # Premium Boost Captcha check
            if settings.get("premium_boost_enabled", True):
                from features.premium_boost import is_premium_user, is_user_boost_verified, handle_premium_user_join
                if is_premium_user(member) and not is_user_boost_verified(chat.id, member.id):
                    await handle_premium_user_join(context.bot, chat, member, context)
                    continue
            # If newcomer was added by a group admin, bypass captcha!
            if actor and actor.id != member.id and await is_user_admin(chat.id, actor.id, context):
                logging.info(f"User {member.id} was added by admin {actor.id} in chat {chat.id}, bypassing captcha.")
                await send_welcome(chat, member, context)
                continue
        except: pass

        # If newcomer joined via accepted join request, bypass captcha!
        try:
            from features.join_request import is_recent_approved_join_request
            if is_recent_approved_join_request(chat.id, member.id):
                logging.info(f"User {member.id} joined via accepted join request in chat {chat.id}, bypassing captcha.")
                await send_welcome(chat, member, context)
                continue
        except: pass
        if captcha_active:
            from features.captcha import start_captcha_verification
            await start_captcha_verification(chat, member, context)
        else:
            await send_welcome(chat, member, context)

async def on_left_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send goodbye message when a member leaves via service message (fallback)."""
    if not update.message or not update.message.left_chat_member:
        return
    left_user = update.message.left_chat_member
    if left_user.is_bot:
        return
    try:
        from features.captcha import cancel_pending_captcha
        cancel_pending_captcha(update.effective_chat.id, left_user.id, context)
    except: pass
    await send_goodbye(update.effective_chat, left_user, context)

async def on_chat_member_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle chat member status changes to detect joins/re-joins and departures."""
    result = update.chat_member
    if not result:
        return
        
    old_status = result.old_chat_member.status
    new_status = result.new_chat_member.status
    chat_id = update.effective_chat.id
    settings = get_chat_settings(chat_id)
    
    # Debug log
    logging.info(f"Chat Member Update in {chat_id}: {old_status} -> {new_status} for user {result.new_chat_member.user.id}")
    
    # Check if the bot itself was added or its status changed
    if result.new_chat_member.user.id == context.bot.id:
        if new_status in ['member', 'administrator']:
            from managers.admin_manager_mongo import sync_admins
            await sync_admins(chat_id, context)
            logging.info(f"Bot joined or refreshed in chat {chat_id}, synced admins.")
            
            # Log to channel when bot is added to a new group
            if old_status in ['left', 'kicked', 'none']:
                try:
                    member_count = await context.bot.get_chat_member_count(chat_id)
                except Exception:
                    member_count = "Unknown"

                added_by = result.from_user
                added_by_str = f"{added_by.mention_html()} (<code>{added_by.id}</code>)" if added_by else "Unknown"

                chat_title = (update.effective_chat.title or "Unknown").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                chat_username = f"@{update.effective_chat.username}" if getattr(update.effective_chat, 'username', None) else "Private Group"

                log_msg = (
                    f"🎉 <b>Bot Added to New Group!</b>\n\n"
                    f"👥 <b>Group Title:</b> {chat_title}\n"
                    f"🆔 <b>Group ID:</b> <code>{chat_id}</code>\n"
                    f"🔗 <b>Username/Link:</b> {chat_username}\n"
                    f"📊 <b>Member Count:</b> <code>{member_count}</code>\n"
                    f"👤 <b>Added By:</b> {added_by_str}"
                )
                await log_to_channel(context, log_msg)

            # Apply group settings like reaction blocking
            if settings.get("block_reactions", False):
                try:
                    await context.bot.set_chat_available_reactions(chat_id, reactions=[])
                    logging.info(f"Reactions for chat {chat_id} blocked on bot join.")
                except: pass
    
    # Active statuses that indicate the user is in the group
    active_statuses = ['member', 'administrator', 'restricted']
    inactive_statuses = ['left', 'kicked', 'none']

    # Check Telegram's is_member field on ChatMemberRestricted (handles users who were previously restricted by captcha)
    was_member = (old_status in ['member', 'administrator', 'creator']) or (
        old_status == 'restricted' and getattr(result.old_chat_member, 'is_member', True)
    )
    is_member_now = (new_status in ['member', 'administrator', 'creator']) or (
        new_status == 'restricted' and getattr(result.new_chat_member, 'is_member', False)
    )

    # Trigger join if transitioning from non-member to member
    is_joining = (not was_member and is_member_now) or (old_status in inactive_statuses and new_status in active_statuses)
    
    if is_joining:
        member = result.new_chat_member.user
        if member.is_bot:
            return
            
        # Skip admins from captcha
        try:
            from managers.user_manager_mongo import is_user_admin
            if await is_user_admin(chat_id, member.id, context):
                return
        except: pass

        # If newcomer was added by an admin of the group, bypass captcha!
        actor = result.from_user
        if actor and actor.id != member.id:
            try:
                from managers.user_manager_mongo import is_user_admin
                if await is_user_admin(chat_id, actor.id, context):
                    logging.info(f"User {member.id} was added by admin {actor.id} in chat {chat_id}, bypassing captcha.")
                    await send_welcome(update.effective_chat, member, context)
                    return
            except Exception as e:
                logging.warning(f"Error checking if actor is admin: {e}")

        # If newcomer joined via accepted join request, bypass captcha!
        try:
            from features.join_request import is_recent_approved_join_request
            via_join_req = (
                getattr(result, "via_join_request", False) or
                bool(getattr(result, "invite_link", None) and getattr(result.invite_link, "creates_join_request", False)) or
                is_recent_approved_join_request(chat_id, member.id)
            )
            if via_join_req:
                logging.info(f"User {member.id} joined via accepted join request in chat {chat_id}, bypassing captcha.")
                await send_welcome(update.effective_chat, member, context)
                return
        except Exception as e:
            logging.warning(f"Error checking if user joined via join request: {e}")

        # Premium Boost Captcha: When any Telegram Premium user joins, send boost captcha with Titanic World link!
        if settings.get("premium_boost_enabled", True):
            from features.premium_boost import is_premium_user, is_user_boost_verified, handle_premium_user_join
            from managers.user_manager_mongo import is_user_admin
            if is_premium_user(member) and member.id != OWNER_ID and not await is_user_admin(chat_id, member.id, context):
                if not is_user_boost_verified(chat_id, member.id):
                    await handle_premium_user_join(context.bot, update.effective_chat, member, context)
                    return

        # Captcha verification: ALWAYS triggers every time a member joins or re-joins on their own!
        if settings.get("captcha_enabled", False):
            from features.captcha import start_captcha_verification
            await start_captcha_verification(update.effective_chat, member, context)
            return

        # If captcha is not enabled, check welcome_rejoin_enabled for standard welcome text
        is_rejoin = old_status in ['left', 'kicked'] or (old_status == 'restricted' and not getattr(result.old_chat_member, 'is_member', True))
        if is_rejoin:
            if not settings.get("welcome_rejoin_enabled", True):
                logging.info(f"Skipping welcome for re-joining user {member.id} in {chat_id} as welcome_rejoin_enabled is False")
                return

        await send_welcome(update.effective_chat, member, context)

    # Trigger goodbye if transitioning from member to non-member
    is_leaving = (was_member and not is_member_now) or (old_status in active_statuses and new_status in inactive_statuses)
    if is_leaving:
        member = result.old_chat_member.user
        if member and not member.is_bot:
            # Clean up any pending captcha when user leaves
            try:
                from features.captcha import cancel_pending_captcha
                cancel_pending_captcha(chat_id, member.id, context)
            except: pass
            await send_goodbye(update.effective_chat, member, context)

def get_welcome_handlers():
    """Return handlers for welcome and goodbye messages."""
    return [
        CommandHandler("setwelcome", set_welcome),
        CommandHandler("setphoto", set_welcome_photo),
        CommandHandler("setbutton", set_welcome_button),
        CommandHandler("setgoodbye", set_goodbye),
        MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, on_new_member),
        MessageHandler(filters.StatusUpdate.LEFT_CHAT_MEMBER, on_left_member),
        # Handle status changes (detects joins, re-joins, and leaves reliably)
        ChatMemberHandler(on_chat_member_update, ChatMemberHandler.CHAT_MEMBER)
    ]
