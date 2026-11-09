"""
Ranking, Chat Score, Level-Up Progression & Daily Top 10 Leaderboard for Doraemon Help Bot.

Features:
- Tracks user text messages and calculates Chat Score (XP) per user in groups.
- Level-up progression system with rank titles, badges, and progress bars.
- Instant in-group promotion celebration announcement when a user levels up.
- Daily, Weekly, and Overall Top 10 ranking leaderboards with interactive toggle buttons.
- Automated scheduled daily post of the Top 10 active chatters directly into groups.
- Personal chat statistics via /myrank, /rank, /score, /level.
- Interactive admin settings panel to toggle ranking, daily auto-posts, and level-up alerts.
"""

import html
import time
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any, List, Tuple
from pymongo import ReturnDocument

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import ContextTypes, CommandHandler, CallbackQueryHandler, MessageHandler, filters

from core.database import get_collection, COLLECTIONS
from core.config import colored_button, to_small_caps, delete_message_job, log_to_channel, send_bot_response
from managers.settings_manager_mongo import get_chat_settings, update_chat_setting
from managers.user_manager_mongo import is_user_admin, can_user_configure_settings

logger = logging.getLogger(__name__)

# Anonymous admin ID to ignore
ANONYMOUS_ADMIN_ID = 1087968824

# In-memory cooldowns to prevent spam-farming and alert floods
_USER_MSG_COOLDOWN: Dict[Tuple[int, int], float] = {}       # {(chat_id, user_id): timestamp}
_LEVELUP_ALERT_COOLDOWN: Dict[Tuple[int, int], float] = {}  # {(chat_id, user_id): timestamp}

# Progression Milestones: (level, msg_threshold, xp_threshold, title, badge)
LEVEL_RANKS: List[Tuple[int, int, int, str, str]] = [
    (1, 0, 0, "Novice Chatter", "🌱"),
    (2, 25, 250, "Active Chatter", "💬"),
    (3, 60, 600, "Bronze Speaker", "🥉"),
    (4, 120, 1200, "Silver Orator", "🥈"),
    (5, 200, 2000, "Gold Conversationalist", "🥇"),
    (6, 350, 3500, "Diamond Debater", "💎"),
    (7, 550, 5500, "Chat Master", "👑"),
    (8, 800, 8000, "Group Legend", "⚡"),
    (9, 1200, 12000, "Mythic Voice", "🔥"),
    (10, 1800, 18000, "Supreme Vanguard", "🌟"),
    (11, 2500, 25000, "Celestial Speaker", "🌠"),
    (12, 3500, 35000, "Grand Champion", "🏆"),
    (13, 5000, 50000, "Cosmic Overlord", "🪐"),
    (14, 7500, 75000, "Galactic Immortal", "🌌"),
    (15, 10000, 100000, "God of Chat", "⚜️"),
]

def get_level_info(total_messages: int) -> Tuple[int, str, str, int, int, str, str]:
    """
    Returns:
    (level, rank_title, badge, cur_level_min_msgs, next_level_min_msgs, next_title, next_badge)
    """
    if total_messages >= 10000:
        extra_levels = (total_messages - 10000) // 3000
        level = 15 + extra_levels
        cur_req = 10000 + (extra_levels * 3000)
        next_req = cur_req + 3000
        rank_title = f"God of Chat Tier {extra_levels + 1}"
        next_title = f"God of Chat Tier {extra_levels + 2}"
        return level, rank_title, "⚜️", cur_req, next_req, next_title, "⚜️"

    # Search in milestone table
    for i in range(len(LEVEL_RANKS) - 1, -1, -1):
        lvl, req_msgs, _, title, badge = LEVEL_RANKS[i]
        if total_messages >= req_msgs:
            if i + 1 < len(LEVEL_RANKS):
                _, next_req, _, next_title, next_badge = LEVEL_RANKS[i + 1]
            else:
                next_req = 10000
                next_title = "God of Chat"
                next_badge = "⚜️"
            return lvl, title, badge, req_msgs, next_req, next_title, next_badge

    # Fallback to level 1
    return 1, "Novice Chatter", "🌱", 0, 25, "Active Chatter", "💬"

def render_progress_bar(pct: int, length: int = 10) -> str:
    """Generate visual progress bar [██████░░░░] 60%."""
    pct = max(0, min(100, pct))
    filled = int(round(length * pct / 100))
    empty = length - filled
    return f"[{'█' * filled}{'░' * empty}] {pct}%"

