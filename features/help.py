from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, CommandHandler, CallbackQueryHandler
from telegram.constants import ParseMode
from core.config import send_bot_response, edit_bot_response, colored_button
from managers.settings_manager_mongo import get_chat_settings
from managers.user_manager_mongo import is_user_admin

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Main help command."""
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    
    # Check command access
    from managers.settings_manager_mongo import check_command_access
    if not await check_command_access(chat_id, user_id, 'help', context):
        await send_bot_response(update, context, "You don't have permission to use the /help command.")
        return

    keyboard = get_help_keyboard()
    await send_bot_response(
        update, context,
        "📚 <b>Bot Help Menu</b>\n\n"
        "Welcome to the help menu! Select a category below to see available commands and features.",
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard
    )

def get_help_keyboard():
    keyboard = [
        [
            InlineKeyboardButton(colored_button("🛡️ Moderation", "red"), callback_data="help_mod"),
            InlineKeyboardButton(colored_button("⚙️ Settings", "blue"), callback_data="help_settings")
        ],
        [
            InlineKeyboardButton(colored_button("👋 Welcome", "green"), callback_data="help_welcome"),
            InlineKeyboardButton(colored_button("🧹 Clean Service", "default"), callback_data="help_clean")
        ],
        [
            InlineKeyboardButton(colored_button("💣 Auto Delete", "red"), callback_data="help_auto"),
            InlineKeyboardButton(colored_button("🔍 Filters", "blue"), callback_data="help_filters")
        ],
        [
            InlineKeyboardButton(colored_button("🚫 Block Content", "red"), callback_data="help_block"),
            InlineKeyboardButton(colored_button("📌 Pinned", "default"), callback_data="help_pinned")
        ],
        [
            InlineKeyboardButton(colored_button("🤖 Bot Protect", "blue"), callback_data="help_bot_prot"),
            InlineKeyboardButton(colored_button("🧠 AI Chat", "green"), callback_data="help_ai")
        ],
        [
            InlineKeyboardButton(colored_button("🌐 Translator", "default"), callback_data="help_tr"),
            InlineKeyboardButton(colored_button("⚡ Multi-Task", "blue"), callback_data="help_multitask")
        ],
        [
            InlineKeyboardButton(colored_button("🔞 NSFW Guard", "red"), callback_data="help_nsfw"),
            InlineKeyboardButton(colored_button("❌ Close", "red"), callback_data="help_close")
        ]
    ]
    return InlineKeyboardMarkup(keyboard)

async def help_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data

    if data == "help_close":
        await query.message.delete()
        await query.answer()
        return

    help_text = ""
    if data == "help_mod":
        help_text = (
            "🛡️ <b>Moderation Commands</b>\n\n"
            "• /ban - Ban a user (reply, ID, or @username)\n"
            "• /unban - Unban a user\n"
            "• /mute - Mute a user (prevents sending messages)\n"
            "• /unmute - Unmute a user\n"
            "• /warn - Give a user a warning\n"
            "• /unwarn - Reset a user's warnings\n"
            "• /pin - Pin a message (reply only)\n"
            "• /unpin - Unpin a message (reply only) or all pinned messages\n"
            "• /promote - Promote a user to admin with custom perms\n"
            "• /demote - Remove admin rights from a user\n"
            "• /setadmintitle - Set custom admin title (16 chars max)\n"
            "• /deladmintitle - Remove admin title from a user\n"
            "• /muter - Toggle 'Muter' role (can mute/unmute + manage voice chats)\n"
            "• /unmuter - Remove 'Muter' role from a user\n"
            "• /voicechatmgr - Toggle 'Voice Chat Manager' role (for non-muters)\n"
            "• /unvoicechatmgr - Remove 'Voice Chat Manager' role from a user"
        )
    elif data == "help_settings":
        help_text = (
            "⚙️ <b>Settings & Config</b>\n\n"
            "• /config - Open the interactive settings menu\n"
            "• /id - Get your user ID and current chat ID\n\n"
            "<i>Note: Only admins with 'Change Info' and 'Ban Users' can access settings.</i>"
        )
    elif data == "help_welcome":
        help_text = (
            "👋 <b>Welcome Message</b>\n\n"
            "Configure how new members are greeted via /config. Supports:\n"
            "• HTML formatting\n"
            "• Custom Media (Photos)\n"
            "• Custom Buttons (Text | URL)\n"
            "• Placeholders: {ID}, {NAME}, {USERNAME}, {MENTION}, etc.\n\n"
            "<i>Note: Goodbye messages are not supported by this bot.</i>"
        )
    elif data == "help_clean":
        help_text = (
            "🧹 <b>Clean Service</b>\n\n"
            "Automatically deletes system messages:\n"
            "• User Joined/Left\n"
            "• Voice Chat Started/Ended/Scheduled/Invited\n"
            "Enable specific types in /config > Clean Service."
        )
    elif data == "help_pinned":
        help_text = (
            "📌 <b>Pinned Messages</b>\n\n"
            "Automatically deletes 'X pinned a message' service notifications.\n"
            "Enable in /config > Pinned Messages."
        )
    elif data == "help_auto":
        help_text = (
            "💣 <b>Auto Delete</b>\n\n"
            "Automatically deletes every message in the group after a custom delay.\n"
            "• Set delay (H/M/S) in /config > Auto Delete.\n"
            "• Toggle ON/OFF as needed."
        )
    elif data == "help_filters":
        help_text = (
            "🔍 <b>Filters (Auto-Reply)</b>\n\n"
            "• /filter &lt;trigger&gt; &lt;reply&gt; - Set a custom auto-reply\n"
            "• /filters - List all active filters\n"
            "• /stop &lt;trigger&gt; - Remove a specific filter\n"
            "• /stopall - Remove all filters in the chat"
        )
    elif data == "help_block":
        help_text = (
            "🚫 <b>ʙʟᴏᴄᴋ ᴄᴏɴᴛᴇɴᴛ</b>\n\n"
            "• /block - Reply to a message (text/media) or use `/block <text>` to add content to the block list.\n"
            "• /unblock - Reply to a message or use `/unblock <text>` to remove content from the block list.\n"
            "• /blockpack - Reply to a sticker or use `/blockpack <link_or_name>` to block an entire sticker pack.\n"
            "• /unblockpack - Reply to a sticker or use `/unblockpack <link_or_name>` to unblock a sticker pack.\n"
            "• /listblock - List all blocked content (text and sticker packs) in the group.\n"
            "• /unblockall - Clear all blocked content in the chat.\n"
            "• /free - Exempt a user from specific blocking rules (Admins only).\n"
            "• /unfree - Remove all exemptions from a user.\n\n"
            "📏 <b>ᴍᴇꜱꜱᴀɢᴇ ʟᴇɴɢᴛʜ ʟɪᴍɪᴛ</b>\n\n"
            "Configure a maximum character limit for messages via `/maxlength <number>` or in /config > Message Length. Messages exceeding this limit will be automatically deleted.\n"
            "• /maxlength &lt;limit&gt; - Set maximum allowed characters (e.g. /maxlength 200)\n"
            "• /maxlength off - Disable character length limits\n\n"
            "🚧 <b>ʙʟᴏᴄᴋɪɴɢ ꜱᴇᴛᴛɪɴɢꜱ</b>\n\n"
            "Toggle specific content types (Text, Stickers, Links, etc.) in /config > Deleting Messages > Block cancellation.\n\n"
            "<i>Note: Admins are exempt from these checks.</i>"
        )
    elif data == "help_bot_prot":
        help_text = (
            "🤖 <b>Bot Protection</b>\n\n"
            "Prevents other bots from being added to the group.\n"
            "• If enabled, added bots can be removed automatically based on the selected Apply On mode.\n"
            "• Apply On supports Members, Admins, and Everyone.\n"
            "• Admins need both Change Group Info and Ban Users to add bots when the protection applies.\n"
            "• Configure it in /config > Bot Protection.\n\n"
            "<i>Note: This bot itself is exempt.</i>"
        )
    elif data == "help_ai":
        help_text = (
            "🧠 <b>AI Assistant (ChatGPT)</b>\n\n"
            "• /ai &lt;message&gt; - Chat with the AI assistant\n"
            "• /gpt &lt;message&gt; - Alternative AI command\n"
            "• /clearchat - Clear your conversation history\n\n"
            "<i>Note: In private chat, the bot replies to every message automatically!</i>"
        )
    elif data == "help_tr":
        help_text = (
            "🌐 <b>Language Translator</b>\n\n"
            "• /tr &lt;lang&gt; &lt;text&gt; - Translate text to target language\n"
            "• /tr &lt;lang&gt; (reply) - Translate replied message to target language\n"
            "• /tl &lt;text&gt; - Open translation menu for text\n"
            "• /langs - View list of supported language codes\n\n"
            "Example: <code>/tr hi Hello</code> translates 'Hello' to Hindi."
        )
    elif data == "help_multitask":
        help_text = (
            "⚡ <b>Multi-Task & Self ID Tools</b>\n\n"
            "📢 <b>Smart Tagger:</b>\n"
            "• /tag [msg] or /utag [msg] - Tag group members with custom message\n"
            "• /etag [msg] - Tag members decorated with emojis\n"
            "• /admintag [msg] - Tag all group administrators\n"
            "• /ftag [msg] - Fast turbo tagger\n"
            "• /stop or /cancel - Instantly stop any active tagging session\n\n"
            "🤖 <b>Auto-DM Responder:</b>\n"
            "• /automsg on/off - Toggle automated PM responder\n"
            "• /setautomsg [text] - Set custom auto-reply message\n"
            "• /myautomsg - View your active auto-reply\n"
            "• /resetautomsg - Reset auto-reply to default\n\n"
            "💾 <b>SaveIt Media Saver:</b>\n"
            "• /saveit (or /save) - Reply to any disappearing or restricted media to save directly to PM\n"
            "• /autosave on/off - Toggle automatic saving of timed media\n\n"
            "⭐ <b>Favorites & Shortcuts:</b>\n"
            "• /addfav &lt;tag&gt; &lt;text/media&gt; - Save a shortcut\n"
            "• /remfav &lt;tag&gt; - Remove a saved favorite\n"
            "• /favs - List all your saved favorites\n"
            "• /getfav &lt;tag&gt; - Retrieve a saved favorite\n\n"
            "🔐 <b>Account Link:</b>\n"
            "• /login &lt;session&gt; - Link your user string session (in PM)\n"
            "• /myaccount - Check your account connection status\n"
            "• /logout - Unlink your session safely"
        )
    elif data == "help_nsfw":
        help_text = (
            "🔞 <b>AI NSFW & Content Safeguard</b>\n\n"
            "State-of-the-art multi-stage content moderation detecting nudity, weapons, gore, drugs/alcohol, and prohibited content in text, stickers, photos, videos, and GIFs.\n\n"
            "<b>Commands:</b>\n"
            "• <code>/nsfw</code> or <code>/safeguard</code> - Open the full interactive NSFW Safeguard settings panel\n"
            "• <code>/scan</code> - Reply to any message or media to scan and view a detailed confidence report\n"
            "• <code>/whitelist @user</code> - Exempt a user from NSFW scanning\n"
            "• <code>/unwhitelist @user</code> - Remove a user from the whitelist\n"
            "• <code>/whitelisted</code> - View list of whitelisted members\n\n"
            "<b>Interactive Panel Controls:</b>\n"
            "• <b>Master Filter:</b> Toggle ON / OFF\n"
            "• <b>7 Categories:</b> Individually toggle Sexual/Nudity, Weapons, Violence, Drugs/Alcohol, Self Harm, Hate, and Harassment\n"
            "• <b>Actions:</b> Delete, Warn, Mute (1 hr), Ban, Kick, or Log Only\n"
            "• <b>Sensitivity:</b> Low (40%), Medium (65%), High (80%), or Max Strict (90%)\n"
            "• <b>Live Stats:</b> Track total analyzed, flagged, and category breakdown"
        )

    keyboard = [[InlineKeyboardButton(colored_button("🔙 Back", "default"), callback_data="help_main")]]
    
    if data == "help_main":
        await edit_bot_response(
            query, context,
            "📚 <b>Bot Help Menu</b>\n\n"
            "Welcome to the help menu! Select a category below to see available commands and features.",
            parse_mode=ParseMode.HTML,
            reply_markup=get_help_keyboard()
        )
    else:
        await edit_bot_response(
            query, context,
            help_text,
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    await query.answer()

def get_help_handlers():
    return [
        CommandHandler("help", help_command),
        CallbackQueryHandler(help_callback, pattern="^help_")
    ]
