"""
NSFW Safeguard & AI Moderation Module for Doraemon Help Bot.
Ported from extra_bots/NSFW-Detector with full settings panel,
category toggles, sensitivity threshold, whitelist, /scan, and stats.
"""
import logging
import html
import asyncio
from typing import List, Dict, Tuple, Optional
from datetime import datetime

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ChatPermissions
from telegram.ext import ContextTypes, CommandHandler, CallbackQueryHandler, MessageHandler, filters
from telegram.constants import ParseMode, ChatType

from core.config import send_bot_response, edit_bot_response, colored_button, OWNER_ID, log_to_channel
from core.database import get_collection
from managers.settings_manager_mongo import get_chat_settings, update_chat_setting
from managers.user_manager_mongo import is_user_admin
from managers.moderation_manager_mongo import add_warn

# Import engine orchestrator
from moderation.nsfw_engine.pipeline import analyze_text_content, analyze_media_bytes
from moderation.nsfw_engine.schemas import ModerationResult, SeverityLevel

logger = logging.getLogger(__name__)

# ─── Categories & Options ───────────────────────────────────────────────────

ALL_CATEGORIES = [
    ("sexual_nsfw", "🔞 Sexual & Nudity"),
    ("violence_gore", "🩸 Violence & Gore"),
    ("weapons", "🔫 Weapons & Firearms"),
    ("drugs_alcohol", "🍺 Drugs & Alcohol"),
    ("self_harm", "💔 Self Harm"),
    ("hate_extremism", "🤬 Hate & Extremism"),
    ("harassment_abuse", "🚫 Harassment & Abuse"),
]

ACTION_OPTIONS = [
    ("delete", "🗑️ Delete"),
    ("warn", "⚠️ Warn"),
    ("mute", "🔇 Mute (1 hr)"),
    ("ban", "🚫 Ban"),
    ("kick", "👞 Kick"),
    ("log_only", "📝 Log Only"),
]

THRESHOLD_OPTIONS = [
    ("Low (40%) - Very Sensitive", 0.40),
    ("Medium (65%) - Recommended", 0.65),
    ("High (80%) - Strict", 0.80),
    ("Maximum (90%) - Ultra Strict", 0.90),
]

def get_nsfw_stats_col():
    return get_collection("nsfw_stats")


# ─── Settings Keyboards ─────────────────────────────────────────────────────

def get_nsfw_panel_keyboard(chat_id: int, settings: dict) -> InlineKeyboardMarkup:
    """Main NSFW Safeguard settings panel keyboard."""
    enabled = settings.get("nsfw_filter_enabled", False)
    status_str = "🟢 Enabled" if enabled else "🔴 Disabled"
    action = settings.get("nsfw_action", "delete").upper()
    threshold = float(settings.get("nsfw_threshold", 0.65))
    exempt_admins = settings.get("nsfw_exempt_admins", False)
    exempt_str = "🟢 ON" if exempt_admins else "🔴 OFF"

    text_on = settings.get("nsfw_detect_text", True)
    sticker_on = settings.get("nsfw_detect_sticker", True)
    media_on = settings.get("nsfw_detect_media", True)

    keyboard = [
        [
            InlineKeyboardButton(
                colored_button(f"Filter: {status_str}", "green" if enabled else "red"),
                callback_data=f"nsfw_toggle_filter:{chat_id}"
            )
        ],
        [
            InlineKeyboardButton(
                colored_button(f"📝 Text: {'ON' if text_on else 'OFF'}", "green" if text_on else "red"),
                callback_data=f"nsfw_toggle_type:text:{chat_id}"
            ),
            InlineKeyboardButton(
                colored_button(f"🎨 Sticker: {'ON' if sticker_on else 'OFF'}", "green" if sticker_on else "red"),
                callback_data=f"nsfw_toggle_type:sticker:{chat_id}"
            ),
            InlineKeyboardButton(
                colored_button(f"🖼️ Media: {'ON' if media_on else 'OFF'}", "green" if media_on else "red"),
                callback_data=f"nsfw_toggle_type:media:{chat_id}"
            ),
        ],
        [
            InlineKeyboardButton(colored_button("🔧 Detection Categories", "blue"), callback_data=f"nsfw_view_categories:{chat_id}"),
            InlineKeyboardButton(colored_button(f"⚡ Action: {action}", "blue"), callback_data=f"nsfw_view_actions:{chat_id}"),
        ],
        [
            InlineKeyboardButton(colored_button(f"🎚 Sensitivity: {threshold:.0%}", "blue"), callback_data=f"nsfw_view_thresholds:{chat_id}"),
            InlineKeyboardButton(colored_button("📊 Moderation Stats", "blue"), callback_data=f"nsfw_view_stats:{chat_id}"),
        ],
        [
            InlineKeyboardButton(colored_button(f"👮 Exempt Admins: {exempt_str}", "green" if exempt_admins else "default"), callback_data=f"nsfw_toggle_exempt_admins:{chat_id}"),
            InlineKeyboardButton(colored_button("🛡️ Whitelisted Users", "blue"), callback_data=f"nsfw_view_whitelist:{chat_id}"),
        ],
        [
            InlineKeyboardButton(colored_button("🔙 Back to Settings", "blue"), callback_data="set_view_main"),
            InlineKeyboardButton(colored_button("❌ Close", "red"), callback_data="set_close")
        ]
    ]
    return InlineKeyboardMarkup(keyboard)