async def handle_ranking_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Tracks text chat activity per user in groups, updates Chat Score (XP),
    and announces Level-Up promotions in the group.
    """
    chat = update.effective_chat
    user = update.effective_user
    msg = update.effective_message

    if not chat or chat.type not in ("group", "supergroup"):
        return
    if not user or user.is_bot or user.id == ANONYMOUS_ADMIN_ID:
        return
    if not msg:
        return

    text = msg.text or msg.caption
    if not text:
        return
    # Ignore bot commands so users cannot farm XP via commands
    if text.strip().startswith('/'):
        return

    # Check group settings
    settings = get_chat_settings(chat.id)
    if not settings.get("ranking_enabled", True):
        return

    # Anti-spam cooldown: 1.5s per user per group
    now = time.time()
    user_key = (chat.id, user.id)
    last_time = _USER_MSG_COOLDOWN.get(user_key, 0.0)
    if now - last_time < 1.5:
        return
    _USER_MSG_COOLDOWN[user_key] = now

    # Calculate XP (10 XP base + 5 XP bonus for messages >= 50 characters)
    xp_gain = 15 if len(text.strip()) >= 50 else 10

    # Date identifiers (UTC)
    now_utc = datetime.now(timezone.utc)
    today_str = now_utc.strftime("%Y-%m-%d")
    week_str = now_utc.strftime("%Y-W%W")

    try:
        rankings_col = get_collection(COLLECTIONS.get("rankings", "chat_rankings"))
        if rankings_col is None:
            return

        # Atomically increment counts and fetch updated document
        doc = rankings_col.find_one_and_update(
            {"chat_id": chat.id, "user_id": user.id},
            {
                "$inc": {
                    f"daily_counts.{today_str}": 1,
                    f"weekly_counts.{week_str}": 1,
                    "total_messages": 1,
                    "total_xp": xp_gain
                },
                "$set": {
                    "first_name": user.first_name or "Chatter",
                    "username": user.username,
                    "last_active": now_utc
                },
                "$setOnInsert": {
                    "level": 1,
                    "rank_title": "🌱 Novice Chatter"
                }
            },
            upsert=True,
            return_document=ReturnDocument.AFTER
        )

        if not doc:
            return

        old_level = doc.get("level", 1)
        total_msgs = doc.get("total_messages", 1)
        total_xp = doc.get("total_xp", xp_gain)

        # Check for Level-Up
        new_level, new_title, new_badge, cur_req, next_req, next_title, next_badge = get_level_info(total_msgs)

        if new_level > old_level:
            # Update new level and title in database
            rankings_col.update_one(
                {"chat_id": chat.id, "user_id": user.id},
                {"$set": {"level": new_level, "rank_title": f"{new_badge} {new_title}"}}
            )

            # Check if group has level-up alerts enabled
            if settings.get("level_up_alerts", True):
                # Alert cooldown: 30s per user to prevent duplicate spam
                last_alert = _LEVELUP_ALERT_COOLDOWN.get(user_key, 0.0)
                if now - last_alert >= 30.0:
                    _LEVELUP_ALERT_COOLDOWN[user_key] = now

                    user_mention = f'<a href="tg://user?id={user.id}">{html.escape(user.first_name or "Chatter")}</a>'
                    alert_text = (
                        f"🎉 <b>─── [ ʟᴇᴠᴇʟ ᴜᴘ &amp; ᴘʀᴏᴍᴏᴛɪᴏɴ ] ───</b> 🎉\n\n"
                        f"👤 <b>ᴜsᴇʀ:</b> {user_mention}\n"
                        f"🎖️ <b>ɴᴇᴡ ʀᴀɴᴋ:</b> {new_badge} <b>{new_title}</b> (ʟᴇᴠᴇʟ {new_level})\n"
                        f"💬 <b>ᴛᴏᴛᴀʟ ᴍᴇssᴀɢᴇs:</b> <b>{total_msgs}</b>\n"
                        f"🌟 <b>ᴄʜᴀᴛ sᴄᴏʀᴇ:</b> <b>{total_xp}</b> xᴘ\n"
                        f"🎯 <b>ɴᴇxᴛ ɢᴏᴀʟ:</b> {next_badge} {next_title} ({next_req} ᴍsɢs)\n\n"
                        f"✨ <i>ᴋᴇᴇᴘ ᴄʜᴀᴛᴛɪɴɢ ᴛᴏ ᴄʟɪᴍʙ ᴛʜᴇ ʟᴇᴀᴅᴇʀʙᴏᴀʀᴅ &amp; ᴜɴʟᴏᴄᴋ ɴᴇᴡ ʀᴀɴᴋs!</i>"
                    )

                    keyboard = InlineKeyboardMarkup([
                        [
                            InlineKeyboardButton(colored_button("🏆 Leaderboard", "blue"), callback_data=f"lb_view:daily:{chat.id}"),
                            InlineKeyboardButton(colored_button("👤 My Rank", "green"), callback_data=f"lb_myrank:{chat.id}")
                        ]
                    ])

                    try:
                        promo_msg = await context.bot.send_message(
                            chat_id=chat.id,
                            text=alert_text,
                            parse_mode=ParseMode.HTML,
                            reply_markup=keyboard,
                            disable_web_page_preview=True
                        )

                        # Auto-delete notification after 45s to keep chat clean
                        if context.job_queue and promo_msg:
                            context.job_queue.run_once(
                                delete_message_job,
                                45,
                                data={"chat_id": chat.id, "message_id": promo_msg.message_id}
                            )
                    except Exception as e:
                        logger.debug(f"[RANKING] Failed to send level up alert: {e}")

    except Exception as e:
        logger.error(f"[RANKING] Error updating chat ranking for user {user.id} in {chat.id}: {e}")

def get_leaderboard_keyboard(chat_id: int, current_period: str) -> InlineKeyboardMarkup:
    """Build interactive buttons with visual indicator for selected timeframe."""
    daily_label = "📅 Daily 🟢" if current_period == "daily" else "📅 Daily"
    weekly_label = "📆 Weekly 🟢" if current_period == "weekly" else "📆 Weekly"
    overall_label = "🏆 Overall 🟢" if current_period == "overall" else "🏆 Overall"

    keyboard = [
        [
            InlineKeyboardButton(colored_button(daily_label, "green" if current_period == "daily" else "default"), callback_data=f"lb_view:daily:{chat_id}"),
            InlineKeyboardButton(colored_button(weekly_label, "blue" if current_period == "weekly" else "default"), callback_data=f"lb_view:weekly:{chat_id}"),
            InlineKeyboardButton(colored_button(overall_label, "red" if current_period == "overall" else "default"), callback_data=f"lb_view:overall:{chat_id}")
        ],
        [
            InlineKeyboardButton(colored_button("👤 My Rank", "blue"), callback_data=f"lb_myrank:{chat_id}"),
            InlineKeyboardButton(colored_button("🔄 Refresh", "default"), callback_data=f"lb_refresh:{current_period}:{chat_id}")
        ],
        [
            InlineKeyboardButton(colored_button("❌ Close", "red"), callback_data="lb_close")
        ]
    ]
    return InlineKeyboardMarkup(keyboard)

def render_leaderboard(chat_id: int, chat_title: str, period: str = "daily", target_date: Optional[str] = None) -> Tuple[str, InlineKeyboardMarkup]:
    """
    Fetches Top 10 users for the chosen timeframe and renders the formatted leaderboard text and buttons.
    """
    now_utc = datetime.now(timezone.utc)
    today_str = target_date or now_utc.strftime("%Y-%m-%d")
    week_str = now_utc.strftime("%Y-W%W")

    rankings_col = get_collection(COLLECTIONS.get("rankings", "chat_rankings"))
    top_users = []

    period_badge = "📅 ᴅᴀɪʟʏ (ᴛᴏᴅᴀʏ)"
    if period == "daily":
        period_badge = f"📅 ᴅᴀɪʟʏ ({today_str})"
        if rankings_col is not None:
            query = {"chat_id": chat_id, f"daily_counts.{today_str}": {"$gt": 0}}
            top_users = list(rankings_col.find(query).sort(f"daily_counts.{today_str}", -1).limit(10))
    elif period == "weekly":
        period_badge = f"📆 ᴡᴇᴇᴋʟʏ ({week_str})"
        if rankings_col is not None:
            query = {"chat_id": chat_id, f"weekly_counts.{week_str}": {"$gt": 0}}
            top_users = list(rankings_col.find(query).sort(f"weekly_counts.{week_str}", -1).limit(10))
    else: # overall
        period_badge = "🏆 ᴏᴠᴇʀᴀʟʟ (ᴀʟʟ-ᴛɪᴍᴇ)"
        if rankings_col is not None:
            query = {"chat_id": chat_id, "total_messages": {"$gt": 0}}
            top_users = list(rankings_col.find(query).sort("total_messages", -1).limit(10))

    header = (
        f"🏆 <b>─── [ ɢʀᴏᴜᴘ ᴄʜᴀᴛ ʟᴇᴀᴅᴇʀʙᴏᴀʀᴅ ] ───</b> 🏆\n\n"
        f"📍 <b>ɢʀᴏᴜᴘ:</b> {html.escape(chat_title or 'This Group')}\n"
        f"⏱ <b>ᴛɪᴍᴇғʀᴀᴍᴇ:</b> {period_badge}\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
    )

    medals = {1: "🥇", 2: "🥈", 3: "🥉"}

    if not top_users:
        body = (
            f"<i>ɴᴏ ᴍᴇssᴀɢᴇs ʀᴇᴄᴏʀᴅᴇᴅ ғᴏʀ ᴛʜɪs ᴘᴇʀɪᴏᴅ ʏᴇᴛ.\n"
            f"sᴛᴀʀᴛ ᴄʜᴀᴛᴛɪɴɢ ɴᴏᴡ ᴛᴏ ᴛᴀᴋᴇ #1 🥇!</i>\n"
        )
    else:
        lines = []
        for rank, user_doc in enumerate(top_users, start=1):
            name = html.escape(user_doc.get("first_name") or "Chatter")
            uid = user_doc.get("user_id")
            lvl = user_doc.get("level", 1)

            if period == "daily":
                count = user_doc.get("daily_counts", {}).get(today_str, 0)
            elif period == "weekly":
                count = user_doc.get("weekly_counts", {}).get(week_str, 0)
            else:
                count = user_doc.get("total_messages", 0)

            medal_or_num = medals.get(rank, f"<b>{rank}.</b>")
            link = f'<a href="tg://user?id={uid}">{name}</a>'
            lines.append(f"{medal_or_num} {link} — <b>{count:,}</b> ᴍsɢs (ʟᴠʟ {lvl})")
        body = "\n".join(lines) + "\n"

    footer = (
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"💬 <i>ᴋᴇᴇᴘ ᴄʜᴀᴛᴛɪɴɢ ᴛᴏ ᴄʟɪᴍʙ ᴛʜᴇ ʀᴀɴᴋɪɴɢs &amp; ʟᴇᴠᴇʟ ᴜᴘ!</i>"
    )

    full_text = header + body + footer
    keyboard = get_leaderboard_keyboard(chat_id, period)
    return full_text, keyboard

async def ranking_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Command /ranking, /leaderboard, /top: Displays group leaderboard."""
    chat = update.effective_chat
    if not chat or chat.type not in ("group", "supergroup"):
        await send_bot_response(update, context, "⚠️ This command can only be used in groups.", parse_mode=ParseMode.HTML)
        return

    text, keyboard = render_leaderboard(chat.id, chat.title or "This Group", period="daily")
    try:
        await update.message.reply_text(
            text=text,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
            disable_web_page_preview=True
        )
    except Exception as e:
        logger.error(f"[RANKING] Failed to send leaderboard: {e}")

