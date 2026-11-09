"""
Interactive High Protection & Security Suite Control Panel
Provides interactive inline buttons for:
- One-click Master Shield Mode
- Anti-Raid velocity join shield
- Anti-Channel sender blocker & banner
- Anti-Mention mass tag shield
- Anti-Duplicate spam flood shield
- Bot Protection & Link Protection shortcuts
"""

import html
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import ContextTypes, CommandHandler, CallbackQueryHandler
from managers.settings_manager_mongo import get_chat_settings, update_chat_setting
from managers.user_manager_mongo import can_user_configure_settings, is_user_admin
from core.config import colored_button

logger = logging.getLogger(__name__)

def render_protection_panel_text(chat_title: str, settings: dict) -> str:
    """Format the high protection overview text."""
    shield_on = settings.get("shield_mode_enabled", False)
    raid_on = settings.get("anti_raid_enabled", True) or shield_on
    chan_on = settings.get("anti_channel_enabled", False) or shield_on
    mention_on = settings.get("mention_spam_enabled", True) or shield_on
    dup_on = settings.get("duplicate_spam_enabled", True) or shield_on
    bot_on = settings.get("bot_protection_enabled", False) or shield_on
    link_on = settings.get("link_spam_protection_enabled", False) or shield_on

    shield_badge = "🟢 <b>ACTIVE (LOCKDOWN)</b>" if shield_on else "⚪ <b>NORMAL</b>"
    r_badge = "🟢 ON" if raid_on else "🔴 OFF"
    c_badge = "🟢 ON" if chan_on else "🔴 OFF"
    m_badge = "🟢 ON" if mention_on else "🔴 OFF"
    d_badge = "🟢 ON" if dup_on else "🔴 OFF"
    b_badge = "🟢 ON" if bot_on else "🔴 OFF"
    l_badge = "🟢 ON" if link_on else "🔴 OFF"

    threshold = settings.get("anti_raid_threshold", 5)
    window = settings.get("anti_raid_window", 10)
    action = settings.get("anti_raid_action", "mute").upper()

    return (
        f"🛡️ <b>─── [ ʜɪɢʜ ᴘʀᴏᴛᴇᴄᴛɪᴏɴ & ꜱʜɪᴇʟᴅ ] ───</b> 🛡️\n\n"
        f"<b>Group:</b> {html.escape(chat_title or 'This Group')}\n"
        f"<b>Master Shield:</b> {shield_badge}\n\n"
        f"• 🚨 <b>Anti-Raid:</b> {r_badge} <i>({threshold} joins/{window}s ➔ {action})</i>\n"
        f"• 📢 <b>Anti-Channel:</b> {c_badge} <i>(Auto-bans sender chats)</i>\n"
        f"• 👥 <b>Anti-Mention:</b> {m_badge} <i>(Max 5 tags per msg)</i>\n"
        f"• 🔁 <b>Anti-Duplicate:</b> {d_badge} <i>(Repetitive spam mute)</i>\n"
        f"• 🤖 <b>Anti-Bot:</b> {b_badge} <i>(Rogue bot auto-kick)</i>\n"
        f"• 🔗 <b>Anti-Link:</b> {l_badge} <i>(Stealth & URL filter)</i>\n\n"
        f"<i>Tap below to toggle individual security modules or engage Master Shield.</i>"
    )

def get_protection_panel_keyboard(chat_id: int, settings: dict) -> InlineKeyboardMarkup:
    """Generate the interactive inline button controls."""
    shield_on = settings.get("shield_mode_enabled", False)
    raid_on = settings.get("anti_raid_enabled", True) or shield_on
    chan_on = settings.get("anti_channel_enabled", False) or shield_on
    mention_on = settings.get("mention_spam_enabled", True) or shield_on
    dup_on = settings.get("duplicate_spam_enabled", True) or shield_on
    bot_on = settings.get("bot_protection_enabled", False) or shield_on
    link_on = settings.get("link_spam_protection_enabled", False) or shield_on

    shield_label = f"🛡️ Master Shield: {'ON 🟢' if shield_on else 'OFF ⚪'}"
    shield_color = "green" if shield_on else "blue"

    keyboard = [
        [InlineKeyboardButton(colored_button(shield_label, shield_color), callback_data=f"prot_toggle_shield:{chat_id}")],
        [
            InlineKeyboardButton(colored_button(f"🚨 Raid: {'ON 🟢' if raid_on else 'OFF 🔴'}", "blue"), callback_data=f"prot_toggle_raid:{chat_id}"),
            InlineKeyboardButton(colored_button(f"📢 Channel: {'ON 🟢' if chan_on else 'OFF 🔴'}", "blue"), callback_data=f"prot_toggle_chan:{chat_id}")
        ],
        [
            InlineKeyboardButton(colored_button(f"👥 Mention: {'ON 🟢' if mention_on else 'OFF 🔴'}", "blue"), callback_data=f"prot_toggle_mention:{chat_id}"),
            InlineKeyboardButton(colored_button(f"🔁 Duplicate: {'ON 🟢' if dup_on else 'OFF 🔴'}", "blue"), callback_data=f"prot_toggle_dup:{chat_id}")
        ],
        [
            InlineKeyboardButton(colored_button(f"🤖 Bot Shield: {'ON 🟢' if bot_on else 'OFF 🔴'}", "blue"), callback_data=f"prot_toggle_bot:{chat_id}"),
            InlineKeyboardButton(colored_button(f"🔗 Link Shield: {'ON 🟢' if link_on else 'OFF 🔴'}", "blue"), callback_data=f"prot_toggle_link:{chat_id}")
        ],
        [
            InlineKeyboardButton(colored_button("⚡ Raid Penalty Action", "blue"), callback_data=f"prot_cycle_action:{chat_id}")
        ],
        [
            InlineKeyboardButton(colored_button("🔙 Settings Menu", "blue"), callback_data="set_view_main"),
            InlineKeyboardButton(colored_button("❌ Close", "red"), callback_data="prot_close")
        ]
    ]
    return InlineKeyboardMarkup(keyboard)