def get_nsfw_categories_keyboard(chat_id: int, settings: dict) -> InlineKeyboardMarkup:
    """Interactive category toggles keyboard."""
    cats = settings.get("nsfw_categories", {})
    keyboard = []

    for cat_id, cat_name in ALL_CATEGORIES:
        is_on = cats.get(cat_id, True)
        icon = "✅" if is_on else "❌"
        keyboard.append([
            InlineKeyboardButton(
                colored_button(f"{icon} {cat_name}", "green" if is_on else "default"),
                callback_data=f"nsfw_toggle_cat:{cat_id}:{chat_id}"
            )
        ])

    keyboard.append([
        InlineKeyboardButton(colored_button("◀ Back to NSFW Menu", "blue"), callback_data=f"nsfw_view_main:{chat_id}")
    ])
    return InlineKeyboardMarkup(keyboard)


def get_nsfw_actions_keyboard(chat_id: int, settings: dict) -> InlineKeyboardMarkup:
    """Interactive enforcement action selector."""
    current_action = settings.get("nsfw_action", "delete").lower()
    keyboard = []

    for act_id, act_label in ACTION_OPTIONS:
        is_selected = current_action == act_id
        label = f"{'🎯 ' if is_selected else ''}{act_label}"
        keyboard.append([
            InlineKeyboardButton(
                colored_button(label, "green" if is_selected else "blue"),
                callback_data=f"nsfw_set_action:{act_id}:{chat_id}"
            )
        ])

    keyboard.append([
        InlineKeyboardButton(colored_button("◀ Back to NSFW Menu", "blue"), callback_data=f"nsfw_view_main:{chat_id}")
    ])
    return InlineKeyboardMarkup(keyboard)


def get_nsfw_thresholds_keyboard(chat_id: int, settings: dict) -> InlineKeyboardMarkup:
    """Sensitivity threshold selector keyboard."""
    current_thresh = float(settings.get("nsfw_threshold", 0.65))
    keyboard = []

    for label, val in THRESHOLD_OPTIONS:
        is_selected = abs(current_thresh - val) < 0.05
        btn_label = f"{'🎯 ' if is_selected else ''}{label}"
        keyboard.append([
            InlineKeyboardButton(
                colored_button(btn_label, "green" if is_selected else "blue"),
                callback_data=f"nsfw_set_threshold:{val}:{chat_id}"
            )
        ])

    keyboard.append([
        InlineKeyboardButton(colored_button("◀ Back to NSFW Menu", "blue"), callback_data=f"nsfw_view_main:{chat_id}")
    ])
    return InlineKeyboardMarkup(keyboard)


# ─── Panel View Renderers ───────────────────────────────────────────────────

def render_nsfw_panel_text(chat_title: str, settings: dict) -> str:
    enabled = settings.get("nsfw_filter_enabled", False)
    status_icon = "🟢 <b>ACTIVE</b>" if enabled else "🔴 <b>DISABLED</b>"
    action = settings.get("nsfw_action", "delete").upper()
    threshold = float(settings.get("nsfw_threshold", 0.65))
    exempt_admins = settings.get("nsfw_exempt_admins", False)
    exempt_icon = "🟢 <b>YES</b>" if exempt_admins else "🔴 <b>NO</b>"
    cats = settings.get("nsfw_categories", {})

    text_on = settings.get("nsfw_detect_text", True)
    sticker_on = settings.get("nsfw_detect_sticker", True)
    media_on = settings.get("nsfw_detect_media", True)
    scope_line = (
        f"📝 Text ({'🟢' if text_on else '🔴'}) | "
        f"🎨 Sticker ({'🟢' if sticker_on else '🔴'}) | "
        f"🖼️ Media ({'🟢' if media_on else '🔴'})"
    )

    active_cats = [cat_name for cat_id, cat_name in ALL_CATEGORIES if cats.get(cat_id, True)]
    active_count = len(active_cats)

    return (
        f"🔞 <b>AI NSFW & Content Safeguard</b>\n"
        f"<i>Chat: {html.escape(chat_title)}</i>\n\n"
        f"• <b>Status:</b> {status_icon}\n"
        f"• <b>Scan Scope:</b> {scope_line}\n"
        f"• <b>Enforcement Action:</b> <code>{action}</code>\n"
        f"• <b>Sensitivity Threshold:</b> <code>{threshold:.0%}</code>\n"
        f"• <b>Exempt Admins:</b> {exempt_icon}\n"
        f"• <b>Active Protection Categories:</b> <b>{active_count}/7</b>\n\n"
        f"💡 <i>Toggle Text, Sticker, and Media (Photo/GIF/Video) filters individually above.</i>"
    )


# ─── Callback Handler ───────────────────────────────────────────────────────