async def myrank_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Command /myrank, /rank, /score, /level: Displays user's chat score, level & rank position."""
    chat = update.effective_chat
    user = update.effective_user
    if not chat or chat.type not in ("group", "supergroup"):
        await send_bot_response(update, context, "⚠️ This command can only be used in groups.", parse_mode=ParseMode.HTML)
        return
    if not user:
        return

    now_utc = datetime.now(timezone.utc)
    today_str = now_utc.strftime("%Y-%m-%d")
    week_str = now_utc.strftime("%Y-W%W")

    rankings_col = get_collection(COLLECTIONS.get("rankings", "chat_rankings"))
    user_doc = None
    if rankings_col is not None:
        user_doc = rankings_col.find_one({"chat_id": chat.id, "user_id": user.id})

    total_msgs = user_doc.get("total_messages", 0) if user_doc else 0
    total_xp = user_doc.get("total_xp", 0) if user_doc else 0
    today_msgs = user_doc.get("daily_counts", {}).get(today_str, 0) if user_doc else 0
    week_msgs = user_doc.get("weekly_counts", {}).get(week_str, 0) if user_doc else 0

    lvl, title, badge, cur_req, next_req, next_title, next_badge = get_level_info(total_msgs)

    # Calculate overall rank position in this chat
    rank_pos = 1
    total_chatters = 1
    if rankings_col is not None:
        total_chatters = max(1, rankings_col.count_documents({"chat_id": chat.id, "total_messages": {"$gt": 0}}))
        if total_msgs > 0:
            rank_pos = rankings_col.count_documents({"chat_id": chat.id, "total_messages": {"$gt": total_msgs}}) + 1
        else:
            rank_pos = total_chatters

    # Progress to next level
    needed = max(0, next_req - total_msgs)
    span = max(1, next_req - cur_req)
    progress_pct = min(100, int(round((total_msgs - cur_req) / span * 100))) if next_req > cur_req else 100
    bar_str = render_progress_bar(progress_pct, length=10)

    user_link = f'<a href="tg://user?id={user.id}">{html.escape(user.first_name or "Chatter")}</a>'
    stats_text = (
        f"👤 <b>─── [ ʏᴏᴜʀ ᴄʜᴀᴛ sᴛᴀᴛɪsᴛɪᴄs ] ───</b> 👤\n\n"
        f"📍 <b>ɢʀᴏᴜᴘ:</b> {html.escape(chat.title or 'This Group')}\n"
        f"👤 <b>ᴜsᴇʀ:</b> {user_link}\n"
        f"🎖️ <b>ʀᴀɴᴋ:</b> {badge} <b>{title}</b> (ʟᴇᴠᴇʟ {lvl})\n"
        f"📊 <b>ɢʀᴏᴜᴘ ʀᴀɴᴋ:</b> #{rank_pos} ᴏғ {total_chatters} ᴄʜᴀᴛᴛᴇʀs\n\n"
        f"💬 <b>ᴛᴏᴛᴀʟ ᴍᴇssᴀɢᴇs:</b> <b>{total_msgs:,}</b>\n"
        f"📅 <b>ᴛᴏᴅᴀʏ:</b> <b>{today_msgs:,}</b> ᴍsɢs\n"
        f"📆 <b>ᴛʜɪs ᴡᴇᴇᴋ:</b> <b>{week_msgs:,}</b> ᴍsɢs\n"
        f"🌟 <b>ᴄʜᴀᴛ sᴄᴏʀᴇ:</b> <b>{total_xp:,}</b> xᴘ\n\n"
        f"🎯 <b>ᴘʀᴏɢʀᴇss ᴛᴏ ʟᴇᴠᴇʟ {lvl + 1} ({next_title}):</b>\n"
        f"{bar_str} <i>({needed} ᴍsɢs ʟᴇғᴛ)</i>\n"
    )

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(colored_button("🏆 Group Leaderboard", "blue"), callback_data=f"lb_view:daily:{chat.id}"),
            InlineKeyboardButton(colored_button("❌ Close", "red"), callback_data="lb_close")
        ]
    ])

    try:
        await update.message.reply_text(
            text=stats_text,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
            disable_web_page_preview=True
        )
    except Exception as e:
        logger.error(f"[RANKING] Failed to send myrank: {e}")