async def protection_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Direct /protection or /security command handler."""
    chat = update.effective_chat
    user = update.effective_user
    if not chat or not user:
        return

    chat_id = chat.id
    if chat.type in ("group", "supergroup"):
        if not await can_user_configure_settings(chat_id, user.id, context):
            await update.message.reply_text("You need administrator permissions with 'Change Group Info' & 'Ban Users' to access security settings.")
            return
    else:
        # PM deep link or fallback
        chat_id = context.user_data.get("settings_chat_id") or chat_id

    settings = get_chat_settings(chat_id)
    title = chat.title if chat.type in ("group", "supergroup") else "Group Security"
    text = render_protection_panel_text(title, settings)
    kb = get_protection_panel_keyboard(chat_id, settings)
    await update.message.reply_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)

async def handle_protection_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle interactive clicks on the protection settings panel."""
    query = update.callback_query
    if not query:
        return
    data = query.data
    user_id = query.from_user.id

    if data == "prot_close":
        try:
            await query.message.delete()
        except Exception:
            pass
        await query.answer()
        return

    if ":" not in data:
        await query.answer()
        return

    action, chat_id_str = data.split(":", 1)
    try:
        chat_id = int(chat_id_str)
    except ValueError:
        await query.answer("Invalid chat ID", show_alert=True)
        return

    # Check permission
    if not await can_user_configure_settings(chat_id, user_id, context):
        await query.answer("You don't have permission to modify group security settings.", show_alert=True)
        return

    settings = get_chat_settings(chat_id)

    if action == "prot_toggle_shield":
        current = settings.get("shield_mode_enabled", False)
        new_val = not current
        update_chat_setting(chat_id, "shield_mode_enabled", new_val)
        if new_val:
            # Engage all protections
            update_chat_setting(chat_id, "anti_raid_enabled", True)
            update_chat_setting(chat_id, "anti_channel_enabled", True)
            update_chat_setting(chat_id, "bot_protection_enabled", True)
            update_chat_setting(chat_id, "link_spam_protection_enabled", True)
            update_chat_setting(chat_id, "forward_protection_enabled", True)
            update_chat_setting(chat_id, "antiflood_enabled", True)
            update_chat_setting(chat_id, "nsfw_filter_enabled", True)
            update_chat_setting(chat_id, "clean_service_enabled", True)
            msg_alert = "🛡️ Master Shield Mode ACTIVATED!"
        else:
            msg_alert = "🛡️ Master Shield Mode DEACTIVATED."
        await query.answer(msg_alert)

    elif action == "prot_toggle_raid":
        new_val = not settings.get("anti_raid_enabled", True)
        update_chat_setting(chat_id, "anti_raid_enabled", new_val)
        await query.answer(f"Anti-Raid {'ENABLED' if new_val else 'DISABLED'}")

    elif action == "prot_toggle_chan":
        new_val = not settings.get("anti_channel_enabled", False)
        update_chat_setting(chat_id, "anti_channel_enabled", new_val)
        await query.answer(f"Anti-Channel {'ENABLED' if new_val else 'DISABLED'}")

    elif action == "prot_toggle_mention":
        new_val = not settings.get("mention_spam_enabled", True)
        update_chat_setting(chat_id, "mention_spam_enabled", new_val)
        await query.answer(f"Anti-Mention {'ENABLED' if new_val else 'DISABLED'}")

    elif action == "prot_toggle_dup":
        new_val = not settings.get("duplicate_spam_enabled", True)
        update_chat_setting(chat_id, "duplicate_spam_enabled", new_val)
        await query.answer(f"Anti-Duplicate {'ENABLED' if new_val else 'DISABLED'}")

    elif action == "prot_toggle_bot":
        new_val = not settings.get("bot_protection_enabled", False)
        update_chat_setting(chat_id, "bot_protection_enabled", new_val)
        await query.answer(f"Bot Protection {'ENABLED' if new_val else 'DISABLED'}")

    elif action == "prot_toggle_link":
        new_val = not settings.get("link_spam_protection_enabled", False)
        update_chat_setting(chat_id, "link_spam_protection_enabled", new_val)
        await query.answer(f"Link Protection {'ENABLED' if new_val else 'DISABLED'}")

    elif action == "prot_cycle_action":
        # Cycle: mute -> kick -> ban -> mute
        curr_act = settings.get("anti_raid_action", "mute")
        acts = ["mute", "kick", "ban"]
        next_act = acts[(acts.index(curr_act) + 1) % len(acts)] if curr_act in acts else "mute"
        update_chat_setting(chat_id, "anti_raid_action", next_act)
        await query.answer(f"Raid Penalty Action set to: {next_act.upper()}")

    # Refresh panel UI
    refreshed_settings = get_chat_settings(chat_id)
    try:
        chat_obj = await context.bot.get_chat(chat_id)
        chat_title = chat_obj.title or "This Group"
    except Exception:
        chat_title = "This Group"

    new_text = render_protection_panel_text(chat_title, refreshed_settings)
    new_kb = get_protection_panel_keyboard(chat_id, refreshed_settings)

    try:
        await query.edit_message_text(new_text, reply_markup=new_kb, parse_mode=ParseMode.HTML)
    except BadRequest:
        pass

def get_protection_panel_handlers():
    """Returns commands and callback handlers for protection panel."""
    return [
        CommandHandler(["protection", "security"], protection_command),
        CallbackQueryHandler(handle_protection_callback, pattern=r"^prot_"),
    ]