async def handle_nsfw_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Router for all nsfw_ callback queries."""
    query = update.callback_query
    data = query.data or ""
    parts = data.split(":")
    action_type = parts[0]

    # Resolve target chat_id safely even when configured via private settings panel
    if len(parts) > 1 and parts[-1].lstrip("-").isdigit():
        chat_id = int(parts[-1])
    else:
        chat_id = update.effective_chat.id
    user_id = query.from_user.id

    # Admin check
    if user_id != OWNER_ID and not await is_user_admin(chat_id, user_id, context):
        return await query.answer("⛔ Only admins can manage NSFW Safeguard settings.", show_alert=True)

    chat_title = update.effective_chat.title or "This Chat"
    settings = get_chat_settings(chat_id)

    # 1. Main View
    if action_type in ["nsfw_view_main", "set_view_nsfw", "set_view_nsfw_panel"]:
        text = render_nsfw_panel_text(chat_title, settings)
        await edit_bot_response(query, context, text, reply_markup=get_nsfw_panel_keyboard(chat_id, settings), parse_mode=ParseMode.HTML)
        await query.answer()

    # 2. Toggle Master Switch
    elif action_type == "nsfw_toggle_filter":
        current = settings.get("nsfw_filter_enabled", False)
        new_state = not current
        update_chat_setting(chat_id, "nsfw_filter_enabled", new_state)
        new_settings = get_chat_settings(chat_id)
        text = render_nsfw_panel_text(chat_title, new_settings)
        await edit_bot_response(query, context, text, reply_markup=get_nsfw_panel_keyboard(chat_id, new_settings), parse_mode=ParseMode.HTML)
        await query.answer(f"NSFW Safeguard {'Enabled' if new_state else 'Disabled'}")

    # 2b. Toggle Admin Exemption
    elif action_type == "nsfw_toggle_exempt_admins":
        current = settings.get("nsfw_exempt_admins", False)
        new_state = not current
        update_chat_setting(chat_id, "nsfw_exempt_admins", new_state)
        new_settings = get_chat_settings(chat_id)
        text = render_nsfw_panel_text(chat_title, new_settings)
        await edit_bot_response(query, context, text, reply_markup=get_nsfw_panel_keyboard(chat_id, new_settings), parse_mode=ParseMode.HTML)
        await query.answer(f"Exempt Admins: {'ON' if new_state else 'OFF'}")

    # 2c. Toggle Content Type (Text / Sticker / Media)
    elif action_type == "nsfw_toggle_type":
        type_key_map = {
            "text": ("nsfw_detect_text", "📝 Text Detection"),
            "sticker": ("nsfw_detect_sticker", "🎨 Sticker Detection"),
            "media": ("nsfw_detect_media", "🖼️ Media Detection"),
        }
        content_kind = parts[1] if len(parts) > 1 else ""
        if content_kind not in type_key_map:
            return await query.answer("Unknown content type.", show_alert=True)

        setting_key, label = type_key_map[content_kind]
        current = settings.get(setting_key, True)
        new_state = not current
        update_chat_setting(chat_id, setting_key, new_state)
        new_settings = get_chat_settings(chat_id)
        text = render_nsfw_panel_text(chat_title, new_settings)
        await edit_bot_response(query, context, text, reply_markup=get_nsfw_panel_keyboard(chat_id, new_settings), parse_mode=ParseMode.HTML)
        await query.answer(f"{label}: {'Enabled' if new_state else 'Disabled'}")

    # 3. View Categories
    elif action_type == "nsfw_view_categories":
        text = (
            "🔧 <b>Select Protection Categories</b>\n\n"
            "Tap any category below to toggle detection on or off for this group:"
        )
        await edit_bot_response(query, context, text, reply_markup=get_nsfw_categories_keyboard(chat_id, settings), parse_mode=ParseMode.HTML)
        await query.answer()

    # 4. Toggle Category
    elif action_type == "nsfw_toggle_cat":
        cat_id = parts[1]
        target_chat = int(parts[2])
        cats = dict(settings.get("nsfw_categories", {}))
        current_cat_state = cats.get(cat_id, True)
        cats[cat_id] = not current_cat_state
        update_chat_setting(target_chat, "nsfw_categories", cats)
        new_settings = get_chat_settings(target_chat)
        await edit_bot_response(
            query, context,
            "🔧 <b>Select Protection Categories</b>\n\nTap any category below to toggle detection on or off:",
            reply_markup=get_nsfw_categories_keyboard(target_chat, new_settings),
            parse_mode=ParseMode.HTML
        )
        await query.answer(f"{cat_id.replace('_', ' ').title()}: {'Enabled' if not current_cat_state else 'Disabled'}")

    # 5. View Actions
    elif action_type == "nsfw_view_actions":
        text = (
            "⚡ <b>Select Enforcement Action</b>\n\n"
            "Choose the penalty applied when prohibited content is detected:"
        )
        await edit_bot_response(query, context, text, reply_markup=get_nsfw_actions_keyboard(chat_id, settings), parse_mode=ParseMode.HTML)
        await query.answer()

    # 6. Set Action
    elif action_type == "nsfw_set_action":
        new_action = parts[1]
        target_chat = int(parts[2])
        update_chat_setting(target_chat, "nsfw_action", new_action)
        new_settings = get_chat_settings(target_chat)
        text = render_nsfw_panel_text(chat_title, new_settings)
        await edit_bot_response(query, context, text, reply_markup=get_nsfw_panel_keyboard(target_chat, new_settings), parse_mode=ParseMode.HTML)
        await query.answer(f"Enforcement action set to: {new_action.upper()}")

    # 7. View Thresholds
    elif action_type == "nsfw_view_thresholds":
        text = (
            "🎚 <b>Detection Sensitivity Threshold</b>\n\n"
            "Lower threshold = more sensitive (catches subtle items).\n"
            "Higher threshold = stricter (only flags blatant explicit content)."
        )
        await edit_bot_response(query, context, text, reply_markup=get_nsfw_thresholds_keyboard(chat_id, settings), parse_mode=ParseMode.HTML)
        await query.answer()

    # 8. Set Threshold
    elif action_type == "nsfw_set_threshold":
        new_val = float(parts[1])
        target_chat = int(parts[2])
        update_chat_setting(target_chat, "nsfw_threshold", new_val)
        new_settings = get_chat_settings(target_chat)
        text = render_nsfw_panel_text(chat_title, new_settings)
        await edit_bot_response(query, context, text, reply_markup=get_nsfw_panel_keyboard(target_chat, new_settings), parse_mode=ParseMode.HTML)
        await query.answer(f"Sensitivity threshold set to: {new_val:.0%}")

    # 9. View Whitelist
    elif action_type == "nsfw_view_whitelist":
        whitelist = settings.get("nsfw_whitelist", [])
        if whitelist:
            users_text = "\n".join([f"• <code>{uid}</code>" for uid in whitelist[:20]])
        else:
            users_text = "<i>No users currently whitelisted.</i>"

        text = (
            f"🛡️ <b>NSFW Whitelisted Members</b>\n\n"
            f"Whitelisted members are exempt from NSFW & content filtering.\n\n"
            f"{users_text}\n\n"
            f"💡 <i>Use <code>/whitelist @user</code> to add or <code>/unwhitelist @user</code> to remove.</i>"
        )
        markup = InlineKeyboardMarkup([[
            InlineKeyboardButton(colored_button("◀ Back to NSFW Menu", "blue"), callback_data=f"nsfw_view_main:{chat_id}")
        ]])
        await edit_bot_response(query, context, text, reply_markup=markup, parse_mode=ParseMode.HTML)
        await query.answer()

    # 10. View Stats
    elif action_type == "nsfw_view_stats":
        stats_col = get_nsfw_stats_col()
        doc = stats_col.find_one({"chat_id": chat_id}) if stats_col is not None else None
        total_analyzed = doc.get("total_analyzed", 0) if doc else 0
        total_flagged = doc.get("total_flagged", 0) if doc else 0
        total_deleted = doc.get("total_deleted", 0) if doc else 0

        cats_breakdown = doc.get("categories", {}) if doc else {}
        cat_lines = []
        for cat_id, cat_name in ALL_CATEGORIES:
            cnt = cats_breakdown.get(cat_id, 0)
            if cnt > 0:
                cat_lines.append(f"• {cat_name}: <b>{cnt}</b>")
        cats_text = "\n".join(cat_lines) if cat_lines else "• <i>No violations recorded yet.</i>"

        text = (
            f"📊 <b>NSFW Safeguard Statistics</b>\n"
            f"<i>Chat: {html.escape(chat_title)}</i>\n\n"
            f"• Total Analyzed: <b>{total_analyzed}</b>\n"
            f"• Total Flagged: <b>{total_flagged}</b>\n"
            f"• Messages Deleted: <b>{total_deleted}</b>\n\n"
            f"<b>Violations Breakdown:</b>\n{cats_text}"
        )
        markup = InlineKeyboardMarkup([
            [InlineKeyboardButton(colored_button("🔄 Reset Statistics", "red"), callback_data=f"nsfw_reset_stats:{chat_id}")],
            [InlineKeyboardButton(colored_button("◀ Back to NSFW Menu", "blue"), callback_data=f"nsfw_view_main:{chat_id}")]
        ])
        await edit_bot_response(query, context, text, reply_markup=markup, parse_mode=ParseMode.HTML)
        await query.answer()

    # 11. Reset Stats
    elif action_type == "nsfw_reset_stats":
        stats_col = get_nsfw_stats_col()
        if stats_col is not None:
            stats_col.delete_one({"chat_id": chat_id})
        await query.answer("Stats reset successfully!", show_alert=True)
        # Refresh stats view
        query.data = f"nsfw_view_stats:{chat_id}"
        await handle_nsfw_callback(update, context)


# ─── Standalone Commands ────────────────────────────────────────────────────

async def nsfw_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Command to open the NSFW Safeguard setting panel: /nsfw or /safeguard"""
    chat = update.effective_chat
    user = update.effective_user

    if not chat or chat.type == "private":
        return await update.message.reply_text("ℹ️ Please use this command in a group to configure group moderation.")

    if user.id != OWNER_ID and not await is_user_admin(chat.id, user.id, context):
        return await update.message.reply_text("❌ Only administrators can configure NSFW Safeguard settings.")

    settings = get_chat_settings(chat.id)
    text = render_nsfw_panel_text(chat.title or "This Chat", settings)
    await update.message.reply_text(
        text,
        reply_markup=get_nsfw_panel_keyboard(chat.id, settings),
        parse_mode=ParseMode.HTML
    )