async def post_daily_top_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command /posttop, /sendtop, /dailytop: Broadcasts Daily Top 10 to group immediately."""
    chat = update.effective_chat
    user = update.effective_user
    if not chat or chat.type not in ("group", "supergroup"):
        await send_bot_response(update, context, "⚠️ This command can only be used in groups.", parse_mode=ParseMode.HTML)
        return

    if not await is_user_admin(chat.id, user.id, context):
        await send_bot_response(update, context, "⚠️ Only group admins can use this command.", parse_mode=ParseMode.HTML)
        return

    now_utc = datetime.now(timezone.utc)
    today_str = now_utc.strftime("%Y-%m-%d")

    text, keyboard = render_leaderboard(chat.id, chat.title or "This Group", period="daily", target_date=today_str)
    banner_text = (
        f"🌟 <b>─── [ ᴅᴀɪʟʏ ᴛᴏᴘ 10 ᴀᴄᴛɪᴠᴇ ᴄʜᴀᴛᴛᴇʀs! ] ───</b> 🌟\n\n"
        + text.split("━━━━━━━━━━━━━━━━━━━━━\n", 1)[-1]
    )
    banner_full = (
        f"🌟 <b>─── [ ᴅᴀɪʟʏ ᴛᴏᴘ 10 ᴀᴄᴛɪᴠᴇ ᴄʜᴀᴛᴛᴇʀs! ] ───</b> 🌟\n\n"
        f"📍 <b>ɢʀᴏᴜᴘ:</b> {html.escape(chat.title or 'This Group')}\n"
        f"📅 <b>ᴅᴀᴛᴇ:</b> <b>{today_str}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        + text.split("━━━━━━━━━━━━━━━━━━━━━\n", 1)[1]
    )

    try:
        await context.bot.send_message(
            chat_id=chat.id,
            text=banner_full,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
            disable_web_page_preview=True
        )
        await log_to_channel(context, f"📢 Daily Top 10 posted manually in <b>{html.escape(chat.title)}</b> by {user.first_name}")
    except Exception as e:
        logger.error(f"[RANKING] Failed to post daily top 10: {e}")

async def handle_leaderboard_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles inline buttons: lb_view, lb_refresh, lb_myrank, lb_close."""
    query = update.callback_query
    if not query or not query.data:
        return

    data = query.data
    chat = update.effective_chat
    user = update.effective_user

    # 1. Close
    if data == "lb_close":
        try:
            await query.message.delete()
        except Exception:
            await query.answer("Could not delete message.", show_alert=False)
        await query.answer()
        return

    # 2. View switch: lb_view:<period>:<chat_id>
    if data.startswith("lb_view:"):
        parts = data.split(":")
        if len(parts) >= 3:
            period = parts[1]
            cid = int(parts[2])
            title = chat.title if chat else "This Group"
            new_text, new_kb = render_leaderboard(cid, title, period=period)
            try:
                await query.edit_message_text(
                    text=new_text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=new_kb,
                    disable_web_page_preview=True
                )
            except BadRequest:
                pass
            await query.answer()
        return

    # 3. Refresh: lb_refresh:<period>:<chat_id>
    if data.startswith("lb_refresh:"):
        parts = data.split(":")
        if len(parts) >= 3:
            period = parts[1]
            cid = int(parts[2])
            title = chat.title if chat else "This Group"
            new_text, new_kb = render_leaderboard(cid, title, period=period)
            try:
                await query.edit_message_text(
                    text=new_text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=new_kb,
                    disable_web_page_preview=True
                )
            except BadRequest:
                pass
            await query.answer("🔄 Leaderboard refreshed!", show_alert=False)
        return

    # 4. My Rank pop-up: lb_myrank:<chat_id>
    if data.startswith("lb_myrank:"):
        parts = data.split(":")
        cid = int(parts[1]) if len(parts) >= 2 else (chat.id if chat else 0)

        now_utc = datetime.now(timezone.utc)
        today_str = now_utc.strftime("%Y-%m-%d")
        week_str = now_utc.strftime("%Y-W%W")

        rankings_col = get_collection(COLLECTIONS.get("rankings", "chat_rankings"))
        user_doc = None
        if rankings_col is not None and user:
            user_doc = rankings_col.find_one({"chat_id": cid, "user_id": user.id})

        total_msgs = user_doc.get("total_messages", 0) if user_doc else 0
        total_xp = user_doc.get("total_xp", 0) if user_doc else 0
        today_msgs = user_doc.get("daily_counts", {}).get(today_str, 0) if user_doc else 0
        week_msgs = user_doc.get("weekly_counts", {}).get(week_str, 0) if user_doc else 0

        lvl, title, badge, _, next_req, next_title, _ = get_level_info(total_msgs)

        rank_pos = 1
        total_chatters = 1
        if rankings_col is not None:
            total_chatters = max(1, rankings_col.count_documents({"chat_id": cid, "total_messages": {"$gt": 0}}))
            if total_msgs > 0:
                rank_pos = rankings_col.count_documents({"chat_id": cid, "total_messages": {"$gt": total_msgs}}) + 1
            else:
                rank_pos = total_chatters

        alert_msg = (
            f"👤 {user.first_name if user else 'Chatter'}\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"📊 Rank: #{rank_pos} of {total_chatters} members\n"
            f"🎖️ Level: {lvl} ({badge} {title})\n"
            f"💬 Total Msgs: {total_msgs:,}\n"
            f"📅 Today: {today_msgs:,} | 📆 Week: {week_msgs:,}\n"
            f"🌟 Chat Score: {total_xp:,} XP\n"
            f"🎯 Next Rank: {next_title} ({next_req} msgs)"
        )
        await query.answer(alert_msg, show_alert=True)
        return

