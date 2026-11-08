import logging
import os
import sys
import html
import random
import fcntl
import time
import datetime
import platform
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, ChatPermissions
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes, MessageReactionHandler
from telegram.constants import ParseMode

BOT_START_TIME = datetime.datetime.now()
from moderation.admin import get_admin_handlers
from handlers.welcome import get_welcome_handlers
from moderation.block_content import get_block_content_handlers
from features.clean_service import get_clean_service_handlers
from features.auto_delete import get_auto_delete_handlers
from moderation.moderation import get_moderation_handlers
from moderation.filter import get_filter_handlers
from moderation.bot_protection import get_bot_protection_handlers
from moderation.link_spam import get_link_spam_handlers
from moderation.forward_protection import get_forward_protection_handlers
from features.settings import get_settings_handlers
from features.captcha import get_captcha_handlers
from features.premium_boost import get_premium_boost_handlers
from features.help import get_help_handlers
from features.translator import get_translation_handlers
from moderation.language_filter import get_language_handlers
from features.sticker_manager import get_sticker_handlers
from handlers.blocking_handler import get_blocking_handlers, get_blocking_command_handlers
from handlers.edit_handler import get_edit_handlers
from handlers.bio_handler import get_bio_handlers
from features.join_request import get_join_request_handlers
from features.broadcast import broadcast_command
from moderation.antiflood import get_antiflood_handlers
from features.nightmode import get_nightmode_handlers
from features.font_normalizer import normalize_text
from features.recurring import get_recurring_handlers, check_recurring_messages, count_recurring_messages
from managers import get_manager_handlers
from features.tagger import get_tagger_handlers
from features.multitask import get_multitask_handlers
from features.nsfw_panel import get_nsfw_handlers
from features.doraemon_ai import get_doraemon_ai_handlers
from core.config import BOT_TOKEN, LOG_CHANNEL_ID, OWNER_ID, log_to_channel, send_bot_response, send_bot_media, START_IMG, to_small_caps, colored_button
from features.voice_chat import start_voice_chat_monitor, stop_voice_chat_monitor, get_voice_chat_handlers
from managers.user_manager_mongo import cache_user_handler, get_user_id, get_user_stats, is_user_admin, get_sangmata_handlers
from managers.settings_manager_mongo import get_chat_settings

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
# Reduce noise from libraries
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.WARNING)
logging.getLogger("telegram.ext.Application").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