MAX_BOT_API_FILE_BYTES = 19_500_000  # Telegram Bot API get_file hard cap is 20MB


async def _download_file_bytes(context: ContextTypes.DEFAULT_TYPE, file_obj) -> Optional[bytes]:
    """Safely download a Telegram File / PhotoSize / Sticker within Bot API size caps."""
    if not file_obj or not getattr(file_obj, "file_id", None):
        return None
    fsize = getattr(file_obj, "file_size", 0) or 0
    if fsize > MAX_BOT_API_FILE_BYTES:
        logger.info(f"[NSFW] Skipping oversized media download ({fsize} bytes > 19.5MB); falling back to thumbnail.")
        return None
    try:
        bot_file = await context.bot.get_file(file_obj.file_id)
        data = await bot_file.download_as_bytearray()
        return bytes(data) if data else None
    except Exception as exc:
        logger.debug(f"[NSFW] File download failed ({file_obj.file_id}): {exc}")
        return None


async def _analyze_message_target(
    target,
    context: ContextTypes.DEFAULT_TYPE,
    cats_config: dict,
    threshold: float,
    detect_text: bool = True,
    detect_sticker: bool = True,
    detect_media: bool = True,
) -> Optional[ModerationResult]:
    """
    Unified content extraction and NSFW analysis for stickers (static/animated/video),
    photos, GIFs/animations, videos, video notes, image/video/gif documents, and text.
    Respects per-type toggles: detect_text, detect_sticker, detect_media.
    """
    caption = target.caption or ""
    effective_caption = caption if detect_text else ""
    text_content = target.text or caption

    # ── 1. Stickers (Static .webp, Animated .tgs, Video .webm) ──────────────
    if target.sticker:
        if not detect_sticker:
            return None
        sticker = target.sticker
        # Match NSFW-Detector: stylized / smaller artwork uses stricter sensitivity cap (0.45)
        sticker_thresh = min(threshold, 0.45)

        thumb_bytes = None
        if sticker.thumbnail:
            thumb_bytes = await _download_file_bytes(context, sticker.thumbnail)

        if sticker.is_animated:
            # Animated .tgs (Lottie JSON vector) cannot be parsed as pixels -> scan server-rendered thumbnail
            if thumb_bytes:
                return await analyze_media_bytes(thumb_bytes, "sticker", cats_config, threshold=sticker_thresh)
            logger.debug(f"[NSFW] Animated sticker without thumbnail (set={sticker.set_name!r}), skipping pixels.")
            return None

        if sticker.is_video:
            # Video .webm sticker: fast-check thumbnail first, then full .webm multi-frame video scan
            if thumb_bytes:
                thumb_res = await analyze_media_bytes(thumb_bytes, "sticker", cats_config, threshold=sticker_thresh)
                if thumb_res.flagged:
                    return thumb_res
            video_bytes = await _download_file_bytes(context, sticker)
            if video_bytes:
                return await analyze_media_bytes(video_bytes, "video", cats_config, threshold=sticker_thresh)
            return None

        # Static .webp sticker
        media_bytes = await _download_file_bytes(context, sticker)
        if not media_bytes and thumb_bytes:
            media_bytes = thumb_bytes
        if media_bytes:
            return await analyze_media_bytes(media_bytes, "sticker", cats_config, threshold=sticker_thresh)
        return None

    # ── 2. Photos ───────────────────────────────────────────────────────────
    if target.photo:
        if not detect_media:
            if detect_text and caption.strip():
                return await analyze_text_content(caption, cats_config, threshold=threshold)
            return None
        photo = max(target.photo, key=lambda p: p.file_size or 0)
        media_bytes = await _download_file_bytes(context, photo)
        if media_bytes:
            return await analyze_media_bytes(media_bytes, "photo", cats_config, threshold=threshold, caption=effective_caption)
        if detect_text and caption.strip():
            return await analyze_text_content(caption, cats_config, threshold=threshold)
        return None

    # ── 3. Animations / GIFs ────────────────────────────────────────────────
    if target.animation:
        if not detect_media:
            if detect_text and caption.strip():
                return await analyze_text_content(caption, cats_config, threshold=threshold)
            return None
        anim = target.animation
        thumb_bytes = await _download_file_bytes(context, anim.thumbnail) if anim.thumbnail else None
        if thumb_bytes:
            thumb_res = await analyze_media_bytes(thumb_bytes, "photo", cats_config, threshold=threshold, caption=effective_caption)
            if thumb_res.flagged:
                return thumb_res

        media_bytes = await _download_file_bytes(context, anim)
        if media_bytes:
            return await analyze_media_bytes(media_bytes, "animation", cats_config, threshold=threshold, caption=effective_caption)
        if detect_text and caption.strip():
            return await analyze_text_content(caption, cats_config, threshold=threshold)
        return None

    # ── 4. Videos & Video Notes ─────────────────────────────────────────────
    if target.video or target.video_note:
        if not detect_media:
            if detect_text and caption.strip():
                return await analyze_text_content(caption, cats_config, threshold=threshold)
            return None
        vid_obj = target.video or target.video_note
        ct = "video" if target.video else "video_note"
        thumb_bytes = await _download_file_bytes(context, vid_obj.thumbnail) if getattr(vid_obj, "thumbnail", None) else None
        if thumb_bytes:
            thumb_res = await analyze_media_bytes(thumb_bytes, "photo", cats_config, threshold=threshold, caption=effective_caption)
            if thumb_res.flagged:
                return thumb_res

        media_bytes = await _download_file_bytes(context, vid_obj)
        if media_bytes:
            return await analyze_media_bytes(media_bytes, ct, cats_config, threshold=threshold, caption=effective_caption)
        if detect_text and caption.strip():
            return await analyze_text_content(caption, cats_config, threshold=threshold)
        return None

    # ── 5. Media sent as Document (Images, GIFs, Videos) ────────────────────
    if target.document:
        doc = target.document
        mime = (doc.mime_type or "").lower()
        fname = (doc.file_name or "").lower()
        is_gif = mime == "image/gif" or fname.endswith(".gif")
        is_img = (not is_gif) and (mime.startswith("image/") or fname.endswith((".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tiff")))
        is_vid = mime.startswith("video/") or fname.endswith((".mp4", ".webm", ".mkv", ".mov", ".avi", ".m4v"))

        if is_gif or is_img or is_vid:
            if not detect_media:
                if detect_text and caption.strip():
                    return await analyze_text_content(caption, cats_config, threshold=threshold)
                return None
            ct = "gif" if is_gif else ("photo" if is_img else "video")
            thumb_bytes = await _download_file_bytes(context, doc.thumbnail) if doc.thumbnail else None
            if thumb_bytes:
                thumb_res = await analyze_media_bytes(thumb_bytes, "photo", cats_config, threshold=threshold, caption=effective_caption)
                if thumb_res.flagged:
                    return thumb_res
            media_bytes = await _download_file_bytes(context, doc)
            if media_bytes:
                return await analyze_media_bytes(media_bytes, ct, cats_config, threshold=threshold, caption=effective_caption)
            if detect_text and caption.strip():
                return await analyze_text_content(caption, cats_config, threshold=threshold)
            return None

    # ── 6. Plain Text / Caption ─────────────────────────────────────────────
    if text_content and text_content.strip():
        if not detect_text:
            return None
        return await analyze_text_content(text_content, cats_config, threshold=threshold)

    return None