async def check_daily_leaderboard_job(context: ContextTypes.DEFAULT_TYPE):
    """
    Automated background job running every 10 minutes:
    At UTC midnight (00:00 - 00:30 UTC), finds the Top 10 users for yesterday in each group
    and sends the Daily Top 10 Leaderboard directly into the group!
    """
    now_utc = datetime.now(timezone.utc)
    # Check if within midnight post window (hour 0)
    if now_utc.hour != 0:
        return

    yesterday_str = (now_utc - timedelta(days=1)).strftime("%Y-%m-%d")

    from managers.settings_manager_mongo import get_all_chat_settings
    all_settings = get_all_chat_settings()

    rankings_col = get_collection(COLLECTIONS.get("rankings", "chat_rankings"))
    meta_col = get_collection(COLLECTIONS.get("ranking_meta", "ranking_meta"))

    if rankings_col is None or meta_col is None:
        return

    for chat_id_str, settings in all_settings.items():
        try:
            chat_id = int(chat_id_str)
        except ValueError:
            continue

        if not settings.get("ranking_enabled", True):
            continue
        if not settings.get("daily_leaderboard_auto_post", True):
            continue

        # Check if already posted for yesterday
        meta_doc = meta_col.find_one({"chat_id": chat_id})
        if meta_doc and meta_doc.get("last_daily_recap_date") == yesterday_str:
            continue

        # Fetch Top 10 for yesterday
        query = {"chat_id": chat_id, f"daily_counts.{yesterday_str}": {"$gt": 0}}
        top_users = list(rankings_col.find(query).sort(f"daily_counts.{yesterday_str}", -1).limit(10))

        if not top_users:
            # No messages recorded yesterday, mark as checked and skip
            meta_col.update_one({"chat_id": chat_id}, {"$set": {"last_daily_recap_date": yesterday_str}}, upsert=True)
            continue

        # Try getting chat title
        chat_title = "This Group"
        try:
            chat_obj = await context.bot.get_chat(chat_id)
            if chat_obj and chat_obj.title:
                chat_title = chat_obj.title
        except Exception:
            pass

        medals = {1: "🥇", 2: "🥈", 3: "🥉"}
        lines = []
        for rank, udoc in enumerate(top_users, start=1):
            name = html.escape(udoc.get("first_name") or "Chatter")
            uid = udoc.get("user_id")
            lvl = udoc.get("level", 1)
            count = udoc.get("daily_counts", {}).get(yesterday_str, 0)
            medal_or_num = medals.get(rank, f"<b>{rank}.</b>")
            link = f'<a href="tg://user?id={uid}">{name}</a>'
            lines.append(f"{medal_or_num} {link} — <b>{count:,}</b> ᴍsɢs (ʟᴠʟ {lvl})")

        body = "\n".join(lines)
        banner = (
            f"🌟 <b>─── [ ᴅᴀɪʟʏ ᴛᴏᴘ 10 ᴀᴄᴛɪᴠᴇ ᴄʜᴀᴛᴛᴇʀs! ] ───</b> 🌟\n\n"
            f"📍 <b>ɢʀᴏᴜᴘ:</b> {html.escape(chat_title)}\n"
            f"📅 <b>ᴅᴀᴛᴇ:</b> <b>{yesterday_str}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"{body}\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"🎉 <i>ᴄᴏɴɢʀᴀᴛᴜʟᴀᴛɪᴏɴs ᴛᴏ ʏᴇsᴛᴇʀᴅᴀʏ's ᴛᴏᴘ ᴄʜᴀᴛᴛᴇʀs!</i>\n"
            f"💬 <i>sᴛᴀʀᴛ ᴄʜᴀᴛᴛɪɴɢ ᴛᴏᴅᴀʏ ᴛᴏ ʙᴇᴄᴏᴍᴇ #1 ᴛᴏᴍᴏʀʀᴏᴡ!</i>"
        )

        keyboard = get_leaderboard_keyboard(chat_id, "daily")

        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=banner,
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
                disable_web_page_preview=True
            )
            meta_col.update_one({"chat_id": chat_id}, {"$set": {"last_daily_recap_date": yesterday_str}}, upsert=True)
            await log_to_channel(context, f"📢 Automated Daily Top 10 posted to <b>{html.escape(chat_title)}</b> for {yesterday_str}")
        except Exception as e:
            logger.debug(f"[RANKING] Failed auto-posting daily top to {chat_id}: {e}")

