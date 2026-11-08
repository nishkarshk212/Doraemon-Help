from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, CommandHandler, CallbackQueryHandler, MessageHandler, filters
from telegram.error import BadRequest
from managers.settings_manager_mongo import get_chat_settings, update_chat_setting
from core.database import get_collection, COLLECTIONS
from core.config import OWNER_ID, send_bot_response, edit_bot_response, colored_button
from managers.user_manager_mongo import can_user_configure_settings
from moderation.anonymous_admin import is_anonymous_admin, check_anonymous_admin_change_info_permission
from handlers.blocking_handler import DEFAULT_BLOCKING_SETTINGS
import logging
import asyncio

async def delete_saved_message(context, message):
    """Delete the saved message job."""
    try:
        await message.delete()
    except Exception as e:
        logging.warning(f"Failed to delete saved message: {e}")

async def get_manageable_groups(user_id, context):
    """Find all groups where the user has permission to change settings."""
    groups = []
    settings_col = get_collection(COLLECTIONS["settings"])
    if settings_col is None:
        return groups
    
    # Get all unique chat_ids from settings
    cursor = settings_col.find({}, {"chat_id": 1})
    for doc in cursor:
        try:
            chat_id = int(doc["chat_id"])
            
            # Skip if it's the user's own private chat ID (though unlikely to be here)
            if chat_id > 0: continue

            # OWNER always has access
            if user_id == OWNER_ID:
                try:
                    chat = await context.bot.get_chat(chat_id)
                    groups.append({"id": chat_id, "title": chat.title or str(chat_id)})
                except Exception:
                    groups.append({"id": chat_id, "title": f"Group {chat_id}"})
                continue

            # Check if user can configure settings in this chat
            if await can_user_configure_settings(chat_id, user_id, context):
                try:
                    chat = await context.bot.get_chat(chat_id)
                    groups.append({"id": chat_id, "title": chat.title or str(chat_id)})
                except Exception:
                    # If we can't get chat info, maybe the bot was removed
                    groups.append({"id": chat_id, "title": f"Group {chat_id}"})
        except Exception:
            continue
    return groups

async def settings_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Settings command handler - shows option to open in group or private."""
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    chat_type = update.effective_chat.type

    # If it's a private chat, show group selection
    if chat_type == "private":
        groups = await get_manageable_groups(user_id, context)
        if not groups:
            await send_bot_response(update, context, "You don't manage any groups with this bot yet.")
            return
        
        keyboard = []
        for group in groups:
            keyboard.append([InlineKeyboardButton(colored_button(group['title'], "blue"), callback_data=f"set_view_main_for_{group['id']}")])
        
        message_text = (
            "Manage group Settings\n"
            "👉🏻 Select the group whose settings you want to change."
        )
        
        await update.message.reply_text(message_text, reply_markup=InlineKeyboardMarkup(keyboard))
        return

    # Check command access for settings
    from managers.settings_manager_mongo import check_command_access
    if not await check_command_access(chat_id, user_id, 'settings', context):
        await send_bot_response(update, context, "You don't have permission to use the /settings command.")
        return
    
    # Check granular permissions (Change Info + Ban Users)
    if not await can_user_configure_settings(chat_id, user_id, context):
        await send_bot_response(update, context, "You need both 'Change Group Info' and 'Ban Users' permissions to configure settings.")
        return
    
    # Create bot URL for private chat
    bot_info = await context.bot.get_me()
    bot_username = bot_info.username
    
    # Store chat_id in user_data to retrieve later
    context.user_data['settings_chat_id'] = chat_id
    
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(colored_button("📖 Open Here", "blue"), callback_data=f"settings_open_here_{chat_id}")],
        [InlineKeyboardButton(colored_button("💬 Open in Private", "blue"), url=f"https://t.me/{bot_username}?start=settings_{chat_id}")]
    ])
    
    settings_text = (
        f"How would you like to open the settings?"
    )
    
    await send_bot_response(update, context, settings_text, reply_markup=keyboard)

async def open_settings_here(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Open settings directly in the group."""
    query = update.callback_query
    
    # Extract chat_id from callback data
    chat_id = int(query.data.split("_")[-1])
    
    # Store chat_id in user_data
    context.user_data['settings_chat_id'] = chat_id
    
    try:
        await query.answer()
    except Exception as e:
        logging.warning(f"Callback query answer failed: {e}")
    
    await show_settings_panel(update, context, chat_id)

async def show_settings_panel(query_or_update, context, chat_id, is_private=False):
    """Show the actual settings panel."""
    # First check if it has message attribute (Update object from deep link)
    if hasattr(query_or_update, 'message') and hasattr(query_or_update, 'effective_user'):
        # It's an Update object (from deep link)
        update = query_or_update
        user_id = update.effective_user.id
        
        # Check permissions
        if not await can_user_configure_settings(chat_id, user_id, context):
            await send_bot_response(update, context, "You need both 'Change Group Info' and 'Ban Users' permissions to configure settings.")
            return
        
        keyboard = get_main_settings_keyboard(chat_id)
        
        # Get group info
        try:
            chat = await context.bot.get_chat(chat_id)
            group_name = chat.title or "Unknown"
        except:
            group_name = "Unknown"
        
        group_id = chat_id
        user_mention = f"<a href='tg://user?id={user_id}'>{update.effective_user.first_name}</a>"
        
        settings_text = (
            f"🛠 <b>ʙᴏᴛ ꜱᴇᴛᴛɪɴɢꜱ 🛠</b>\n\n"
            f"ɢʀᴏᴜᴘ: {group_name}\n"
            f"ɪᴅ: {group_id}\n"
            f"ᴏᴘᴇɴᴇᴅ ʙʏ: {user_mention}\n\n"
            f"ꜱᴇʟᴇᴄᴛ ᴏɴᴇ ᴏꜰ ᴛʜᴇ ꜱᴇᴛᴛɪɴɢꜱ ᴛʜᴀᴛ ʏᴏᴜ ᴡᴀɴᴛ ᴛᴏ ᴄʜᴀɴɢᴇ:"
        )
        
        # Send directly with HTML parse mode to preserve links
        try:
            msg = await update.message.reply_text(settings_text, reply_markup=keyboard, parse_mode='HTML')
        except:
            msg = await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=settings_text,
                reply_markup=keyboard,
                parse_mode='HTML'
            )
    elif hasattr(query_or_update, 'callback_query'):
        # It's a CallbackQuery wrapped in Update
        query = query_or_update.callback_query
        if query is None:
            logging.error("callback_query is None")
            return
        user_id = query.from_user.id
        
        # Check permissions
        if not await can_user_configure_settings(chat_id, user_id, context):
            await query.answer("You don't have permission to access settings.", show_alert=True)
            return
        
        keyboard = get_main_settings_keyboard(chat_id)
        
        # Get group info
        try:
            chat = await context.bot.get_chat(chat_id)
            group_name = chat.title or "Unknown"
        except:
            group_name = "Unknown"
        
        group_id = chat_id
        user_mention = f"<a href='tg://user?id={user_id}'>{query.from_user.first_name}</a>"
        
        settings_text = (
            f"🛠 <b>ʙᴏᴛ ꜱᴇᴛᴛɪɴɢꜱ 🛠</b>\n\n"
            f"ɢʀᴏᴜᴘ: {group_name}\n"
            f"ɪᴅ: {group_id}\n"
            f"ᴏᴘᴇɴᴇᴅ ʙʏ: {user_mention}\n\n"
            f"ꜱᴇʟᴇᴄᴛ ᴏɴᴇ ᴏꜰ ᴛʜᴇ ꜱᴇᴛᴛɪɴɢꜱ ᴛʜᴀᴛ ʏᴏᴜ ᴡᴀɴᴛ ᴛᴏ ᴄʜᴀɴɢᴇ:"
        )
        
        try:
            await query.edit_message_text(settings_text, reply_markup=keyboard, parse_mode='HTML')
        except BadRequest:
            await query.message.edit_text(settings_text, reply_markup=keyboard, parse_mode='HTML')
    else:
        # It's a CallbackQuery directly
        query = query_or_update
        user_id = query.from_user.id
        
        # Check permissions
        if not await can_user_configure_settings(chat_id, user_id, context):
            await query.answer("You don't have permission to access settings.", show_alert=True)
            return
        
        keyboard = get_main_settings_keyboard(chat_id)
        
        # Get group info
        try:
            chat = await context.bot.get_chat(chat_id)
            group_name = chat.title or "Unknown"
        except:
            group_name = "Unknown"
        
        group_id = chat_id
        user_mention = f"<a href='tg://user?id={user_id}'>{query.from_user.first_name}</a>"
        
        settings_text = (
            f"🛠 <b>ʙᴏᴛ ꜱᴇᴛᴛɪɴɢꜱ 🛠</b>\n\n"
            f"ɢʀᴏᴜᴘ: {group_name}\n"
            f"ɪᴅ: {group_id}\n"
            f"ᴏᴘᴇɴᴇᴅ ʙʏ: {user_mention}\n\n"
            f"ꜱᴇʟᴇᴄᴛ ᴏɴᴇ ᴏꜰ ᴛʜᴇ ꜱᴇᴛᴛɪɴɢꜱ ᴛʜᴀᴛ ʏᴏᴜ ᴡᴀɴᴛ ᴛᴏ ᴄʜᴀɴɢᴇ:"
        )
        
        try:
            await query.edit_message_text(settings_text, reply_markup=keyboard, parse_mode='HTML')
        except BadRequest:
            await query.message.edit_text(settings_text, reply_markup=keyboard, parse_mode='HTML')

def get_main_settings_keyboard(chat_id):
    """Get main settings keyboard with layout support."""
    settings = get_chat_settings(chat_id)
    layout_type = settings.get("ui_layout_type", 1)
    
    if layout_type == 2:
        # Compact layout (Small)
        keyboard = [
            [InlineKeyboardButton(colored_button("👋 Welcome", "blue"), callback_data="set_view_welcome"),
             InlineKeyboardButton(colored_button("📥 JoinReq", "blue"), callback_data="set_view_join_request")],
            [InlineKeyboardButton(colored_button("👋 Goodbye", "blue"), callback_data="set_view_goodbye"),
             InlineKeyboardButton(colored_button("📏 Length", "blue"), callback_data="set_view_msg_length")],
            [InlineKeyboardButton(colored_button("🛡️ Mod", "blue"), callback_data="set_view_mod"),
             InlineKeyboardButton(colored_button("🤖 BotProt", "blue"), callback_data="set_view_bot_protection")],
            [InlineKeyboardButton(colored_button("🔗 LinkProt", "blue"), callback_data="set_view_link_spam"),
             InlineKeyboardButton(colored_button("🔄 Fwd", "blue"), callback_data="set_view_forward_protection")],
            [InlineKeyboardButton(colored_button("🔑 Cmd", "blue"), callback_data="set_view_command_access")],
            [InlineKeyboardButton(colored_button("🕒 Recur", "blue"), callback_data="set_view_recurring"),
             InlineKeyboardButton(colored_button("🌙 Night", "blue"), callback_data="set_view_nightmode")],
            [InlineKeyboardButton(colored_button("🌊 Flood", "blue"), callback_data="set_view_antiflood"),
             InlineKeyboardButton(colored_button("🌐 Lang", "blue"), callback_data="set_view_language_filter")],
            [InlineKeyboardButton(colored_button("🚫 Words", "blue"), callback_data="set_view_banned_words"),
             InlineKeyboardButton(colored_button("🏷️ Tag", "blue"), callback_data="set_view_tagger")],
            [InlineKeyboardButton(colored_button("🔗 Link", "blue"), callback_data="set_view_group_link"),
             InlineKeyboardButton(colored_button("📜 Regul", "blue"), callback_data="set_view_regulations")],
            [InlineKeyboardButton(colored_button("👥 Mem", "blue"), callback_data="set_view_members_mgmt"),
             InlineKeyboardButton(colored_button("🎙 VC", "blue"), callback_data="set_view_vc")],
            [InlineKeyboardButton(colored_button("🗑️ Del", "blue"), callback_data="set_view_deleting"),
             InlineKeyboardButton(colored_button("👥 Mgr", "blue"), callback_data="set_view_manager")],
            [InlineKeyboardButton(colored_button("🚨 Emergency", "blue"), callback_data="set_view_emergency"),
             InlineKeyboardButton(colored_button("🛡️ Captcha", "blue"), callback_data="set_view_captcha")],
            [InlineKeyboardButton(colored_button("🔞 NSFW", "blue"), callback_data="set_view_nsfw"),
             InlineKeyboardButton(colored_button("📋 Freed", "blue"), callback_data="free_list_members")],
            [InlineKeyboardButton(colored_button("🎨 Layout: Small", "blue"), callback_data="set_toggle_ui_layout")],
            [InlineKeyboardButton(colored_button("❌ Close", "blue"), callback_data="set_close")]
        ]
    else:
        # Standard layout (Large)
        keyboard = [
            [InlineKeyboardButton(colored_button("👋 Welcome", "blue"), callback_data="set_view_welcome")],
            [InlineKeyboardButton(colored_button("👋 Goodbye Message", "blue"), callback_data="set_view_goodbye")],
            [InlineKeyboardButton(colored_button("📥 Join Request", "blue"), callback_data="set_view_join_request")],
            [InlineKeyboardButton(colored_button("📏 Message Length", "blue"), callback_data="set_view_msg_length"),
             InlineKeyboardButton(colored_button("🛡️ Moderation", "blue"), callback_data="set_view_mod")],
            [InlineKeyboardButton(colored_button("🤖 Bot Protection", "blue"), callback_data="set_view_bot_protection"),
             InlineKeyboardButton(colored_button("🔗 Link Protection", "blue"), callback_data="set_view_link_spam")],
            [InlineKeyboardButton(colored_button("🔄 Forward Protect", "blue"), callback_data="set_view_forward_protection"),
             InlineKeyboardButton(colored_button("🔑 Cmd Access", "blue"), callback_data="set_view_command_access")],
            [InlineKeyboardButton(colored_button("🌐 Language Filter", "blue"), callback_data="set_view_language_filter")],
            [InlineKeyboardButton(colored_button("🕒 Recurring Msg", "blue"), callback_data="set_view_recurring"),
             InlineKeyboardButton(colored_button("🌙 Night Mode", "blue"), callback_data="set_view_nightmode")],
            [InlineKeyboardButton(colored_button("🌊 Anti-Flood", "blue"), callback_data="set_view_antiflood"),
             InlineKeyboardButton(colored_button("🚫 Banned Words", "blue"), callback_data="set_view_banned_words")],
            [InlineKeyboardButton(colored_button("🏷️ Tagger", "blue"), callback_data="set_view_tagger")],
            [InlineKeyboardButton(colored_button("🔗 Group Link", "blue"), callback_data="set_view_group_link"),
             InlineKeyboardButton(colored_button("📜 Regulations", "blue"), callback_data="set_view_regulations")],
            [InlineKeyboardButton(colored_button("👥 Members Mgmt", "blue"), callback_data="set_view_members_mgmt"),
             InlineKeyboardButton(colored_button("🎙 Voice Chat", "blue"), callback_data="set_view_vc")],
            [InlineKeyboardButton(colored_button("🗑️ Deleting Messages", "blue"), callback_data="set_view_deleting"),
             InlineKeyboardButton(colored_button("👥 Manager", "blue"), callback_data="set_view_manager")],
            [InlineKeyboardButton(colored_button("🚨 Emergency", "blue"), callback_data="set_view_emergency"),
             InlineKeyboardButton(colored_button("🛡️ Captcha", "blue"), callback_data="set_view_captcha")],
            [InlineKeyboardButton(colored_button("🔞 NSFW Filter", "blue"), callback_data="set_view_nsfw")],
            [InlineKeyboardButton(colored_button("📋 Freed Members", "blue"), callback_data="free_list_members")],
            [InlineKeyboardButton(colored_button("🎨 Layout: Large", "blue"), callback_data="set_toggle_ui_layout")],
            [InlineKeyboardButton(colored_button("❌ Close Menu", "blue"), callback_data="set_close")]
        ]
    return InlineKeyboardMarkup(keyboard)