async def scan_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Deep scan replied content and output detailed scores: /scan"""
    msg = update.effective_message
    target = msg.reply_to_message

    if not target:
        return await msg.reply_text(
            "🔍 <b>NSFW & Content Deep Scanner</b>\n\n"
            "Reply to any message (text, photo, sticker, webm, video, video note, GIF, or document) with <code>/scan</code> "
            "to run multi-model analysis and view confidence scores.",
            parse_mode=ParseMode.HTML
        )

    status_msg = await msg.reply_text("🔬 Analyzing content with AI vision & neural lexicons...")

    try:
        cats_map = {c[0]: True for c in ALL_CATEGORIES}
        result = await _analyze_message_target(target, context, cats_map, threshold=0.50)

        if result is None:
            return await status_msg.edit_text("ℹ️ No scannable text, image, sticker, GIF, or video found in the replied message.")

        # Build visual score report
        status_banner = "🔴 <b>FLAGGED (PROHIBITED)</b>" if result.flagged else "🟢 <b>CLEAN (SAFE)</b>"
        sev_label = result.severity.value.upper()

        lines = [
            f"🔍 <b>Moderation Scan Report</b>\n",
            f"• <b>Status:</b> {status_banner}",
            f"• <b>Content Type:</b> <code>{result.content_type.upper()}</code>",
            f"• <b>Severity:</b> <code>{sev_label}</code>",
            f"• <b>Peak Confidence:</b> <code>{result.max_confidence:.1%}</code>\n",
            "<b>Category Confidence Breakdown:</b>"
        ]

        for cat in result.categories:
            pct = cat.confidence * 100
            bar_len = int(pct // 10)
            bar = "█" * bar_len + "░" * (10 - bar_len)
            icon = "⚠️" if cat.triggered else "▫️"
            c_name = cat.category.replace('_', ' ').title()
            detail = f" — <i>{html.escape(cat.reason)}</i>" if cat.reason else ""
            lines.append(f"{icon} <b>{c_name}:</b> {bar} <code>{pct:.1f}%</code>{detail}")

        await status_msg.edit_text("\n".join(lines), parse_mode=ParseMode.HTML)

    except Exception as e:
        logger.error(f"Error executing /scan: {e}")
        await status_msg.edit_text(f"❌ Scan failed: {e}")


async def whitelist_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Add user to NSFW whitelist: /whitelist @user or reply"""
    chat = update.effective_chat
    user = update.effective_user

    if not await is_user_admin(chat.id, user.id, context):
        return await update.message.reply_text("❌ Only administrators can manage the whitelist.")

    target_id = None
    target_name = "User"

    if update.message.reply_to_message:
        target_user = update.message.reply_to_message.from_user
        target_id = target_user.id
        target_name = target_user.first_name
    elif context.args:
        arg = context.args[0].lstrip("@")
        if arg.isdigit():
            target_id = int(arg)
            target_name = f"User {target_id}"

    if not target_id:
        return await update.message.reply_text("⚠️ Usage: Reply to a user's message with <code>/whitelist</code> or send <code>/whitelist &lt;user_id&gt;</code>", parse_mode=ParseMode.HTML)

    settings = get_chat_settings(chat.id)
    wl = list(settings.get("nsfw_whitelist", []))
    if target_id not in wl:
        wl.append(target_id)
        update_chat_setting(chat.id, "nsfw_whitelist", wl)
        await update.message.reply_text(f"✅ Added <b>{html.escape(target_name)}</b> (<code>{target_id}</code>) to NSFW whitelist.", parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text(f"ℹ️ User <code>{target_id}</code> is already whitelisted.", parse_mode=ParseMode.HTML)


async def unwhitelist_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Remove user from NSFW whitelist: /unwhitelist @user or reply"""
    chat = update.effective_chat
    user = update.effective_user

    if not await is_user_admin(chat.id, user.id, context):
        return await update.message.reply_text("❌ Only administrators can manage the whitelist.")

    target_id = None
    if update.message.reply_to_message:
        target_id = update.message.reply_to_message.from_user.id
    elif context.args and context.args[0].lstrip("@").isdigit():
        target_id = int(context.args[0].lstrip("@"))

    if not target_id:
        return await update.message.reply_text("⚠️ Usage: Reply with <code>/unwhitelist</code> or send <code>/unwhitelist &lt;user_id&gt;</code>", parse_mode=ParseMode.HTML)

    settings = get_chat_settings(chat.id)
    wl = list(settings.get("nsfw_whitelist", []))
    if target_id in wl:
        wl.remove(target_id)
        update_chat_setting(chat.id, "nsfw_whitelist", wl)
        await update.message.reply_text(f"🗑️ Removed <code>{target_id}</code> from NSFW whitelist.", parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text(f"ℹ️ User <code>{target_id}</code> was not in the whitelist.", parse_mode=ParseMode.HTML)


async def whitelisted_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """View all whitelisted users: /whitelisted"""
    chat = update.effective_chat
    settings = get_chat_settings(chat.id)
    wl = settings.get("nsfw_whitelist", [])

    if not wl:
        return await update.message.reply_text("🛡️ No users are currently on the NSFW whitelist.")

    lines = ["🛡️ <b>NSFW Whitelisted Users:</b>\n"]
    for uid in wl:
        lines.append(f"• <code>{uid}</code>")

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


# ─── Message Moderation Pipeline Hook ───────────────────────────────────────

def _record_nsfw_stats_sync(chat_id: int, flagged: bool, triggered_cats: dict):
    try:
        stats_col = get_nsfw_stats_col()
        if stats_col is not None:
            inc_dict = {"total_analyzed": 1}
            if flagged:
                inc_dict["total_flagged"] = 1
                inc_dict["total_deleted"] = 1
                for cat_k in triggered_cats:
                    inc_dict[f"categories.{cat_k}"] = 1
            stats_col.update_one({"chat_id": chat_id}, {"$inc": inc_dict}, upsert=True)
    except Exception as e:
        logger.debug(f"[NSFW] Stats update failed: {e}")


async def process_nsfw_moderation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """
    Evaluates every incoming message against the NSFW engine.
    Returns True if message was deleted/handled, False otherwise.
    """
    msg = update.effective_message
    chat = update.effective_chat
    user = update.effective_user

    if not msg or not chat or chat.type == "private":
        return False

    settings = get_chat_settings(chat.id)
    if not settings.get("nsfw_filter_enabled", False):
        return False

    user_id = user.id if user else 0
    is_privileged = bool(user_id == OWNER_ID or (user_id and await is_user_admin(chat.id, user_id, context)))

    # Exempt admins only when admin exemption is explicitly enabled
    if is_privileged and settings.get("nsfw_exempt_admins", False):
        return False

    whitelist = settings.get("nsfw_whitelist", [])
    if user_id and user_id in whitelist:
        return False

    cats_config = settings.get("nsfw_categories", {c[0]: True for c in ALL_CATEGORIES})
    threshold = float(settings.get("nsfw_threshold", 0.65))
    detect_text = settings.get("nsfw_detect_text", True)
    detect_sticker = settings.get("nsfw_detect_sticker", True)
    detect_media = settings.get("nsfw_detect_media", True)

    if not (detect_text or detect_sticker or detect_media):
        return False

    try:
        result = await _analyze_message_target(
            msg,
            context,
            cats_config,
            threshold=threshold,
            detect_text=detect_text,
            detect_sticker=detect_sticker,
            detect_media=detect_media,
        )
        if result is None:
            return False

        # Non-blocking MongoDB stat counters
        asyncio.create_task(
            asyncio.to_thread(_record_nsfw_stats_sync, chat.id, result.flagged, result.triggered_categories)
        )

        if not result.flagged:
            return False

        # Build violation reason
        top_cats = sorted(result.triggered_categories.values(), key=lambda c: c.confidence, reverse=True)
        top_category_name = top_cats[0].category.replace('_', ' ').title() if top_cats else "Prohibited Content"
        top_conf = top_cats[0].confidence if top_cats else result.max_confidence

        logger.info(f"[NSFW ENFORCEMENT] Flagged {result.content_type} in chat {chat.id} from user {user_id}: {top_category_name} ({top_conf:.0%})")

        # Execute penalty action
        action = settings.get("nsfw_action", "delete").lower()
        effective_action = "delete" if (is_privileged and action in ["warn", "mute", "ban", "kick"]) else action

        # Delete message
        if effective_action in ["delete", "warn", "mute", "ban", "kick"]:
            try:
                await msg.delete()
            except Exception as e:
                logger.error(f"Failed to delete NSFW message: {e}")

        # Send alert notification
        user_name = user.first_name if user else "User"
        user_mention = f"<a href='tg://user?id={user_id}'>{html.escape(user_name)}</a>"
        alert_text = (
            f"<blockquote>"
            f"<b><code>[ nsfw detected ]</code></b>\n\n"
            f"<b><code>user: </code></b>{user_mention} [<code>{user_id}</code>]\n"
            f"<b><code>type: </code></b><b><code>{result.content_type}</code></b>\n"
            f"<b><code>category: </code></b><b><code>{top_category_name.lower()}</code></b> [<code>{top_conf:.0%}</code>]\n"
            f"<b><code>action: </code></b><b><code>{effective_action.upper()}</code></b>"
            f"</blockquote>"
        )

        try:
            alert_msg = await context.bot.send_message(
                chat_id=chat.id,
                text=alert_text,
                parse_mode=ParseMode.HTML
            )
            # Auto-delete notification after 5 seconds to keep chat clean
            asyncio.create_task(_delete_alert_after_delay(context.bot, chat.id, alert_msg.message_id, delay=5.0))
        except Exception as e:
            logger.warning(f"Could not send NSFW alert message: {e}")

        # Log to Log Channel (non-blocking)
        try:
            await log_to_channel(
                context,
                f"🔞 NSFW {result.content_type.upper()} detected in <b>{html.escape(chat.title or str(chat.id))}</b>\n"
                f"User: {html.escape(user_name)} (<code>{user_id}</code>)\n"
                f"Category: {top_category_name} ({top_conf:.0%})\n"
                f"Action: {effective_action.upper()}"
            )
        except Exception:
            pass

        # Apply member restriction (only for non-admins/non-owner)
        if not is_privileged and user_id:
            if action == "warn":
                try:
                    await asyncio.to_thread(add_warn, chat.id, user_id, f"NSFW ({top_category_name})")
                except Exception as e:
                    logger.error(f"Failed to record NSFW warn for {user_id}: {e}")
            elif action == "mute":
                try:
                    await context.bot.restrict_chat_member(chat.id, user_id, permissions=ChatPermissions(can_send_messages=False))
                except Exception as e:
                    logger.error(f"Failed to mute user {user_id}: {e}")
            elif action == "ban":
                try:
                    await context.bot.ban_chat_member(chat.id, user_id)
                except Exception as e:
                    logger.error(f"Failed to ban user {user_id}: {e}")
            elif action == "kick":
                try:
                    await context.bot.ban_chat_member(chat.id, user_id)
                    await context.bot.unban_chat_member(chat.id, user_id)
                except Exception as e:
                    logger.error(f"Failed to kick user {user_id}: {e}")

        return True

    except Exception as exc:
        logger.error(f"Error in NSFW moderation pipeline: {exc}", exc_info=True)
        return False


async def _delete_alert_after_delay(bot, chat_id: int, message_id: int, delay: float = 5.0):
    await asyncio.sleep(delay)
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        pass


# ─── Handler Factory ────────────────────────────────────────────────────────

def get_nsfw_handlers() -> List:
    """Returns all command and callback handlers for NSFW module."""
    return [
        CommandHandler(["nsfw", "safeguard", "nsfwsettings"], nsfw_command),
        CommandHandler("scan", scan_command),
        CommandHandler("whitelist", whitelist_command),
        CommandHandler("unwhitelist", unwhitelist_command),
        CommandHandler("whitelisted", whitelisted_command),
        CallbackQueryHandler(handle_nsfw_callback, pattern=r"^nsfw_"),
    ]