# --- Admin Ranking Settings UI Panel ---

def render_ranking_panel_text(chat_title: str, settings: dict) -> str:
    """Format the Ranking settings overview text."""
    rank_on = settings.get("ranking_enabled", True)
    daily_post_on = settings.get("daily_leaderboard_auto_post", True)
    levelup_on = settings.get("level_up_alerts", True)

    r_badge = "🟢 ON" if rank_on else "🔴 OFF"
    d_badge = "🟢 ON" if daily_post_on else "🔴 OFF"
    l_badge = "🟢 ON" if levelup_on else "🔴 OFF"

    return (
        f"🏆 <b>─── [ ʀᴀɴᴋɪɴɢ &amp; ʟᴇᴀᴅᴇʀʙᴏᴀʀᴅ sᴇᴛᴛɪɴɢs ] ───</b> 🏆\n\n"
        f"📍 <b>ɢʀᴏᴜᴘ:</b> {html.escape(chat_title or 'This Group')}\n\n"
        f"• 🏆 <b>ʀᴀɴᴋɪɴɢ sʏsᴛᴇᴍ:</b> {r_badge}\n"
        f"  <i>Tracks user text messages & Chat Score (XP)</i>\n\n"
        f"• 📢 <b>ᴅᴀɪʟʏ ᴛᴏᴘ 10 ᴀᴜᴛᴏ-ᴘᴏsᴛ:</b> {d_badge}\n"
        f"  <i>Sends daily top 10 chatters at midnight UTC</i>\n\n"
        f"• 🎉 <b>ʟᴇᴠᴇʟ ᴜᴘ &amp; ᴘʀᴏᴍᴏᴛɪᴏɴ ᴀʟᴇʀᴛs:</b> {l_badge}\n"
        f"  <i>Announces rank promotions & celebrates level ups</i>\n\n"
        f"<i>Tap the buttons below to toggle options or post the leaderboard immediately.</i>"
    )