def get_group_link_keyboard(settings):
    """Get group link settings keyboard."""
    keyboard = [
        [InlineKeyboardButton(colored_button("✍️ Set", "blue"), callback_data="set_config_group_link")],
        [InlineKeyboardButton(colored_button("🔙 Back", "blue"), callback_data="set_view_main")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_nsfw_settings_keyboard(settings):
    """Get NSFW filter settings keyboard."""
    enabled = "✅ Enabled" if settings.get("nsfw_filter_enabled", False) else "❌ Disabled"
    action = settings.get("nsfw_action", "delete").lower()
    threshold = settings.get("nsfw_threshold", 0.7)
    
    # Action labels
    actions = ["delete", "warn", "mute", "ban", "kick"]
    action_buttons = []
    for a in actions:
        label = f"🎯 {a.title()}" if action == a else a.title()
        color = "blue"
        action_buttons.append(InlineKeyboardButton(colored_button(label, color), callback_data=f"set_nsfw_action_{a}"))
        
    # Strictness: Low (0.85), Medium (0.70), High (0.50)
    strictness = "Medium"
    if threshold <= 0.55:
        strictness = "High"
    elif threshold >= 0.80:
        strictness = "Low"
        
    keyboard = [
        [
            InlineKeyboardButton(colored_button(f"Filter: {enabled}", "blue"), 
                                 callback_data="set_toggle_nsfw_filter")
        ],
        [
            InlineKeyboardButton(colored_button(f"Strictness: {strictness} ({threshold:.2f})", "blue"),
                                 callback_data="set_view_nsfw_threshold_options")
        ],
        [
            InlineKeyboardButton(colored_button("Penalty Action:", "blue"), callback_data="set_none")
        ],
        action_buttons[:3],
        action_buttons[3:],
        [
            InlineKeyboardButton(colored_button("🔙 Back", "blue"), callback_data="set_view_main")
        ]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_nsfw_threshold_keyboard(settings):
    """Get threshold options keyboard."""
    threshold = settings.get("nsfw_threshold", 0.7)
    keyboard = [
        [
            InlineKeyboardButton(colored_button(f"{'✅ ' if threshold == 0.85 else ''}Low Strictness (0.85)", "blue"), callback_data="set_nsfw_threshold_0.85"),
        ],
        [
            InlineKeyboardButton(colored_button(f"{'✅ ' if threshold == 0.70 else ''}Medium Strictness (0.70)", "blue"), callback_data="set_nsfw_threshold_0.70"),
        ],
        [
            InlineKeyboardButton(colored_button(f"{'✅ ' if threshold == 0.50 else ''}High Strictness (0.50)", "blue"), callback_data="set_nsfw_threshold_0.50")
        ],
        [
            InlineKeyboardButton(colored_button("🔙 Back", "blue"), callback_data="set_view_nsfw")
        ]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_banned_words_keyboard(settings):
    """Get banned words settings keyboard."""
    penalty = settings.get("banned_words_penalty", "off").lower()
    deletion = "✅" if settings.get("banned_words_deletion", True) else "❌"
    target = settings.get("banned_words_target", "members").title()
    warn_delete_time = settings.get("banned_words_warning_delete_time", 30)
    
    keyboard = [
        [
            InlineKeyboardButton(colored_button(f"{'❌ ' if penalty == 'off' else ''}Off", "blue"), callback_data="set_banned_penalty_off"),
            InlineKeyboardButton(colored_button(f"{'❗ ' if penalty == 'warn' else ''}Warn", "blue"), callback_data="set_banned_penalty_warn"),
            InlineKeyboardButton(colored_button(f"{'❗ ' if penalty == 'kick' else ''}Kick", "blue"), callback_data="set_banned_penalty_kick")
        ],
        [
            InlineKeyboardButton(colored_button(f"{'🔊 ' if penalty == 'mute' else ''}Mute", "blue"), callback_data="set_banned_penalty_mute"),
            InlineKeyboardButton(colored_button(f"{'🚫 ' if penalty == 'ban' else ''}Ban", "blue"), callback_data="set_banned_penalty_ban")
        ],
        [InlineKeyboardButton(colored_button(f"🎯 Target: {target}", "blue"), callback_data="set_toggle_banned_words_target")],
        [InlineKeyboardButton(colored_button(f"🗑️ Delete Messages {deletion}", "blue"), callback_data="set_toggle_banned_words_deletion")],
        [
            InlineKeyboardButton(colored_button("-10s", "blue"), callback_data="set_banned_warn_time_sub_10"),
            InlineKeyboardButton(colored_button(f"🕒 Warn Delete: {warn_delete_time}s", "blue"), callback_data="set_none"),
            InlineKeyboardButton(colored_button("+10s", "blue"), callback_data="set_banned_warn_time_add_10")
        ],
        [
            InlineKeyboardButton(colored_button("➕ Add", "blue"), callback_data="set_config_banned_words_add"),
            InlineKeyboardButton(colored_button("➖ Remove", "blue"), callback_data="set_config_banned_words_remove")
        ],
        [InlineKeyboardButton(colored_button("🔤 List", "blue"), callback_data="set_view_banned_words_list")],
        [InlineKeyboardButton(colored_button("🔙 Back", "blue"), callback_data="set_view_main")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_deleting_messages_keyboard():
    """Get Deleting Messages sub-menu keyboard."""
    keyboard = [
        [InlineKeyboardButton(colored_button("✍️ Edit Checks", "blue"), callback_data="set_view_edit_checks")],
        [InlineKeyboardButton(colored_button("🔗 Bio Link Check", "blue"), callback_data="set_view_bio_link")],
        [InlineKeyboardButton(colored_button("💭 Service Messages", "blue"), callback_data="set_view_clean")],
        [InlineKeyboardButton(colored_button("📓 Block cancellation", "blue"), callback_data="set_view_blocking")],
        [InlineKeyboardButton(colored_button("🕒 Warning Time", "blue"), callback_data="set_view_warning_time")],
        [InlineKeyboardButton(colored_button("💥 Delete all messages", "blue"), callback_data="set_view_global_purge")],
        [InlineKeyboardButton(colored_button("🧹 Delete event messages", "blue"), callback_data="set_view_purge_service")],
        [InlineKeyboardButton(colored_button("♻️ Self-Destruction", "blue"), callback_data="set_view_auto_delete")],
        [InlineKeyboardButton(colored_button("🔙 Back", "blue"), callback_data="set_view_main")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_warning_time_settings_keyboard(settings):
    """Get Warning Deletion Time settings keyboard."""
    warn_delete_time = settings.get("warning_delete_time", 300) # Default 5 mins
    
    # Calculate H, M, S
    h = warn_delete_time // 3600
    m = (warn_delete_time % 3600) // 60
    s = warn_delete_time % 60
    
    time_str = ""
    if h > 0: time_str += f"{h}h "
    if m > 0: time_str += f"{m}m "
    if s > 0 or (h == 0 and m == 0): time_str += f"{s}s"
    
    keyboard = [
        [InlineKeyboardButton(colored_button(f"🕒 Warning Deletion: {time_str}", "blue"), callback_data="set_none")],
        [
            InlineKeyboardButton(colored_button("-1min", "blue"), callback_data="set_warn_time_sub_60"),
            InlineKeyboardButton(colored_button("-10sec", "blue"), callback_data="set_warn_time_sub_10"),
            InlineKeyboardButton(colored_button("+10sec", "blue"), callback_data="set_warn_time_add_10"),
            InlineKeyboardButton(colored_button("+1min", "blue"), callback_data="set_warn_time_add_60")
        ],
        [InlineKeyboardButton(colored_button("🔙 Back", "blue"), callback_data="set_view_deleting")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_bio_link_settings_keyboard(settings):
    """Get Bio Link Check settings keyboard."""
    status = "✅" if settings.get("bio_link_check_enabled", False) else "❌"
    target = settings.get("bio_link_target", "members").title()
    penalty = settings.get("bio_link_penalty", "warn").title()
    limit = settings.get("bio_link_warn_limit", 3)
    
    keyboard = [
        [InlineKeyboardButton(colored_button(f"Bio Link Check: {status}", "blue"), callback_data="set_toggle_bio_link_check_enabled")],
        [InlineKeyboardButton(colored_button(f"🎯 Target: {target}", "blue"), callback_data="set_toggle_bio_link_target")],
        [InlineKeyboardButton(colored_button(f"⚖️ Penalty: {penalty}", "blue"), callback_data="set_toggle_bio_link_penalty")],
    ]
    
    if penalty.lower() != "off":
        keyboard.append([
            InlineKeyboardButton(colored_button("➖", "blue"), callback_data="set_bio_warn_sub"),
            InlineKeyboardButton(colored_button(f"⚠ Limit: {limit}", "blue"), callback_data="set_none"),
            InlineKeyboardButton(colored_button("➕", "blue"), callback_data="set_bio_warn_add")
        ])
        
    keyboard.append([InlineKeyboardButton(colored_button("🔙 Back", "blue"), callback_data="set_view_deleting")])
    return InlineKeyboardMarkup(keyboard)

def get_edit_checks_keyboard(settings):
    """Get Edit Checks settings keyboard."""
    status = "✅" if settings.get("edit_checks_enabled", False) else "❌"
    target = settings.get("edit_checks_target", "members").title()
    penalty = settings.get("edit_checks_penalty", "off").title()
    limit = settings.get("edit_checks_warn_limit", 3)
    
    keyboard = [
        [InlineKeyboardButton(colored_button(f"Edit Protection: {status}", "blue"), callback_data="set_toggle_edit_checks_enabled")],
        [InlineKeyboardButton(colored_button(f"🎯 Target: {target}", "blue"), callback_data="set_toggle_edit_checks_target")],
        [InlineKeyboardButton(colored_button(f"⚖️ Penalty: {penalty}", "blue"), callback_data="set_toggle_edit_checks_penalty")],
    ]
    
    if penalty.lower() != "off":
        keyboard.append([
            InlineKeyboardButton(colored_button("➖", "blue"), callback_data="set_edit_warn_sub"),
            InlineKeyboardButton(colored_button(f"⚠ Limit: {limit}", "blue"), callback_data="set_none"),
            InlineKeyboardButton(colored_button("➕", "blue"), callback_data="set_edit_warn_add")
        ])
        
    keyboard.append([InlineKeyboardButton(colored_button("🔙 Back", "blue"), callback_data="set_view_deleting")])
    return InlineKeyboardMarkup(keyboard)

def get_members_mgmt_keyboard():
    """Get members management keyboard."""
    keyboard = [
        [
            InlineKeyboardButton(colored_button("🔊 Unmute all", "blue"), callback_data="mgmt_unmute_all"),
            InlineKeyboardButton(colored_button("🚫 Unban all", "blue"), callback_data="mgmt_unban_all")
        ],
        [InlineKeyboardButton(colored_button("❗ Kick muted/restricted users", "blue"), callback_data="mgmt_kick_restricted")],
        [InlineKeyboardButton(colored_button("💀 Kick deleted accounts", "blue"), callback_data="mgmt_kick_deleted")],
        [InlineKeyboardButton(colored_button("🔙 Back", "blue"), callback_data="set_view_main")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_regulations_keyboard():
    """Get regulations main keyboard."""
    keyboard = [
        [InlineKeyboardButton(colored_button("✍️ Customize message", "blue"), callback_data="set_view_regulations_custom")],
        [InlineKeyboardButton(colored_button("🕹️ Commands Permissions", "blue"), callback_data="set_view_command_permissions")],
        [InlineKeyboardButton(colored_button("🔙 Back", "blue"), callback_data="set_view_main")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_regulations_custom_keyboard(settings):
    """Get regulations customization keyboard."""
    text_icon = "✅" if settings.get("rules_text") else "❌"
    media_icon = "✅" if settings.get("rules_media") else "❌"
    btns_icon = "✅" if settings.get("rules_buttons") else "❌"
    
    keyboard = [
        [InlineKeyboardButton(colored_button("📄 Text", "blue"), callback_data="set_config_rules_text"),
         InlineKeyboardButton(colored_button("👀 See", "blue"), callback_data="set_preview_rules_text")],
        [InlineKeyboardButton(colored_button("📸 Media", "blue"), callback_data="set_config_rules_media"),
         InlineKeyboardButton(colored_button("👀 See", "blue"), callback_data="set_preview_rules_media")],
        [InlineKeyboardButton(colored_button("🔡 Url Buttons", "blue"), callback_data="set_config_rules_buttons"),
         InlineKeyboardButton(colored_button("👀 See", "blue"), callback_data="set_preview_rules_buttons")],
        [InlineKeyboardButton(colored_button("👀 Full preview", "blue"), callback_data="set_preview_rules_full")],
        [InlineKeyboardButton(colored_button("🔙 Back", "blue"), callback_data="set_view_regulations")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_welcome_settings_keyboard(settings):
    welcome_status = "✅" if settings.get("welcome_enabled", True) else "❌"
    rejoin_status = "✅" if settings.get("welcome_rejoin_enabled", True) else "❌"
    clean_status = "✅" if settings.get("welcome_clean_enabled", True) else "❌"
    delete_enabled_status = "✅" if settings.get("welcome_delete_enabled", True) else "❌"
    media_status = "✅" if settings.get("welcome_media_enabled", True) else "❌"
    button_status = "✅" if settings.get("welcome_button_enabled", True) else "❌"
    delete_time = settings.get("welcome_delete_time", 60)
    welcome_buttons = settings.get("welcome_buttons", [])
    
    keyboard = [
        [InlineKeyboardButton(colored_button(f"Welcome: {welcome_status}", "blue"), callback_data="set_toggle_welcome_enabled")],
        [InlineKeyboardButton(colored_button(f"Welcome on Re-join: {rejoin_status}", "blue"), callback_data="set_toggle_welcome_rejoin_enabled")],
        [InlineKeyboardButton(colored_button(f"Auto-Delete Previous: {clean_status}", "blue"), callback_data="set_toggle_welcome_clean_enabled")],
        [InlineKeyboardButton(colored_button(f"Timed Deletion: {delete_enabled_status}", "blue"), callback_data="set_toggle_welcome_delete_enabled")],
        [
            InlineKeyboardButton(colored_button(f"Media: {media_status}", "blue"), callback_data="set_toggle_welcome_media_enabled"),
            InlineKeyboardButton(colored_button("🖼️ Set Media", "blue"), callback_data="set_config_welcome_media")
        ],
        [
            InlineKeyboardButton(colored_button(f"Button: {button_status}", "blue"), callback_data="set_toggle_welcome_button_enabled"),
            InlineKeyboardButton(colored_button("➕ Add Button", "blue"), callback_data="set_config_welcome_buttons_add"),
            InlineKeyboardButton(colored_button("🗑️ Clear", "blue"), callback_data="set_config_welcome_buttons_clear")
        ]
    ]
    
    # List added buttons if any
    if welcome_buttons:
        for idx, btn in enumerate(welcome_buttons):
            btn_text = btn.get("text", f"Btn {idx+1}")
            keyboard.append([InlineKeyboardButton(colored_button(f"Button {idx+1}: {btn_text}", "blue"), callback_data=f"set_config_welcome_btn_manage_{idx}")])
    
    keyboard.extend([
        [InlineKeyboardButton(colored_button("📝 Set Welcome Text", "blue"), callback_data="set_config_welcome_text")],
        [
            InlineKeyboardButton(colored_button("-10s", "blue"), callback_data="set_welcome_time_sub_10"),
            InlineKeyboardButton(colored_button(f"🗑️ Delete: {delete_time}s", "blue"), callback_data="set_none"),
            InlineKeyboardButton(colored_button("+10s", "blue"), callback_data="set_welcome_time_add_10")
        ],
        [InlineKeyboardButton(colored_button("🔙 Back", "blue"), callback_data="set_view_main")]
    ])
    return InlineKeyboardMarkup(keyboard)

def get_captcha_settings_keyboard(settings):
    is_enabled = settings.get("captcha_enabled", False)
    status_str = "✅ Enabled" if is_enabled else "❌ Disabled"
    vc_enabled = settings.get("captcha_vc_enabled", False)
    vc_status_str = "✅ Enabled" if vc_enabled else "❌ Disabled"
    test_type = settings.get("captcha_type", "button").capitalize()
    action = settings.get("captcha_action", "kick").capitalize()
    timeout = settings.get("captcha_timeout", 120)

    keyboard = [
        [InlineKeyboardButton(colored_button(f"Group Captcha: {status_str}", "blue"), callback_data="set_toggle_captcha_enabled")],
        [InlineKeyboardButton(colored_button(f"VC Join Captcha: {vc_status_str}", "blue"), callback_data="set_toggle_captcha_vc_enabled")],
        [InlineKeyboardButton(colored_button(f"📝 Test Type: {test_type}", "blue"), callback_data="set_view_captcha_type")],
        [InlineKeyboardButton(colored_button(f"⚡ Penalty: {action}", "blue"), callback_data="set_captcha_toggle_action")],
        [
            InlineKeyboardButton(colored_button("-30s", "blue"), callback_data="set_captcha_time_sub"),
            InlineKeyboardButton(colored_button(f"⏳ Timeout: {timeout}s", "blue"), callback_data="set_none"),
            InlineKeyboardButton(colored_button("+30s", "blue"), callback_data="set_captcha_time_add")
        ],
        [InlineKeyboardButton(colored_button("🔙 Back", "blue"), callback_data="set_view_main")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_captcha_test_type_keyboard(settings):
    current = settings.get("captcha_type", "button").lower()
    
    simple_lbl = "• Simple" + (" ✅" if current == "simple" else "")
    button_lbl = "• Button" + (" ✅" if current == "button" else "")
    digits_lbl = "• Digits" + (" ✅" if current == "digits" else "")
    image_lbl = "• Image" + (" ✅" if current == "image" else "")
    
    keyboard = [
        [
            InlineKeyboardButton(colored_button(simple_lbl, "blue"), callback_data="set_captcha_type_simple"),
            InlineKeyboardButton(colored_button(button_lbl, "blue"), callback_data="set_captcha_type_button")
        ],
        [
            InlineKeyboardButton(colored_button(digits_lbl, "blue"), callback_data="set_captcha_type_digits"),
            InlineKeyboardButton(colored_button(image_lbl, "blue"), callback_data="set_captcha_type_image")
        ],
        [InlineKeyboardButton(colored_button("🔙 Back", "blue"), callback_data="set_view_captcha")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_welcome_button_manage_keyboard(idx):
    """Keyboard for managing a specific welcome button."""
    keyboard = [
        [
            InlineKeyboardButton(colored_button("🗑️ Remove", "blue"), callback_data=f"set_config_welcome_btn_remove_{idx}")
        ],
        [
            InlineKeyboardButton(colored_button("✍️ Modify Text", "blue"), callback_data=f"set_config_welcome_btn_modtext_{idx}"),
            InlineKeyboardButton(colored_button("🔗 Modify Link", "blue"), callback_data=f"set_config_welcome_btn_modlink_{idx}")
        ],
        [InlineKeyboardButton(colored_button("🔙 Back", "blue"), callback_data="set_view_welcome")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_goodbye_settings_keyboard(settings):
    goodbye_status = "✅" if settings.get("goodbye_enabled", True) else "❌"
    dm_status = "✅" if settings.get("goodbye_dm_enabled", True) else "❌"

    keyboard = [
        [InlineKeyboardButton(colored_button(f"Goodbye: {goodbye_status}", "blue"), callback_data="set_toggle_goodbye_enabled")],
        [InlineKeyboardButton(colored_button(f"Send via DM: {dm_status}", "blue"), callback_data="set_toggle_goodbye_dm_enabled")],
        [InlineKeyboardButton(colored_button("📝 Set Goodbye Text", "blue"), callback_data="set_config_goodbye_text")],
        [InlineKeyboardButton(colored_button("🔙 Back", "blue"), callback_data="set_view_main")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_join_request_settings_keyboard(settings):
    is_enabled = settings.get("join_request_enabled", True)
    current_mode = settings.get("join_request_mode", "manual").lower()
    mode_accept = is_enabled and current_mode == "accept"
    mode_decline = is_enabled and current_mode == "decline"
    mode_manual = is_enabled and current_mode == "manual"
    mode_off = not is_enabled or current_mode == "off"
    
    status_btn_text = f"Join Requests: {'✅ Enabled' if is_enabled else '❌ Disabled'}"
    accept_label = "✅ Auto-Accept" + (" ✓" if mode_accept else "")
    decline_label = "❌ Auto-Decline" + (" ✓" if mode_decline else "")
    manual_label = "⚙️ Manual Approval" + (" ✓" if mode_manual else "")
    off_label = "🚫 Turn Off" + (" ✓" if mode_off else "")
    
    keyboard = [
        [
            InlineKeyboardButton(colored_button(status_btn_text, "blue"), callback_data="set_toggle_join_request_enabled")
        ],
        [
            InlineKeyboardButton(colored_button(accept_label, "blue"), callback_data="set_joinreq_mode_accept"),
            InlineKeyboardButton(colored_button(decline_label, "blue"), callback_data="set_joinreq_mode_decline"),
        ],
        [
            InlineKeyboardButton(colored_button(manual_label, "blue"), callback_data="set_joinreq_mode_manual"),
            InlineKeyboardButton(colored_button(off_label, "blue"), callback_data="set_joinreq_mode_off")
        ],
        [InlineKeyboardButton(colored_button("🔙 Back", "blue"), callback_data="set_view_main")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_clean_settings_keyboard(settings):
    master_status = "✅" if settings.get("clean_service_enabled", True) else "❌"
    join_status = "✅" if settings.get("clean_join", True) else "❌"
    left_status = "✅" if settings.get("clean_left", True) else "❌"
    started_status = "✅" if settings.get("clean_video_chat_started", True) else "❌"
    ended_status = "✅" if settings.get("clean_video_chat_ended", True) else "❌"
    invited_status = "✅" if settings.get("clean_video_chat_invited", True) else "❌"
    scheduled_status = "✅" if settings.get("clean_video_chat_scheduled", True) else "❌"
    
    keyboard = [
        [InlineKeyboardButton(colored_button(f"Master Clean: {master_status}", "blue"), callback_data="set_toggle_clean_service_enabled")],
        [InlineKeyboardButton(colored_button(f"Clean Join: {join_status}", "blue"), callback_data="set_toggle_clean_join")],
        [InlineKeyboardButton(colored_button(f"Clean Left: {left_status}", "blue"), callback_data="set_toggle_clean_left")],
        [InlineKeyboardButton(colored_button(f"Voice Chat Started: {started_status}", "blue"), callback_data="set_toggle_clean_video_chat_started")],
        [InlineKeyboardButton(colored_button(f"Voice Chat Ended: {ended_status}", "blue"), callback_data="set_toggle_clean_video_chat_ended")],
        [InlineKeyboardButton(colored_button(f"Voice Chat Invited: {invited_status}", "blue"), callback_data="set_toggle_clean_video_chat_invited")],
        [InlineKeyboardButton(colored_button(f"Voice Chat Scheduled: {scheduled_status}", "blue"), callback_data="set_toggle_clean_video_chat_scheduled")],
        [InlineKeyboardButton(colored_button("🔙 Back", "blue"), callback_data="set_view_main")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_auto_delete_settings_keyboard(settings):
    status = "✅" if settings.get("auto_delete_enabled", False) else "❌"
    text_status = "✅" if settings.get("auto_delete_text", True) else "❌"
    stickers_status = "✅" if settings.get("auto_delete_stickers", True) else "❌"
    media_status = "✅" if settings.get("auto_delete_media", True) else "❌"
    total_seconds = settings.get("auto_delete_time", 60)
    
    # Calculate H, M, S
    h = total_seconds // 3600
    m = (total_seconds % 3600) // 60
    s = total_seconds % 60
    
    keyboard = [
        [InlineKeyboardButton(colored_button(f"Self-Destruction: {status}", "blue"), callback_data="set_toggle_auto_delete_enabled")],
        [InlineKeyboardButton(colored_button(f"Text Messages: {text_status}", "blue"), callback_data="set_toggle_auto_delete_text")],
        [InlineKeyboardButton(colored_button(f"Stickers: {stickers_status}", "blue"), callback_data="set_toggle_auto_delete_stickers")],
        [InlineKeyboardButton(colored_button(f"Media: {media_status}", "blue"), callback_data="set_toggle_auto_delete_media")],
        [
            InlineKeyboardButton(colored_button("-H", "blue"), callback_data="set_time_sub_3600"),
            InlineKeyboardButton(colored_button(f"{h} Hours", "blue"), callback_data="set_none"),
            InlineKeyboardButton(colored_button("+H", "blue"), callback_data="set_time_add_3600")
        ],
        [
            InlineKeyboardButton(colored_button("-M", "blue"), callback_data="set_time_sub_60"),
            InlineKeyboardButton(colored_button(f"{m} Minutes", "blue"), callback_data="set_none"),
            InlineKeyboardButton(colored_button("+M", "blue"), callback_data="set_time_add_60")
        ],
        [
            InlineKeyboardButton(colored_button("-S", "blue"), callback_data="set_time_sub_1"),
            InlineKeyboardButton(colored_button(f"{s} Seconds", "blue"), callback_data="set_none"),
            InlineKeyboardButton(colored_button("+S", "blue"), callback_data="set_time_add_1")
        ],
        [InlineKeyboardButton(colored_button("🔙 Back", "blue"), callback_data="set_view_main")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_msg_length_settings_keyboard(settings):
    current_limit = settings.get("msg_length_limit", 0)
    status = f"{current_limit} chars" if current_limit > 0 else "Disabled"
    
    keyboard = [
        [
            InlineKeyboardButton(colored_button("-", "blue"), callback_data="set_msg_length_minus"),
            InlineKeyboardButton(colored_button(f"Limit: {status}", "blue"), callback_data="set_none"),
            InlineKeyboardButton(colored_button("+", "blue"), callback_data="set_msg_length_plus")
        ],
        [
            InlineKeyboardButton(colored_button("100", "blue"), callback_data="set_msg_length_100"),
            InlineKeyboardButton(colored_button("200", "blue"), callback_data="set_msg_length_200"),
            InlineKeyboardButton(colored_button("300", "blue"), callback_data="set_msg_length_300")
        ],
        [
            InlineKeyboardButton(colored_button("Custom", "blue"), callback_data="set_config_msg_length_limit"),
            InlineKeyboardButton(colored_button("Disable", "blue"), callback_data="set_msg_length_0")
        ],
        [InlineKeyboardButton(colored_button("🔙 Back", "blue"), callback_data="set_view_main")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_mod_settings_keyboard(settings):
    limit = settings.get("warn_limit", 3)
    penalty = settings.get("warn_penalty", "ban").title()
    
    keyboard = [
        [
            InlineKeyboardButton(colored_button("-", "blue"), callback_data="set_warn_limit_sub"),
            InlineKeyboardButton(colored_button(f"Warn Limit: {limit}", "blue"), callback_data="set_none"),
            InlineKeyboardButton(colored_button("+", "blue"), callback_data="set_warn_limit_add")
        ],
        [InlineKeyboardButton(colored_button(f"Warn Penalty: {penalty}", "blue"), callback_data="set_toggle_warn_penalty")],
        [InlineKeyboardButton(colored_button("🔙 Back", "blue"), callback_data="set_view_main")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_command_deletion_keyboard(settings):
    status = "✅" if settings.get("command_deletion", False) else "❌"
    keyboard = [
        [InlineKeyboardButton(colored_button(f"Delete Admin Commands: {status}", "blue"), callback_data="set_toggle_command_deletion")],
        [InlineKeyboardButton(colored_button("🔙 Back", "blue"), callback_data="set_view_main")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_command_access_keyboard(settings):
    access = settings.get("command_access", "all").title()
    keyboard = [
        [InlineKeyboardButton(colored_button(f"Command Access: {access}", "blue"), callback_data="set_toggle_command_access")],
        [InlineKeyboardButton(colored_button("ℹ️ Global setting for basic commands", "blue"), callback_data="set_none")],
        [InlineKeyboardButton(colored_button("🔙 Back", "blue"), callback_data="set_view_main")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_command_permissions_keyboard(settings):
    """Get detailed command permissions keyboard."""
    # Define command categories with their access levels
    commands = [
        ("📖 Basic Commands", [
            ("Start", "cmd_access_start"),
            ("Help", "cmd_access_help"),
            ("ID", "cmd_access_id"),
            ("Info", "cmd_access_info"),
            ("Report", "cmd_access_report"),
        ]),
        ("🛡️ Moderation Commands", [
            ("Ban", "cmd_access_ban"),
            ("Unban", "cmd_access_unban"),
            ("Mute", "cmd_access_mute"),
            ("Unmute", "cmd_access_unmute"),
            ("Warn", "cmd_access_warn"),
            ("Unwarn", "cmd_access_unwarn"),
            ("Kick", "cmd_access_kick"),
        ]),
        ("👥 Management Commands", [
            ("Purge", "cmd_access_purge"),
            ("Pin", "cmd_access_pin"),
            ("Unpin", "cmd_access_unpin"),
            ("Promote", "cmd_access_promote"),
            ("Demote", "cmd_access_demote"),
            ("Send", "cmd_access_send"),
            ("PSend", "cmd_access_psend"),
        ]),
        ("📋 Info & Utilities", [
            ("Staff", "cmd_access_staff"),
            ("Bots", "cmd_access_bots"),
            ("Zombies", "cmd_access_zombies"),
            ("History", "cmd_access_history"),
        ]),
        ("⚠️ Mass Actions", [
            ("Mass Actions", "cmd_access_mass_actions"),
        ]),
        ("⚙️ Settings", [
            ("Settings/Config", "cmd_access_settings"),
        ]),
    ]
    
    keyboard = []
    for category, cmds in commands:
        keyboard.append([InlineKeyboardButton(colored_button(f"── {category} ──", "blue"), callback_data="set_none")])
        for cmd_name, cmd_key in cmds:
            current_access = settings.get(cmd_key, "admin").title()
            keyboard.append([InlineKeyboardButton(colored_button(f"/{cmd_name.lower()}: {current_access}", "blue"), callback_data=f"set_cmd_perm_{cmd_key}")])
    
    keyboard.append([InlineKeyboardButton(colored_button("ℹ️ Tap a command to toggle access level", "blue"), callback_data="set_none")])
    keyboard.append([InlineKeyboardButton(colored_button("🔙 Back", "blue"), callback_data="set_view_main")])
    
    return InlineKeyboardMarkup(keyboard)

def get_pinned_message_settings_keyboard(settings):
    pinned_status = "✅" if settings.get("clean_pinned_message", True) else "❌"
    keyboard = [
        [InlineKeyboardButton(colored_button(f"Clean Pinned Message: {pinned_status}", "blue"), callback_data="set_toggle_clean_pinned_message")],
        [InlineKeyboardButton(colored_button("🔙 Back", "blue"), callback_data="set_view_main")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_bot_protection_settings_keyboard(settings):
    status = "✅" if settings.get("bot_protection_enabled", False) else "❌"
    apply_on = settings.get("bot_protection_apply_on", "members").capitalize()
    keyboard = [
        [InlineKeyboardButton(colored_button(f"Bot Protection: {status}", "blue"), callback_data="set_toggle_bot_protection_enabled")],
        [InlineKeyboardButton(colored_button(f"Apply On: {apply_on}", "blue"), callback_data="set_toggle_bot_protection_apply_on")],
        [InlineKeyboardButton(colored_button("ℹ️ Admins need Change Info + Ban Users to add bots", "blue"), callback_data="set_none")],
        [InlineKeyboardButton(colored_button("🔙 Back", "blue"), callback_data="set_view_main")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_link_spam_settings_keyboard(settings):
    status = "✅" if settings.get("link_spam_protection_enabled", False) else "❌"
    apply_on = settings.get("link_spam_apply_on", "members").capitalize()
    keyboard = [
        [InlineKeyboardButton(colored_button(f"Link Protection: {status}", "blue"), callback_data="set_toggle_link_spam_protection_enabled")],
        [InlineKeyboardButton(colored_button(f"Apply On: {apply_on}", "blue"), callback_data="set_toggle_link_spam_apply_on")],
        [InlineKeyboardButton(colored_button("🔙 Back", "blue"), callback_data="set_view_main")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_forward_protection_settings_keyboard(settings):
    status = "✅" if settings.get("forward_protection_enabled", False) else "❌"
    apply_on = settings.get("forward_protection_apply_on", "members").capitalize()
    keyboard = [
        [InlineKeyboardButton(colored_button(f"Forward Protection: {status}", "blue"), callback_data="set_toggle_forward_protection_enabled")],
        [InlineKeyboardButton(colored_button(f"Apply On: {apply_on}", "blue"), callback_data="set_toggle_forward_protection_apply_on")],
        [InlineKeyboardButton(colored_button("🔙 Back", "blue"), callback_data="set_view_main")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_language_filter_settings_keyboard(settings):
    """Get language filter settings keyboard."""
    status = "✅" if settings.get("language_filter_enabled", False) else "❌"
    allowed = settings.get("allowed_languages", ["en", "hi", "hinglish"])
    
    en_status = "✅" if "en" in allowed else "❌"
    hi_status = "✅" if "hi" in allowed else "❌"
    hinglish_status = "✅" if "hinglish" in allowed else "❌"
    
    # Emoji blocking settings
    emoji_block = "✅" if settings.get("emoji_block_enabled", False) else "❌"
    block_emoji_only = "✅" if settings.get("block_emoji_only", True) else "❌"
    block_punct_only = "✅" if settings.get("block_punctuation_only", True) else "❌"
    
    keyboard = [
        [InlineKeyboardButton(colored_button(f"Language Filter: {status}", "blue"), callback_data="set_toggle_language_filter_enabled")],
        [InlineKeyboardButton(colored_button(f"🇬🇧 English: {en_status}", "blue"), callback_data="set_toggle_lang_en")],
        [InlineKeyboardButton(colored_button(f"🇮🇳 Hindi: {hi_status}", "blue"), callback_data="set_toggle_lang_hi")],
        [InlineKeyboardButton(colored_button(f"💬 Hinglish: {hinglish_status}", "blue"), callback_data="set_toggle_lang_hinglish")],
        [InlineKeyboardButton(colored_button("── Emoji & Symbols ──", "blue"), callback_data="set_none")],
        [InlineKeyboardButton(colored_button(f"Block Emojis: {emoji_block}", "blue"), callback_data="set_toggle_emoji_block_enabled")],
        [InlineKeyboardButton(colored_button(f"  Emoji Only: {block_emoji_only}", "blue"), callback_data="set_toggle_block_emoji_only")],
        [InlineKeyboardButton(colored_button(f"  Punctuation Only: {block_punct_only}", "blue"), callback_data="set_toggle_block_punctuation_only")],
        [InlineKeyboardButton(colored_button("ℹ️ Deletes messages in other languages", "blue"), callback_data="set_none")],
        [InlineKeyboardButton(colored_button("🔙 Back", "blue"), callback_data="set_view_main")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_blocking_settings_keyboard(settings):
    """Get blocking settings keyboard."""
    # Merge with defaults if needed
    for key, value in DEFAULT_BLOCKING_SETTINGS.items():
        if key not in settings:
            settings[key] = value
    
    master_status = "✅" if settings.get("blocking_enabled", True) else "❌"
    stickers_status = "✅" if settings.get("block_stickers", False) else "❌"
    premium_sticker_status = "✅" if settings.get("block_premium_sticker", False) else "❌"
    link_status = "✅" if settings.get("block_link", False) else "❌"
    embed_link_status = "✅" if settings.get("block_embed_link", False) else "❌"
    media_status = "✅" if settings.get("block_media", False) else "❌"
    documents_status = "✅" if settings.get("block_documents", False) else "❌"
    forward_status = "✅" if settings.get("block_forward", False) else "❌"
    channel_post_status = "✅" if settings.get("block_channel_post", False) else "❌"
    command_status = "✅" if settings.get("block_command", False) else "❌"
    contact_status = "✅" if settings.get("block_contact", False) else "❌"
    location_status = "✅" if settings.get("block_location", False) else "❌"
    text_status = "✅" if settings.get("block_text", False) else "❌"
    voice_status = "✅" if settings.get("block_voice", False) else "❌"
    audio_status = "✅" if settings.get("block_audio", False) else "❌"
    video_note_status = "✅" if settings.get("block_video_note", False) else "❌"
    poll_status = "✅" if settings.get("block_poll", False) else "❌"
    dice_status = "✅" if settings.get("block_dice", False) else "❌"
    game_status = "✅" if settings.get("block_game", False) else "❌"
    reaction_status = "✅" if settings.get("block_reactions", False) else "❌"
    
    keyboard = [
        [InlineKeyboardButton(colored_button(f"Master Blocking: {master_status}", "blue"), callback_data="set_toggle_blocking_enabled")],
        [InlineKeyboardButton(colored_button("🔔 Notification Settings", "blue"), callback_data="set_view_blocking_notifications")],
        [InlineKeyboardButton(colored_button("── Stickers ──", "blue"), callback_data="set_none")],
        [InlineKeyboardButton(colored_button(f"Block Stickers: {stickers_status}", "blue"), callback_data="set_toggle_block_stickers")],
        [InlineKeyboardButton(colored_button(f"Block Premium Stickers: {premium_sticker_status}", "blue"), callback_data="set_toggle_block_premium_sticker")],
        [InlineKeyboardButton(colored_button("── Links ──", "blue"), callback_data="set_none")],
        [InlineKeyboardButton(colored_button(f"Block Links: {link_status}", "blue"), callback_data="set_toggle_block_link")],
        [InlineKeyboardButton(colored_button(f"Block Embed Links: {embed_link_status}", "blue"), callback_data="set_toggle_block_embed_link")],
        [InlineKeyboardButton(colored_button("── Media & Files ──", "blue"), callback_data="set_none")],
        [InlineKeyboardButton(colored_button(f"Block Media: {media_status}", "blue"), callback_data="set_toggle_block_media")],
        [InlineKeyboardButton(colored_button(f"Block Documents: {documents_status}", "blue"), callback_data="set_toggle_block_documents")],
        [InlineKeyboardButton(colored_button(f"Block Audio/Music: {audio_status}", "blue"), callback_data="set_toggle_block_audio")],
        [InlineKeyboardButton(colored_button("── Messages ──", "blue"), callback_data="set_none")],
        [InlineKeyboardButton(colored_button(f"Block Forward: {forward_status}", "blue"), callback_data="set_toggle_block_forward")],
        [InlineKeyboardButton(colored_button(f"Block Channel Posts: {channel_post_status}", "blue"), callback_data="set_toggle_block_channel_post")],
        [InlineKeyboardButton(colored_button(f"Block Commands: {command_status}", "blue"), callback_data="set_toggle_block_command")],
        [InlineKeyboardButton(colored_button("── Other ──", "blue"), callback_data="set_none")],
        [InlineKeyboardButton(colored_button(f"Block Contact: {contact_status}", "blue"), callback_data="set_toggle_block_contact")],
        [InlineKeyboardButton(colored_button(f"Block Text Messages: {text_status}", "blue"), callback_data="set_toggle_block_text")],
        [InlineKeyboardButton(colored_button(f"Block Location: {location_status}", "blue"), callback_data="set_toggle_block_location")],
        [InlineKeyboardButton(colored_button(f"Block Voice: {voice_status}", "blue"), callback_data="set_toggle_block_voice")],
        [InlineKeyboardButton(colored_button(f"Block Video Note: {video_note_status}", "blue"), callback_data="set_toggle_block_video_note")],
        [InlineKeyboardButton(colored_button(f"Block Poll: {poll_status}", "blue"), callback_data="set_toggle_block_poll")],
        [InlineKeyboardButton(colored_button(f"Block Dice: {dice_status}", "blue"), callback_data="set_toggle_block_dice")],
        [InlineKeyboardButton(colored_button(f"Block Game: {game_status}", "blue"), callback_data="set_toggle_block_game")],
        [InlineKeyboardButton(colored_button(f"Block Reactions: {reaction_status}", "blue"), callback_data="set_toggle_block_reactions")],
        [InlineKeyboardButton(colored_button("ℹ️ Toggle to block content instantly", "blue"), callback_data="set_none")],
        [InlineKeyboardButton(colored_button("🔙 Back", "blue"), callback_data="set_view_main")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_antiflood_settings_keyboard(settings):
    """Get Anti-Flood settings keyboard."""
    status = "✅" if settings.get("antiflood_enabled", False) else "❌"
    limit = settings.get("antiflood_limit", 5)
    window = settings.get("antiflood_window", 3)
    penalty = settings.get("antiflood_penalty", "mute").title()
    apply_on = settings.get("antiflood_apply_on", "members").title()
    
    keyboard = [
        [InlineKeyboardButton(colored_button(f"Anti-Flood: {status}", "blue"), callback_data="set_toggle_antiflood_enabled")],
        [
            InlineKeyboardButton(colored_button("-", "blue"), callback_data="set_antiflood_limit_sub"),
            InlineKeyboardButton(colored_button(f"Limit: {limit} msgs", "blue"), callback_data="set_none"),
            InlineKeyboardButton(colored_button("+", "blue"), callback_data="set_antiflood_limit_add")
        ],
        [
            InlineKeyboardButton(colored_button("-", "blue"), callback_data="set_antiflood_window_sub"),
            InlineKeyboardButton(colored_button(f"Window: {window}s", "blue"), callback_data="set_none"),
            InlineKeyboardButton(colored_button("+", "blue"), callback_data="set_antiflood_window_add")
        ],
        [InlineKeyboardButton(colored_button(f"Penalty: {penalty}", "blue"), callback_data="set_toggle_antiflood_penalty")],
        [InlineKeyboardButton(colored_button(f"Apply On: {apply_on}", "blue"), callback_data="set_toggle_antiflood_apply_on")],
        [InlineKeyboardButton(colored_button("ℹ️ Protects against fast messaging", "blue"), callback_data="set_none")],
        [InlineKeyboardButton(colored_button("🔙 Back", "blue"), callback_data="set_view_main")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_nightmode_settings_keyboard(settings):
    """Get Night Mode settings keyboard based on user provided UI."""
    status = "✅ On" if settings.get("nightmode_enabled", False) else "❌ Off"
    media_status = "✅" if settings.get("nightmode_restrict_media", False) else "❌"
    sticker_status = "✅" if settings.get("nightmode_restrict_stickers", False) else "❌"
    link_status = "✅" if settings.get("nightmode_restrict_links", False) else "❌"
    silence_status = "✅" if settings.get("nightmode_global_silence", False) else "❌"
    advise_status = "✅" if settings.get("nightmode_advise_enabled", True) else "❌"
    apply_on = settings.get("nightmode_apply_on", "members").capitalize()
    
    keyboard = [
        [InlineKeyboardButton(colored_button(f"{status}", "blue"), callback_data="set_toggle_nightmode_enabled")],
        [
            InlineKeyboardButton(colored_button(f"📸 Medias {media_status}", "blue"), callback_data="set_toggle_nightmode_restrict_media"),
            InlineKeyboardButton(colored_button(f"🗑 Stickers {sticker_status}", "blue"), callback_data="set_toggle_nightmode_restrict_stickers")
        ],
        [
            InlineKeyboardButton(colored_button(f"🔗 Links {link_status}", "blue"), callback_data="set_toggle_nightmode_restrict_links"),
            InlineKeyboardButton(colored_button(f"🤫 Silence {silence_status}", "blue"), callback_data="set_toggle_nightmode_global_silence")
        ],
        [InlineKeyboardButton(colored_button(f"Apply On: {apply_on}", "blue"), callback_data="set_toggle_nightmode_apply_on")],
        [InlineKeyboardButton(colored_button("🕒 Set time slot", "blue"), callback_data="set_view_nightmode_start_grid")],
        [InlineKeyboardButton(colored_button(f"📣 Start&End advises {advise_status}", "blue"), callback_data="set_toggle_nightmode_advise_enabled")],
        [
            InlineKeyboardButton(colored_button("⬅️ Back", "blue"), callback_data="set_view_main"),
            InlineKeyboardButton(colored_button("🌍 Time Zone", "blue"), callback_data="set_config_nightmode_timezone_private")
        ]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_nightmode_hour_grid(user_id, is_start=True):
    """Generate 24-hour grid for selecting start/end times."""
    keyboard = []
    type_str = "starting" if is_start else "ending"
    callback_prefix = "set_nm_start_" if is_start else "set_nm_end_"
    
    for row_start in range(0, 24, 5):
        row = []
        for hour in range(row_start, min(row_start + 5, 24)):
            row.append(InlineKeyboardButton(colored_button(str(hour), "blue"), callback_data=f"{callback_prefix}{hour}"))
        keyboard.append(row)
        
    keyboard.append([InlineKeyboardButton(colored_button("⬅️ Back", "blue"), callback_data="set_view_nightmode")])
    return InlineKeyboardMarkup(keyboard)

def get_vc_settings_keyboard(settings):
    """Get Voice Chat settings keyboard."""
    vc_user_join_status = "✅" if settings.get("vc_user_join_enabled", True) else "❌"
    vc_user_leave_status = "✅" if settings.get("vc_user_leave_enabled", True) else "❌"
    vc_invite_status = "✅" if settings.get("vc_invite_notification_enabled", True) else "❌"
    vc_safety_status = "✅" if settings.get("vc_safety_enabled", False) else "❌"
    vc_panic_status = "✅" if settings.get("vc_panic_mode_enabled", False) else "❌"
    
    keyboard = [
        [InlineKeyboardButton(colored_button(f"User Join Notification: {vc_user_join_status}", "blue"), callback_data="set_toggle_vc_user_join_enabled")],
        [InlineKeyboardButton(colored_button(f"User Leave Notification: {vc_user_leave_status}", "blue"), callback_data="set_toggle_vc_user_leave_enabled")],
        [InlineKeyboardButton(colored_button(f"Invite Notification: {vc_invite_status}", "blue"), callback_data="set_toggle_vc_invite_notification_enabled")],
        [InlineKeyboardButton(colored_button(f"VC Safety (Anti-Ghost): {vc_safety_status}", "blue"), callback_data="set_toggle_vc_safety_enabled")],
        [InlineKeyboardButton(colored_button(f"VC Panic Mode: {vc_panic_status}", "blue"), callback_data="set_toggle_vc_panic_mode_enabled")],
        [InlineKeyboardButton(colored_button("ℹ️ Voice chat related notifications", "blue"), callback_data="set_none")],
        [InlineKeyboardButton(colored_button("🔙 Back", "blue"), callback_data="set_view_main")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_tagger_settings_keyboard(settings):
    """Get Tagger settings keyboard."""
    tagger_status = "✅" if settings.get("tagger_enabled", True) else "❌"
    
    keyboard = [
        [InlineKeyboardButton(colored_button(f"Tagger Feature: {tagger_status}", "blue"), callback_data="set_toggle_tagger_enabled")],
        [InlineKeyboardButton(colored_button("ℹ️ Tag everyone with /tag", "blue"), callback_data="set_none")],
        [InlineKeyboardButton(colored_button("ℹ️ Tag admins with /atag", "blue"), callback_data="set_none")],
        [InlineKeyboardButton(colored_button("🔙 Back", "blue"), callback_data="set_view_main")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_manager_settings_keyboard(settings):
    """Get Manager module settings keyboard."""
    ban_status = "✅" if settings.get("manager_ban_enabled", True) else "❌"
    unban_status = "✅" if settings.get("manager_unban_enabled", True) else "❌"
    mute_status = "✅" if settings.get("manager_mute_enabled", True) else "❌"
    unmute_status = "✅" if settings.get("manager_unmute_enabled", True) else "❌"
    kick_status = "✅" if settings.get("manager_kick_enabled", True) else "❌"
    promote_status = "✅" if settings.get("manager_promote_enabled", True) else "❌"
    demote_status = "✅" if settings.get("manager_demote_enabled", True) else "❌"
    purge_status = "✅" if settings.get("manager_purge_enabled", True) else "❌"
    pin_status = "✅" if settings.get("manager_pin_enabled", True) else "❌"
    mass_status = "✅" if settings.get("manager_mass_actions_enabled", True) else "❌"
    zombie_status = "✅" if settings.get("manager_zombie_enabled", True) else "❌"
    sg_status = "✅" if settings.get("manager_sg_enabled", True) else "❌"
    id_status = "✅" if settings.get("manager_id_enabled", True) else "❌"
    info_status = "✅" if settings.get("manager_info_enabled", True) else "❌"
    
    keyboard = [
        [InlineKeyboardButton(colored_button("── Moderation Actions ──", "blue"), callback_data="set_none")],
        [InlineKeyboardButton(colored_button(f"Ban: {ban_status}", "blue"), callback_data="set_toggle_manager_ban_enabled")],
        [InlineKeyboardButton(colored_button(f"Unban: {unban_status}", "blue"), callback_data="set_toggle_manager_unban_enabled")],
        [InlineKeyboardButton(colored_button(f"Mute: {mute_status}", "blue"), callback_data="set_toggle_manager_mute_enabled")],
        [InlineKeyboardButton(colored_button(f"Unmute: {unmute_status}", "blue"), callback_data="set_toggle_manager_unmute_enabled")],
        [InlineKeyboardButton(colored_button(f"Kick: {kick_status}", "blue"), callback_data="set_toggle_manager_kick_enabled")],
        [InlineKeyboardButton(colored_button("── Admin Management ──", "blue"), callback_data="set_none")],
        [InlineKeyboardButton(colored_button(f"Promote: {promote_status}", "blue"), callback_data="set_toggle_manager_promote_enabled")],
        [InlineKeyboardButton(colored_button(f"Demote: {demote_status}", "blue"), callback_data="set_toggle_manager_demote_enabled")],
        [InlineKeyboardButton(colored_button("── Message Control ──", "blue"), callback_data="set_none")],
        [InlineKeyboardButton(colored_button(f"Purge: {purge_status}", "blue"), callback_data="set_toggle_manager_purge_enabled")],
        [InlineKeyboardButton(colored_button(f"Pin/Unpin: {pin_status}", "blue"), callback_data="set_toggle_manager_pin_enabled")],
        [InlineKeyboardButton(colored_button("── Mass Actions ──", "blue"), callback_data="set_none")],
        [InlineKeyboardButton(colored_button(f"Mass Actions: {mass_status}", "blue"), callback_data="set_toggle_manager_mass_actions_enabled")],
        [InlineKeyboardButton(colored_button("── Utilities ──", "blue"), callback_data="set_none")],
        [InlineKeyboardButton(colored_button(f"Zombie Clean: {zombie_status}", "blue"), callback_data="set_toggle_manager_zombie_enabled")],
        [InlineKeyboardButton(colored_button(f"Username History (SG): {sg_status}", "blue"), callback_data="set_toggle_manager_sg_enabled")],
        [InlineKeyboardButton(colored_button(f"ID Command: {id_status}", "blue"), callback_data="set_toggle_manager_id_enabled")],
        [InlineKeyboardButton(colored_button(f"Info Command: {info_status}", "blue"), callback_data="set_toggle_manager_info_enabled")],
        [InlineKeyboardButton(colored_button("ℹ️ Toggle to enable/disable commands", "blue"), callback_data="set_none")],
        [InlineKeyboardButton(colored_button("🔙 Back", "blue"), callback_data="set_view_main")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_blocking_notification_settings_keyboard(settings):
    """Get blocking notification settings keyboard."""
    delete_status = "✅" if settings.get("blocking_delete_notifications", True) else "❌"
    timer = settings.get("blocking_notification_timer", 30)
    custom_text = "✅ Set" if settings.get("blocking_custom_text") else "❌ Not Set"
    
    keyboard = [
        [InlineKeyboardButton(colored_button(f"Delete Notifications: {delete_status}", "blue"), callback_data="set_toggle_blocking_delete_notifications")],
        [
            InlineKeyboardButton(colored_button("-5s", "blue"), callback_data="set_blocking_timer_sub_5"),
            InlineKeyboardButton(colored_button(f"🕒 Timer: {timer}s", "blue"), callback_data="set_none"),
            InlineKeyboardButton(colored_button("+5s", "blue"), callback_data="set_blocking_timer_add_5")
        ],
        [InlineKeyboardButton(colored_button(f"Custom Block Text: {custom_text}", "blue"), callback_data="set_config_blocking_custom_text")],
        [InlineKeyboardButton(colored_button("🗑️ Clear Custom Text", "blue"), callback_data="set_clear_blocking_custom_text")],
        [InlineKeyboardButton(colored_button("🔙 Back", "blue"), callback_data="set_view_blocking")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_time_picker_keyboard(settings, setting_key):
    """Get an interactive time picker keyboard."""
    current_time = settings.get(setting_key, "00:00")
    try:
        h, m = map(int, current_time.split(":"))
    except:
        h, m = 0, 0
        
    keyboard = [
        [
            InlineKeyboardButton(colored_button("➕ Hour", "blue"), callback_data=f"set_time_adj_{setting_key}_h+"),
            InlineKeyboardButton(colored_button("➕ Minute", "blue"), callback_data=f"set_time_adj_{setting_key}_m+")
        ],
        [
            InlineKeyboardButton(colored_button(f"⏰ {h:02d}:{m:02d}", "blue"), callback_data="set_none")
        ],
        [
            InlineKeyboardButton(colored_button("➖ Hour", "blue"), callback_data=f"set_time_adj_{setting_key}_h-"),
            InlineKeyboardButton(colored_button("➖ Minute", "blue"), callback_data=f"set_time_adj_{setting_key}_m-")
        ],
        [InlineKeyboardButton(colored_button("✍️ Manual Input", "blue"), callback_data=f"set_manual_time_{setting_key}")],
        [InlineKeyboardButton(colored_button("✅ Save & Back", "blue"), callback_data="set_view_emergency")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_emergency_settings_keyboard(settings):
    """Get Emergency Mode settings keyboard."""
    enabled = "✅ Enabled" if settings.get("emergency_enabled", False) else "❌ Disabled"
    text = "✅ Text" if settings.get("emergency_block_text", False) else "❌ Text"
    stickers = "✅ Stickers" if settings.get("emergency_block_stickers", False) else "❌ Stickers"
    media = "✅ Media" if settings.get("emergency_block_media", False) else "❌ Media"
    links = "✅ Links" if settings.get("emergency_block_links", False) else "❌ Links"
    premium = "✅ Premium" if settings.get("emergency_block_premium", False) else "❌ Premium"
    contact = "✅ Contact" if settings.get("emergency_block_contact", False) else "❌ Contact"
    location = "✅ Location" if settings.get("emergency_block_location", False) else "❌ Location"
    voice = "✅ Voice" if settings.get("emergency_block_voice", False) else "❌ Voice"
    audio = "✅ Audio" if settings.get("emergency_block_audio", False) else "❌ Audio"
    forward = "✅ Forward" if settings.get("emergency_block_forward", False) else "❌ Forward"
    poll = "✅ Poll" if settings.get("emergency_block_poll", False) else "❌ Poll"
    
    mode = settings.get("emergency_mode", "daily").title()
    start = settings.get("emergency_start_time", "00:00")
    end = settings.get("emergency_end_time", "23:59")
    apply_on = settings.get("emergency_apply_on", "members").title()
    
    keyboard = [
        [InlineKeyboardButton(colored_button(enabled, "blue"), callback_data="set_toggle_emergency_enabled")],
        [InlineKeyboardButton(colored_button(text, "blue"), callback_data="set_toggle_emergency_block_text"),
         InlineKeyboardButton(colored_button(stickers, "blue"), callback_data="set_toggle_emergency_block_stickers")],
        [InlineKeyboardButton(colored_button(media, "blue"), callback_data="set_toggle_emergency_block_media"),
         InlineKeyboardButton(colored_button(links, "blue"), callback_data="set_toggle_emergency_block_links")],
        [InlineKeyboardButton(colored_button(premium, "blue"), callback_data="set_toggle_emergency_block_premium"),
         InlineKeyboardButton(colored_button(contact, "blue"), callback_data="set_toggle_emergency_block_contact")],
        [InlineKeyboardButton(colored_button(location, "blue"), callback_data="set_toggle_emergency_block_location"),
         InlineKeyboardButton(colored_button(voice, "blue"), callback_data="set_toggle_emergency_block_voice")],
        [InlineKeyboardButton(colored_button(audio, "blue"), callback_data="set_toggle_emergency_block_audio"),
         InlineKeyboardButton(colored_button(forward, "blue"), callback_data="set_toggle_emergency_block_forward")],
        [InlineKeyboardButton(colored_button(poll, "blue"), callback_data="set_toggle_emergency_block_poll"),
         InlineKeyboardButton(colored_button(f"Mode: {mode}", "blue"), callback_data="set_toggle_emergency_mode")],
        [InlineKeyboardButton(colored_button(f"Apply on: {apply_on}", "blue"), callback_data="set_toggle_emergency_apply_on")],
        [InlineKeyboardButton(colored_button(f"🕒 Start: {start}", "blue"), callback_data="set_config_emergency_start_time"),
         InlineKeyboardButton(colored_button(f"🕒 End: {end}", "blue"), callback_data="set_config_emergency_end_time")],
        [InlineKeyboardButton(colored_button("🔙 Back", "blue"), callback_data="set_view_main")]
    ]
    return InlineKeyboardMarkup(keyboard)

async def settings_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    logging.info(f"[SETTINGS_CALLBACK] Received callback: {query.data if query else 'None'} from user {update.effective_user.id if update.effective_user else 'unknown'}")
    interaction_chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    
    # Handle "Open Here" button
    if query.data.startswith("settings_open_here_"):
        await open_settings_here(update, context)
        return
    
    # Handle "Go to Private" button
    if query.data.startswith("settings_go_private_"):
        group_chat_id = int(query.data.split("_")[-1])
        bot_info = await context.bot.get_me()
        bot_username = bot_info.username
        
        await query.answer("Opening settings in private chat...", show_alert=False)
        
        # Send message with button to go to private
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton(colored_button("💬 Click Here to Open in Private", "blue"), url=f"https://t.me/{bot_username}?start=settings_{group_chat_id}")]
        ])
        
        await query.edit_message_text(
            "💬 <b>Settings will be opened in private chat.</b>\n\n"
            "Click the button below to continue:",
            reply_markup=keyboard,
            parse_mode='HTML'
        )
        return

    # Handle Group Selection in Private
    if query.data.startswith("set_view_main_for_"):
        actual_chat_id = int(query.data.replace("set_view_main_for_", ""))
        context.user_data['settings_chat_id'] = actual_chat_id
        await show_settings_panel(query, context, actual_chat_id)
        await query.answer()
        return
    
    # Determine the actual chat_id (could be from private chat with stored group_id)
    chat_id = context.user_data.get('settings_chat_id', interaction_chat_id)
    
    # Granular permission check
    if not await can_user_configure_settings(chat_id, user_id, context):
        await query.answer("You need 'Change Info' and 'Ban Users' permissions to access settings.", show_alert=True)
        return

    data = query.data
    
    # Quick response for all setting buttons
    if data.startswith("set_") or data.startswith("mgmt_") or data == "free_list_members":
        try:
            await query.answer()
        except:
            pass

    if data == "set_close":
        chat_id = update.effective_chat.id
        
        # Delete the settings message
        try:
            await query.message.delete()
        except:
            pass
        
        # Send "Settings saved" message
        try:
            saved_msg = await context.bot.send_message(chat_id=chat_id, text="ꜱᴇᴛᴛɪɴɢ ꜱᴀᴠᴇᴅ ✅")
            
            # Delete the saved message after 5 seconds
            if context.job_queue:
                async def delete_job(ctx):
                    try:
                        await saved_msg.delete()
                    except:
                        pass
                
                context.job_queue.run_once(delete_job, 5)
        except:
            pass
        
        await query.answer()
        return
        
    if data == "set_view_main":
        await show_settings_panel(query, context, chat_id)
        await query.answer()
        return

    if data == "set_view_captcha":
        settings = get_chat_settings(chat_id)
        is_enabled = settings.get("captcha_enabled", False)
        status_str = "✅ Enabled" if is_enabled else "❌ Disabled"
        vc_enabled = settings.get("captcha_vc_enabled", False)
        vc_status_str = "✅ Enabled" if vc_enabled else "❌ Disabled"
        test_type = settings.get("captcha_type", "button").capitalize()
        action = settings.get("captcha_action", "kick").capitalize()
        timeout = settings.get("captcha_timeout", 120)
        text = (
            f"🛡️ <b>Captcha Verification Configuration</b>\n\n"
            f"Group Join Captcha: <b>{status_str}</b>\n"
            f"VC Join Captcha: <b>{vc_status_str}</b>\n"
            f"Test Type: <b>{test_type}</b>\n"
            f"Penalty on Failure: <b>{action}</b>\n"
            f"Timeout: <b>{timeout}s</b>\n\n"
            f"Newcomers must pass the verification test when joining the group or Voice Chat."
        )
        try:
            await edit_bot_response(
                query, context,
                text,
                reply_markup=get_captcha_settings_keyboard(settings),
                parse_mode='HTML'
            )
        except BadRequest: pass
        await query.answer()
        return

    if data == "set_toggle_captcha_enabled":
        if not await can_user_configure_settings(chat_id, user_id, context):
            await query.answer("❌ Only group admins can change this setting.", show_alert=True)
            return
        settings = get_chat_settings(chat_id)
        new_val = not settings.get("captcha_enabled", False)
        update_chat_setting(chat_id, "captcha_enabled", new_val)
        settings = get_chat_settings(chat_id)
        is_enabled = new_val
        status_str = "✅ Enabled" if is_enabled else "❌ Disabled"
        vc_enabled = settings.get("captcha_vc_enabled", False)
        vc_status_str = "✅ Enabled" if vc_enabled else "❌ Disabled"
        test_type = settings.get("captcha_type", "button").capitalize()
        action = settings.get("captcha_action", "kick").capitalize()
        timeout = settings.get("captcha_timeout", 120)
        text = (
            f"🛡️ <b>Captcha Verification Configuration</b>\n\n"
            f"Group Join Captcha: <b>{status_str}</b>\n"
            f"VC Join Captcha: <b>{vc_status_str}</b>\n"
            f"Test Type: <b>{test_type}</b>\n"
            f"Penalty on Failure: <b>{action}</b>\n"
            f"Timeout: <b>{timeout}s</b>\n\n"
            f"Newcomers must pass the verification test when joining the group or Voice Chat."
        )
        try:
            await edit_bot_response(
                query, context,
                text,
                reply_markup=get_captcha_settings_keyboard(settings),
                parse_mode='HTML'
            )
        except BadRequest: pass
        await query.answer(f"Group Captcha is now {status_str}!")
        return

    if data == "set_toggle_captcha_vc_enabled":
        if not await can_user_configure_settings(chat_id, user_id, context):
            await query.answer("❌ Only group admins can change this setting.", show_alert=True)
            return
        settings = get_chat_settings(chat_id)
        new_val = not settings.get("captcha_vc_enabled", False)
        update_chat_setting(chat_id, "captcha_vc_enabled", new_val)
        settings = get_chat_settings(chat_id)
        is_enabled = settings.get("captcha_enabled", False)
        status_str = "✅ Enabled" if is_enabled else "❌ Disabled"
        vc_enabled = new_val
        vc_status_str = "✅ Enabled" if vc_enabled else "❌ Disabled"
        test_type = settings.get("captcha_type", "button").capitalize()
        action = settings.get("captcha_action", "kick").capitalize()
        timeout = settings.get("captcha_timeout", 120)
        text = (
            f"🛡️ <b>Captcha Verification Configuration</b>\n\n"
            f"Group Join Captcha: <b>{status_str}</b>\n"
            f"VC Join Captcha: <b>{vc_status_str}</b>\n"
            f"Test Type: <b>{test_type}</b>\n"
            f"Penalty on Failure: <b>{action}</b>\n"
            f"Timeout: <b>{timeout}s</b>\n\n"
            f"Newcomers must pass the verification test when joining the group or Voice Chat."
        )
        try:
            await edit_bot_response(
                query, context,
                text,
                reply_markup=get_captcha_settings_keyboard(settings),
                parse_mode='HTML'
            )
        except BadRequest: pass
        await query.answer(f"VC Join Captcha is now {vc_status_str}!")
        return

    if data == "set_view_captcha_type":
        settings = get_chat_settings(chat_id)
        text = (
            "Please, select the test type for the newcomers:\n\n"
            "• Simple — bot will ask to send anything to the chat\n"
            "• Button — bot will ask to press a button\n"
            "• Digits — bot will ask to solve a simple arithmetic operation\n"
            "• Image — bot will ask to recognize characters on an image"
        )
        try:
            await edit_bot_response(
                query, context,
                text,
                reply_markup=get_captcha_test_type_keyboard(settings)
            )
        except BadRequest: pass
        await query.answer()
        return

    if data.startswith("set_captcha_type_"):
        if not await can_user_configure_settings(chat_id, user_id, context):
            await query.answer("❌ Only group admins can change this setting.", show_alert=True)
            return
        chosen = data.replace("set_captcha_type_", "").lower()
        if chosen in ("simple", "button", "digits", "image"):
            update_chat_setting(chat_id, "captcha_type", chosen)
        settings = get_chat_settings(chat_id)
        text = (
            "Please, select the test type for the newcomers:\n\n"
            "• Simple — bot will ask to send anything to the chat\n"
            "• Button — bot will ask to press a button\n"
            "• Digits — bot will ask to solve a simple arithmetic operation\n"
            "• Image — bot will ask to recognize characters on an image"
        )
        try:
            await edit_bot_response(
                query, context,
                text,
                reply_markup=get_captcha_test_type_keyboard(settings)
            )
        except BadRequest: pass
        await query.answer(f"Selected test type: {chosen.capitalize()}!")
        return

    if data == "set_captcha_toggle_action":
        if not await can_user_configure_settings(chat_id, user_id, context):
            await query.answer("❌ Only group admins can change this setting.", show_alert=True)
            return
        settings = get_chat_settings(chat_id)
        actions = ["kick", "mute", "ban"]
        cur_action = settings.get("captcha_action", "kick").lower()
        next_action = actions[(actions.index(cur_action) + 1) % len(actions)] if cur_action in actions else "kick"
        update_chat_setting(chat_id, "captcha_action", next_action)
        settings = get_chat_settings(chat_id)
        is_enabled = settings.get("captcha_enabled", False)
        status_str = "✅ Enabled" if is_enabled else "❌ Disabled"
        test_type = settings.get("captcha_type", "button").capitalize()
        action = next_action.capitalize()
        timeout = settings.get("captcha_timeout", 120)
        text = (
            f"🛡️ <b>Captcha Verification Configuration</b>\n\n"
            f"Status: <b>{status_str}</b>\n"
            f"Test Type: <b>{test_type}</b>\n"
            f"Penalty on Failure: <b>{action}</b>\n"
            f"Timeout: <b>{timeout}s</b>\n\n"
            f"Newcomers must pass the verification test before being allowed to send messages in the group."
        )
        try:
            await edit_bot_response(
                query, context,
                text,
                reply_markup=get_captcha_settings_keyboard(settings),
                parse_mode='HTML'
            )
        except BadRequest: pass
        await query.answer(f"Penalty action set to: {action}!")
        return

    if data in ("set_captcha_time_add", "set_captcha_time_sub"):
        if not await can_user_configure_settings(chat_id, user_id, context):
            await query.answer("❌ Only group admins can change this setting.", show_alert=True)
            return
        settings = get_chat_settings(chat_id)
        cur_time = int(settings.get("captcha_timeout", 120))
        if data == "set_captcha_time_add":
            new_time = min(600, cur_time + 30)
        else:
            new_time = max(30, cur_time - 30)
        update_chat_setting(chat_id, "captcha_timeout", new_time)
        settings = get_chat_settings(chat_id)
        is_enabled = settings.get("captcha_enabled", False)
        status_str = "✅ Enabled" if is_enabled else "❌ Disabled"
        test_type = settings.get("captcha_type", "button").capitalize()
        action = settings.get("captcha_action", "kick").capitalize()
        text = (
            f"🛡️ <b>Captcha Verification Configuration</b>\n\n"
            f"Status: <b>{status_str}</b>\n"
            f"Test Type: <b>{test_type}</b>\n"
            f"Penalty on Failure: <b>{action}</b>\n"
            f"Timeout: <b>{new_time}s</b>\n\n"
            f"Newcomers must pass the verification test before being allowed to send messages in the group."
        )
        try:
            await edit_bot_response(
                query, context,
                text,
                reply_markup=get_captcha_settings_keyboard(settings),
                parse_mode='HTML'
            )
        except BadRequest: pass
        await query.answer(f"Timeout set to {new_time}s")
        return

    if data == "set_view_welcome":
        settings = get_chat_settings(chat_id)
        try:
            await edit_bot_response(
                query, context,
                "👋 Welcome Configuration\n\nConfigure how new members are greeted:",
                reply_markup=get_welcome_settings_keyboard(settings)
            )
        except BadRequest: pass
        await query.answer()
        return

    if data == "set_view_goodbye":
        settings = get_chat_settings(chat_id)
        current_text = settings.get("goodbye_text", "👋 Goodbye {MENTION}! We're sad to see you leave {GROUPNAME}.")
        try:
            await edit_bot_response(
                query, context,
                f"👋 <b>Goodbye Configuration</b>\n\n"
                f"Configure the message sent when members leave the group.\n\n"
                f"<b>Current text:</b>\n{current_text}\n\n"
                f"Available placeholders: <code>{{MENTION}}</code>, <code>{{NAME}}</code>, <code>{{ID}}</code>, <code>{{USERNAME}}</code>, "
                f"<code>{{GROUPNAME}}</code>, <code>{{GROUPMENTION}}</code>, <code>{{DATE}}</code>, <code>{{TIME}}</code>",
                reply_markup=get_goodbye_settings_keyboard(settings),
                parse_mode='HTML'
            )
        except BadRequest: pass
        await query.answer()
        return

    if data == "set_view_join_request":
        settings = get_chat_settings(chat_id)
        is_enabled = settings.get("join_request_enabled", True)
        current_mode = settings.get("join_request_mode", "manual").lower()
        mode_text = "OFF" if not is_enabled or current_mode == "off" else {"accept": "Auto-Accept", "decline": "Auto-Decline", "manual": "Manual Approval"}.get(current_mode, current_mode.upper())
        status_str = "ENABLED" if is_enabled else "DISABLED"
        try:
            await edit_bot_response(
                query, context,
                f"📥 <b>Join Request Settings</b>\n\n"
                f"Status: <b>{status_str}</b>\n"
                f"Current Mode: <b>{mode_text}</b>\n\n"
                f"Control how the bot handles join requests from users wanting to enter the group:\n"
                f"• <b>Enable/Disable:</b> Toggle entire join request feature on or off.\n"
                f"• <b>Auto-Accept:</b> Approves all requests automatically & sends welcome DM.\n"
                f"• <b>Auto-Decline:</b> Rejects all join requests.\n"
                f"• <b>Manual:</b> Shows Accept/Decline buttons in the group for admins.\n"
                f"• <b>Turn Off:</b> Ignores incoming join requests.",
                reply_markup=get_join_request_settings_keyboard(settings),
                parse_mode='HTML'
            )
        except BadRequest: pass
        await query.answer()
        return

    if data == "set_toggle_join_request_enabled":
        if not await can_user_configure_settings(chat_id, user_id, context):
            await query.answer("❌ Only group admins can change this setting.", show_alert=True)
            return
        settings = get_chat_settings(chat_id)
        new_val = not settings.get("join_request_enabled", True)
        update_chat_setting(chat_id, "join_request_enabled", new_val)
        settings = get_chat_settings(chat_id)
        current_mode = settings.get("join_request_mode", "manual").lower()
        mode_text = "OFF" if not new_val or current_mode == "off" else {"accept": "Auto-Accept", "decline": "Auto-Decline", "manual": "Manual Approval"}.get(current_mode, current_mode.upper())
        status_str = "ENABLED" if new_val else "DISABLED"
        try:
            await edit_bot_response(
                query, context,
                f"📥 <b>Join Request Settings</b>\n\n"
                f"Status: <b>{status_str}</b>\n"
                f"Current Mode: <b>{mode_text}</b>\n\n"
                f"Control how the bot handles join requests from users wanting to enter the group:\n"
                f"• <b>Enable/Disable:</b> Toggle entire join request feature on or off.\n"
                f"• <b>Auto-Accept:</b> Approves all requests automatically & sends welcome DM.\n"
                f"• <b>Auto-Decline:</b> Rejects all join requests.\n"
                f"• <b>Manual:</b> Shows Accept/Decline buttons in the group for admins.\n"
                f"• <b>Turn Off:</b> Ignores incoming join requests.",
                reply_markup=get_join_request_settings_keyboard(settings),
                parse_mode='HTML'
            )
        except BadRequest: pass
        await query.answer(f"Join Request feature set to {status_str}!")
        return

    if data.startswith("set_joinreq_mode_"):
        if not await can_user_configure_settings(chat_id, user_id, context):
            await query.answer("❌ Only group admins can change this setting.", show_alert=True)
            return
        new_mode = data.replace("set_joinreq_mode_", "")
        if new_mode in ("accept", "decline", "manual", "off"):
            if new_mode == "off":
                update_chat_setting(chat_id, "join_request_enabled", False)
                update_chat_setting(chat_id, "join_request_mode", "off")
            else:
                update_chat_setting(chat_id, "join_request_enabled", True)
                update_chat_setting(chat_id, "join_request_mode", new_mode)

            settings = get_chat_settings(chat_id)
            is_enabled = settings.get("join_request_enabled", True)
            mode_text = "OFF" if not is_enabled or new_mode == "off" else {"accept": "Auto-Accept", "decline": "Auto-Decline", "manual": "Manual Approval"}.get(new_mode, new_mode.upper())
            status_str = "ENABLED" if is_enabled else "DISABLED"
            try:
                await edit_bot_response(
                    query, context,
                    f"📥 <b>Join Request Settings</b>\n\n"
                    f"Status: <b>{status_str}</b>\n"
                    f"Current Mode: <b>{mode_text}</b>\n\n"
                    f"Control how the bot handles join requests from users wanting to enter the group:\n"
                    f"• <b>Enable/Disable:</b> Toggle entire join request feature on or off.\n"
                    f"• <b>Auto-Accept:</b> Approves all requests automatically & sends welcome DM.\n"
                    f"• <b>Auto-Decline:</b> Rejects all join requests.\n"
                    f"• <b>Manual:</b> Shows Accept/Decline buttons in the group for admins.\n"
                    f"• <b>Turn Off:</b> Ignores incoming join requests.",
                    reply_markup=get_join_request_settings_keyboard(settings),
                    parse_mode='HTML'
                )
            except BadRequest: pass
            await query.answer(f"Join Request mode updated to {mode_text}!")
        return

    if data == "set_view_clean":
        settings = get_chat_settings(chat_id)
        try:
            await edit_bot_response(
                query, context,
                "🧹 Clean Service Configuration\n\nAuto-delete service messages from the chat:",
                reply_markup=get_clean_settings_keyboard(settings)
            )
        except BadRequest: pass
        await query.answer()
        return

    if data == "set_view_auto_delete":
        settings = get_chat_settings(chat_id)
        try:
            await edit_bot_response(
                query, context,
                "♻️ <b>Self-Destruction Configuration</b>\n\nAutomatically delete group messages after a set time:",
                reply_markup=get_auto_delete_settings_keyboard(settings),
                parse_mode='HTML'
            )
        except BadRequest: pass
        await query.answer()
        return

    if data == "set_view_blocking":
        settings = get_chat_settings(chat_id)
        try:
            await edit_bot_response(
                query, context,
                "🚧 <b>ʙʟᴏᴄᴋɪɴɢ ꜱᴇᴛᴛɪɴɢꜱ 🚧</b>\n\n"
                "ᴄᴏɴᴛʀᴏʟ ᴡʜᴀᴛ ᴛʏᴘᴇꜱ ᴏꜰ ᴄᴏɴᴛᴇɴᴛ ᴀʀᴇ ᴀʟʟᴏᴡᴇᴅ ɪɴ ʏᴏᴜʀ ɢʀᴏᴜᴘ.\n"
                "ᴛᴏɢɢʟᴇ ᴇᴀᴄʜ ꜱᴇᴛᴛɪɴɢ ᴛᴏ ʙʟᴏᴄᴋ ᴏʀ ᴀʟʟᴏᴡ ꜱᴘᴇᴄɪꜰɪᴄ ᴄᴏɴᴛᴇɴᴛ:",
                reply_markup=get_blocking_settings_keyboard(settings),
                parse_mode='HTML'
            )
        except BadRequest: pass
        return

    if data == "set_view_blocking_notifications":
        settings = get_chat_settings(chat_id)
        try:
            await edit_bot_response(
                query, context,
                "🔔 <b>ʙʟᴏᴄᴋɪɴɢ ɴᴏᴛɪꜰɪᴄᴀᴛɪᴏɴꜱ ꜱᴇᴛᴛɪɴɢꜱ 🔔</b>\n\n"
                "ᴄᴏɴꜰɪɢᴜʀᴇ ʜᴏᴡ ᴛʜᴇ ʙᴏᴛ ɴᴏᴛɪꜰɪᴇꜱ ᴜꜱᴇʀꜱ ᴡʜᴇɴ ᴛʜᴇɪʀ ᴍᴇꜱꜱᴀɢᴇꜱ ᴀʀᴇ ʙʟᴏᴄᴋᴇᴅ.",
                reply_markup=get_blocking_notification_settings_keyboard(settings),
                parse_mode='HTML'
            )
        except BadRequest: pass
        return

    if data == "set_view_msg_length":
        settings = get_chat_settings(chat_id)
        try:
            await edit_bot_response(
                query, context,
                "📏 Message Length Configuration\n\nSet the maximum character limit for messages:",
                reply_markup=get_msg_length_settings_keyboard(settings)
            )
        except BadRequest: pass
        await query.answer()
        return

    if data == "set_view_mod":
        settings = get_chat_settings(chat_id)
        try:
            await edit_bot_response(
                query, context,
                "🛡️ Moderation Settings\n\nConfigure warning limits and penalties:",
                reply_markup=get_mod_settings_keyboard(settings)
            )
        except BadRequest: pass
        await query.answer()
        return

    if data == "set_view_recurring" or data.startswith("set_recurring_"):
        from features.recurring import recurring_callback
        await recurring_callback(update, context)
        return

    if data == "set_view_command_deletion":
        settings = get_chat_settings(chat_id)
        try:
            await edit_bot_response(
                query, context,
                "🗑️ Command Deletion\n\nAutomatically delete admin command messages if enabled:",
                reply_markup=get_command_deletion_keyboard(settings)
            )
        except BadRequest: pass
        await query.answer()
        return

    if data == "set_view_command_access":
        settings = get_chat_settings(chat_id)
        try:
            await edit_bot_response(
                query, context,
                "🔑 Command Access\n\nChoose who can use normal commands (help, id, info):",
                reply_markup=get_command_access_keyboard(settings)
            )
        except BadRequest: pass
        await query.answer()
        return

    if data == "set_view_command_permissions":
        settings = get_chat_settings(chat_id)
        try:
            await edit_bot_response(
                query, context,
                "🎛️ <b>Command Permissions</b>\n\nConfigure who can use each command:\n• <b>All</b> - Everyone can use it\n• <b>Admin</b> - Only admins can use it\n• <b>Owner</b> - Only group owner can use it\n\nTap a command to change its access level:",
                reply_markup=get_command_permissions_keyboard(settings)
            )
        except BadRequest: pass
        await query.answer()
        return

    if data == "set_view_pinned_messages":
        settings = get_chat_settings(chat_id)
        try:
            await edit_bot_response(
                query, context,
                "📌 Pinned Messages Settings\n\nConfigure how pinned messages are handled:",
                reply_markup=get_pinned_message_settings_keyboard(settings)
            )
        except BadRequest: pass
        await query.answer()
        return

    if data == "set_view_bot_protection":
        settings = get_chat_settings(chat_id)
        try:
            await edit_bot_response(
                query, context,
                "🤖 Bot Protection Settings\n\nConfigure how bots are handled when added to the group:",
                reply_markup=get_bot_protection_settings_keyboard(settings)
            )
        except BadRequest: pass
        await query.answer()
        return

    if data == "set_view_link_spam":
        settings = get_chat_settings(chat_id)
        try:
            await edit_bot_response(
                query, context,
                "🔗 <b>Link Protection Settings</b>\n\nAutomatically delete messages containing links from the selected group:",
                reply_markup=get_link_spam_settings_keyboard(settings),
                parse_mode='HTML'
            )
        except BadRequest: pass
        await query.answer()
        return

    if data == "set_view_forward_protection":
        settings = get_chat_settings(chat_id)
        try:
            await edit_bot_response(
                query, context,
                "🔄 Forward Protection Settings\n\nAutomatically delete forwarded messages from members:",
                reply_markup=get_forward_protection_settings_keyboard(settings)
            )
        except BadRequest: pass
        await query.answer()
        return

    if data == "set_view_language_filter":
        settings = get_chat_settings(chat_id)
        try:
            await edit_bot_response(
                query, context,
                "🌐 Language Filter Settings\n\nAutomatically delete messages in languages other than allowed ones.\n\nAllowed: English, Hindi, Hinglish\n\nEmoji & Symbol Blocking Options:",
                reply_markup=get_language_filter_settings_keyboard(settings)
            )
        except BadRequest: pass
        await query.answer()
        return

    if data == "set_view_antiflood":
        settings = get_chat_settings(chat_id)
        try:
            await edit_bot_response(
                query, context,
                "🌊 <b>Anti-Flood Settings</b>\n\n"
                "Protect your group from fast messaging and spamming.\n\n"
                "• <b>Limit:</b> Number of messages allowed\n"
                "• <b>Window:</b> Time window for the limit\n"
                "• <b>Penalty:</b> Action taken when limit is reached",
                reply_markup=get_antiflood_settings_keyboard(settings),
                parse_mode='HTML'
            )
        except BadRequest: pass
        await query.answer()
        return

    if data == "set_view_nightmode_start_grid":
        try:
            await query.edit_message_text(
                "🌙 <b>Night mode</b>\n"
                "In this menu you can set an interval of hour and every day, in that hours will be enabled the night mode.\n\n"
                "👉 Select the <b>starting time</b>:",
                reply_markup=get_nightmode_hour_grid(user_id, is_start=True),
                parse_mode='HTML'
            )
        except BadRequest: pass
        await query.answer()
        return

    if data.startswith("set_nm_start_"):
        hour = int(data.replace("set_nm_start_", ""))
        update_chat_setting(chat_id, "nightmode_start", hour)
        try:
            await query.edit_message_text(
                "🌙 <b>Night mode</b>\n"
                "In this menu you can set an interval of hour and every day, in that hours will be enabled the night mode.\n\n"
                "👉 Select the <b>ending time</b>:",
                reply_markup=get_nightmode_hour_grid(user_id, is_start=False),
                parse_mode='HTML'
            )
        except BadRequest: pass
        await query.answer(f"Start time set to {hour}:00")
        return

    if data.startswith("set_nm_end_"):
        hour = int(data.replace("set_nm_end_", ""))
        update_chat_setting(chat_id, "nightmode_end", hour)
        await query.answer(f"End time set to {hour}:00")
        # Return to main nightmode menu
        query.data = "set_view_nightmode"
        await settings_callback(update, context)
        return

    if data == "set_config_nightmode_timezone_private":
        bot_info = await context.bot.get_me()
        bot_username = bot_info.username
        
        # Check if we are already in private
        if update.effective_chat.type == "private":
            from telegram import KeyboardButton, ReplyKeyboardMarkup
            keyboard = ReplyKeyboardMarkup([
                [KeyboardButton("📍 Send the position", request_location=True)],
                [KeyboardButton("❌ Cancel")]
            ], resize_keyboard=True, one_time_keyboard=True)
            
            try:
                # Delete the settings message first to avoid clutter
                await query.message.delete()
                
                await context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text="🌍 <b>Time Zone</b>\n"
                         "Now <b>send your position</b> in order to auto detect Time Zone to be set in the group.\n\n"
                         "You can send it using the button in the keyboard or touching 📎 Attach, so 📍 Position (with this second way you can chose a specific position also different from yours).\n\n"
                         "Alternatively you can <b>write the name of your city</b> directly.\n\n"
                         "<i>Your position will not be saved, we will save only the Time Zone detected.</i>",
                    parse_mode='HTML',
                    reply_markup=keyboard
                )
                context.user_data['config_state'] = ('nightmode_timezone', chat_id)
            except Exception as e:
                logging.error(f"Error in private timezone setup: {e}")
        else:
            # In group, prompt to go private
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton(colored_button("👤 Open in Private Chat", "blue"), url=f"https://t.me/{bot_username}?start=tz_{chat_id}")],
                [InlineKeyboardButton(colored_button("⬅️ Back", "blue"), callback_data="set_view_nightmode")]
            ])
            try:
                await query.edit_message_text(
                    "<b>Time Zone</b> can be set only using settings in private chat with the Bot.",
                    reply_markup=keyboard,
                    parse_mode='HTML'
                )
            except BadRequest: pass
        await query.answer()
        return

    if data == "set_view_nightmode":
        settings = get_chat_settings(chat_id)
        from datetime import datetime
        import pytz
        
        timezone_str = settings.get("nightmode_timezone", "Asia/Kolkata")
        try:
            tz = pytz.timezone(timezone_str)
        except:
            tz = pytz.timezone("Asia/Kolkata")
        
        now = datetime.now(tz)
        current_time_str = now.strftime("%d %b %Y, %H:%M")
        
        status_text = "❌ Off"
        if settings.get("nightmode_enabled", False):
            active_actions = []
            if settings.get("nightmode_restrict_media", False): active_actions.append("📸 Delete medias")
            if settings.get("nightmode_global_silence", False): active_actions.append("🤫 Global Silence")
            
            status_text = "\n".join(active_actions) if active_actions else "✅ Active (No restrictions set)"
        
        start = settings.get("nightmode_start", 23)
        end = settings.get("nightmode_end", 9)
        advise = "✅" if settings.get("nightmode_advise_enabled", True) else "❌"
        
        text = (
            f"🌙 <b>Night mode</b>\n"
            f"Select the actions you want to limit every night.\n\n"
            f"<b>Status:</b> {status_text}\n"
            f" ├ Active from hour <b>{start} to {end}</b>\n"
            f" └ Start&End advises: {advise}\n\n"
            f"<b>Current time:</b> {current_time_str}"
        )
        
        try:
            await edit_bot_response(
                query, context,
                text,
                reply_markup=get_nightmode_settings_keyboard(settings),
                parse_mode='HTML'
            )
        except BadRequest: pass
        await query.answer()
        return

    if data == "set_view_group_link":
        settings = get_chat_settings(chat_id)
        link = settings.get("group_link")
        status = "Activated" if link else "Deactivated"
        text = (
            "🔗 <b>Group link</b>\n"
            "Here you can set the link of the group, which will be visible with the command /link.\n\n"
            f"<b>Status:</b> {status}"
        )
        if link:
            text += f"\n<b>Current Link:</b> {link}"
            
        try:
            await edit_bot_response(query, context, text, reply_markup=get_group_link_keyboard(settings), parse_mode='HTML')
        except BadRequest: pass
        await query.answer()
        return

    if data == "set_view_banned_words":
        settings = get_chat_settings(chat_id)
        penalty = settings.get("banned_words_penalty", "off").title()
        deletion = "Yes ✅" if settings.get("banned_words_deletion", True) else "No ❌"
        text = (
            "🔤 <b>Banned Words</b>\n"
            "From this menu you can set a punishment for users who use the words you decide to ban.\n\n"
            f"<b>Penalty:</b> {penalty}\n"
            f"<b>Deletion:</b> {deletion}"
        )
        try:
            await edit_bot_response(query, context, text, reply_markup=get_banned_words_keyboard(settings), parse_mode='HTML')
        except BadRequest: pass
        await query.answer()
        return

    if data == "set_view_banned_words_list":
        settings = get_chat_settings(chat_id)
        words = settings.get("banned_words", [])
        if not words:
            text = "No words have been banned yet."
        else:
            text = "📋 <b>Banned Words List:</b>\n\n"
            text += ", ".join([f"<code>{w}</code>" for w in words])
            
        try:
            await edit_bot_response(query, context, text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(colored_button("🔙 Back", "blue"), callback_data="set_view_banned_words")]]), parse_mode='HTML')
        except BadRequest: pass
        await query.answer()
        return

    if data == "set_view_members_mgmt":
        text = (
            "👥 <b>Members Management</b>\n"
            "From this menu you can manage general actions on group members"
        )
        try:
            await edit_bot_response(query, context, text, reply_markup=get_members_mgmt_keyboard(), parse_mode='HTML')
        except BadRequest: pass
        await query.answer()
        return

    if data == "set_view_regulations":
        text = (
            "📜 <b>Group's regulations</b>\n"
            "From this menu you can manage the group's regulations, that will be "
            "shown with the command /rules.\n\n"
            "<i>To edit who can use the /rules command, go to the \"Commands permissions\" section.</i>"
        )
        try:
            await edit_bot_response(query, context, text, reply_markup=get_regulations_keyboard(), parse_mode='HTML')
        except BadRequest: pass
        await query.answer()
        return

    if data == "set_view_regulations_custom":
        settings = get_chat_settings(chat_id)
        text_status = "✅" if settings.get("rules_text") else "❌"
        media_status = "✅" if settings.get("rules_media") else "❌"
        btns_status = "✅" if settings.get("rules_buttons") else "❌"
        
        text = (
            "📜 <b>Regulation</b>\n\n"
            f"📄 Text {text_status}\n"
            f"📸 Media {media_status}\n"
            f"🔡 Url Buttons {btns_status}\n\n"
            "👉 Use the buttons below to choose what you want to set"
        )
        try:
            await edit_bot_response(query, context, text, reply_markup=get_regulations_custom_keyboard(settings), parse_mode='HTML')
        except BadRequest: pass
        await query.answer()
        return

    if data.startswith("set_preview_rules_"):
        settings = get_chat_settings(chat_id)
        p_type = data.replace("set_preview_rules_", "")
        
        if p_type == "text":
            val = settings.get("rules_text", "Not set")
            await query.answer(f"Text: {val[:50]}...", show_alert=True)
        elif p_type == "media":
            val = settings.get("rules_media", "Not set")
            await query.answer(f"Media ID: {val[:50]}...", show_alert=True)
        elif p_type == "buttons":
            val = settings.get("rules_buttons", [])
            await query.answer(f"Buttons: {len(val)} set", show_alert=True)
        elif p_type == "full":
            # Actually send the rules message as a test
            from bot import send_rules_message
            await send_rules_message(context.bot, chat_id, chat_id, settings)
            await query.answer("Full preview sent to the chat!")
        return

    if data.startswith("set_banned_penalty_"):
        new_penalty = data.split("_")[-1]
        update_chat_setting(chat_id, "banned_words_penalty", new_penalty)
        # Refresh
        query.data = "set_view_banned_words"
        await settings_callback(update, context)
        return

    if data == "set_toggle_banned_words_deletion":
        settings = get_chat_settings(chat_id)
        current = settings.get("banned_words_deletion", True)
        update_chat_setting(chat_id, "banned_words_deletion", not current)
        # Refresh
        query.data = "set_view_banned_words"
        await settings_callback(update, context)
        return

    if data.startswith("mgmt_"):
        action = data.split("_", 1)[1]
        
        # Don't answer yet if it's a confirmation that needs to edit message
        if action != "confirm_delete_all":
            try:
                await query.answer(f"Action '{action}' in progress...")
            except:
                pass
        
        # Use existing mass action functions
        from managers.mass_actions import do_unmuteall, do_unbanall, do_kickall
        
        if action == "unmute_all":
            await do_unmuteall(context.bot, chat_id)
        elif action == "unban_all":
            await do_unbanall(context.bot, chat_id)
        elif action == "kick_restricted":
            # Find users who are restricted
            count, errors = 0, 0
            async for member in context.bot.get_chat_members(chat_id):
                if member.status == 'restricted':
                    try:
                        await context.bot.ban_chat_member(chat_id, member.user.id)
                        await asyncio.sleep(0.1)
                        await context.bot.unban_chat_member(chat_id, member.user.id)
                        count += 1
                    except:
                        errors += 1
            await context.bot.send_message(chat_id, f"✅ Done! Kicked {count} restricted users. ({errors} errors)")
        elif action == "kick_deleted":
            # Find deleted accounts
            from managers.zombie import scan_deleted_members
            zombies = await scan_deleted_members(context.bot, chat_id)
            count, errors = 0, 0
            for user in zombies:
                try:
                    await context.bot.ban_chat_member(chat_id, user.id)
                    await asyncio.sleep(0.1)
                    await context.bot.unban_chat_member(chat_id, user.id)
                    count += 1
                except:
                    errors += 1
            await context.bot.send_message(chat_id, f"✅ Done! Kicked {count} deleted accounts. ({errors} errors)")
        elif action == "confirm_delete_all":
            # Start deletion process
            logging.info(f"🚀 Global purge requested for chat {chat_id} by user {user_id}")
            try:
                await query.edit_message_text("🚀 <b>Global purge in progress...</b>\nYou can close this menu, the process will continue in the background.", parse_mode='HTML')
            except Exception as e:
                logging.error(f"Failed to edit message for global purge: {e}")
            
            # Run in background to not block the bot
            async def background_purge(b, c_id, start_msg_id):
                deleted = 0
                logging.info(f"Starting background purge for {c_id} from ID {start_msg_id}")
                try:
                    # If start_msg_id is too low (e.g. from private chat), 
                    # we should try to get a more realistic starting ID
                    if start_msg_id < 1000 and str(c_id).startswith('-100'):
                        try:
                            temp_msg = await b.send_message(c_id, "...")
                            start_msg_id = temp_msg.message_id
                            await temp_msg.delete()
                        except:
                            # Fallback to a high number if we can't send message
                            start_msg_id = 1000000 
                    
                    # We delete in batches of 100 backwards
                    # This will cover all messages including every type of service message
                    consecutive_errors = 0
                    for i in range(start_msg_id, 0, -100):
                        batch = list(range(max(1, i - 100), i))
                        try:
                            await b.delete_messages(c_id, batch)
                            deleted += len(batch)
                            consecutive_errors = 0
                            await asyncio.sleep(0.1) # Further optimized
                        except BadRequest as e:
                            err = str(e)
                            if "Message to delete not found" in err or "Message can't be deleted" in err:
                                consecutive_errors += 1
                                # Stop if 5000 messages in a row are missing/undeletable
                                if consecutive_errors > 50:
                                    logging.info(f"Purge stopped for {c_id}: too many consecutive errors at ID {i}")
                                    break
                                continue
                            elif "Flood control exceeded" in err:
                                import re
                                seconds = re.search(r'wait (\d+)', err)
                                wait_time = int(seconds.group(1)) if seconds else 30
                                await asyncio.sleep(wait_time + 1)
                                continue
                            else: 
                                logging.warning(f"Purge batch failed for {c_id}: {err}")
                                break
                        except Exception as e:
                            logging.error(f"Unexpected error in purge batch for {c_id}: {e}")
                            break
                    
                    await b.send_message(c_id, f"✅ <b>Global purge complete!</b>\nTotal deleted: `{deleted}` messages\nAll service messages, VC invites, and joins have been cleared.", parse_mode='HTML')
                    logging.info(f"✅ Global purge complete for {c_id}. Total: {deleted}")
                except Exception as e:
                    logging.error(f"Error in background purge for {c_id}: {e}")

            # Get a fresh message ID if possible
            actual_start_id = query.message.message_id
            asyncio.create_task(background_purge(context.bot, chat_id, actual_start_id))
            try:
                await query.answer("Global purge started!")
            except:
                pass
            return
        elif action == "confirm_purge_service":
            # Start service messages deletion process
            logging.info(f"🧹 Service purge requested for chat {chat_id} by user {user_id}")
            try:
                await query.edit_message_text("🧹 <b>Event messages purge in progress...</b>\nYou can close this menu, the process will continue in the background.", parse_mode='HTML')
            except Exception as e:
                logging.error(f"Failed to edit message for service purge: {e}")
            
            # Run in background to not block the bot
            async def background_service_purge(b, c_id, start_msg_id):
                deleted = 0
                logging.info(f"Starting background service purge for {c_id} from ID {start_msg_id}")
                try:
                    # If start_msg_id is too low (e.g. from private chat), 
                    # we should try to get a more realistic starting ID
                    if start_msg_id < 1000 and str(c_id).startswith('-100'):
                        try:
                            temp_msg = await b.send_message(c_id, "...")
                            start_msg_id = temp_msg.message_id
                            await temp_msg.delete()
                        except:
                            # Fallback to a high number if we can't send message
                            start_msg_id = 1000000 
                    
                    # We delete in batches of 100 backwards
                    # This will cover all messages including service messages
                    consecutive_errors = 0
                    for i in range(start_msg_id, 0, -100):
                        batch = list(range(max(1, i - 100), i))
                        try:
                            await b.delete_messages(c_id, batch)
                            deleted += len(batch)
                            consecutive_errors = 0
                            await asyncio.sleep(0.1)
                        except BadRequest as e:
                            err = str(e)
                            if "Message to delete not found" in err or "Message can't be deleted" in err:
                                consecutive_errors += 1
                                # Stop if 5000 messages in a row are missing/undeletable
                                if consecutive_errors > 50:
                                    logging.info(f"Service purge stopped for {c_id}: too many consecutive errors at ID {i}")
                                    break
                                continue
                            elif "Flood control exceeded" in err:
                                import re
                                seconds = re.search(r'wait (\d+)', err)
                                wait_time = int(seconds.group(1)) if seconds else 30
                                await asyncio.sleep(wait_time + 1)
                                continue
                            else: 
                                logging.warning(f"Service purge batch failed for {c_id}: {err}")
                                break
                        except Exception as e:
                            logging.error(f"Unexpected error in service purge batch for {c_id}: {e}")
                            break
                    
                    await b.send_message(c_id, f"✅ <b>Event messages purge complete!</b>\nTotal deleted: `{deleted}` messages\nAll service messages, VC invites, and joins have been cleared.", parse_mode='HTML')
                    logging.info(f"✅ Service purge complete for {c_id}. Total: {deleted}")
                except Exception as e:
                    logging.error(f"Error in background service purge for {c_id}: {e}")

            # Get a fresh message ID if possible
            actual_start_id = query.message.message_id
            asyncio.create_task(background_service_purge(context.bot, chat_id, actual_start_id))
            try:
                await query.answer("Event purge started!")
            except:
                pass
            return
            
        try:
            await query.answer("Members Management action completed.")
        except:
            pass
        return

    if data == "set_view_vc":
        settings = get_chat_settings(chat_id)
        try:
            await edit_bot_response(
                query, context,
                "🎙 <b>ᴠᴏɪᴄᴇ ᴄʜᴀᴛ ꜱᴇᴛᴛɪɴɢꜱ 🎙</b>\n\n"
                "ᴄᴏɴᴛʀᴏʟ ᴠᴏɪᴄᴇ ᴄʜᴀᴛ ʀᴇʟᴀᴛᴇᴅ ɴᴏᴛɪꜰɪᴄᴀᴛɪᴏɴꜱ ᴀɴᴅ ꜰᴇᴀᴛᴜʀᴇꜱ:",
                reply_markup=get_vc_settings_keyboard(settings),
                parse_mode='HTML'
            )
        except BadRequest: pass
        await query.answer()
        return

    if data == "set_view_tagger":
        settings = get_chat_settings(chat_id)
        try:
            await edit_bot_response(
                query, context,
                "🏷️ <b>ᴛᴀɢɢᴇʀ ᴄᴏɴꜰɪɢᴜʀᴀᴛɪᴏɴ 🏷️</b>\n\n"
                "ᴍᴀɴᴀɢᴇ ᴛʜᴇ ᴍᴇɴᴛɪᴏɴ/ᴛᴀɢɢᴇʀ ꜰᴇᴀᴛᴜʀᴇ ꜰᴏʀ ʏᴏᴜʀ ɢʀᴏᴜᴘ:",
                reply_markup=get_tagger_settings_keyboard(settings),
                parse_mode='HTML'
            )
        except BadRequest: pass
        await query.answer()
        return

    if data == "set_view_manager":
        settings = get_chat_settings(chat_id)
        try:
            await edit_bot_response(
                query, context,
                "👥 Manager Module Settings\n\nEnable or disable Manager commands:",
                reply_markup=get_manager_settings_keyboard(settings)
            )
        except BadRequest: pass
        await query.answer()
        return

    if data == "set_view_emergency":
        settings = get_chat_settings(chat_id)
        try:
            await edit_bot_response(
                query, context,
                "🚨 <b>Emergency Mode Settings</b>\n\n"
                "ꜱᴇᴛ ᴜᴘ ᴀ ꜱᴄʜᴇᴅᴜʟᴇᴅ ʙʟᴏᴄᴋɪɴɢ ᴘᴇʀɪᴏᴅ ꜰᴏʀ ꜱᴘᴇᴄɪꜰɪᴄ ᴄᴏɴᴛᴇɴᴛ ᴛʏᴘᴇꜱ. "
                "ᴛʜɪꜱ ᴄᴀɴ ʙᴇ ᴀᴘᴘʟɪᴇᴅ ᴅᴀɪʟʏ ᴏʀ ᴊᴜꜱᴛ ꜰᴏʀ ᴛᴏᴅᴀʏ.",
                reply_markup=get_emergency_settings_keyboard(settings),
                parse_mode='HTML'
            )
        except BadRequest: pass
        await query.answer()
        return

    if data == "set_view_nsfw" or data.startswith("nsfw_"):
        from features.nsfw_panel import handle_nsfw_callback, get_nsfw_panel_keyboard, render_nsfw_panel_text
        if data == "set_view_nsfw":
            settings = get_chat_settings(chat_id)
            title = update.effective_chat.title or "This Chat"
            text = render_nsfw_panel_text(title, settings)
            try:
                await edit_bot_response(
                    query, context,
                    text,
                    reply_markup=get_nsfw_panel_keyboard(chat_id, settings),
                    parse_mode='HTML'
                )
            except BadRequest: pass
            await query.answer()
            return
        else:
            await handle_nsfw_callback(update, context)
            return

    if data == "set_view_nsfw_threshold_options":
        from features.nsfw_panel import handle_nsfw_callback
        query.data = f"nsfw_view_thresholds:{chat_id}"
        await handle_nsfw_callback(update, context)
        return

    if data == "set_toggle_nsfw_filter":
        from features.nsfw_panel import handle_nsfw_callback
        query.data = f"nsfw_toggle_filter:{chat_id}"
        await handle_nsfw_callback(update, context)
        return

    if data.startswith("set_nsfw_action_"):
        from features.nsfw_panel import handle_nsfw_callback
        action = data.replace("set_nsfw_action_", "")
        query.data = f"nsfw_set_action:{action}:{chat_id}"
        await handle_nsfw_callback(update, context)
        return

    if data.startswith("set_nsfw_threshold_"):
        from features.nsfw_panel import handle_nsfw_callback
        threshold_str = data.replace("set_nsfw_threshold_", "")
        query.data = f"nsfw_set_threshold:{threshold_str}:{chat_id}"
        await handle_nsfw_callback(update, context)
        return

    if data == "set_toggle_ui_layout":
        settings = get_chat_settings(chat_id)
        current_layout = settings.get("ui_layout_type", 1)
        # Rotate: 1 (Large) -> 2 (Small) -> 1
        new_layout = 2 if current_layout == 1 else 1
        update_chat_setting(chat_id, "ui_layout_type", new_layout)
        try:
            await edit_bot_response(
                query, context,
                "🛠 <b>Bot Settings</b>\n\nSelect one of the settings that you want to change:",
                reply_markup=get_main_settings_keyboard(chat_id),
                parse_mode='HTML'
            )
        except BadRequest: pass
        await query.answer(f"Layout changed to {'Small' if new_layout == 2 else 'Large'}")
        return

    # Handle Emergency Toggles
    if data.startswith("set_toggle_emergency_"):
        field = data.replace("set_toggle_emergency_", "emergency_")
        settings = get_chat_settings(chat_id)
        
        if field == "emergency_mode":
            current = settings.get(field, "daily")
            new_val = "today" if current == "daily" else "daily"
        elif field == "emergency_apply_on":
            options = ["members", "admins", "everyone"]
            current = settings.get(field, "members")
            next_idx = (options.index(current) + 1) % len(options)
            new_val = options[next_idx]
        else:
            current = settings.get(field, False)
            new_val = not current
            
        update_chat_setting(chat_id, field, new_val)
        settings[field] = new_val # Update local copy for keyboard
        
        try:
            await query.edit_message_reply_markup(reply_markup=get_emergency_settings_keyboard(settings))
        except BadRequest: pass
        await query.answer("Updated!")
        return

    if data.startswith("set_config_emergency_"):
        field = data.replace("set_config_", "")
        settings = get_chat_settings(chat_id)
        
        try:
            await edit_bot_response(
                query, context,
                f"⏰ <b>Set Time: {field.replace('_', ' ').title()}</b>\n\n"
                "Use the buttons below to adjust the time or click 'Manual Input' to type it.",
                reply_markup=get_time_picker_keyboard(settings, field),
                parse_mode='HTML'
            )
        except BadRequest: pass
        await query.answer()
        return

    if data.startswith("set_manual_time_"):
        field = data.replace("set_manual_time_", "")
        context.chat_data['setting_to_config'] = field
        context.chat_data['setting_type'] = "text"
        
        prompt = f"Please send the new value for <b>{field.replace('_', ' ').title()}</b> (e.g., 09:00):"
        await query.message.reply_text(prompt, parse_mode='HTML')
        await query.answer()
        return

    if data == "set_view_deleting":
        try:
            await edit_bot_response(
                query, context,
                "🗑️ <b>Deleting Messages</b>\nWhat messages do you want the Bot to delete?",
                reply_markup=get_deleting_messages_keyboard(),
                parse_mode='HTML'
            )
        except BadRequest: pass
        await query.answer()
        return

    if data == "set_view_edit_checks":
        settings = get_chat_settings(chat_id)
        try:
            await edit_bot_response(
                query, context,
                "✍️ <b>Edit Checks Configuration</b>\nAutomatically delete messages when they are edited:",
                reply_markup=get_edit_checks_keyboard(settings),
                parse_mode='HTML'
            )
        except BadRequest: pass
        await query.answer()
        return

    if data == "set_view_warning_time":
        settings = get_chat_settings(chat_id)
        try:
            await edit_bot_response(
                query, context,
                "🕒 <b>Warning Deletion Time</b>\n\nConfigure how long warning messages (like Bio Link or Edit Protection) should stay in the group before being deleted:",
                reply_markup=get_warning_time_settings_keyboard(settings),
                parse_mode='HTML'
            )
        except BadRequest: pass
        await query.answer()
        return

    if data == "set_view_bio_link":
        settings = get_chat_settings(chat_id)
        try:
            await edit_bot_response(
                query, context,
                "🔗 <b>Bio Link Checker Configuration</b>\nAutomatically apply penalties to users with links in their bio:",
                reply_markup=get_bio_link_settings_keyboard(settings),
                parse_mode='HTML'
            )
        except BadRequest: pass
        await query.answer()
        return

    if data == "set_view_global_purge":
        bot_info = await context.bot.get_me()
        bot_username = f"@{bot_info.username}"
        
        # Check bot permissions
        try:
            bot_member = await context.bot.get_chat_member(chat_id, context.bot.id)
            has_perms = (
                bot_member.status == 'administrator' and
                bot_member.can_restrict_members and
                bot_member.can_delete_messages and
                bot_member.can_promote_members and
                bot_member.can_invite_users
            )
        except:
            has_perms = False
            
        text = (
            f"💥 <b>Delete all messages</b>\n\n"
            f"In order to delete the entire history, the Bot {bot_username} must be "
            f"Admin with privileges of <b>block users</b>, <b>delete messages</b>, <b>add "
            f"Administrators</b> and <b>invite users with group link</b>.\n\n"
            f"If you already see that the Bot has these privileges, try to remove it from "
            f"the group and add it again with all privileges."
        )
        
        keyboard = []
        if has_perms:
            keyboard.append([InlineKeyboardButton(colored_button("🚀 Start Global Purge", "blue"), callback_data="mgmt_confirm_delete_all")])
        
        keyboard.append([InlineKeyboardButton(colored_button("🔙 Back", "blue"), callback_data="set_view_deleting")])
        
        try:
            await edit_bot_response(
                query, context,
                text,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode='HTML'
            )
        except BadRequest: pass
        await query.answer()
        return

    if data == "set_view_purge_service":
        bot_info = await context.bot.get_me()
        bot_username = f"@{bot_info.username}"
        
        # Check bot permissions
        try:
            bot_member = await context.bot.get_chat_member(chat_id, context.bot.id)
            has_perms = (
                bot_member.status == 'administrator' and
                bot_member.can_delete_messages
            )
        except:
            has_perms = False
            
        text = (
            f"🧹 <b>Delete Event Messages</b>\n\n"
            f"This will delete all service/event messages (user joined/left, pinned messages, etc.) in the group.\n\n"
            f"The Bot {bot_username} must be Admin with <b>delete messages</b> permission."
        )
        
        keyboard = []
        if has_perms:
            keyboard.append([InlineKeyboardButton(colored_button("🚀 Start Event Purge", "blue"), callback_data="mgmt_confirm_purge_service")])
        
        keyboard.append([InlineKeyboardButton(colored_button("🔙 Back", "blue"), callback_data="set_view_deleting")])
        
        try:
            await edit_bot_response(
                query, context,
                text,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode='HTML'
            )
        except BadRequest: pass
        await query.answer()
        return

    if data.startswith("set_edit_warn_"):
        action = data.split("_")[-1]
        settings = get_chat_settings(chat_id)
        current = settings.get("edit_checks_warn_limit", 3)
        
        if action == "add":
            new_val = min(10, current + 1)
        else:
            new_val = max(1, current - 1)
            
        update_chat_setting(chat_id, "edit_checks_warn_limit", new_val)
        new_settings = get_chat_settings(chat_id)
        try:
            await query.edit_message_reply_markup(reply_markup=get_edit_checks_keyboard(new_settings))
        except BadRequest: pass
        await query.answer(f"Warn limit set to {new_val}")
        return

    if data.startswith("set_bio_warn_"):
        action = data.split("_")[-1]
        settings = get_chat_settings(chat_id)
        current = settings.get("bio_link_warn_limit", 3)
        
        if action == "add":
            new_val = min(10, current + 1)
        else:
            new_val = max(1, current - 1)
            
        update_chat_setting(chat_id, "bio_link_warn_limit", new_val)
        new_settings = get_chat_settings(chat_id)
        try:
            await query.edit_message_reply_markup(reply_markup=get_bio_link_settings_keyboard(new_settings))
        except BadRequest: pass
        await query.answer(f"Warn limit set to {new_val}")
        return

    if data.startswith("set_toggle_"):
        key = data.replace("set_toggle_", "")
        settings = get_chat_settings(chat_id)
        
        if key == "warn_penalty":
            # Rotate: ban -> mute -> kick -> ban
            current = settings.get("warn_penalty", "ban")
            new_val = "mute" if current == "ban" else "kick" if current == "mute" else "ban"
            update_chat_setting(chat_id, "warn_penalty", new_val)
        elif key == "command_access":
            # Toggle: all -> admins -> all
            current = settings.get("command_access", "all")
            new_val = "admins" if current == "all" else "all"
            update_chat_setting(chat_id, "command_access", new_val)
        elif key == "banned_words_target":
            # Toggle: members -> everyone -> members
            current = settings.get("banned_words_target", "members")
            new_val = "everyone" if current == "members" else "members"
            update_chat_setting(chat_id, "banned_words_target", new_val)
        elif key == "edit_checks_target":
            # Rotate: members -> everyone -> members
            current = settings.get("edit_checks_target", "members")
            new_val = "everyone" if current == "members" else "members"
            update_chat_setting(chat_id, "edit_checks_target", new_val)
        elif key == "bio_link_target":
            # Rotate: members -> admin -> everyone -> members
            targets = ["members", "admin", "everyone"]
            current = settings.get("bio_link_target", "members")
            next_idx = (targets.index(current) + 1) % len(targets)
            new_val = targets[next_idx]
            update_chat_setting(chat_id, "bio_link_target", new_val)
        elif key == "edit_checks_penalty":
            # Rotate: off -> warn -> mute -> kick -> ban -> off
            penalties = ["off", "warn", "mute", "kick", "ban"]
            current = settings.get("edit_checks_penalty", "off")
            next_idx = (penalties.index(current) + 1) % len(penalties)
            new_val = penalties[next_idx]
            update_chat_setting(chat_id, "edit_checks_penalty", new_val)
        elif key == "bio_link_penalty":
            # Rotate: off -> warn -> mute -> kick -> ban -> off
            penalties = ["off", "warn", "mute", "kick", "ban"]
            current = settings.get("bio_link_penalty", "warn")
            next_idx = (penalties.index(current) + 1) % len(penalties)
            new_val = penalties[next_idx]
            update_chat_setting(chat_id, "bio_link_penalty", new_val)
        elif key == "antiflood_penalty":
            # Rotate: warn -> mute -> kick -> ban -> warn
            penalties = ["warn", "mute", "kick", "ban"]
            current = settings.get("antiflood_penalty", "mute")
            next_idx = (penalties.index(current) + 1) % len(penalties)
            new_val = penalties[next_idx]
            update_chat_setting(chat_id, "antiflood_penalty", new_val)
        elif key == "antiflood_apply_on":
            # Rotate: members -> everyone -> members
            options = ["members", "everyone"]
            current = settings.get("antiflood_apply_on", "members")
            next_idx = (options.index(current) + 1) % len(options)
            new_val = options[next_idx]
            update_chat_setting(chat_id, "antiflood_apply_on", new_val)
        elif key == "link_spam_apply_on":
            # Rotate: members -> admins -> everyone -> members
            options = ["members", "admins", "everyone"]
            current = settings.get("link_spam_apply_on", "members")
            next_idx = (options.index(current) + 1) % len(options)
            new_val = options[next_idx]
            update_chat_setting(chat_id, "link_spam_apply_on", new_val)
        elif key == "bot_protection_apply_on":
            # Rotate: members -> admins -> everyone -> members
            options = ["members", "admins", "everyone"]
            current = settings.get("bot_protection_apply_on", "members")
            next_idx = (options.index(current) + 1) % len(options)
            new_val = options[next_idx]
            update_chat_setting(chat_id, "bot_protection_apply_on", new_val)
        elif key == "forward_protection_apply_on":
            # Rotate: members -> admins -> everyone -> members
            options = ["members", "admins", "everyone"]
            current = settings.get("forward_protection_apply_on", "members")
            next_idx = (options.index(current) + 1) % len(options)
            new_val = options[next_idx]
            update_chat_setting(chat_id, "forward_protection_apply_on", new_val)
        elif key == "emergency_mode":
            # Rotate: daily -> today -> daily
            current = settings.get("emergency_mode", "daily")
            new_val = "today" if current == "daily" else "daily"
            update_chat_setting(chat_id, "emergency_mode", new_val)
        elif key == "blocking_delete_notifications":
            new_val = not settings.get(key, True)
            update_chat_setting(chat_id, key, new_val)
        elif key == "block_reactions":
            new_val = not settings.get(key, False)
            update_chat_setting(chat_id, key, new_val)
            # We no longer call set_chat_available_reactions to ensure the block is silent
            # and handled exclusively by the MessageReactionHandler in blocking_handler.py.
            logging.info(f"Reaction blocking for chat {chat_id} toggled to {new_val}. Silent deletion enabled.")
        elif key == "nightmode_enabled":
            new_val = not settings.get(key, False)
            update_chat_setting(chat_id, key, new_val)
        elif key == "nightmode_restrict_media":
            new_val = not settings.get(key, False)
            update_chat_setting(chat_id, key, new_val)
        elif key == "nightmode_restrict_stickers":
            new_val = not settings.get(key, False)
            update_chat_setting(chat_id, key, new_val)
        elif key == "nightmode_restrict_links":
            new_val = not settings.get(key, False)
            update_chat_setting(chat_id, key, new_val)
        elif key == "nightmode_global_silence":
            new_val = not settings.get(key, False)
            update_chat_setting(chat_id, key, new_val)
        elif key == "nightmode_advise_enabled":
            new_val = not settings.get(key, True)
            update_chat_setting(chat_id, key, new_val)
        elif key == "nightmode_apply_on":
            # Rotate: members -> admins -> everyone -> members
            options = ["members", "admins", "everyone"]
            current = settings.get("nightmode_apply_on", "members")
            next_idx = (options.index(current) + 1) % len(options)
            new_val = options[next_idx]
            update_chat_setting(chat_id, "nightmode_apply_on", new_val)
        else:
            # Welcome, Goodbye, Clean and Auto Delete (specific types) settings enabled by default, others disabled by default
            default_val = True if "welcome_" in key or "goodbye_" in key or "media_enabled" in key or "button_enabled" in key or "clean_" in key or "vc_" in key or "auto_delete_text" in key or "auto_delete_stickers" in key or "auto_delete_media" in key else False
            new_val = not settings.get(key, default_val)
            update_chat_setting(chat_id, key, new_val)
        
        # Handle language toggles
        if key.startswith("lang_"):
            lang_code = key.replace("lang_", "")
            allowed = settings.get("allowed_languages", ["en", "hi", "hinglish"])
            if lang_code in allowed:
                allowed.remove(lang_code)
            else:
                allowed.append(lang_code)
            update_chat_setting(chat_id, "allowed_languages", allowed)
        
        # Refresh current menu
        new_settings = get_chat_settings(chat_id)
        try:
            if "goodbye" in key:
                await query.edit_message_reply_markup(reply_markup=get_goodbye_settings_keyboard(new_settings))
            elif "welcome" in key:
                await query.edit_message_reply_markup(reply_markup=get_welcome_settings_keyboard(new_settings))
            elif "clean" in key: 
                if "pinned" in key:
                    await query.edit_message_reply_markup(reply_markup=get_pinned_message_settings_keyboard(new_settings))
                else:
                    await query.edit_message_reply_markup(reply_markup=get_clean_settings_keyboard(new_settings))
            elif "auto_delete" in key: 
                await query.edit_message_reply_markup(reply_markup=get_auto_delete_settings_keyboard(new_settings))
            elif key.startswith("block_") or key == "blocking_enabled":
                await query.edit_message_reply_markup(reply_markup=get_blocking_settings_keyboard(new_settings))
            elif "warn" in key: 
                await query.edit_message_reply_markup(reply_markup=get_mod_settings_keyboard(new_settings))
            elif "command_deletion" in key: 
                await query.edit_message_reply_markup(reply_markup=get_command_deletion_keyboard(new_settings))
            elif "bot_protection" in key:
                await query.edit_message_reply_markup(reply_markup=get_bot_protection_settings_keyboard(new_settings))
            elif "link_spam" in key:
                await query.edit_message_reply_markup(reply_markup=get_link_spam_settings_keyboard(new_settings))
            elif "forward_protection" in key:
                await query.edit_message_reply_markup(reply_markup=get_forward_protection_settings_keyboard(new_settings))
            elif "language_filter" in key or key.startswith("lang_"):
                await query.edit_message_reply_markup(reply_markup=get_language_filter_settings_keyboard(new_settings))
            elif "command_access" in key: 
                await query.edit_message_reply_markup(reply_markup=get_command_access_keyboard(new_settings))
            elif "banned_words" in key:
                await query.edit_message_reply_markup(reply_markup=get_banned_words_keyboard(new_settings))
            elif "antiflood" in key:
                await query.edit_message_reply_markup(reply_markup=get_antiflood_settings_keyboard(new_settings))
            elif "emergency" in key:
                await query.edit_message_reply_markup(reply_markup=get_emergency_settings_keyboard(new_settings))
            elif "blocking" in key:
                if "notification" in key or "timer" in key or "custom_text" in key:
                    await query.edit_message_reply_markup(reply_markup=get_blocking_notification_settings_keyboard(new_settings))
                else:
                    await query.edit_message_reply_markup(reply_markup=get_blocking_settings_keyboard(new_settings))
            elif "nightmode" in key:
                from datetime import datetime
                import pytz
                timezone_str = new_settings.get("nightmode_timezone", "Asia/Kolkata")
                try: tz = pytz.timezone(timezone_str)
                except: tz = pytz.timezone("Asia/Kolkata")
                now = datetime.now(tz)
                current_time_str = now.strftime("%d %b %Y, %H:%M")
                status_text = "❌ Off"
                if new_settings.get("nightmode_enabled", False):
                    active_actions = []
                    if new_settings.get("nightmode_restrict_media", False): active_actions.append("📸 Medias")
                    if new_settings.get("nightmode_restrict_stickers", False): active_actions.append("🗑 Stickers")
                    if new_settings.get("nightmode_restrict_links", False): active_actions.append("🔗 Links")
                    if new_settings.get("nightmode_global_silence", False): active_actions.append("🤫 Silence")
                    status_text = "\n".join(active_actions) if active_actions else "✅ Active (No restrictions set)"
                start = new_settings.get("nightmode_start", 23)
                end = new_settings.get("nightmode_end", 9)
                advise = "✅" if new_settings.get("nightmode_advise_enabled", True) else "❌"
                apply_on_text = new_settings.get("nightmode_apply_on", "members").capitalize()
                text = (
                    f"🌙 <b>Night mode</b>\n"
                    f"Select the actions you want to limit every night.\n\n"
                    f"<b>Status:</b> {status_text}\n"
                    f" ├ Active from hour <b>{start} to {end}</b>\n"
                    f" ├ Start&End advises: {advise}\n"
                    f" └ Apply on: <b>{apply_on_text}</b>\n\n"
                    f"<b>Current time:</b> {current_time_str}"
                )
                await query.edit_message_text(text, reply_markup=get_nightmode_settings_keyboard(new_settings), parse_mode='HTML')
            elif key.startswith("block_") or key == "blocking_enabled":
                await query.edit_message_reply_markup(reply_markup=get_blocking_settings_keyboard(new_settings))
            elif key.startswith("manager_"):
                await query.edit_message_reply_markup(reply_markup=get_manager_settings_keyboard(new_settings))
            elif "vc_" in key:
                await query.edit_message_reply_markup(reply_markup=get_vc_settings_keyboard(new_settings))
            elif "tagger" in key:
                await query.edit_message_reply_markup(reply_markup=get_tagger_settings_keyboard(new_settings))
            elif "edit_checks" in key:
                await query.edit_message_reply_markup(reply_markup=get_edit_checks_keyboard(new_settings))
            elif "bio_link" in key:
                await query.edit_message_reply_markup(reply_markup=get_bio_link_settings_keyboard(new_settings))
            elif "tagger" in key:
                await query.edit_message_reply_markup(reply_markup=get_tagger_settings_keyboard(new_settings))
        except BadRequest: pass
        await query.answer(f"Setting updated!")
        return

    # Handle command permission toggles
    if data.startswith("set_cmd_perm_"):
        cmd_key = data.replace("set_cmd_perm_", "")
        settings = get_chat_settings(chat_id)
        
        # Rotate: all -> admin -> owner -> all
        current = settings.get(cmd_key, "admin")
        if current == "all":
            new_val = "admin"
        elif current == "admin":
            new_val = "owner"
        else:
            new_val = "all"
        
        update_chat_setting(chat_id, cmd_key, new_val)
        
        # Refresh the command permissions menu
        new_settings = get_chat_settings(chat_id)
        try:
            await query.edit_message_reply_markup(reply_markup=get_command_permissions_keyboard(new_settings))
        except BadRequest: pass
        
        access_label = new_val.title()
        await query.answer(f"Command access set to: {access_label}")
        return

    if data.startswith("set_warn_limit_"):
        action = data.replace("set_warn_limit_", "")
        settings = get_chat_settings(chat_id)
        current = settings.get("warn_limit", 3)
        new_limit = current + 1 if action == "add" else max(1, current - 1)
        update_chat_setting(chat_id, "warn_limit", new_limit)
        new_settings = get_chat_settings(chat_id)
        try:
            await query.edit_message_reply_markup(reply_markup=get_mod_settings_keyboard(new_settings))
        except BadRequest: pass
        await query.answer(f"Warn limit set to {new_limit}")
        return

    if data.startswith("set_msg_length_"):
        val = data.replace("set_msg_length_", "")
        settings = get_chat_settings(chat_id)
        current_limit = settings.get("msg_length_limit", 0)
        
        if val == "plus":
            new_limit = current_limit + 50
        elif val == "minus":
            new_limit = max(0, current_limit - 50)
        else:
            new_limit = max(0, int(val))
            
        update_chat_setting(chat_id, "msg_length_limit", new_limit)
        update_chat_setting(chat_id, "msg_length_max", new_limit)
        update_chat_setting(chat_id, "msg_length_delete", new_limit > 0)
        new_settings = get_chat_settings(chat_id)
        try:
            await query.edit_message_reply_markup(reply_markup=get_msg_length_settings_keyboard(new_settings))
        except BadRequest: pass
        status = f"set to {new_limit} chars" if new_limit > 0 else "disabled"
        await query.answer(f"Message length limit {status}")
        return

    # Handle Warning Deletion Time adjustment buttons
    if data.startswith("set_warn_time_"):
        action, amount = data.replace("set_warn_time_", "").split("_")
        amount = int(amount)
        settings = get_chat_settings(chat_id)
        current_time = settings.get("warning_delete_time", 300)
        new_time = current_time + amount if action == "add" else max(0, current_time - amount)
        update_chat_setting(chat_id, "warning_delete_time", new_time)
        new_settings = get_chat_settings(chat_id)
        try:
            await query.edit_message_reply_markup(reply_markup=get_warning_time_settings_keyboard(new_settings))
        except BadRequest: pass
        await query.answer(f"Warning deletion time set to {new_time}s")
        return

    # Handle Time adjustment buttons
    if data.startswith("set_time_") and not data.startswith("set_time_adj_"):
        action, amount = data.replace("set_time_", "").split("_")
        amount = int(amount)
        settings = get_chat_settings(chat_id)
        current_time = settings.get("auto_delete_time", 60)
        new_time = current_time + amount if action == "add" else max(1, current_time - amount)
        update_chat_setting(chat_id, "auto_delete_time", new_time)
        new_settings = get_chat_settings(chat_id)
        try:
            await query.edit_message_reply_markup(reply_markup=get_auto_delete_settings_keyboard(new_settings))
        except BadRequest: pass
        await query.answer(f"Time updated to {new_time}s")
        return

    # Handle Welcome Time adjustment buttons
    if data.startswith("set_welcome_time_"):
        action, amount = data.replace("set_welcome_time_", "").split("_")
        amount = int(amount)
        settings = get_chat_settings(chat_id)
        current_time = settings.get("welcome_delete_time", 60)
        new_time = current_time + amount if action == "add" else max(0, current_time - amount)
        update_chat_setting(chat_id, "welcome_delete_time", new_time)
        new_settings = get_chat_settings(chat_id)
        try:
            await query.edit_message_reply_markup(reply_markup=get_welcome_settings_keyboard(new_settings))
        except BadRequest: pass
        await query.answer(f"Welcome deletion time set to {new_time}s")
        return

    # Handle Banned Words Warning Time adjustment buttons
    if data.startswith("set_banned_warn_time_"):
        action, amount = data.replace("set_banned_warn_time_", "").split("_")
        amount = int(amount)
        settings = get_chat_settings(chat_id)
        current_time = settings.get("banned_words_warning_delete_time", 60)
        new_time = current_time + amount if action == "add" else max(0, current_time - amount)
        update_chat_setting(chat_id, "banned_words_warning_delete_time", new_time)
        new_settings = get_chat_settings(chat_id)
        try:
            await query.edit_message_reply_markup(reply_markup=get_banned_words_keyboard(new_settings))
        except BadRequest: pass
        await query.answer(f"Warning deletion time set to {new_time}s")
        return

    if data.startswith("set_blocking_timer_"):
        action = data.replace("set_blocking_timer_", "")
        settings = get_chat_settings(chat_id)
        current = settings.get("blocking_notification_timer", 30)
        
        if action == "add_5":
            new_val = min(3600, current + 5)
        else:
            new_val = max(0, current - 5)
            
        update_chat_setting(chat_id, "blocking_notification_timer", new_val)
        new_settings = get_chat_settings(chat_id)
        try:
            await query.edit_message_reply_markup(reply_markup=get_blocking_notification_settings_keyboard(new_settings))
        except BadRequest: pass
        await query.answer(f"Timer set to {new_val}s")
        return

    if data == "set_clear_blocking_custom_text":
        update_chat_setting(chat_id, "blocking_custom_text", None)
        new_settings = get_chat_settings(chat_id)
        try:
            await query.edit_message_reply_markup(reply_markup=get_blocking_notification_settings_keyboard(new_settings))
        except BadRequest: pass
        await query.answer("Custom text cleared!")
        return

    if data.startswith("set_time_adj_"):
        # Format: set_time_adj_{setting_key}_{h+|h-|m+|m-}
        parts = data.split("_")
        action = parts[-1]
        setting_key = "_".join(parts[3:-1])
        
        settings = get_chat_settings(chat_id)
        current_time = settings.get(setting_key, "00:00")
        try:
            h, m = map(int, current_time.split(":"))
        except:
            h, m = 0, 0
            
        if action == "h+": h = (h + 1) % 24
        elif action == "h-": h = (h - 1) % 24
        elif action == "m+": m = (m + 5) % 60
        elif action == "m-": m = (m - 5) % 60
        
        new_time = f"{h:02d}:{m:02d}"
        update_chat_setting(chat_id, setting_key, new_time)
        settings[setting_key] = new_time
        
        try:
            # We also update the message text to show the new time clearly in the title
            title_field = setting_key.replace("emergency_", "").replace("_", " ").title()
            new_text = (
                f"⏰ <b>Set Time: {title_field}</b>\n\n"
                f"ᴄᴜʀʀᴇɴᴛ ᴛɪᴍᴇ: <code>{new_time}</code>\n\n"
                "Use the buttons below to adjust the time or click 'Manual Input' to type it."
            )
            await query.edit_message_text(
                text=new_text,
                reply_markup=get_time_picker_keyboard(settings, setting_key),
                parse_mode='HTML'
            )
        except BadRequest: pass
        await query.answer(f"Time: {new_time}")
        return

    # Handle Anti-Flood adjustment buttons
    if data.startswith("set_antiflood_limit_"):
        action = data.replace("set_antiflood_limit_", "")
        settings = get_chat_settings(chat_id)
        current = settings.get("antiflood_limit", 5)
        new_val = current + 1 if action == "add" else max(1, current - 1)
        update_chat_setting(chat_id, "antiflood_limit", new_val)
        new_settings = get_chat_settings(chat_id)
        try:
            await query.edit_message_reply_markup(reply_markup=get_antiflood_settings_keyboard(new_settings))
        except BadRequest: pass
        await query.answer(f"Flood limit set to {new_val}")
        return

    if data.startswith("set_antiflood_window_"):
        action = data.replace("set_antiflood_window_", "")
        settings = get_chat_settings(chat_id)
        current = settings.get("antiflood_window", 3)
        new_val = current + 1 if action == "add" else max(1, current - 1)
        update_chat_setting(chat_id, "antiflood_window", new_val)
        new_settings = get_chat_settings(chat_id)
        try:
            await query.edit_message_reply_markup(reply_markup=get_antiflood_settings_keyboard(new_settings))
        except BadRequest: pass
        await query.answer(f"Flood window set to {new_val}s")
        return

    # Handle Goodbye Time adjustment buttons
    if data == "set_none":
        await query.answer()
        return

    # Handle Config requests (Set Media, Text, Button)
    if data.startswith("set_config_"):
        config_full = data.replace("set_config_", "")
        
        # Handle welcome button management
        if config_full.startswith("welcome_btn_"):
            parts = config_full.split("_")
            action = parts[2] # manage, remove, modify
            btn_idx = int(parts[3])
            
            settings = get_chat_settings(chat_id)
            welcome_buttons = settings.get("welcome_buttons", [])
            
            if action == "manage":
                if btn_idx < len(welcome_buttons):
                    btn = welcome_buttons[btn_idx]
                    text = (
                        f"🔧 <b>Manage Welcome Button {btn_idx+1}</b>\n\n"
                        f"<b>Text:</b> {btn.get('text')}\n"
                        f"<b>URL:</b> {btn.get('url')}\n\n"
                        "What do you want to do with this button?"
                    )
                    try:
                        await edit_bot_response(query, context, text, reply_markup=get_welcome_button_manage_keyboard(btn_idx), parse_mode='HTML')
                    except BadRequest: pass
                await query.answer()
                return
            
            elif action == "remove":
                if btn_idx < len(welcome_buttons):
                    welcome_buttons.pop(btn_idx)
                    update_chat_setting(chat_id, "welcome_buttons", welcome_buttons)
                    await query.answer("Button removed!")
                    # Go back to welcome settings
                    query.data = "set_view_welcome"
                    await settings_callback(update, context)
                return
            
            elif action == "modtext":
                if btn_idx < len(welcome_buttons):
                    context.user_data["waiting_for_config"] = {
                        "section": "welcome", 
                        "type": "buttons_modtext", 
                        "chat_id": chat_id, 
                        "user_id": user_id,
                        "btn_idx": btn_idx
                    }
                    await query.message.reply_text(f"Please send the NEW text for <b>Button {btn_idx+1}</b>:", parse_mode='HTML')
                    await query.answer("Please send the new text.")
                return

            elif action == "modlink":
                if btn_idx < len(welcome_buttons):
                    context.user_data["waiting_for_config"] = {
                        "section": "welcome", 
                        "type": "buttons_modlink", 
                        "chat_id": chat_id, 
                        "user_id": user_id,
                        "btn_idx": btn_idx
                    }
                    await query.message.reply_text(f"Please send the NEW URL for <b>Button {btn_idx+1}</b>:", parse_mode='HTML')
                    await query.answer("Please send the new link.")
                return

            elif action == "modify":
                if btn_idx < len(welcome_buttons):
                    # We'll use the same input logic but mark it as a modification
                    context.user_data["waiting_for_config"] = {
                        "section": "welcome", 
                        "type": "buttons_modify", 
                        "chat_id": chat_id, 
                        "user_id": user_id,
                        "btn_idx": btn_idx
                    }
                    prompt = (
                        f"Please send the NEW text and URL for <b>Button {btn_idx+1}</b> in this format:\n"
                        "`Button Text | https://t.me/yourlink`"
                    )
                    await query.message.reply_text(prompt, parse_mode='HTML')
                    await query.answer("Please send the new button details.")
                return

        # Handle clear buttons separately
        if config_full.endswith("_clear"):
            section = config_full.replace("_buttons_clear", "")
            update_chat_setting(chat_id, f"{section}_buttons", [])
            update_chat_setting(chat_id, f"{section}_button_text", None)
            update_chat_setting(chat_id, f"{section}_button_url", None)
            await query.answer("All buttons cleared!")
            new_settings = get_chat_settings(chat_id)
            if section == "welcome": await query.edit_message_reply_markup(reply_markup=get_welcome_settings_keyboard(new_settings))
            return

        if "goodbye" in config_full: section = "goodbye"
        elif "welcome" in config_full: section = "welcome"
        elif "auto_delete" in config_full: section = "auto_delete"
        elif "msg_length" in config_full: section = "msg_length"
        elif "group_link" in config_full: section = "group_link"
        elif "banned_words" in config_full: section = "banned_words"
        elif "rules" in config_full: section = "rules"
        elif "emergency" in config_full: section = "emergency"
        elif "blocking" in config_full: section = "blocking"
        else: section = "unknown"
        
        config_type = config_full.replace(f"{section}_", "")
        context.user_data["waiting_for_config"] = {"section": section, "type": config_type, "chat_id": chat_id, "user_id": user_id}
        
        prompt_map = {
            "media": f"Please send the photo you want to use as the {section} media.",
            "group_link": "Please send the link for this group (e.g., https://t.me/joinchat/...).",
            "add": "Please send the word(s) you want to ban. You can send multiple words separated by space or new line.",
            "remove": "Please send the word(s) you want to unban.",
            "text": (
                f"˹{update.effective_user.first_name}˼, send now the message you want to set!\n\n"
                "You can use HTML and:\n"
                "• {ID} = user ID\n"
                "• {NAME} = user name\n"
                "• {SURNAME} = user surname\n"
                "• {NAMESURNAME} = name and surname\n"
                "• {LANG} = user language\n"
                "• {DATE} = current date\n"
                "• {TIME} = current time\n"
                "• {WEEKDAY} = week day\n"
                "• {MENTION} = link to the user profile\n"
                "• {USERNAME} = username\n"
                "• {GROUPNAME} = group name\n"
                "• {RULES} = group regulation\n\n"
                "Send /cancel to stop."
            ),
            "buttons_add": f"Please send the {section} button text and URL in this format:\n`Button Text | https://t.me/yourlink`",
            "button": f"Please send the {section} button text and URL in this format:\n`Button Text | https://t.me/yourlink`",
            "limit": f"Please send the maximum character limit for messages (e.g., 500).",
            "start_time": "Please send the start time for Emergency Mode in HH:MM format (e.g., 09:00).",
            "end_time": "Please send the end time for Emergency Mode in HH:MM format (e.g., 22:00).",
            "custom_text": "Please send the custom message you want to show when content is blocked. Use {NAME} for user name, {ID} for ID."
        }
        
        await edit_bot_response(
            query, context, 
            f"📥 **Configuring {section.title()} {config_type.replace('_', ' ').title()}**\n\n{prompt_map.get(config_type, 'Please send the value.')}", 
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(colored_button("Cancel", "blue"), callback_data=f"set_view_{section}")]]),
            parse_mode="HTML"
        )
        await query.answer()

async def handle_setting_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if "waiting_for_config" not in context.user_data: return
    config_data = context.user_data["waiting_for_config"]
    if update.effective_user.id != config_data["user_id"]: return
    chat_id, section, config_type = config_data["chat_id"], config_data["section"], config_data["type"]
    if update.message.text == "/cancel":
        del context.user_data["waiting_for_config"]
        await update.message.reply_text("Configuration cancelled.")
        return
    success = False
    setting_key = f"{section}_{config_type}"
    if config_type == "media":
        if update.message.photo:
            update_chat_setting(chat_id, setting_key, update.message.photo[-1].file_id)
            update_chat_setting(chat_id, f"{section}_media_type", "photo")
            success = True
        elif update.message.video:
            update_chat_setting(chat_id, setting_key, update.message.video.file_id)
            update_chat_setting(chat_id, f"{section}_media_type", "video")
            success = True
        elif update.message.animation:
            update_chat_setting(chat_id, setting_key, update.message.animation.file_id)
            update_chat_setting(chat_id, f"{section}_media_type", "animation")
            success = True
        elif update.message.document:
            # Check if it's a gif or video
            mime = update.message.document.mime_type
            if mime and ("video" in mime or "gif" in mime or "image" in mime):
                update_chat_setting(chat_id, setting_key, update.message.document.file_id)
                update_chat_setting(chat_id, f"{section}_media_type", "document")
                success = True
    elif config_type == "text":
        if update.message.text:
            # Use text_html to preserve formatting and premium emojis
            text_to_save = update.message.text_html if update.message.text_html else update.message.text
            update_chat_setting(chat_id, setting_key, text_to_save)
            success = True
    elif config_type == "buttons_add":
        if update.message.text and "|" in update.message.text:
            try:
                btn_text, btn_url = [x.strip() for x in update.message.text.split("|", 1)]
                if btn_url.startswith("http"):
                    settings = get_chat_settings(chat_id)
                    current_buttons = settings.get(f"{section}_buttons", [])
                    current_buttons.append({"text": btn_text, "url": btn_url})
                    update_chat_setting(chat_id, f"{section}_buttons", current_buttons)
                    success = True
            except Exception: pass
    elif config_type == "buttons_modify":
        if update.message.text and "|" in update.message.text:
            try:
                btn_text, btn_url = [x.strip() for x in update.message.text.split("|", 1)]
                if btn_url.startswith("http"):
                    btn_idx = config_data.get("btn_idx")
                    settings = get_chat_settings(chat_id)
                    current_buttons = settings.get(f"{section}_buttons", [])
                    if btn_idx is not None and btn_idx < len(current_buttons):
                        current_buttons[btn_idx] = {"text": btn_text, "url": btn_url}
                        update_chat_setting(chat_id, f"{section}_buttons", current_buttons)
                        success = True
            except Exception: pass
    elif config_type == "buttons_modtext":
        if update.message.text:
            try:
                btn_idx = config_data.get("btn_idx")
                settings = get_chat_settings(chat_id)
                current_buttons = settings.get(f"{section}_buttons", [])
                if btn_idx is not None and btn_idx < len(current_buttons):
                    current_buttons[btn_idx]["text"] = update.message.text
                    update_chat_setting(chat_id, f"{section}_buttons", current_buttons)
                    success = True
            except Exception: pass
    elif config_type == "buttons_modlink":
        if update.message.text:
            try:
                btn_url = update.message.text.strip()
                if btn_url.startswith("http"):
                    btn_idx = config_data.get("btn_idx")
                    settings = get_chat_settings(chat_id)
                    current_buttons = settings.get(f"{section}_buttons", [])
                    if btn_idx is not None and btn_idx < len(current_buttons):
                        current_buttons[btn_idx]["url"] = btn_url
                        update_chat_setting(chat_id, f"{section}_buttons", current_buttons)
                        success = True
            except Exception: pass
    elif config_type == "button":
        if update.message.text and "|" in update.message.text:
            try:
                btn_text, btn_url = [x.strip() for x in update.message.text.split("|", 1)]
                if btn_url.startswith("http"):
                    update_chat_setting(chat_id, f"{section}_button_text", btn_text)
                    update_chat_setting(chat_id, f"{section}_button_url", btn_url)
                    success = True
            except Exception: pass
    elif config_type == "time":
        if update.message.text and update.message.text.isdigit():
            update_chat_setting(chat_id, setting_key, int(update.message.text))
            success = True
    elif config_type in ["start_time", "end_time"]:
        if update.message.text and ":" in update.message.text:
            try:
                # Basic validation for HH:MM format
                h, m = map(int, update.message.text.split(":"))
                if 0 <= h <= 23 and 0 <= m <= 59:
                    # Ensure we use the correct key based on section
                    key_to_use = f"{section}_{config_type}" if section != "unknown" else setting_key
                    update_chat_setting(chat_id, key_to_use, update.message.text)
                    success = True
            except (ValueError, TypeError): pass
    elif config_type == "custom_text":
        if update.message.text:
            text_to_save = update.message.text_html if update.message.text_html else update.message.text
            update_chat_setting(chat_id, setting_key, text_to_save)
            success = True
    elif config_type == "limit":
        if section == "msg_length":
            if update.message.text and update.message.text.strip().isdigit():
                new_limit = max(0, int(update.message.text.strip()))
                update_chat_setting(chat_id, "msg_length_limit", new_limit)
                update_chat_setting(chat_id, "msg_length_max", new_limit)
                update_chat_setting(chat_id, "msg_length_delete", new_limit > 0)
                success = True
            elif update.message.text and update.message.text.strip().lower() in ["off", "disable", "none", "0"]:
                update_chat_setting(chat_id, "msg_length_limit", 0)
                update_chat_setting(chat_id, "msg_length_max", 0)
                update_chat_setting(chat_id, "msg_length_delete", False)
                success = True
        elif update.message.text and update.message.text.isdigit():
            update_chat_setting(chat_id, setting_key, int(update.message.text))
            success = True
    elif section == "group_link":
        update_chat_setting(chat_id, "group_link", update.message.text)
        success = True
    elif section == "banned_words":
        settings = get_chat_settings(chat_id)
        current_words = settings.get("banned_words", [])
        new_words = update.message.text.lower().split()
        
        if config_type == "add":
            for word in new_words:
                if word not in current_words:
                    current_words.append(word)
        elif config_type == "remove":
            for word in new_words:
                if word in current_words:
                    current_words.remove(word)
        
        update_chat_setting(chat_id, "banned_words", current_words)
        success = True

    elif section == "rules":
        if config_type == "text":
            update_chat_setting(chat_id, "rules_text", update.message.text)
        elif config_type == "media":
            if update.message.photo:
                update_chat_setting(chat_id, "rules_media", update.message.photo[-1].file_id)
                update_chat_setting(chat_id, "rules_media_type", "photo")
            elif update.message.video:
                update_chat_setting(chat_id, "rules_media", update.message.video.file_id)
                update_chat_setting(chat_id, "rules_media_type", "video")
        elif config_type == "buttons":
            # Expected format: Label1 | url1, Label2 | url2
            lines = update.message.text.split("\n")
            btns = []
            for line in lines:
                if "|" in line:
                    label, url = line.split("|", 1)
                    btns.append({"label": label.strip(), "url": url.strip()})
            update_chat_setting(chat_id, "rules_buttons", btns)
        success = True
    
    if success:
        del context.user_data["waiting_for_config"]
        view_map = {"rules": "regulations_custom", "emergency": "emergency"}
        target_view = view_map.get(section, section)
        await update.message.reply_text(f"✅ {section.replace('_', ' ').title()} updated successfully!", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(colored_button(f"Back to Settings", "blue"), callback_data=f"set_view_{target_view}")]]))
    else:
        await update.message.reply_text("❌ Failed to update setting. Please try again.")

def get_settings_handlers():
    return [
        CommandHandler(["settings", "config"], settings_menu),
        CallbackQueryHandler(open_settings_here, pattern="^settings_open_here_"),
        CallbackQueryHandler(settings_callback, pattern="^set_"),
        CallbackQueryHandler(settings_callback, pattern="^settings_"),
        CallbackQueryHandler(settings_callback, pattern="^mgmt_"),
        CallbackQueryHandler(settings_callback, pattern="^nsfw_"),
        MessageHandler(filters.ALL & ~filters.COMMAND, handle_setting_input)
    ]