def _start_keyboard(add_to_group_url):
    """Build the /start inline keyboard (shared by start + Back nav)."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(colored_button(to_small_caps("ᴀᴅᴅ ᴍᴇ ɪɴ ɢʀᴏᴜᴘ"), "green"), url=add_to_group_url)],
        [
            InlineKeyboardButton(colored_button(to_small_caps("「 Support 」"), "green"), url="https://t.me/titanic_network"),
            InlineKeyboardButton(colored_button(to_small_caps("「 Updates 」"), "green"), url="https://t.me/Developer_World_Advay")
        ],
        [
            InlineKeyboardButton(colored_button(to_small_caps("「 Owner 」"), "green"), url=f"tg://user?id={OWNER_ID}")
        ]
    ])

def _start_caption(bot_mention, user_mention):
    """Build the /start caption (shared by start + Back nav)."""
    return (
        f"<tg-emoji emoji-id=\"6242354082741230982\">🐺</tg-emoji>  Hey {user_mention}, \n"
        f"This is {bot_mention} ! \n\n"
        f"<tg-emoji emoji-id=\"5280555367221197240\">😽</tg-emoji>  A group help bot with some awesome and useful features."
        f"<tg-emoji emoji-id=\"5364125616801073577\">✈️</tg-emoji> "
    )

async def start(update, context):
    """Start command handler."""
    if update.effective_chat and update.effective_chat.type == "private" and update.effective_user:
        from managers.user_manager_mongo import mark_user_started_dm
        mark_user_started_dm(update.effective_user.id)

        # Log user start event to log channel
        user = update.effective_user
        username_str = f"@{user.username}" if user.username else "No Username"
        log_text = (
            f"🤖 <b>New User Started Bot</b>\n\n"
            f"👤 <b>User:</b> {user.mention_html()}\n"
            f"🆔 <b>ID:</b> <code>{user.id}</code>\n"
            f"🏷 <b>Username:</b> {username_str}"
        )
        await log_to_channel(context, log_text)

    if context.args:
        arg = context.args[0]
        if arg.startswith('welcome_'):
            try:
                group_chat_id = int(arg.split('_')[1])
                target_chat = await context.bot.get_chat(group_chat_id)
                from handlers.welcome import send_welcome
                await send_welcome(target_chat, update.effective_user, context)
                return
            except Exception as e:
                logging.error(f"Error serving DM welcome deep link for {arg}: {e}")
        elif arg.startswith('settings_'):
            group_chat_id = int(arg.split('_')[1])
            context.user_data['settings_chat_id'] = group_chat_id
            from features.settings import show_settings_panel
            await show_settings_panel(update, context, group_chat_id, is_private=True)
            return
        elif arg.startswith('tz_'):
            group_chat_id = int(arg.split('_')[1])
            context.user_data['settings_chat_id'] = group_chat_id
            context.user_data['config_state'] = ('nightmode_timezone', group_chat_id)
            
            # Add keyboard with location request button
            from telegram import KeyboardButton, ReplyKeyboardMarkup
            keyboard = ReplyKeyboardMarkup([
                [KeyboardButton("📍 Send the position", request_location=True)],
                [KeyboardButton("❌ Cancel")]
            ], resize_keyboard=True, one_time_keyboard=True)
            
            await update.message.reply_text(
                "🌍 <b>Time Zone</b>\n"
                "Now <b>send your position</b> in order to auto detect Time Zone to be set in the group.\n\n"
                "You can send it using the button in the keyboard or touching 📎 Attach, so 📍 Position (with this second way you can chose a specific position also different from yours).\n\n"
                "Alternatively you can <b>write the name of your city</b> directly.\n\n"
                "<i>Your position will not be saved, we will save only the Time Zone detected.</i>",
                parse_mode='HTML',
                reply_markup=keyboard
            )
            return
    
    bot_info = await context.bot.get_me()
    bot_name = bot_info.first_name
    bot_username = bot_info.username
    bot_id = bot_info.id
    escaped_bot_name = bot_name.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    bot_mention = f'<a href="tg://user?id={bot_id}">{escaped_bot_name}</a>'
    add_to_group_url = f"https://t.me/{bot_username}?startgroup=true"

    keyboard = _start_keyboard(add_to_group_url)
    photo_url = random.choice(START_IMG)
    user_mention = update.effective_user.mention_html()
    start_message = _start_caption(bot_mention, user_mention)

    await send_bot_media(
        update, context,
        photo=photo_url,
        caption=start_message,
        reply_markup=keyboard,
        parse_mode=ParseMode.HTML,
        has_spoiler=True
    )

# Music World bots (label -> username link)
MUSIC_WORLD_BOTS = [
    ("𝑳𝒊𝒍𝒚 🦢 ♪", "https://t.me/Lilyy_music_bot"),
    ("𓂃❛ ⟶̽ 𝑺𝒉𝒓𝒂𝒅𝒉𝒂 ⌯ <𝟑⤹🤍", "https://t.me/shraddha_musicbot"),
    ("❛ 𝙄𝙨𝙝𝙪 ✗ 𝙈𝙪𝙨𝙞𝙘 ❜", "https://t.me/Ishu_musics_bot"),
    ("❛ ʀᴀᴅʜɪᴋᴀ ✗ ᴍᴜѕɪᴄ ❜🥂", "https://t.me/RadhikaX_bot"),
    ("ɪɴꜰɪ x ᴍᴜꜱɪᴄ 🥂", "https://t.me/infixmusicx_bot"),
    ("вιℓℓυ χ мυѕι¢ 🎶", "https://t.me/billuxmusicc_bot"),
    ("❛ 𝑳𝒊𝒍𝒚 ✗ 𝑴𝒖𝒔𝒊𝒄 ❜", "https://t.me/lilly_music_pro_bot"),
]

async def music_world_callback(update, context):
    """Handle Music World panel, Settings (help menu), and Back navigation."""
    query = update.callback_query
    data = query.data
    await query.answer()

    if data == "help_open":
        from features.help import get_help_keyboard
        text = ("📚 <b>Bot Help Menu</b>\n\n"
                "Select a category below to see available commands and features.")
        try:
            await query.edit_message_caption(caption=text, reply_markup=get_help_keyboard(), parse_mode=ParseMode.HTML)
        except Exception:
            await query.edit_message_text(text, reply_markup=get_help_keyboard(), parse_mode=ParseMode.HTML)
        return

    if data == "music_back":
        bot_info = await context.bot.get_me()
        add_url = f"https://t.me/{bot_info.username}?startgroup=true"
        escaped_bot_name = (bot_info.first_name or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        bot_mention = f'<a href="tg://user?id={bot_info.id}">{escaped_bot_name}</a>'
        keyboard = _start_keyboard(add_url)
        caption = _start_caption(bot_mention, update.effective_user.mention_html())
        try:
            await query.edit_message_caption(caption=caption, reply_markup=keyboard, parse_mode=ParseMode.HTML)
        except Exception:
            await query.edit_message_text(caption, reply_markup=keyboard, parse_mode=ParseMode.HTML)
        return

    # music_world
    rows = [[InlineKeyboardButton(colored_button(label, "blue"), url=url)]
            for label, url in MUSIC_WORLD_BOTS]
    rows.append([InlineKeyboardButton(colored_button("« Back", "red"), callback_data="music_back")])
    keyboard = InlineKeyboardMarkup(rows)

    # Custom emoji 5470135030393090150 renders via <tg-emoji> in text (not on buttons)
    caption = (
        '<tg-emoji emoji-id="5470135030393090150">🎶</tg-emoji> '
        "<b>𝗠𝘂𝘀𝗶𝗰 𝗪𝗼𝗿𝗹𝗱</b>\n\n"
        "<blockquote>» ᴄʜᴏᴏsᴇ ʏᴏᴜʀ ꜰᴀᴠᴏᴜʀɪᴛᴇ ᴍᴜsɪᴄ ʙᴏᴛ ᴀɴᴅ ꜱᴛᴀʀᴛ ᴠɪʙɪɴɢ 🎧</blockquote>"
    )
    try:
        await query.edit_message_caption(caption=caption, reply_markup=keyboard, parse_mode=ParseMode.HTML)
    except Exception:
        await query.edit_message_text(caption, reply_markup=keyboard, parse_mode=ParseMode.HTML)


async def cache_user_handler_old(update, context):
    """Legacy MessageHandler to cache user data to MongoDB for resolution."""
    # This is replaced by the enhanced cache_user_handler in user_manager_mongo.py
    pass
    
    # 2. Cache any users mentioned in the message
    if update.message and update.message.entities:
        for entity in update.message.entities:
            if entity.type == 'text_mention':
                cache_user(entity.user.id, entity.user.username, entity.user.first_name)

async def info_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Command to show detailed user info."""
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    
    # Check command access
    from managers.settings_manager_mongo import check_command_access
    if not await check_command_access(chat_id, user_id, 'info', context):
        await send_bot_response(update, context, "You don't have permission to use the /info command.")
        return

    user_id, first_name = await get_user_id(update, context)
    if not user_id:
        user = update.effective_user
        user_id, first_name = user.id, user.first_name
    # Check if target is a channel
    is_channel = str(user_id).startswith('-100')
    
    if is_channel:
        try:
            chat = await context.bot.get_chat(user_id)
            
            from managers.moderation_manager_mongo import is_channel_banned
            banned = is_channel_banned(chat_id, user_id)
            channel_status = "Banned ⛔" if banned else "Active ✅"
            
            channel_link = f"• <b>Channel Link:</b> <a href=\"https://t.me/{chat.username}\">Direct Link</a>\n" if chat.username else ""
            info_text = (
                f"📢 <b>Channel Information</b>\n\n"
                f"• <b>Title:</b> {html.escape(chat.title)}\n"
                f"• <b>Username:</b> @{chat.username if chat.username else 'None'}\n"
                f"• <b>Channel ID:</b> <code>{user_id}</code>\n"
                f"{channel_link}"
                f"• <b>Status:</b> {channel_status}\n"
                f"• <b>Type:</b> Channel\n"
                f"• <b>Description:</b> {html.escape(chat.description) if chat.description else 'None'}"
            )
            
            # Keyboard for channels with Toggle Ban/Unban
            reply_markup = None
            if await is_user_admin(chat_id, update.effective_user.id, context):
                keyboard = [
                    [InlineKeyboardButton(colored_button("🔓 Unban Channel" if banned else "🔨 Ban Channel", "red"), callback_data=f"info_ban_{user_id}")],
                    [InlineKeyboardButton(colored_button("🔄 Restart", "green"), callback_data=f"info_restart_{user_id}"), InlineKeyboardButton(colored_button("❌ Close", "gray"), callback_data="info_close")]
                ]
                reply_markup = InlineKeyboardMarkup(keyboard)
            
            await send_bot_response(update, context, info_text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
            return
        except Exception as e:
            logging.error(f"Error getting channel info: {e}")
            await send_bot_response(update, context, f"Error getting channel info: {e}")
            return

    stats = get_user_stats(user_id)
    username = stats.get("username") if stats else None
    joined_date = stats.get("joined_date", "Unknown") if stats else "Unknown"
    msg_count = stats.get("msg_count", 0) if stats else 0
    
    # Get user status and role in group
    user_status = "Unknown"
    user_role = "Member"
    is_muted = False
    
    try:
        member = await context.bot.get_chat_member(chat_id, user_id)
        status = member.status
        
        # Determine Status & Role
        if status == 'creator':
            user_status = "Present"
            user_role = "Owner/Creator"
        elif status == 'administrator':
            user_status = "Present"
            user_role = "Administrator"
            if member.custom_title:
                user_role += f" ({member.custom_title})"
        elif status == 'member':
            user_status = "Present"
            user_role = "Member"
        elif status == 'restricted':
            user_status = "Present (Restricted)"
            user_role = "Restricted Member"
            if not member.can_send_messages:
                is_muted = True
        elif status == 'left':
            user_status = "Left"
            user_role = "None"
        elif status == 'kicked':
            user_status = "Banned"
            user_role = "None"
            
    except Exception as e:
        logging.error(f"Error getting member status for info: {e}")

    # Link formatting - ensure user_id is positive for tg://user?id=
    # For channels/groups, we use the t.me link if available
    user_link = f"tg://user?id={user_id}"
    if is_channel and username:
        user_link = f"https://t.me/{username}"
    elif is_channel:
        # For private channels by ID, there's no direct t.me link without a message ID
        # but we can try the c/ format if we strip -100
        clean_id = str(user_id).replace("-100", "")
        user_link = f"https://t.me/c/{clean_id}/1"

    info_text = (
        f"👤 <b>User Information</b>\n\n"
        f"• <b>Name:</b> {html.escape(first_name)}\n"
        f"• <b>Username:</b> @{username if username else 'None'}\n"
        f"• <b>User ID:</b> <code>{user_id}</code>\n"
        f"• <b>User Link:</b> <a href=\"{user_link}\">Direct Link</a>\n"
        f"• <b>Status:</b> {user_status}\n"
        f"• <b>Role:</b> {user_role}\n"
        f"• <b>Muted:</b> {'Yes 🔇' if is_muted else 'No 🔊'}\n"
        f"• <b>Joined Date:</b> {joined_date}\n"
        f"• <b>Total Messages:</b> {msg_count}"
    )
    
    # Create keyboard for admins
    reply_markup = None
    if await is_user_admin(chat_id, update.effective_user.id, context):
        keyboard = [
            [
                InlineKeyboardButton(colored_button("⚠️ Warns", "red"), callback_data=f"info_warns_{user_id}"),
                InlineKeyboardButton(colored_button("🎭 Roles", "blue"), callback_data=f"info_roles_{user_id}")
            ],
            [
                InlineKeyboardButton(colored_button("🔇 Mute" if not is_muted else "🔊 Unmute", "yellow"), callback_data=f"info_mute_{user_id}"),
                InlineKeyboardButton(colored_button("🔨 Ban" if user_status != "Banned" else "🔓 Unban", "red"), callback_data=f"info_ban_{user_id}")
            ],
            [
                InlineKeyboardButton(colored_button("🔄 Restart", "green"), callback_data=f"info_restart_{user_id}"),
                InlineKeyboardButton(colored_button("❌ Close", "gray"), callback_data="info_close")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

    await send_bot_response(update, context, info_text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)

async def report_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Report a user/message to group admins."""
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    
    # Check if replying to a message
    if not update.message.reply_to_message:
        await send_bot_response(update, context, 
            "Usage: Reply to a message with /report or @admin\n\n"
            "This will notify all admins about the reported message.")
        return
    
    reported_user = update.message.reply_to_message.from_user
    if not reported_user:
        await send_bot_response(update, context, "Could not find the user to report.")
        return
    
    # Get all admins
    try:
        admins = await context.bot.get_chat_administrators(chat_id)
        admin_mentions = []
        
        for admin in admins:
            if admin.user.is_bot:
                continue
            admin_mentions.append(f"[admin](tg://user?id={admin.user.id})")
        
        if not admin_mentions:
            await send_bot_response(update, context, "No admins found in this group.")
            return
        
        # Create report message
        report_text = (
            f"🚨 <b>Report</b>\n\n"
            f"👤 <b>Reported by:</b> {update.effective_user.first_name}\n"
            f"👤 <b>Reported user:</b> {reported_user.first_name} (ID: <code>{reported_user.id}</code>)\n\n"
            f"{' '.join(admin_mentions)}"
        )
        
        # Forward the reported message to show context
        try:
            await context.bot.forward_message(
                chat_id=chat_id,
                from_chat_id=chat_id,
                message_id=update.message.reply_to_message.message_id
            )
            await context.bot.send_message(
                chat_id=chat_id,
                text=report_text,
                parse_mode='HTML',
                disable_web_page_preview=True
            )
        except:
            await send_bot_response(update, context, report_text, parse_mode='HTML')
        
        await send_bot_response(update, context, "✅ Report sent to admins!")
        
    except Exception as e:
        logging.error(f"Error in report command: {e}")
        await send_bot_response(update, context, "Failed to send report. Please try again.")

async def handle_admin_mention(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle @admin or @admins mentions - same as /report."""
    # Reuse the report command logic
    await report_command(update, context)

async def groupdetail_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show group details - bot owner only."""
    user_id = update.effective_user.id
    
    # Check if user is bot owner
    if user_id != OWNER_ID:
        await send_bot_response(update, context, "❌ This command is only available to the bot owner.")
        return
    
    try:
        # Get all chats from MongoDB database
        from core.database import get_all_chats
        chats = get_all_chats()
        
        if not chats:
            await send_bot_response(update, context, "📊 <b>Group Details</b>\n\nNo groups found in database.")
            return
        
        dialogs = []
        for chat_data in chats:
            chat_id = chat_data.get('chat_id')
            if not chat_id:
                continue
            
            # Skip private chats and channels
            if str(chat_id).startswith('-100'):
                try:
                    chat = await context.bot.get_chat(chat_id)
                    if chat.type in ['group', 'supergroup']:
                        member_count = await context.bot.get_chat_member_count(chat_id)
                        dialogs.append({
                            'id': chat_id,
                            'title': chat.title,
                            'username': f"@{chat.username}" if chat.username else "Private",
                            'member_count': member_count,
                            'type': chat.type
                        })
                except Exception as e:
                    logging.error(f"Error getting chat info for {chat_id}: {e}")
        
        if not dialogs:
            await send_bot_response(update, context, "📊 <b>Group Details</b>\n\nNo groups found.")
            return
        
        # Sort by member count
        dialogs.sort(key=lambda x: x['member_count'] if isinstance(x['member_count'], int) else 0, reverse=True)
        
        # Build response message
        response_text = f"📊 <b>Group Details</b>\n\n"
        response_text += f"📈 Total Groups: <code>{len(dialogs)}</code>\n\n"
        
        for i, group in enumerate(dialogs, 1):
            response_text += (
                f"<b>{i}. {html.escape(group['title'])}</b>\n"
                f"   🆔 ID: <code>{group['id']}</code>\n"
                f"   👥 Members: <code>{group['member_count']}</code>\n"
                f"   🔗 {group['username']}\n"
                f"   📁 Type: {group['type']}\n\n"
            )
        
        await send_bot_response(update, context, response_text, parse_mode=ParseMode.HTML)
        
    except Exception as e:
        logging.error(f"Error in groupdetail command: {e}")
        await send_bot_response(update, context, f"❌ Error fetching group details: {e}")

async def ping_stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show ping latency and bot statistics — bot owner only."""
    user_id = update.effective_user.id
    if user_id != OWNER_ID:
        await send_bot_response(update, context, "❌ This command is only available to the bot owner.")
        return

    start_time = time.time()
    msg = await update.message.reply_text("⚡ <i>Pinging server & gathering stats...</i>", parse_mode=ParseMode.HTML)
    end_time = time.time()
    
    latency_ms = round((end_time - start_time) * 1000, 2)

    # Uptime calculation
    uptime_delta = datetime.datetime.now() - BOT_START_TIME
    days = uptime_delta.days
    hours, remainder = divmod(uptime_delta.seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    uptime_str = f"{days}d {hours}h {minutes}m {seconds}s" if days > 0 else f"{hours}h {minutes}m {seconds}s"

    # RAM & CPU Usage
    try:
        import psutil
        process = psutil.Process()
        ram_mb = round(process.memory_info().rss / (1024 * 1024), 2)
        cpu_percent = psutil.cpu_percent(interval=None)
    except Exception:
        ram_mb = "N/A"
        cpu_percent = "N/A"

    # Database Stats
    chats_count = 0
    users_count = 0
    db_status = "Disconnected ❌"
    try:
        from core.database import get_database, COLLECTIONS
        db = get_database()
        if db is not None:
            db_start = time.time()
            db.command('ping')
            db_ping_ms = round((time.time() - db_start) * 1000, 2)
            db_status = f"Connected ({db_ping_ms} ms) ✅"
            
            chats_count = db[COLLECTIONS["settings"]].count_documents({})
            users_count = db[COLLECTIONS["users"]].count_documents({})
    except Exception as e:
        db_status = f"Error ({e}) ❌"

    # Telethon Userbot Status
    telethon_status = "Disabled ❌"
    try:
        from features.voice_chat import telethon_client
        if telethon_client and telethon_client.is_connected():
            telethon_status = "Connected ✅"
        elif telethon_client:
            telethon_status = "Disconnected ⚠️"
    except Exception:
        pass

    import telegram
    text = (
        f"👑 <b><u>BOT OWNER PING & STATS</u></b>\n\n"
        f"⚡ <b>Ping Latency:</b> <code>{latency_ms} ms</code>\n"
        f"⏱ <b>Uptime:</b> <code>{uptime_str}</code>\n"
        f"💾 <b>RAM Usage:</b> <code>{ram_mb} MB</code>\n"
        f"🖥 <b>CPU Usage:</b> <code>{cpu_percent}%</code>\n\n"
        f"📊 <b><u>DATABASE STATS</u></b>\n"
        f"🌐 <b>MongoDB:</b> <code>{db_status}</code>\n"
        f"👥 <b>Total Groups:</b> <code>{chats_count}</code>\n"
        f"👤 <b>Total Users:</b> <code>{users_count}</code>\n\n"
        f"🤖 <b><u>SYSTEM INFO</u></b>\n"
        f"📱 <b>Telethon Userbot:</b> <code>{telethon_status}</code>\n"
        f"🐍 <b>Python Version:</b> <code>{platform.python_version()}</code>\n"
        f"📦 <b>PTB Version:</b> <code>{telegram.__version__}</code>"
    )

    try:
        await msg.edit_text(text, parse_mode=ParseMode.HTML)
    except Exception:
        await update.message.reply_text(text, parse_mode=ParseMode.HTML)

async def info_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle management buttons from the /info command."""
    query = update.callback_query
    chat_id = update.effective_chat.id
    admin_id = update.effective_user.id
    
    # Permission check: only admins can use these buttons
    if not await is_user_admin(chat_id, admin_id, context):
        await query.answer("Admin only feature.", show_alert=True)
        return

    data = query.data.split("_")
    action = data[1]
    
    if action == "close":
        try:
            await query.message.delete()
        except:
            try:
                await query.edit_message_text("Menu closed.")
            except BadRequest: pass
        await query.answer()
        return

    # All other actions require a user_id
    try:
        user_id = int(data[-1])
    except (IndexError, ValueError):
        await query.answer("Invalid action data.", show_alert=True)
        return
    
    # Import handlers locally to avoid circular imports
    from moderation.moderation import mute_command, unmute_command, ban_command, warn_command, unwarn_command, unban_command, muter_command
    from managers.moderation_manager_mongo import get_user_warns, is_muter as check_is_muter, is_channel_banned
    from moderation.admin import promote_user, demote_command, DEFAULT_PERMISSIONS, get_promotion_keyboard
    
    # Create a mock update to reuse existing command logic
    class MockMessage:
        def __init__(self, chat, from_user, reply_to_message=None):
            self.chat = chat
            self.from_user = from_user
            self.reply_to_message = reply_to_message
            self.text = ""
        async def reply_text(self, text, *args, **kwargs):
            # We don't want to spam the group when clicking buttons
            pass

    mock_update = Update(update.update_id, message=MockMessage(update.effective_chat, update.effective_user))
    context.args = [str(user_id)]

    keyboard = []
    text_override = None

    if action == "warns":
        warns = get_user_warns(chat_id, user_id)
        text_override = f"⚠️ <b>Warn Management</b> for user <code>{user_id}</code>\n\nCurrent Warns: <code>{warns}</code>"
        keyboard = [
            [
                InlineKeyboardButton(colored_button("➕ Add Warn", "red"), callback_data=f"info_addwarn_{user_id}"),
                InlineKeyboardButton(colored_button("🔄 Reset Warns", "green"), callback_data=f"info_resetwarn_{user_id}")
            ],
            [InlineKeyboardButton(colored_button("🔙 Back", "blue"), callback_data=f"info_back_{user_id}"), InlineKeyboardButton(colored_button("❌ Close", "gray"), callback_data="info_close")]
        ]
    elif action == "addwarn":
        await warn_command(mock_update, context)
        # Re-trigger warns view
        query.data = f"info_warns_{user_id}"
        await info_callback_handler(update, context)
        return
    elif action == "resetwarn":
        await unwarn_command(mock_update, context)
        # Re-trigger warns view
        query.data = f"info_warns_{user_id}"
        await info_callback_handler(update, context)
        return
    elif action == "mute":
        member = await context.bot.get_chat_member(chat_id, user_id)
        is_muted = member.status == 'restricted' and not member.can_send_messages
        text_override = f"🔇 <b>Mute Management</b> for user <code>{user_id}</code>\n\nStatus: {'Muted 🔇' if is_muted else 'Unmuted 🔊'}"
        keyboard = [
            [InlineKeyboardButton(colored_button("🔊 Unmute" if is_muted else "🔇 Mute", "yellow"), callback_data=f"info_domute_{user_id}")],
            [InlineKeyboardButton(colored_button("🔙 Back", "blue"), callback_data=f"info_back_{user_id}"), InlineKeyboardButton(colored_button("❌ Close", "gray"), callback_data="info_close")]
        ]
    elif action == "domute":
        try:
            member = await context.bot.get_chat_member(chat_id, user_id)
            if member.status == 'restricted' and not member.can_send_messages:
                # Unmute: grant basic permissions
                await context.bot.restrict_chat_member(chat_id, user_id, permissions=ChatPermissions(
                    can_send_messages=True, can_send_audios=True, can_send_documents=True,
                    can_send_photos=True, can_send_videos=True, can_send_video_notes=True,
                    can_send_voice_notes=True, can_send_polls=True, can_send_other_messages=True,
                    can_add_web_page_previews=True
                ))
                await query.answer("User unmuted.")
            else:
                # Mute: restrict sending messages
                await context.bot.restrict_chat_member(chat_id, user_id, permissions=ChatPermissions(can_send_messages=False))
                await query.answer("User muted.")
        except Exception as e:
            await query.answer(f"Action failed: {e}", show_alert=True)
        query.data = f"info_mute_{user_id}"
        await info_callback_handler(update, context)
        return
    elif action == "ban":
        # Check if target is a channel
        is_channel = str(user_id).startswith('-100')
        
        if is_channel:
            banned = is_channel_banned(chat_id, user_id)
            text_override = f"🔨 <b>Channel Management</b> for <code>{user_id}</code>\n\nStatus: {'Banned ⛔' if banned else 'Active ✅'}\n\nChoose an action:"
            keyboard = [
                [InlineKeyboardButton(colored_button("🔓 Confirm Unban" if banned else "🔨 Confirm Ban", "red"), callback_data=f"info_doban_{user_id}")],
                [InlineKeyboardButton(colored_button("🔙 Back", "blue"), callback_data=f"info_back_{user_id}"), InlineKeyboardButton(colored_button("❌ Close", "gray"), callback_data="info_close")]
            ]
        else:
            member = await context.bot.get_chat_member(chat_id, user_id)
            is_banned = member.status == 'kicked'
            text_override = f"🔨 <b>Ban Management</b> for user <code>{user_id}</code>\n\nStatus: {'Banned ⛔' if is_banned else 'Active ✅'}"
            keyboard = [
                [InlineKeyboardButton(colored_button("🔓 Unban" if is_banned else "🔨 Ban", "red"), callback_data=f"info_doban_{user_id}")],
                [InlineKeyboardButton(colored_button("🔙 Back", "blue"), callback_data=f"info_back_{user_id}"), InlineKeyboardButton(colored_button("❌ Close", "gray"), callback_data="info_close")]
            ]
    elif action == "unban":
        # Redirect to the management view for channel unbanning
        query.data = f"info_ban_{user_id}"
        await info_callback_handler(update, context)
        return
    elif action == "dounban":
        # Handled by doban for channels
        query.data = f"info_doban_{user_id}"
        await info_callback_handler(update, context)
        return
    elif action == "doban":
        try:
            from managers.moderation_manager_mongo import add_banned_channel, remove_banned_channel
            # Check if target is a channel
            is_channel = str(user_id).startswith('-100')
            
            if is_channel:
                if is_channel_banned(chat_id, user_id):
                    await context.bot.unban_chat_sender_chat(chat_id, user_id)
                    remove_banned_channel(chat_id, user_id)
                    await query.answer("Channel unbanned from group.")
                else:
                    await context.bot.ban_chat_sender_chat(chat_id, user_id)
                    add_banned_channel(chat_id, user_id)
                    await query.answer("Channel banned from group.")
            else:
                member = await context.bot.get_chat_member(chat_id, user_id)
                if member.status == 'kicked':
                    await context.bot.unban_chat_member(chat_id, user_id, only_if_banned=True)
                    await query.answer("User unbanned.")
                else:
                    await context.bot.ban_chat_member(chat_id, user_id)
                    await query.answer("User banned.")
        except Exception as e:
            await query.answer(f"Action failed: {e}", show_alert=True)
        query.data = f"info_ban_{user_id}"
        await info_callback_handler(update, context)
        return
    elif action == "roles":
        is_muter_role = check_is_muter(chat_id, user_id)
        
        # Get actual status of the user
        is_in_group = True
        try:
            member = await context.bot.get_chat_member(chat_id, user_id)
            is_admin = member.status in ['administrator', 'creator']
            is_member = member.status in ['member', 'administrator', 'creator', 'restricted']
            if member.status in ['left', 'kicked']:
                is_in_group = False
                is_admin = False
                is_member = False
        except Exception:
            is_in_group = False
            is_admin = False
            is_member = False
        
        text_override = f"🎭 <b>Role Management</b> for user <code>{user_id}</code>"
        keyboard = [
            [InlineKeyboardButton(colored_button(f"Admin: {'✅' if is_admin else '❌'}", "blue"), callback_data=f"info_toadmin_{user_id}")],
            [InlineKeyboardButton(colored_button(f"Member: {'✅' if is_member else '❌'}", "blue"), callback_data=f"info_tomember_{user_id}")],
            [InlineKeyboardButton(colored_button(f"Muter: {'✅' if is_muter_role else '❌'}", "blue"), callback_data=f"info_tomuter_{user_id}")],
            [InlineKeyboardButton(colored_button("🔙 Back", "blue"), callback_data=f"info_back_{user_id}"), InlineKeyboardButton(colored_button("❌ Close", "gray"), callback_data="info_close")]
        ]
    elif action == "toadmin":
        try:
            member = await context.bot.get_chat_member(chat_id, user_id)
            if member.status in ['left', 'kicked']:
                await query.answer("❌ This user is not in the group. Please add them first.", show_alert=True)
                return
            
            # Show promotion menu with permissions (even if already admin, to edit)
            promote_key = f"promote_{user_id}"
            
            # If already admin, try to get current perms
            current_perms = DEFAULT_PERMISSIONS.copy()
            if member.status == 'administrator':
                for key in DEFAULT_PERMISSIONS.keys():
                    current_perms[key] = getattr(member, key, False)
            
            context.user_data[promote_key] = current_perms
            context.user_data[promote_key]["back_to_info"] = True
            
            keyboard = get_promotion_keyboard(user_id, context.user_data[promote_key], back_to_info=True)
            text_override = f"🛡️ <b>Edit Admin Permissions</b> for user <code>{user_id}</code>\n\nSelect permissions to grant/revoke:"
            try:
                await query.edit_message_text(text_override, parse_mode=ParseMode.HTML, reply_markup=keyboard)
            except BadRequest: pass
            await query.answer()
            return
        except Exception as e:
            await query.answer(f"Error checking user status: {e}", show_alert=True)
            return

    elif action == "tomember":
        try:
            member = await context.bot.get_chat_member(chat_id, user_id)
            if member.status in ['left', 'kicked']:
                # User not in group, show add/invite option
                chat = await context.bot.get_chat(chat_id)
                invite_link = chat.invite_link
                if not invite_link:
                    invite_link = await context.bot.export_chat_invite_link(chat_id)
                
                text_override = f"👤 <b>Add Member</b>\n\nUser <code>{user_id}</code> is not in this group.\n\nShare this invite link with them:\n{invite_link}"
                keyboard = [[InlineKeyboardButton(colored_button("🔙 Back", "blue"), callback_data=f"info_roles_{user_id}"), InlineKeyboardButton(colored_button("❌ Close", "gray"), callback_data="info_close")]]
                try:
                    await query.edit_message_text(text_override, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(keyboard))
                except BadRequest: pass
                await query.answer()
                return
            else:
                # Already a member, maybe demote from admin?
                if member.status == 'administrator':
                    await demote_command(mock_update, context)
                    await query.answer("Admin demoted to regular member.")
                else:
                    await query.answer("User is already a member.", show_alert=True)
        except Exception as e:
            await query.answer(f"Error: {e}", show_alert=True)
            
        query.data = f"info_roles_{user_id}"
        await info_callback_handler(update, context)
        return
    elif action == "tomuter":
        from moderation.moderation import add_muter, remove_muter
        if check_is_muter(chat_id, user_id):
            remove_muter(chat_id, user_id)
            await query.answer("Muter role removed.")
        else:
            add_muter(chat_id, user_id)
            await query.answer("Muter role granted.")
        query.data = f"info_roles_{user_id}"
        await info_callback_handler(update, context)
        return
    elif action == "perms":
        member = await context.bot.get_chat_member(chat_id, user_id)
        if member.status in ['creator', 'administrator']:
            text_override = f"🛡️ <b>Permissions</b> for user <code>{user_id}</code>\n\nAdmin/Owner permissions cannot be toggled here. Use 🎭 Roles."
            keyboard = [[InlineKeyboardButton(colored_button("🔙 Back", "blue"), callback_data=f"info_back_{user_id}"), InlineKeyboardButton(colored_button("❌ Close", "gray"), callback_data="info_close")]]
        else:
            p = {
                "msg": member.can_send_messages if member.status == 'restricted' else True,
                "media": member.can_send_media_messages if member.status == 'restricted' else True,
                "voice": member.can_send_voice_notes if member.status == 'restricted' else True,
                "doc": member.can_send_documents if member.status == 'restricted' else True,
                "poll": member.can_send_polls if member.status == 'restricted' else True,
                "other": member.can_send_other_messages if member.status == 'restricted' else True,
                "music": member.can_send_audios if member.status == 'restricted' else True,
                "video": member.can_send_videos if member.status == 'restricted' else True
            }
            
            text_override = f"🛡️ <b>Permission Management</b> for user <code>{user_id}</code>"
            keyboard = [
                [InlineKeyboardButton(colored_button(f"Send Message: {'✅' if p['msg'] else '❌'}", "blue"), callback_data=f"info_toperm_msg_{user_id}")],
                [InlineKeyboardButton(colored_button(f"Send Media: {'✅' if p['media'] else '❌'}", "blue"), callback_data=f"info_toperm_media_{user_id}")],
                [InlineKeyboardButton(colored_button(f"Send Voice: {'✅' if p['voice'] else '❌'}", "blue"), callback_data=f"info_toperm_voice_{user_id}")],
                [InlineKeyboardButton(colored_button(f"Send Document/File: {'✅' if p['doc'] else '❌'}", "blue"), callback_data=f"info_toperm_doc_{user_id}")],
                [InlineKeyboardButton(colored_button(f"Send Music: {'✅' if p['music'] else '❌'}", "blue"), callback_data=f"info_toperm_music_{user_id}")],
                [InlineKeyboardButton(colored_button(f"Send Poll: {'✅' if p['poll'] else '❌'}", "blue"), callback_data=f"info_toperm_poll_{user_id}")],
                [InlineKeyboardButton(colored_button(f"Send Stickers/GIFs: {'✅' if p['other'] else '❌'}", "blue"), callback_data=f"info_toperm_other_{user_id}")],
                [InlineKeyboardButton(colored_button("🔙 Back", "blue"), callback_data=f"info_back_{user_id}"), InlineKeyboardButton(colored_button("❌ Close", "gray"), callback_data="info_close")]
            ]
    elif action.startswith("toperm"):
        perm_type = data[2]
        user_id = int(data[3])
        member = await context.bot.get_chat_member(chat_id, user_id)
        
        # Determine current state or default to True if not restricted
        is_restricted = member.status == 'restricted'
        
        # Build current permissions object
        current_p = {
            "can_send_messages": member.can_send_messages if is_restricted else True,
            "can_send_media_messages": member.can_send_media_messages if is_restricted else True,
            "can_send_audios": member.can_send_audios if is_restricted else True,
            "can_send_documents": member.can_send_documents if is_restricted else True,
            "can_send_photos": member.can_send_photos if is_restricted else True,
            "can_send_videos": member.can_send_videos if is_restricted else True,
            "can_send_video_notes": member.can_send_video_notes if is_restricted else True,
            "can_send_voice_notes": member.can_send_voice_notes if is_restricted else True,
            "can_send_polls": member.can_send_polls if is_restricted else True,
            "can_send_other_messages": member.can_send_other_messages if is_restricted else True,
            "can_add_web_page_previews": member.can_add_web_page_previews if is_restricted else True
        }
        
        # Toggle the specific permission
        if perm_type == "msg": current_p["can_send_messages"] = not current_p["can_send_messages"]
        elif perm_type == "media": current_p["can_send_media_messages"] = not current_p["can_send_media_messages"]
        elif perm_type == "voice": current_p["can_send_voice_notes"] = not current_p["can_send_voice_notes"]
        elif perm_type == "doc": current_p["can_send_documents"] = not current_p["can_send_documents"]
        elif perm_type == "music": current_p["can_send_audios"] = not current_p["can_send_audios"]
        elif perm_type == "poll": current_p["can_send_polls"] = not current_p["can_send_polls"]
        elif perm_type == "other": current_p["can_send_other_messages"] = not current_p["can_send_other_messages"]
        
        try:
            await context.bot.restrict_chat_member(chat_id, user_id, permissions=ChatPermissions(**current_p))
            await query.answer("Permissions updated.")
        except Exception as e:
            await query.answer(f"Failed to update permissions: {e}", show_alert=True)
            
        query.data = f"info_perms_{user_id}"
        await info_callback_handler(update, context)
        return
    elif action == "restart":
        # Restart the bot
        try:
            await query.answer("Restarting bot...", show_alert=True)
            import os
            import sys
            await query.edit_message_text("🔄 Bot is restarting...")
            # Restart the bot by exiting with a special code that can be caught by a wrapper script
            # Or use os.execv to replace the current process
            logging.info("Bot restart triggered via info panel")
            os.execv(sys.executable, [sys.executable] + sys.argv)
        except Exception as e:
            await query.answer(f"Restart failed: {e}", show_alert=True)
            logging.error(f"Bot restart failed: {e}")
        return
    elif action == "back":
        # Check if target is a channel
        is_channel = str(user_id).startswith('-100')
        
        if is_channel:
            try:
                chat = await context.bot.get_chat(user_id)
                channel_link = f"• <b>Channel Link:</b> <a href=\"https://t.me/{chat.username}\">Direct Link</a>\n" if chat.username else ""
                info_text = (
                    f"📢 <b>Channel Information</b>\n\n"
                    f"• <b>Title:</b> {html.escape(chat.title)}\n"
                    f"• <b>Username:</b> @{chat.username if chat.username else 'None'}\n"
                    f"• <b>Channel ID:</b> <code>{user_id}</code>\n"
                    f"{channel_link}"
                    f"• <b>Type:</b> Channel\n"
                    f"• <b>Description:</b> {html.escape(chat.description) if chat.description else 'None'}"
                )
                
                keyboard = [
                    [InlineKeyboardButton(colored_button("🔨 Ban Channel", "red"), callback_data=f"info_ban_{user_id}")],
                    [InlineKeyboardButton(colored_button("❌ Close", "gray"), callback_data="info_close")]
                ]
                
                await query.edit_message_text(info_text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(keyboard))
                await query.answer()
                return
            except Exception as e:
                logging.error(f"Error returning to channel info: {e}")
                await query.answer(f"Error: {e}", show_alert=True)
                return

        # We need to reconstruct the original info message
        # Easiest way is to call info_command with a mock update
        mock_update.message.text = f"/info {user_id}"
        # We need to delete the current menu first or just edit it back
        # Let's just edit it back to the original info text and main buttons
        # For simplicity, we can reuse the code from info_command
        from managers.user_manager_mongo import get_user_stats
        import html
        stats = get_user_stats(user_id)
        username = stats.get("username") if stats else None
        joined_date = stats.get("joined_date", "Unknown") if stats else "Unknown"
        msg_count = stats.get("msg_count", 0) if stats else 0
        
        # Get user status and role in group
        user_status = "Unknown"; user_role = "Member"; is_muted = False
        try:
            member = await context.bot.get_chat_member(chat_id, user_id)
            status = member.status
            if status == 'creator': user_status = "Present"; user_role = "Owner/Creator"
            elif status == 'administrator': user_status = "Present"; user_role = "Administrator"
            elif status == 'member': user_status = "Present"; user_role = "Member"
            elif status == 'restricted': 
                user_status = "Present (Restricted)"; user_role = "Restricted Member"
                if not member.can_send_messages: is_muted = True
            elif status == 'left': user_status = "Left"; user_role = "None"
            elif status == 'kicked': user_status = "Banned"; user_role = "None"
        except: pass

        # Link formatting
        is_channel = str(user_id).startswith('-100')
        user_link = f"tg://user?id={user_id}"
        if is_channel and username:
            user_link = f"https://t.me/{username}"
        elif is_channel:
            clean_id = str(user_id).replace("-100", "")
            user_link = f"https://t.me/c/{clean_id}/1"

        info_text = (
            f"👤 <b>User Information</b>\n\n"
            f"• <b>Name:</b> {html.escape(stats.get('name', 'Unknown')) if stats else 'Unknown'}\n"
            f"• <b>Username:</b> @{username if username else 'None'}\n"
            f"• <b>User ID:</b> <code>{user_id}</code>\n"
            f"• <b>User Link:</b> <a href=\"{user_link}\">Direct Link</a>\n"
            f"• <b>Status:</b> {user_status}\n"
            f"• <b>Role:</b> {user_role}\n"
            f"• <b>Muted:</b> {'Yes 🔇' if is_muted else 'No 🔊'}\n"
            f"• <b>Joined Date:</b> {joined_date}\n"
            f"• <b>Total Messages:</b> {msg_count}"
        )
        
        keyboard = [
            [InlineKeyboardButton(colored_button("⚠️ Warns", "red"), callback_data=f"info_warns_{user_id}"), InlineKeyboardButton(colored_button("🎭 Roles", "blue"), callback_data=f"info_roles_{user_id}")],
            [InlineKeyboardButton(colored_button("🔇 Mute" if not is_muted else "🔊 Unmute", "yellow"), callback_data=f"info_mute_{user_id}"), InlineKeyboardButton(colored_button("🔨 Ban" if user_status != "Banned" else "🔓 Unban", "red"), callback_data=f"info_ban_{user_id}")],
            [InlineKeyboardButton(colored_button("🔄 Restart", "green"), callback_data=f"info_restart_{user_id}"), InlineKeyboardButton(colored_button("❌ Close", "gray"), callback_data="info_close")]
        ]
        
        await query.edit_message_text(info_text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(keyboard))
        await query.answer()
        return


    if text_override and keyboard:
        try:
            await query.edit_message_text(text_override, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(keyboard))
        except BadRequest: pass
    
    await query.answer()

async def get_id_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Simple command to show user ID and ensure they are cached."""
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    
    # Check command access
    from managers.settings_manager_mongo import check_command_access
    if not await check_command_access(chat_id, user_id, 'id', context):
        await send_bot_response(update, context, "You don't have permission to use the /id command.")
        return

    user = update.effective_user
    chat = update.effective_chat
    
    # Use HTML and escape inputs to prevent parsing errors
    user_name = html.escape(user.first_name)
    chat_title = html.escape(chat.title) if chat.title else "Private Chat"
    
    await send_bot_response(
        update, context,
        f"👤 <b>User Info:</b>\n"
        f"• Name: {user_name}\n"
        f"• ID: <code>{user.id}</code>\n"
        f"• Username: @{user.username if user.username else 'None'}\n\n"
        f"📍 <b>Chat Info:</b>\n"
        f"• Title: {chat_title}\n"
        f"• ID: <code>{chat.id}</code>",
        parse_mode=ParseMode.HTML
    )

async def check_command_permission(update: Update, context: ContextTypes.DEFAULT_TYPE, command: str) -> bool:
    """Check if the user has permission to use a command based on settings."""
    if not update.effective_chat:
        return True
    
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    
    # Always allow in private
    if update.effective_chat.type == 'private':
        return True
        
    settings = get_chat_settings(chat_id)
    perms = settings.get("command_permissions", {})
    level = perms.get(command, "all")
    
    if level == "all":
        return True
    elif level == "staff":
        return await is_user_admin(chat_id, user_id, context)
    elif level == "private":
        # Handled by command implementation (redirect to private)
        return True
    elif level == "nobody":
        return False
    return True

async def send_rules_message(bot, chat_id, target_chat_id, settings, user=None, chat=None):
    """Helper to send the formatted rules message."""
    text = settings.get("rules_text", "No rules have been set for this group yet.")
    
    # Format placeholders if user and chat are provided
    if user and chat:
        from handlers.welcome import format_welcome_message
        text = format_welcome_message(text, user, chat)
        
    media = settings.get("rules_media")
    media_type = settings.get("rules_media_type")
    buttons = settings.get("rules_buttons", [])
    
    reply_markup = None
    if buttons:
        keyboard = []
        for btn in buttons:
            # Handle both 'label' and 'text' keys for compatibility
            label = btn.get('label') or btn.get('text')
            if label:
                keyboard.append([InlineKeyboardButton(label, url=btn['url'])])
        if keyboard:
            reply_markup = InlineKeyboardMarkup(keyboard)
        
    try:
        if media:
            if media_type == "photo":
                await bot.send_photo(target_chat_id, media, caption=text, reply_markup=reply_markup, parse_mode='HTML')
            elif media_type == "video":
                await bot.send_video(target_chat_id, media, caption=text, reply_markup=reply_markup, parse_mode='HTML')
            else:
                await bot.send_message(target_chat_id, text, reply_markup=reply_markup, parse_mode='HTML')
        else:
            await bot.send_message(target_chat_id, text, reply_markup=reply_markup, parse_mode='HTML')
        return True
    except Exception as e:
        logging.error(f"Error sending rules: {e}")
        # Try sending without HTML if it failed
        try:
            if media:
                if media_type == "photo":
                    await bot.send_photo(target_chat_id, media, caption=text, reply_markup=reply_markup)
                elif media_type == "video":
                    await bot.send_video(target_chat_id, media, caption=text, reply_markup=reply_markup)
            else:
                await bot.send_message(target_chat_id, text, reply_markup=reply_markup)
            return True
        except Exception:
            return False

async def rules_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Command to show group rules."""
    if not update.effective_chat: return
    
    chat_id = update.effective_chat.id
    if not await check_command_permission(update, context, "rules"):
        return
        
    settings = get_chat_settings(chat_id)
    perms = settings.get("command_permissions", {})
    
    user = update.effective_user
    chat = update.effective_chat

    if perms.get("rules") == "private":
        # Send in private
        success = await send_rules_message(context.bot, chat_id, user.id, settings, user, chat)
        if success:
            try:
                await update.message.reply_text("✅ I've sent the rules to you in private chat.")
            except Exception: pass
        else:
            try:
                await update.message.reply_text("❌ I couldn't send you the rules. Please make sure you've started me in private.")
            except Exception: pass
    else:
        await send_rules_message(context.bot, chat_id, chat_id, settings, user, chat)

async def me_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Command to show user info."""
    if not update.effective_chat: return
    
    if not await check_command_permission(update, context, "me"):
        return
        
    user = update.effective_user
    text = (
        f"👤 <b>User Info</b>\n\n"
        f"<b>Name:</b> {user.full_name}\n"
        f"<b>ID:</b> <code>{user.id}</code>\n"
        f"<b>Username:</b> @{user.username if user.username else 'None'}"
    )
    
    if update.effective_chat.type != 'private':
        try:
            await context.bot.send_message(user.id, text, parse_mode='HTML')
            await update.message.reply_text("✅ I've sent your info to you in private chat.")
            return
        except Exception:
            await update.message.reply_text("❌ Please start me in private chat first so I can send you your info.")
            return
                
    await update.message.reply_text(text, parse_mode='HTML')

async def staff_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Command to show group staff."""
    if not update.effective_chat: return
    chat_id = update.effective_chat.id
    
    if not await check_command_permission(update, context, "staff"):
        return
        
    admins = await context.bot.get_chat_administrators(chat_id)
    text = "👮 <b>Group Staff:</b>\n\n"
    for admin in admins:
        status = "Owner" if admin.status == 'creator' else "Admin"
        name = admin.user.full_name
        text += f"• {name} ({status})\n"
        
    await update.message.reply_text(text, parse_mode='HTML')

async def translate_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Placeholder for translate command."""
    if not await check_command_permission(update, context, "translate"):
        return
    await update.message.reply_text("🌐 Translation feature is coming soon!")

async def get_link_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Command to show the group link."""
    if not update.effective_chat: return
    chat_id = update.effective_chat.id
    
    if not await check_command_permission(update, context, "link"):
        return
        
    settings = get_chat_settings(chat_id)
    link = settings.get("group_link")
    
    if not link:
        await send_bot_response(update, context, "❌ No group link has been set yet.")
        return
        
    # Send directly without using send_bot_response to avoid small caps formatting
    from telegram.constants import ParseMode
    await update.message.reply_text(f"🔗 <b>Group Link:</b>\n{link}", parse_mode=ParseMode.HTML)

async def adminlist_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Command to list all admins in the group."""
    if not update.effective_chat: return
    chat_id = update.effective_chat.id
    
    if not await check_command_permission(update, context, "adminlist"):
        return
        
    try:
        admins = await context.bot.get_chat_administrators(chat_id)
        
        creators = []
        administrators = []
        bots = []
        
        for admin in admins:
            user = admin.user
            status = admin.status
            custom_title = admin.custom_title or ""
            
            name = user.first_name
            if user.last_name:
                name += f" {user.last_name}"
            
            # Escape HTML characters in name
            name = name.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            
            mention = f"<a href='tg://user?id={user.id}'>{name}</a>"
            if user.username:
                mention = f"<a href='https://t.me/{user.username}'>{name}</a>"
                
            title_str = f" [<i>{custom_title}</i>]" if custom_title else ""
            line = f"• {mention}{title_str}"
            
            if user.is_bot:
                bots.append(line)
            elif status == "creator":
                creators.append(line)
            else:
                administrators.append(line)
        
        text = f"🛡️ <b>Admins for {update.effective_chat.title}:</b>\n\n"
        
        if creators:
            text += "👤 <b>Creator:</b>\n" + "\n".join(creators) + "\n\n"
            
        if administrators:
            text += f"👮 <b>Administrators ({len(administrators)}):</b>\n" + "\n".join(administrators) + "\n\n"
            
        if bots:
            text += f"🤖 <b>Bots ({len(bots)}):</b>\n" + "\n".join(bots) + "\n"
            
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton(colored_button("❌ Close", "default"), callback_data="info_close")]])
        await update.message.reply_text(text, parse_mode='HTML', disable_web_page_preview=True, reply_markup=keyboard)
        
    except Exception as e:
        logging.error(f"Error in adminlist command: {e}")
        await send_bot_response(update, context, "❌ Failed to retrieve admin list.")

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log the error and send a telegram message to notify the developer."""
    logging.error(f"Exception while handling an update: {context.error}")
    import traceback
    tb_list = traceback.format_exception(None, context.error, context.error.__traceback__)
    tb_string = "".join(tb_list)
    logging.error(tb_string)

def main():
    """Main function to run the bot."""
    # Prevent multiple instances
    lock_file = open('/tmp/bot.lock', 'w')
    try:
        fcntl.lockf(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except IOError:
        print("❌ Another instance of the bot is already running. Exiting.")
        sys.exit(0)

    if not BOT_TOKEN:
        print("BOT_TOKEN not found in .env file. Please add it.")
        return

    # Initialize MongoDB connection
    print("Connecting to MongoDB...")
    from core.database import connect_to_mongodb, initialize_collections
    if not connect_to_mongodb():
        print("WARNING: Failed to connect to MongoDB. Bot will run with limited functionality.")
    else:
        initialize_collections()
        print("✅ MongoDB database initialized successfully!")

    # Initialize the bot application with optimization
    application = ApplicationBuilder().token(BOT_TOKEN).concurrent_updates(True).build()

    async def startup_info(app):
        me = await app.bot.get_me()
        print(f"✅ Bot started as @{me.username} ({me.id})")
    
    # Add startup info to post_init
    original_post_init = application.post_init
    async def new_post_init(app):
        await startup_info(app)
        if original_post_init:
            await original_post_init(app)
    
    application.post_init = new_post_init

    # Add error handler
    application.add_error_handler(error_handler)

    # Add general handlers (Group 0)
    from handlers.blocking_handler import handle_reaction_blocking
    application.add_handler(MessageReactionHandler(handle_reaction_blocking), group=0)
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("broadcast", broadcast_command))
    application.add_handler(CommandHandler("id", get_id_command))
    application.add_handler(CommandHandler("link", get_link_command))
    application.add_handler(CommandHandler("adminlist", adminlist_command))
    application.add_handler(CommandHandler("rules", rules_command))
    application.add_handler(MessageHandler(filters.Regex(r'^/rules(@\w+)?$'), rules_command))
    application.add_handler(CommandHandler("staff", staff_command))
    application.add_handler(CommandHandler("me", me_command))
    application.add_handler(CommandHandler("translate", translate_command))
    application.add_handler(CommandHandler("info", info_command))
    application.add_handler(CommandHandler("report", report_command))
    application.add_handler(CommandHandler("groupdetail", groupdetail_command))
    application.add_handler(CommandHandler(["ping", "stats", "pingstats"], ping_stats_command))
    application.add_handler(MessageHandler(
        filters.Regex(r'^@admin(s)?\b') & filters.ChatType.GROUPS,
        handle_admin_mention
    ))
    application.add_handler(CallbackQueryHandler(info_callback_handler, pattern="^info_"))
    application.add_handler(CallbackQueryHandler(music_world_callback, pattern="^(music_world|music_back|help_open)$"))
    application.add_handler(MessageHandler(filters.ALL & (filters.ChatType.GROUPS | filters.ChatType.SUPERGROUP), cache_user_handler), group=-1)

    # Add support for commands in channels
    from moderation.moderation import ban_command, unban_command
    application.add_handler(CommandHandler("ban", ban_command, filters=filters.ChatType.CHANNEL))
    application.add_handler(CommandHandler("unban", unban_command, filters=filters.ChatType.CHANNEL))

    # Add Voice Chat handlers (Group 0)
    for handler in get_voice_chat_handlers():
        application.add_handler(handler)

    # Add Doraemon AI Chatbot handlers
    for handler in get_doraemon_ai_handlers():
        application.add_handler(handler)

    # Add Admin handlers (Group 0)
    for handler in get_admin_handlers():
        application.add_handler(handler)

    # Add Sudo handlers (Group 0) — owner-only sudo management
    from moderation.sudo import get_sudo_handlers
    for handler in get_sudo_handlers():
        application.add_handler(handler)

    # Add Blocking commands (Group 0)
    for handler in get_blocking_command_handlers():
        application.add_handler(handler)

    # Add help handlers (Group 0)
    for handler in get_help_handlers():
        application.add_handler(handler)

    # Add settings handlers (Group 0)
    for handler in get_settings_handlers():
        application.add_handler(handler)

    # Add moderation handlers (Group 0)
    for handler in get_moderation_handlers():
        application.add_handler(handler)

    # Add Bio Link handlers (Group 5)
    for handler in get_bio_handlers():
        application.add_handler(handler, group=5)

    # Add welcome handlers (Group 0)
    for handler in get_welcome_handlers():
        application.add_handler(handler)

    # Add captcha handlers (Group 0)
    for handler in get_captcha_handlers():
        application.add_handler(handler)

    # Add Premium Boost Captcha handlers (Group 6 for member join & message checks, Group 0 for commands/callbacks)
    for handler in get_premium_boost_handlers():
        if isinstance(handler, (CommandHandler, CallbackQueryHandler)):
            application.add_handler(handler, group=0)
        else:
            application.add_handler(handler, group=6)

    # Add block content handlers (Group 0)
    for handler in get_block_content_handlers():
        # The MessageHandler for block content check should be in its own group to run concurrently
        if isinstance(handler, MessageHandler) and not isinstance(handler, CommandHandler):
            application.add_handler(handler, group=4)
        else:
            application.add_handler(handler)

    # Add clean service handlers in a separate group (Group 1)
    # This ensures they run even if other handlers match
    for handler in get_clean_service_handlers():
        application.add_handler(handler, group=1)

    # Add auto delete handlers in another separate group (Group 2)
    for handler in get_auto_delete_handlers():
        application.add_handler(handler, group=2)

    # Add filter handlers in another group (Group 3)
    for handler in get_filter_handlers():
        # The filter command handlers go to group 0 (default), 
        # but the MessageHandler for triggers should be in its own group to run concurrently
        if isinstance(handler, MessageHandler):
            application.add_handler(handler, group=3)
        else:
            application.add_handler(handler)

    # Night Mode handlers
    for handler in get_nightmode_handlers():
        if isinstance(handler, MessageHandler) and not isinstance(handler, CommandHandler):
            if handler.filters == (filters.ALL & ~filters.COMMAND & filters.ChatType.GROUPS):
                # Group filter goes to Group 4 for higher priority
                application.add_handler(handler, group=4)
            else:
                # Private chat config handler goes to Group 0 to catch location/text reliably
                application.add_handler(handler, group=0)
        else:
            application.add_handler(handler)

    # Add bot protection handlers (Group 5)
    for handler in get_bot_protection_handlers():
        application.add_handler(handler, group=5)

    # Add link spam protection handlers (Group 0)
    for handler in get_link_spam_handlers():
        application.add_handler(handler)

    # Add forward protection handlers (Group 7)
    for handler in get_forward_protection_handlers():
        application.add_handler(handler, group=7)

    # Add Translation handlers (Group 9)
    for handler in get_translation_handlers():
        application.add_handler(handler, group=9)

    # Add Language Filter handlers (Group 10)
    for handler in get_language_handlers():
        application.add_handler(handler, group=10)

    # Add Sticker handlers (Group 11)
    for handler in get_sticker_handlers():
        application.add_handler(handler, group=11)

    # Add Blocking handlers (Group 12)
    for handler in get_blocking_handlers():
        application.add_handler(handler, group=12)
    
    # Add Sangmata handlers (Group 0)
    for handler in get_sangmata_handlers():
        application.add_handler(handler)
    
    # Add Recurring Message handlers (Group 13)
    for handler in get_recurring_handlers():
        if isinstance(handler, MessageHandler) and not isinstance(handler, CommandHandler):
            application.add_handler(handler, group=13)
        else:
            application.add_handler(handler)
    
    # Message count for recurring messages (Group 14)
    application.add_handler(MessageHandler(filters.ALL & filters.ChatType.GROUPS, count_recurring_messages), group=14)

    # Add Recurring Messages background job
    if application.job_queue:
        application.job_queue.run_repeating(check_recurring_messages, interval=60, first=10)
    
    # Edit Checks (Group 16)
    for handler in get_edit_handlers():
        application.add_handler(handler, group=16)

    # Anti-Flood handlers (Group 15)
    for handler in get_antiflood_handlers():
        application.add_handler(handler, group=15)

    # Add Manager handlers (Group 0) - Group management commands
    for handler in get_manager_handlers():
        application.add_handler(handler)

    # Add Tagger handlers (Group 17)
    for handler in get_tagger_handlers():
        application.add_handler(handler, group=17)

    # Add Multi-Task tools handlers (Group 0)
    for handler in get_multitask_handlers():
        application.add_handler(handler)

    # Add NSFW Safeguard handlers (Group 0)
    for handler in get_nsfw_handlers():
        application.add_handler(handler)

    # Add Join Request handlers
    for handler in get_join_request_handlers():
        application.add_handler(handler)

    # Start the bot
    print("Bot is starting...")
    
    # Add startup hook to show zombie count
    async def startup_notification(bot):
        """Send startup notification with statistics."""
        from core.config import log_to_channel, LOG_CHANNEL_ID
        from managers.zombie import get_total_blocked_count, get_total_zombies_count
        from core.database import get_collection, COLLECTIONS
        
        try:
            # Get database stats
            settings_col = get_collection(COLLECTIONS["settings"])
            users_col = get_collection(COLLECTIONS["users"])
            
            total_groups = settings_col.count_documents({}) if settings_col is not None else 0
            total_users = users_col.count_documents({}) if users_col is not None else 0
            
            # Get blocked/banned members count
            blocked_info = await get_total_blocked_count(bot)
            total_blocked = blocked_info["total_blocked"]
            groups_with_bans = blocked_info["groups_with_bans"]
            
            # Get zombie count (optional)
            zombie_info = await get_total_zombies_count(bot)
            total_zombies = zombie_info["total_zombies"]
            
            # Create startup message
            startup_msg = (
                f"🚀 <b>BOT RESTARTED</b>\n\n"
                f"📊 <b>Statistics:</b>\n"
                f"• 👥 Total Groups: {total_groups}\n"
                f"• 👤 Cached Users: {total_users}\n"
                f"• 🔨 Total Blocked Members: {total_blocked}\n"
                f"• 📁 Groups with Bans: {groups_with_bans}\n"
                f"• 🧹 Total Freed Members: {total_zombies}\n\n"
                f"✅ Bot is now running!\n"
                f"⏰ {__import__('datetime').datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            )
            
            # Send to log channel directly
            if LOG_CHANNEL_ID:
                await bot.send_message(chat_id=LOG_CHANNEL_ID, text=f"🔔 #LOG\n\n{startup_msg}", parse_mode='HTML')
            
            print(f"\n✅ Bot started successfully!")
            print(f"📊 Groups: {total_groups} | Users: {total_users} | Blocked: {total_blocked} | Freed: {total_zombies}\n")
            
        except Exception as e:
            logging.error(f"Startup notification error: {e}")
            print(f"Bot started (with error: {e})")
    
    # Run startup notification after bot starts
    async def post_init(application):
        """Called after bot initialization."""
        import asyncio
        from telegram import BotCommand
        
        # Set bot commands for suggestions
        commands = [
            BotCommand("start", "Start the bot"),
            BotCommand("id", "Get chat/user ID"),
            BotCommand("link", "Get group link"),
            BotCommand("adminlist", "List group admins"),
            BotCommand("rules", "View group rules"),
            BotCommand("staff", "Show group staff"),
            BotCommand("me", "Show your info"),
            BotCommand("info", "Show chat info"),
            BotCommand("send", "Send message through bot"),
            BotCommand("psend", "Send protected message"),
            BotCommand("report", "Report a message to admins"),
            BotCommand("settings", "Bot settings (Admins only)"),
            BotCommand("blocktext", "Toggle text blocking (Admins only)"),
            BotCommand("blockreaction", "Toggle reaction blocking (Admins only)"),
            BotCommand("free", "Exempt a user (Admins only)"),
            BotCommand("joinreq", "Manage group join requests (Admins only)"),
            BotCommand("captcha", "Captcha verification settings (Admins only)"),
            BotCommand("premiumboost", "Manage premium user boost requirements (Admins only)"),
            BotCommand("doraemon", "Chat with Doraemon AI Assistant"),
        ]
        try:
            await application.bot.set_my_commands(commands)
            print("✅ Bot commands registered for suggestions")
        except Exception as e:
            print(f"❌ Failed to register commands: {e}")

        # Start voice chat monitor (Telethon) immediately
        print("🎙 Starting Voice Chat Monitor...")
        asyncio.create_task(start_voice_chat_monitor(application))

        # Run startup notification in background after a short delay
        async def delayed_startup():
            await asyncio.sleep(2)
            await startup_notification(application.bot)
        
        asyncio.create_task(delayed_startup())
    
    async def post_stop(application):
        """Called after bot stops."""
        await stop_voice_chat_monitor()
    
    application.post_init = post_init
    application.post_stop = post_stop
    
    print("Bot is starting...")
    application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)

if __name__ == '__main__':
    main()