def get_ranking_panel_keyboard(chat_id: int, settings: dict) -> InlineKeyboardMarkup:
    """Generate interactive buttons for ranking configuration."""
    rank_on = settings.get("ranking_enabled", True)
    daily_post_on = settings.get("daily_leaderboard_auto_post", True)
    levelup_on = settings.get("level_up_alerts", True)

    btn_rank = colored_button(f"🏆 Ranking: {'ON 🟢' if rank_on else 'OFF 🔴'}", "green" if rank_on else "red")
    btn_daily = colored_button(f"📢 Daily Top 10: {'ON 🟢' if daily_post_on else 'OFF 🔴'}", "green" if daily_post_on else "red")
    btn_levelup = colored_button(f"🎉 Level Up Alerts: {'ON 🟢' if levelup_on else 'OFF 🔴'}", "green" if levelup_on else "red")

    keyboard = [
        [InlineKeyboardButton(btn_rank, callback_data=f"rank_set_toggle_ranking:{chat_id}")],
        [InlineKeyboardButton(btn_daily, callback_data=f"rank_set_toggle_daily_post:{chat_id}")],
        [InlineKeyboardButton(btn_levelup, callback_data=f"rank_set_toggle_level_alerts:{chat_id}")],
        [InlineKeyboardButton(colored_button("🚀 Post Top 10 Now", "blue"), callback_data=f"rank_set_post_now:{chat_id}")],
        [InlineKeyboardButton(colored_button("🔙 Back to Settings", "default"), callback_data="set_view_main")]
    ]
    return InlineKeyboardMarkup(keyboard)

async def ranking_settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Command /rankingsettings: Opens the ranking settings menu for admins."""
    chat = update.effective_chat
    user = update.effective_user
    if not chat or chat.type not in ("group", "supergroup"):
        await send_bot_response(update, context, "⚠️ This command can only be used in groups.", parse_mode=ParseMode.HTML)
        return

    if not await can_user_configure_settings(chat.id, user.id, context):
        await send_bot_response(update, context, "⚠️ Only group admins can configure ranking settings.", parse_mode=ParseMode.HTML)
        return

    settings = get_chat_settings(chat.id)
    text = render_ranking_panel_text(chat.title or "This Group", settings)
    keyboard = get_ranking_panel_keyboard(chat.id, settings)

    await update.message.reply_text(
        text=text,
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
        disable_web_page_preview=True
    )

async def handle_ranking_panel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles ranking settings callbacks."""
    query = update.callback_query
    if not query or not query.data:
        return

    data = query.data
    chat = update.effective_chat
    user = update.effective_user

    if not chat or not user or not await can_user_configure_settings(chat.id, user.id, context):
        await query.answer("⚠️ You do not have permission to modify settings.", show_alert=True)
        return

    settings = get_chat_settings(chat.id)

    if data.startswith("rank_set_toggle_ranking:"):
        current = settings.get("ranking_enabled", True)
        update_chat_setting(chat.id, "ranking_enabled", not current)
        await query.answer(f"🏆 Ranking tracking {'Enabled' if not current else 'Disabled'}")

    elif data.startswith("rank_set_toggle_daily_post:"):
        current = settings.get("daily_leaderboard_auto_post", True)
        update_chat_setting(chat.id, "daily_leaderboard_auto_post", not current)
        await query.answer(f"📢 Daily Top 10 Auto-Post {'Enabled' if not current else 'Disabled'}")

    elif data.startswith("rank_set_toggle_level_alerts:"):
        current = settings.get("level_up_alerts", True)
        update_chat_setting(chat.id, "level_up_alerts", not current)
        await query.answer(f"🎉 Level-Up Promotions {'Enabled' if not current else 'Disabled'}")

    elif data.startswith("rank_set_post_now:"):
        now_utc = datetime.now(timezone.utc)
        today_str = now_utc.strftime("%Y-%m-%d")
        lb_text, lb_kb = render_leaderboard(chat.id, chat.title or "This Group", period="daily", target_date=today_str)
        banner = (
            f"🌟 <b>─── [ ᴅᴀɪʟʏ ᴛᴏᴘ 10 ᴀᴄᴛɪᴠᴇ ᴄʜᴀᴛᴛᴇʀs! ] ───</b> 🌟\n\n"
            f"📍 <b>ɢʀᴏᴜᴘ:</b> {html.escape(chat.title or 'This Group')}\n"
            f"📅 <b>ᴅᴀᴛᴇ:</b> <b>{today_str}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            + lb_text.split("━━━━━━━━━━━━━━━━━━━━━\n", 1)[1]
        )
        try:
            await context.bot.send_message(
                chat_id=chat.id,
                text=banner,
                parse_mode=ParseMode.HTML,
                reply_markup=lb_kb,
                disable_web_page_preview=True
            )
            await query.answer("🚀 Daily Top 10 posted to the group!", show_alert=True)
        except Exception as e:
            await query.answer(f"Error posting: {e}", show_alert=True)
        return

    # Re-render updated panel
    new_settings = get_chat_settings(chat.id)
    new_text = render_ranking_panel_text(chat.title or "This Group", new_settings)
    new_kb = get_ranking_panel_keyboard(chat.id, new_settings)

    try:
        await query.edit_message_text(
            text=new_text,
            parse_mode=ParseMode.HTML,
            reply_markup=new_kb,
            disable_web_page_preview=True
        )
    except BadRequest:
        pass

def get_ranking_handlers():
    """Returns all handlers related to Ranking, Levels, and Leaderboards."""
    return [
        # User & Admin Commands
        CommandHandler(["ranking", "leaderboard", "top"], ranking_command),
        CommandHandler(["myrank", "rank", "score", "level"], myrank_command),
        CommandHandler(["posttop", "sendtop", "dailytop"], post_daily_top_command),
        CommandHandler(["rankingsettings", "ranksettings"], ranking_settings_command),
        # Callback query handlers
        CallbackQueryHandler(handle_leaderboard_callbacks, pattern="^lb_"),
        CallbackQueryHandler(handle_ranking_panel_callback, pattern="^rank_set_"),
        # Message activity listener (Text and Captioned messages in groups)
        MessageHandler(
            (filters.TEXT | filters.CAPTION) & ~filters.COMMAND & (filters.ChatType.GROUPS | filters.ChatType.SUPERGROUP),
            handle_ranking_message
        )
    ]
