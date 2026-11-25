import io
import random
import string
import time
import logging
from PIL import Image, ImageDraw, ImageFont
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ChatPermissions
from telegram.constants import ParseMode
from telegram.ext import ContextTypes, CallbackQueryHandler, MessageHandler, filters, CommandHandler
from telegram.error import BadRequest

from core.config import colored_button
from managers.settings_manager_mongo import get_chat_settings, update_chat_setting

# In-memory tracking of pending newcomer captchas: key = (chat_id, user_id)
PENDING_CAPTCHAS = {}

# ─────────────────────────────────────────────────────────────────────────────
# Challenge Generators
# ─────────────────────────────────────────────────────────────────────────────

def generate_digits_challenge():
    """Generates a simple arithmetic problem and 4 options."""
    a = random.randint(2, 19)
    b = random.randint(2, 19)
    op = random.choice(["+", "-"])
    if op == "-":
        if a < b:
            a, b = b, a
        correct = a - b
    else:
        correct = a + b
        
    question = f"{a} {op} {b}"
    
    # Generate 3 distinct wrong options close to correct
    options = {correct}
    deltas = [-3, -2, -1, 1, 2, 3, 4, -4, 5]
    random.shuffle(deltas)
    for d in deltas:
        wrong = correct + d
        if wrong >= 0 and wrong != correct:
            options.add(wrong)
        if len(options) == 4:
            break
            
    # If still not 4, add randoms
    while len(options) < 4:
        wrong = random.randint(0, 40)
        if wrong != correct:
            options.add(wrong)
            
    opt_list = list(options)
    random.shuffle(opt_list)
    return question, str(correct), [str(o) for o in opt_list]


def generate_image_challenge():
    """Generates an image captcha using Pillow and 4 options."""
    # 4 alphanumeric characters (excluding ambiguous chars like 0/O, 1/I/l)
    chars = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
    code = "".join(random.choices(chars, k=4))
    
    # Image dimensions
    width, height = 220, 80
    bg_color = (random.randint(235, 255), random.randint(235, 255), random.randint(235, 255))
    image = Image.new("RGB", (width, height), bg_color)
    draw = ImageDraw.Draw(image)
    
    # Draw subtle background noise lines
    for _ in range(6):
        x1 = random.randint(0, width)
        y1 = random.randint(0, height)
        x2 = random.randint(0, width)
        y2 = random.randint(0, height)
        line_color = (random.randint(140, 200), random.randint(140, 200), random.randint(140, 200))
        draw.line([(x1, y1), (x2, y2)], fill=line_color, width=random.randint(1, 2))
        
    # Draw noise points
    for _ in range(120):
        px = random.randint(0, width)
        py = random.randint(0, height)
        dot_color = (random.randint(150, 210), random.randint(150, 210), random.randint(150, 210))
        draw.point((px, py), fill=dot_color)
        
    # Draw characters
    try:
        font = ImageFont.load_default(size=34)
    except:
        font = ImageFont.load_default()
        
    x_offset = 25
    for char in code:
        char_color = (random.randint(20, 100), random.randint(20, 100), random.randint(20, 100))
        y_offset = random.randint(16, 26)
        draw.text((x_offset, y_offset), char, font=font, fill=char_color)
        x_offset += random.randint(40, 48)
        
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    buf.seek(0)
    
    # Generate 3 realistic wrong choices
    options = {code}
    while len(options) < 4:
        wrong_chars = list(code)
        idx_to_change = random.randint(0, 3)
        wrong_chars[idx_to_change] = random.choice([c for c in chars if c != code[idx_to_change]])
        options.add("".join(wrong_chars))
        
    opt_list = list(options)
    random.shuffle(opt_list)
    return buf, code, opt_list


# ─────────────────────────────────────────────────────────────────────────────
# Permissions Helpers
# ─────────────────────────────────────────────────────────────────────────────

RESTRICTED_PERMISSIONS = ChatPermissions(
    can_send_messages=False,
    can_send_audios=False,
    can_send_documents=False,
    can_send_photos=False,
    can_send_videos=False,
    can_send_video_notes=False,
    can_send_voice_notes=False,
    can_send_polls=False,
    can_send_other_messages=False,
    can_add_web_page_previews=False,
    can_change_info=False,
    can_invite_users=False,
    can_pin_messages=False,
)

SIMPLE_MODE_PERMISSIONS = ChatPermissions(
    can_send_messages=True,
    can_send_audios=False,
    can_send_documents=False,
    can_send_photos=False,
    can_send_videos=False,
    can_send_video_notes=False,
    can_send_voice_notes=False,
    can_send_polls=False,
    can_send_other_messages=False,
    can_add_web_page_previews=False,
    can_change_info=False,
    can_invite_users=False,
    can_pin_messages=False,
)

UNRESTRICTED_PERMISSIONS = ChatPermissions(
    can_send_messages=True,
    can_send_audios=True,
    can_send_documents=True,
    can_send_photos=True,
    can_send_videos=True,
    can_send_video_notes=True,
    can_send_voice_notes=True,
    can_send_polls=True,
    can_send_other_messages=True,
    can_add_web_page_previews=True,
    can_change_info=False,
    can_invite_users=True,
    can_pin_messages=False,
)


# ─────────────────────────────────────────────────────────────────────────────
# Verification Start & Timeout
# ─────────────────────────────────────────────────────────────────────────────

def cancel_pending_captcha(chat_id, user_id, context=None):
    """Cancels and cleans up any pending captcha when a user leaves the chat."""
    key = (chat_id, user_id)
    state = PENDING_CAPTCHAS.pop(key, None)
    if state:
        job = state.get("job")
        if job:
            try: job.schedule_removal()
            except: pass
        msg_id = state.get("message_id")
        if msg_id and context:
            try:
                import asyncio
                asyncio.create_task(context.bot.delete_message(chat_id=chat_id, message_id=msg_id))
            except: pass
        pm_msg_id = state.get("pm_message_id")
        if pm_msg_id and context:
            try:
                import asyncio
                asyncio.create_task(context.bot.delete_message(chat_id=user_id, message_id=pm_msg_id))
            except: pass


async def start_captcha_verification(chat, user, context: ContextTypes.DEFAULT_TYPE, added_by=None):
    """Initiates newcomer verification test according to chat settings."""
    chat_id = chat.id
    user_id = user.id
    
    if user.is_bot:
        return
        
    settings = get_chat_settings(chat_id)
    if not settings.get("captcha_enabled", False):
        from handlers.welcome import send_welcome
        await send_welcome(chat, user, context)
        return

    # If the user was added by an admin of the group, bypass captcha!
    if added_by and getattr(added_by, 'id', None) != user_id:
        try:
            from managers.user_manager_mongo import is_user_admin
            if await is_user_admin(chat_id, added_by.id, context):
                logging.info(f"User {user_id} was added by admin {added_by.id} in chat {chat_id}, bypassing captcha.")
                from handlers.welcome import send_welcome
                await send_welcome(chat, user, context)
                return
        except Exception as e:
            logging.warning(f"Error checking if inviter {added_by.id} is admin: {e}")

    # If the user joined via accepted join request, bypass captcha!
    try:
        from features.join_request import is_recent_approved_join_request
        if is_recent_approved_join_request(chat_id, user_id):
            logging.info(f"User {user_id} joined via accepted join request in chat {chat_id}, bypassing captcha.")
            from handlers.welcome import send_welcome
            await send_welcome(chat, user, context)
            return
    except Exception as e:
        logging.warning(f"Error checking if user joined via join request: {e}")

    # Debounce: if captcha was already created for this user in the last 6 seconds, ignore duplicate trigger
    existing = PENDING_CAPTCHAS.get((chat_id, user_id))
    if existing and (time.time() - existing.get("created_at", 0)) < 6:
        logging.info(f"Captcha already pending for user {user_id} in {chat_id}, skipping duplicate trigger.")
        return

    # If there is an older pending captcha (e.g. from previous join), clean it up first
    old_state = PENDING_CAPTCHAS.pop((chat_id, user_id), None)
    if old_state:
        old_job = old_state.get("job")
        if old_job:
            try: old_job.schedule_removal()
            except: pass
        old_msg_id = old_state.get("message_id")
        if old_msg_id:
            try: await context.bot.delete_message(chat_id=chat_id, message_id=old_msg_id)
            except: pass

    test_type = settings.get("captcha_type", "button").lower()
    timeout = int(settings.get("captcha_timeout", 120))
    action = settings.get("captcha_action", "kick").lower()
    
    # 1. Restrict user
    try:
        perms = SIMPLE_MODE_PERMISSIONS if test_type == "simple" else RESTRICTED_PERMISSIONS
        await context.bot.restrict_chat_member(chat_id, user_id, permissions=perms)
    except Exception as e:
        logging.warning(f"Could not restrict user {user_id} in {chat_id} on join: {e}")

    # Mention string
    user_name = user.first_name or "Newcomer"
    safe_name = user_name.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    mention = f'<a href="tg://user?id={user_id}">{safe_name}</a>'
    
    # Check if PM verification mode is enabled
    pm_mode = settings.get("captcha_pm_mode", False)
    if pm_mode:
        bot_info = await context.bot.get_me()
        bot_username = bot_info.username
        verify_url = f"https://t.me/{bot_username}?start=verify_{chat_id}_{user_id}"
        text = (
            f"👋 Welcome {mention} to <b>{chat.title}</b>!\n\n"
            f"🛡️ <b>Anti-Bot Verification</b>\n"
            f"To send messages in this group, please verify that you are human in private within <b>{timeout}s</b>.\n\n"
            f"👇 <i>Click the button below to solve the captcha in PM:</i>"
        )
        keyboard = [
            [InlineKeyboardButton(colored_button("🔐 Verify in Private", "green"), url=verify_url)]
        ]
        try:
            msg = await context.bot.send_message(
                chat_id=chat_id,
                text=text,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode=ParseMode.HTML
            )
        except Exception as e:
            logging.error(f"Error sending PM captcha group prompt: {e}")
            msg = None

        if not msg:
            try:
                await context.bot.restrict_chat_member(chat_id, user_id, permissions=UNRESTRICTED_PERMISSIONS)
            except: pass
            from handlers.welcome import send_welcome
            await send_welcome(chat, user, context)
            return

        job = None
        if context.job_queue:
            try:
                job = context.job_queue.run_once(
                    captcha_timeout_job,
                    timeout,
                    data={"chat_id": chat_id, "user_id": user_id},
                    name=f"cpt_timeout_{chat_id}_{user_id}"
                )
            except Exception as e:
                logging.error(f"Failed to schedule captcha timeout job: {e}")

        PENDING_CAPTCHAS[(chat_id, user_id)] = {
            "chat_id": chat_id,
            "user_id": user_id,
            "chat": chat,
            "user": user,
            "type": test_type,
            "answer": "",
            "attempts": 0,
            "max_attempts": 2,
            "message_id": msg.message_id,
            "job": job,
            "timeout": timeout,
            "action": action,
            "created_at": time.time(),
            "is_vc_join": False,
            "is_pm_mode": True,
            "pm_message_id": None
        }
        return

    msg = None
    correct_ans = ""
    
    # 2. Build challenge prompt and keyboard
    if test_type == "simple":
        text = (
            f"👋 Welcome {mention} to <b>{chat.title}</b>!\n\n"
            f"🛡️ <b>Anti-Bot Verification</b>\n"
            f"Please <b>send any message</b> in this chat within <b>{timeout}s</b> to verify that you are not a bot."
        )
        try:
            msg = await context.bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.HTML)
        except Exception as e:
            logging.error(f"Error sending simple captcha prompt: {e}")

    elif test_type == "digits":
        q_str, correct_ans, opt_list = generate_digits_challenge()
        text = (
            f"👋 Welcome {mention} to <b>{chat.title}</b>!\n\n"
            f"🛡️ <b>Anti-Bot Verification</b>\n"
            f"Please solve this simple arithmetic operation within <b>{timeout}s</b>:\n\n"
            f"👉 <b>{q_str} = ?</b>"
        )
        keyboard = [
            [
                InlineKeyboardButton(colored_button(opt_list[0], "blue"), callback_data=f"cpt_ans_{user_id}_{opt_list[0]}"),
                InlineKeyboardButton(colored_button(opt_list[1], "blue"), callback_data=f"cpt_ans_{user_id}_{opt_list[1]}")
            ],
            [
                InlineKeyboardButton(colored_button(opt_list[2], "blue"), callback_data=f"cpt_ans_{user_id}_{opt_list[2]}"),
                InlineKeyboardButton(colored_button(opt_list[3], "blue"), callback_data=f"cpt_ans_{user_id}_{opt_list[3]}")
            ]
        ]
        try:
            msg = await context.bot.send_message(
                chat_id=chat_id,
                text=text,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode=ParseMode.HTML
            )
        except Exception as e:
            logging.error(f"Error sending digits captcha prompt: {e}")

    elif test_type == "image":
        img_buf, correct_ans, opt_list = generate_image_challenge()
        caption = (
            f"👋 Welcome {mention} to <b>{chat.title}</b>!\n\n"
            f"🛡️ <b>Anti-Bot Verification</b>\n"
            f"Please recognize the characters on the image below within <b>{timeout}s</b>:"
        )
        keyboard = [
            [
                InlineKeyboardButton(colored_button(opt_list[0], "blue"), callback_data=f"cpt_ans_{user_id}_{opt_list[0]}"),
                InlineKeyboardButton(colored_button(opt_list[1], "blue"), callback_data=f"cpt_ans_{user_id}_{opt_list[1]}")
            ],
            [
                InlineKeyboardButton(colored_button(opt_list[2], "blue"), callback_data=f"cpt_ans_{user_id}_{opt_list[2]}"),
                InlineKeyboardButton(colored_button(opt_list[3], "blue"), callback_data=f"cpt_ans_{user_id}_{opt_list[3]}")
            ]
        ]
        try:
            msg = await context.bot.send_photo(
                chat_id=chat_id,
                photo=img_buf,
                caption=caption,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode=ParseMode.HTML
            )
        except Exception as e:
            logging.error(f"Error sending image captcha prompt: {e}")

    else:
        # Default: "button"
        test_type = "button"
        text = (
            f"👋 Welcome {mention} to <b>{chat.title}</b>!\n\n"
            f"🛡️ <b>Anti-Bot Verification</b>\n"
            f"Please press the verification button below within <b>{timeout}s</b> to confirm you are human."
        )
        keyboard = [
            [InlineKeyboardButton(colored_button("✅ I'm not a bot", "green"), callback_data=f"cpt_btn_{user_id}")]
        ]
        try:
            msg = await context.bot.send_message(
                chat_id=chat_id,
                text=text,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode=ParseMode.HTML
            )
        except Exception as e:
            logging.error(f"Error sending button captcha prompt: {e}")

    if not msg:
        # Fallback if sending failed: restore permissions and send normal welcome
        try:
            await context.bot.restrict_chat_member(chat_id, user_id, permissions=UNRESTRICTED_PERMISSIONS)
        except: pass
        from handlers.welcome import send_welcome
        await send_welcome(chat, user, context)
        return

    # 3. Schedule timeout job
    job = None
    if context.job_queue:
        try:
            job = context.job_queue.run_once(
                captcha_timeout_job,
                timeout,
                data={"chat_id": chat_id, "user_id": user_id},
                name=f"cpt_timeout_{chat_id}_{user_id}"
            )
        except Exception as e:
            logging.error(f"Failed to schedule captcha timeout job: {e}")

    # 4. Save state
    PENDING_CAPTCHAS[(chat_id, user_id)] = {
        "chat_id": chat_id,
        "user_id": user_id,
        "chat": chat,
        "user": user,
        "type": test_type,
        "answer": correct_ans,
        "attempts": 0,
        "max_attempts": 2,
        "message_id": msg.message_id,
        "job": job,
        "timeout": timeout,
        "action": action,
        "created_at": time.time(),
        "is_vc_join": False
    }


async def start_vc_captcha_verification(chat_id, user_id, user_entity, call_id, ptb_app):
    """Initiates verification for users who join Voice Chat without joining the group."""
    settings = get_chat_settings(chat_id)
    if not settings.get("captcha_vc_enabled", False):
        return

    test_type = settings.get("captcha_type", "button").lower()
    timeout = int(settings.get("captcha_timeout", 120))
    action = settings.get("captcha_action", "kick").lower()

    user_name = getattr(user_entity, 'first_name', "User") or "User"
    safe_name = user_name.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    mention = f'<a href="tg://user?id={user_id}">{safe_name}</a>'

    chat_title = "the Voice Chat"
    try:
        chat_obj = await ptb_app.bot.get_chat(chat_id)
        if chat_obj and chat_obj.title:
            chat_title = chat_obj.title
    except: pass

    msg = None
    correct_ans = ""

    if test_type == "simple":
        text = (
            f"🎙️ <b>Voice Chat Verification</b>\n"
            f"👋 Welcome {mention} to <b>{chat_title}</b>'s Voice Chat!\n\n"
            f"⚠️ <i>You joined the Voice Chat without joining the group.</i>\n\n"
            f"🛡️ <b>Anti-Bot Verification</b>\n"
            f"Please <b>send any message</b> in this chat or click the button below within <b>{timeout}s</b> to verify:"
        )
        keyboard = [
            [InlineKeyboardButton(colored_button("✅ I'm not a bot", "green"), callback_data=f"cpt_btn_{user_id}")]
        ]
        try:
            msg = await ptb_app.bot.send_message(
                chat_id=chat_id,
                text=text,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode=ParseMode.HTML
            )
        except Exception as e:
            logging.error(f"Error sending simple VC captcha prompt: {e}")

    elif test_type == "digits":
        q_str, correct_ans, opt_list = generate_digits_challenge()
        text = (
            f"🎙️ <b>Voice Chat Verification</b>\n"
            f"👋 Welcome {mention} to <b>{chat_title}</b>'s Voice Chat!\n\n"
            f"⚠️ <i>You joined the Voice Chat without joining the group.</i>\n\n"
            f"🛡️ <b>Anti-Bot Verification</b>\n"
            f"Please solve this simple arithmetic operation within <b>{timeout}s</b> to verify:\n\n"
            f"👉 <b>{q_str} = ?</b>"
        )
        keyboard = [
            [
                InlineKeyboardButton(colored_button(opt_list[0], "blue"), callback_data=f"cpt_ans_{user_id}_{opt_list[0]}"),
                InlineKeyboardButton(colored_button(opt_list[1], "blue"), callback_data=f"cpt_ans_{user_id}_{opt_list[1]}")
            ],
            [
                InlineKeyboardButton(colored_button(opt_list[2], "blue"), callback_data=f"cpt_ans_{user_id}_{opt_list[2]}"),
                InlineKeyboardButton(colored_button(opt_list[3], "blue"), callback_data=f"cpt_ans_{user_id}_{opt_list[3]}")
            ]
        ]
        try:
            msg = await ptb_app.bot.send_message(
                chat_id=chat_id,
                text=text,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode=ParseMode.HTML
            )
        except Exception as e:
            logging.error(f"Error sending digits VC captcha prompt: {e}")

    elif test_type == "image":
        img_buf, correct_ans, opt_list = generate_image_challenge()
        caption = (
            f"🎙️ <b>Voice Chat Verification</b>\n"
            f"👋 Welcome {mention} to <b>{chat_title}</b>'s Voice Chat!\n\n"
            f"⚠️ <i>You joined the Voice Chat without joining the group.</i>\n\n"
            f"🛡️ <b>Anti-Bot Verification</b>\n"
            f"Please recognize the characters on the image below within <b>{timeout}s</b> to verify:"
        )
        keyboard = [
            [
                InlineKeyboardButton(colored_button(opt_list[0], "blue"), callback_data=f"cpt_ans_{user_id}_{opt_list[0]}"),
                InlineKeyboardButton(colored_button(opt_list[1], "blue"), callback_data=f"cpt_ans_{user_id}_{opt_list[1]}")
            ],
            [
                InlineKeyboardButton(colored_button(opt_list[2], "blue"), callback_data=f"cpt_ans_{user_id}_{opt_list[2]}"),
                InlineKeyboardButton(colored_button(opt_list[3], "blue"), callback_data=f"cpt_ans_{user_id}_{opt_list[3]}")
            ]
        ]
        try:
            msg = await ptb_app.bot.send_photo(
                chat_id=chat_id,
                photo=img_buf,
                caption=caption,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode=ParseMode.HTML
            )
        except Exception as e:
            logging.error(f"Error sending image VC captcha prompt: {e}")

    else:
        test_type = "button"
        text = (
            f"🎙️ <b>Voice Chat Verification</b>\n"
            f"👋 Welcome {mention} to <b>{chat_title}</b>'s Voice Chat!\n\n"
            f"⚠️ <i>You joined the Voice Chat without joining the group.</i>\n\n"
            f"🛡️ <b>Anti-Bot Verification</b>\n"
            f"Please press the verification button below within <b>{timeout}s</b> to verify you are human:"
        )
        keyboard = [
            [InlineKeyboardButton(colored_button("✅ I'm not a bot", "green"), callback_data=f"cpt_btn_{user_id}")]
        ]
        try:
            msg = await ptb_app.bot.send_message(
                chat_id=chat_id,
                text=text,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode=ParseMode.HTML
            )
        except Exception as e:
            logging.error(f"Error sending button VC captcha prompt: {e}")

    if not msg:
        # Fallback: unmute user in VC if prompt couldn't be sent
        try:
            from features.voice_chat import mute_vc_participant
            await mute_vc_participant(chat_id, user_id, muted=False)
        except: pass
        return

    job = None
    if ptb_app.job_queue:
        try:
            job = ptb_app.job_queue.run_once(
                captcha_timeout_job,
                timeout,
                data={"chat_id": chat_id, "user_id": user_id},
                name=f"cpt_timeout_{chat_id}_{user_id}"
            )
        except Exception as e:
            logging.error(f"Failed to schedule VC captcha timeout job: {e}")

    PENDING_CAPTCHAS[(chat_id, user_id)] = {
        "chat_id": chat_id,
        "user_id": user_id,
        "chat": None,
        "user": user_entity,
        "type": test_type,
        "answer": correct_ans,
        "attempts": 0,
        "max_attempts": 2,
        "message_id": msg.message_id,
        "job": job,
        "timeout": timeout,
        "action": action,
        "created_at": time.time(),
        "is_vc_join": True,
        "call_id": call_id
    }


async def captcha_timeout_job(context: ContextTypes.DEFAULT_TYPE):
    """Executed when verification time expires."""
    data = context.job.data
    chat_id = data.get("chat_id")
    user_id = data.get("user_id")
    
    key = (chat_id, user_id)
    state = PENDING_CAPTCHAS.pop(key, None)
    if not state:
        return
        
    await resolve_captcha_failure(chat_id, user_id, state, context, reason="timeout")


# ─────────────────────────────────────────────────────────────────────────────
# Success & Failure Handlers
# ─────────────────────────────────────────────────────────────────────────────

async def resolve_captcha_success(chat_id, user_id, state, context: ContextTypes.DEFAULT_TYPE):
    """Unmutes user, cleans up prompt, and sends the welcome message."""
    # Cancel timeout job if active
    job = state.get("job")
    if job:
        try:
            job.schedule_removal()
        except: pass

    # Delete captcha prompt message
    msg_id = state.get("message_id")
    if msg_id:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=msg_id)
        except: pass

    is_vc_join = state.get("is_vc_join", False)
    if is_vc_join:
        # 1. Unmute in VC
        try:
            from features.voice_chat import mute_vc_participant
            await mute_vc_participant(chat_id, user_id, muted=False)
        except Exception as e:
            logging.warning(f"Could not unmute user {user_id} in VC: {e}")

        # 2. Send temporary success notice into chat
        user = state.get("user")
        name = getattr(user, 'first_name', "User") or "User"
        safe_name = name.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        mention = f'<a href="tg://user?id={user_id}">{safe_name}</a>'
        try:
            succ_msg = await context.bot.send_message(
                chat_id=chat_id,
                text=f"✅ {mention} has verified successfully and is unmuted in <b>Voice Chat</b>!",
                parse_mode=ParseMode.HTML
            )
            if context.job_queue:
                from core.config import delete_message_job
                context.job_queue.run_once(delete_message_job, 15, data={"chat_id": chat_id, "message_id": succ_msg.message_id})
        except: pass
        return

    # Standard group join success flow:
    try:
        await context.bot.restrict_chat_member(chat_id, user_id, permissions=UNRESTRICTED_PERMISSIONS)
    except Exception as e:
        logging.warning(f"Could not restore permissions for user {user_id} in {chat_id}: {e}")

    chat = state.get("chat")
    user = state.get("user")
    if chat and user:
        from handlers.welcome import send_welcome
        await send_welcome(chat, user, context)


async def resolve_captcha_failure(chat_id, user_id, state, context: ContextTypes.DEFAULT_TYPE, reason="timeout"):
    """Applies configured penalty (kick/mute/ban) when user fails captcha or times out."""
    job = state.get("job")
    if job:
        try:
            job.schedule_removal()
        except: pass

    action = state.get("action", "kick")
    user = state.get("user")
    user_name = getattr(user, 'first_name', "User") or "User"
    safe_name = user_name.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    mention = f'<a href="tg://user?id={user_id}">{safe_name}</a>'

    # Delete challenge prompt
    msg_id = state.get("message_id")
    if msg_id:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=msg_id)
        except: pass

    # If PM verification prompt is active, notify/update PM message
    pm_msg_id = state.get("pm_message_id")
    if pm_msg_id:
        try:
            reason_pm = "timed out" if reason == "timeout" else "failed"
            await context.bot.edit_message_text(
                chat_id=user_id,
                message_id=pm_msg_id,
                text=f"❌ <b>Verification {reason_pm.capitalize()}</b>\n\nYou did not complete the verification test.",
                parse_mode=ParseMode.HTML
            )
        except: pass

    is_vc_join = state.get("is_vc_join", False)
    if is_vc_join:
        # Kick / remove user from VC
        try:
            from features.voice_chat import kick_user_from_vc
            await kick_user_from_vc(chat_id, user_id, context=context, action=action)
        except Exception as e:
            logging.error(f"Failed to kick user {user_id} from VC: {e}")

        try:
            reason_str = "timed out" if reason == "timeout" else "failed verification"
            action_text = "banned" if action == "ban" else "removed from Voice Chat"
            notice = await context.bot.send_message(
                chat_id=chat_id,
                text=f"⚠️ {mention} {reason_str} and was <b>{action_text}</b>.",
                parse_mode=ParseMode.HTML
            )
            if context.job_queue:
                from core.config import delete_message_job
                context.job_queue.run_once(delete_message_job, 15, data={"chat_id": chat_id, "message_id": notice.message_id})
        except: pass
        return

    # Standard group join failure flow:
    action_text = "kicked"
    try:
        if action == "ban":
            await context.bot.ban_chat_member(chat_id, user_id)
            action_text = "banned"
        elif action == "mute":
            await context.bot.restrict_chat_member(chat_id, user_id, permissions=RESTRICTED_PERMISSIONS)
            action_text = "permanently muted"
        else:
            await context.bot.ban_chat_member(chat_id, user_id)
            await context.bot.unban_chat_member(chat_id, user_id)
            action_text = "kicked"
    except Exception as e:
        logging.error(f"Failed to execute captcha action {action} on user {user_id} in {chat_id}: {e}")

    # Send temporary notice into chat
    try:
        reason_str = "timed out" if reason == "timeout" else "failed the verification"
        notice = await context.bot.send_message(
            chat_id=chat_id,
            text=f"⚠️ {mention} {reason_str} and was <b>{action_text}</b>.",
            parse_mode=ParseMode.HTML
        )
        if context.job_queue:
            from core.config import delete_message_job
            context.job_queue.run_once(delete_message_job, 15, data={"chat_id": chat_id, "message_id": notice.message_id})
    except: pass


# ─────────────────────────────────────────────────────────────────────────────
# Incoming Interaction Handlers
# ─────────────────────────────────────────────────────────────────────────────

async def captcha_button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles button-based verification clicks."""
    query = update.callback_query
    if not query or not query.data:
        return
        
    data = query.data
    clicker_id = query.from_user.id
    chat_id = update.effective_chat.id
    
    # Check if this is a button captcha callback
    if not data.startswith("cpt_btn_"):
        return
        
    target_user_id = int(data.replace("cpt_btn_", ""))
    
    # Reject other users clicking someone else's captcha
    if clicker_id != target_user_id:
        await query.answer("❌ This verification button is not for you.", show_alert=True)
        return
        
    key = (chat_id, target_user_id)
    state = PENDING_CAPTCHAS.pop(key, None)
    if not state:
        await query.answer("⚠️ This verification has already expired or been completed.", show_alert=True)
        try:
            await query.message.delete()
        except: pass
        return
        
    await query.answer("✅ Verification successful! Welcome to the group.")
    await resolve_captcha_success(chat_id, target_user_id, state, context)


async def captcha_options_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles multiple-choice option clicks for digits and image captchas."""
    query = update.callback_query
    if not query or not query.data:
        return
        
    data = query.data
    clicker_id = query.from_user.id
    chat_id = update.effective_chat.id
    
    if not data.startswith("cpt_ans_"):
        return
        
    parts = data.split("_", 3)
    if len(parts) < 4:
        return
        
    target_user_id = int(parts[2])
    selected_val = parts[3]
    
    if clicker_id != target_user_id:
        await query.answer("❌ This verification option is not for you.", show_alert=True)
        return
        
    key = (chat_id, target_user_id)
    state = PENDING_CAPTCHAS.get(key)
    if not state:
        await query.answer("⚠️ This verification has already expired or been completed.", show_alert=True)
        try:
            await query.message.delete()
        except: pass
        return
        
    correct_val = state.get("answer")
    if selected_val == correct_val:
        PENDING_CAPTCHAS.pop(key, None)
        await query.answer("✅ Correct answer! Verification successful.")
        await resolve_captcha_success(chat_id, target_user_id, state, context)
    else:
        state["attempts"] += 1
        if state["attempts"] >= state["max_attempts"]:
            PENDING_CAPTCHAS.pop(key, None)
            await query.answer("❌ Incorrect answer! Verification failed.", show_alert=True)
            await resolve_captcha_failure(chat_id, target_user_id, state, context, reason="wrong_answer")
        else:
            remaining = state["max_attempts"] - state["attempts"]
            await query.answer(f"❌ Incorrect answer! You have {remaining} attempt(s) remaining.", show_alert=True)


async def captcha_simple_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles text message sent by newcomers in simple captcha mode."""
    if not update.message or not update.effective_chat or not update.effective_user:
        return
        
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    
    key = (chat_id, user_id)
    state = PENDING_CAPTCHAS.get(key)
    if not state or state.get("type") != "simple":
        return
        
    # User has sent a message: verify immediately!
    PENDING_CAPTCHAS.pop(key, None)
    
    # Delete the user's test message to keep chat clean
    try:
        await update.message.delete()
    except: pass
    
    await resolve_captcha_success(chat_id, user_id, state, context)


# ─────────────────────────────────────────────────────────────────────────────
# Private Message (PM) Captcha Verification Handlers
# ─────────────────────────────────────────────────────────────────────────────

async def handle_pm_captcha_start(update: Update, context: ContextTypes.DEFAULT_TYPE, start_arg: str):
    """Handles deep-link from group to start captcha verification in PM."""
    if not update.effective_user or not update.message:
        return
    user = update.effective_user
    user_id = user.id

    parts = start_arg.split("_")
    if len(parts) < 3:
        await update.message.reply_text("❌ Invalid verification link.")
        return

    try:
        chat_id = int(parts[1])
        target_user_id = int(parts[2])
    except ValueError:
        await update.message.reply_text("❌ Invalid verification link parameters.")
        return

    if user_id != target_user_id:
        await update.message.reply_text("❌ This verification link was generated for someone else.")
        return

    key = (chat_id, user_id)
    state = PENDING_CAPTCHAS.get(key)
    if not state:
        await update.message.reply_text("⚠️ This verification session has already expired or been completed.")
        return

    chat_obj = state.get("chat")
    chat_title = getattr(chat_obj, 'title', 'the group') if chat_obj else "the group"
    test_type = state.get("type", "button")
    timeout = state.get("timeout", 120)

    if test_type == "simple":
        text = (
            f"👋 Hello <b>{user.first_name}</b>!\n\n"
            f"🛡️ <b>Anti-Bot Verification for {chat_title}</b>\n"
            f"Please <b>send any text message</b> in this private chat within <b>{timeout}s</b> to verify you are human."
        )
        pm_msg = await update.message.reply_text(text, parse_mode=ParseMode.HTML)
        state["pm_message_id"] = pm_msg.message_id
        return

    elif test_type == "digits":
        q_str, correct_ans, opt_list = generate_digits_challenge()
        state["answer"] = correct_ans
        text = (
            f"👋 Hello <b>{user.first_name}</b>!\n\n"
            f"🛡️ <b>Anti-Bot Verification for {chat_title}</b>\n"
            f"Please solve this simple arithmetic problem within <b>{timeout}s</b>:\n\n"
            f"👉 <b>{q_str} = ?</b>"
        )
        keyboard = [
            [
                InlineKeyboardButton(colored_button(opt_list[0], "blue"), callback_data=f"cpt_pm_ans_{chat_id}_{user_id}_{opt_list[0]}"),
                InlineKeyboardButton(colored_button(opt_list[1], "blue"), callback_data=f"cpt_pm_ans_{chat_id}_{user_id}_{opt_list[1]}")
            ],
            [
                InlineKeyboardButton(colored_button(opt_list[2], "blue"), callback_data=f"cpt_pm_ans_{chat_id}_{user_id}_{opt_list[2]}"),
                InlineKeyboardButton(colored_button(opt_list[3], "blue"), callback_data=f"cpt_pm_ans_{chat_id}_{user_id}_{opt_list[3]}")
            ]
        ]
        pm_msg = await update.message.reply_text(
            text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.HTML
        )
        state["pm_message_id"] = pm_msg.message_id
        return

    elif test_type == "image":
        img_buf, correct_ans, opt_list = generate_image_challenge()
        state["answer"] = correct_ans
        caption = (
            f"👋 Hello <b>{user.first_name}</b>!\n\n"
            f"🛡️ <b>Anti-Bot Verification for {chat_title}</b>\n"
            f"Please recognize the characters on the image below within <b>{timeout}s</b>:"
        )
        keyboard = [
            [
                InlineKeyboardButton(colored_button(opt_list[0], "blue"), callback_data=f"cpt_pm_ans_{chat_id}_{user_id}_{opt_list[0]}"),
                InlineKeyboardButton(colored_button(opt_list[1], "blue"), callback_data=f"cpt_pm_ans_{chat_id}_{user_id}_{opt_list[1]}")
            ],
            [
                InlineKeyboardButton(colored_button(opt_list[2], "blue"), callback_data=f"cpt_pm_ans_{chat_id}_{user_id}_{opt_list[2]}"),
                InlineKeyboardButton(colored_button(opt_list[3], "blue"), callback_data=f"cpt_pm_ans_{chat_id}_{user_id}_{opt_list[3]}")
            ]
        ]
        pm_msg = await update.message.reply_photo(
            photo=img_buf,
            caption=caption,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.HTML
        )
        state["pm_message_id"] = pm_msg.message_id
        return

    else:
        text = (
            f"👋 Hello <b>{user.first_name}</b>!\n\n"
            f"🛡️ <b>Anti-Bot Verification for {chat_title}</b>\n"
            f"Please press the verification button below within <b>{timeout}s</b> to confirm you are human:"
        )
        keyboard = [
            [InlineKeyboardButton(colored_button("✅ I'm not a bot", "green"), callback_data=f"cpt_pm_btn_{chat_id}_{user_id}")]
        ]
        pm_msg = await update.message.reply_text(
            text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.HTML
        )
        state["pm_message_id"] = pm_msg.message_id
        return


async def resolve_captcha_pm_success(chat_id, user_id, state, query_or_update, context: ContextTypes.DEFAULT_TYPE):
    """Unmutes newcomer in group, cleans up prompt, and sends success DM."""
    job = state.get("job")
    if job:
        try: job.schedule_removal()
        except: pass

    # Un-restrict user in group
    try:
        await context.bot.restrict_chat_member(chat_id, user_id, permissions=UNRESTRICTED_PERMISSIONS)
    except Exception as e:
        logging.warning(f"Failed to un-restrict user {user_id} in group {chat_id}: {e}")

    # Delete group verification prompt message
    msg_id = state.get("message_id")
    if msg_id:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=msg_id)
        except Exception:
            pass

    from features.join_request import get_group_link
    group_url = await get_group_link(chat_id, context)

    chat_obj = state.get("chat")
    chat_title = getattr(chat_obj, 'title', 'the group') if chat_obj else "the group"

    keyboard = None
    if group_url:
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton(colored_button("💬 Open Group", "green"), url=group_url)]
        ])

    success_text = (
        f"✅ <b>Verification Successful!</b>\n\n"
        f"You have been verified and can now send messages in <b>{chat_title}</b>!"
    )

    try:
        if hasattr(query_or_update, "edit_message_text"):
            await query_or_update.edit_message_text(success_text, reply_markup=keyboard, parse_mode=ParseMode.HTML)
        elif hasattr(query_or_update, "message") and query_or_update.message:
            await query_or_update.message.reply_text(success_text, reply_markup=keyboard, parse_mode=ParseMode.HTML)
    except Exception as e:
        logging.warning(f"Error editing PM success message: {e}")

    # Trigger standard welcome in group if enabled
    chat = state.get("chat")
    user = state.get("user")
    if chat and user:
        from handlers.welcome import send_welcome
        await send_welcome(chat, user, context)


async def captcha_pm_button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles button clicks for PM captcha verification."""
    query = update.callback_query
    if not query or not query.data:
        return
    data = query.data
    if not data.startswith("cpt_pm_btn_"):
        return
    parts = data.split("_")
    if len(parts) < 5:
        return
    try:
        chat_id = int(parts[3])
        target_user_id = int(parts[4])
    except ValueError:
        return
    clicker_id = query.from_user.id
    if clicker_id != target_user_id:
        await query.answer("❌ This button is not for you.", show_alert=True)
        return

    key = (chat_id, target_user_id)
    state = PENDING_CAPTCHAS.pop(key, None)
    if not state:
        await query.answer("⚠️ This verification has expired or already been completed.", show_alert=True)
        try: await query.message.delete()
        except: pass
        return

    await query.answer("✅ Verification successful!")
    await resolve_captcha_pm_success(chat_id, target_user_id, state, query, context)


async def captcha_pm_options_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles option clicks for PM digits and image captchas."""
    query = update.callback_query
    if not query or not query.data:
        return
    data = query.data
    if not data.startswith("cpt_pm_ans_"):
        return
    parts = data.split("_", 5)
    if len(parts) < 6:
        return
    try:
        chat_id = int(parts[3])
        target_user_id = int(parts[4])
        selected_val = parts[5]
    except (ValueError, IndexError):
        return

    clicker_id = query.from_user.id
    if clicker_id != target_user_id:
        await query.answer("❌ This option is not for you.", show_alert=True)
        return

    key = (chat_id, target_user_id)
    state = PENDING_CAPTCHAS.get(key)
    if not state:
        await query.answer("⚠️ This verification has expired or already been completed.", show_alert=True)
        try: await query.message.delete()
        except: pass
        return

    correct_val = state.get("answer")
    if selected_val == correct_val:
        PENDING_CAPTCHAS.pop(key, None)
        await query.answer("✅ Correct answer! Verification successful.")
        await resolve_captcha_pm_success(chat_id, target_user_id, state, query, context)
    else:
        state["attempts"] += 1
        if state["attempts"] >= state["max_attempts"]:
            PENDING_CAPTCHAS.pop(key, None)
            await query.answer("❌ Incorrect answer! Verification failed.", show_alert=True)
            action = state.get("action", "kick")
            try:
                if action == "ban":
                    await context.bot.ban_chat_member(chat_id, target_user_id)
                elif action == "mute":
                    await context.bot.restrict_chat_member(chat_id, target_user_id, permissions=RESTRICTED_PERMISSIONS)
                else:
                    await context.bot.ban_chat_member(chat_id, target_user_id)
                    await context.bot.unban_chat_member(chat_id, target_user_id)
            except Exception as e:
                logging.error(f"Error applying penalty in group: {e}")

            msg_id = state.get("message_id")
            if msg_id:
                try: await context.bot.delete_message(chat_id=chat_id, message_id=msg_id)
                except: pass

            fail_text = "❌ <b>Verification Failed!</b>\n\nYou failed the captcha verification test."
            try:
                await query.edit_message_text(fail_text, parse_mode=ParseMode.HTML)
            except: pass
        else:
            remaining = state["max_attempts"] - state["attempts"]
            await query.answer(f"❌ Incorrect answer! You have {remaining} attempt(s) remaining.", show_alert=True)


# ─────────────────────────────────────────────────────────────────────────────
# Join Request PM Captcha Verification Handlers
# ─────────────────────────────────────────────────────────────────────────────

async def start_join_request_captcha(join_req, context: ContextTypes.DEFAULT_TYPE):
    """Initiates captcha verification directly in PM for a ChatJoinRequest."""
    chat = join_req.chat
    user = join_req.from_user
    chat_id = chat.id
    user_id = user.id

    if user.is_bot:
        return

    settings = get_chat_settings(chat_id)
    test_type = settings.get("captcha_type", "button").lower()
    timeout = int(settings.get("captcha_timeout", 120))

    old_state = PENDING_CAPTCHAS.pop((chat_id, user_id), None)
    if old_state:
        old_job = old_state.get("job")
        if old_job:
            try: old_job.schedule_removal()
            except: pass

    correct_ans = ""
    pm_msg = None

    if test_type == "simple":
        text = (
            f"👋 Hello <b>{user.first_name}</b>!\n\n"
            f"🛡️ You requested to join <b>{chat.title}</b>.\n"
            f"Please <b>send any text message</b> in this private chat within <b>{timeout}s</b> to be approved:"
        )
        try:
            pm_msg = await context.bot.send_message(chat_id=user_id, text=text, parse_mode=ParseMode.HTML)
        except Exception as e:
            logging.warning(f"Could not send join request simple captcha DM to {user_id}: {e}")

    elif test_type == "digits":
        q_str, correct_ans, opt_list = generate_digits_challenge()
        text = (
            f"👋 Hello <b>{user.first_name}</b>!\n\n"
            f"🛡️ You requested to join <b>{chat.title}</b>.\n"
            f"Please solve this arithmetic problem within <b>{timeout}s</b> to be approved:\n\n"
            f"👉 <b>{q_str} = ?</b>"
        )
        keyboard = [
            [
                InlineKeyboardButton(colored_button(opt_list[0], "blue"), callback_data=f"cpt_jr_ans_{chat_id}_{user_id}_{opt_list[0]}"),
                InlineKeyboardButton(colored_button(opt_list[1], "blue"), callback_data=f"cpt_jr_ans_{chat_id}_{user_id}_{opt_list[1]}")
            ],
            [
                InlineKeyboardButton(colored_button(opt_list[2], "blue"), callback_data=f"cpt_jr_ans_{chat_id}_{user_id}_{opt_list[2]}"),
                InlineKeyboardButton(colored_button(opt_list[3], "blue"), callback_data=f"cpt_jr_ans_{chat_id}_{user_id}_{opt_list[3]}")
            ]
        ]
        try:
            pm_msg = await context.bot.send_message(
                chat_id=user_id,
                text=text,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode=ParseMode.HTML
            )
        except Exception as e:
            logging.warning(f"Could not send join request digits captcha DM to {user_id}: {e}")

    elif test_type == "image":
        img_buf, correct_ans, opt_list = generate_image_challenge()
        caption = (
            f"👋 Hello <b>{user.first_name}</b>!\n\n"
            f"🛡️ You requested to join <b>{chat.title}</b>.\n"
            f"Please recognize the characters on the image within <b>{timeout}s</b> to be approved:"
        )
        keyboard = [
            [
                InlineKeyboardButton(colored_button(opt_list[0], "blue"), callback_data=f"cpt_jr_ans_{chat_id}_{user_id}_{opt_list[0]}"),
                InlineKeyboardButton(colored_button(opt_list[1], "blue"), callback_data=f"cpt_jr_ans_{chat_id}_{user_id}_{opt_list[1]}")
            ],
            [
                InlineKeyboardButton(colored_button(opt_list[2], "blue"), callback_data=f"cpt_jr_ans_{chat_id}_{user_id}_{opt_list[2]}"),
                InlineKeyboardButton(colored_button(opt_list[3], "blue"), callback_data=f"cpt_jr_ans_{chat_id}_{user_id}_{opt_list[3]}")
            ]
        ]
        try:
            pm_msg = await context.bot.send_photo(
                chat_id=user_id,
                photo=img_buf,
                caption=caption,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode=ParseMode.HTML
            )
        except Exception as e:
            logging.warning(f"Could not send join request image captcha DM to {user_id}: {e}")

    else:
        test_type = "button"
        text = (
            f"👋 Hello <b>{user.first_name}</b>!\n\n"
            f"🛡️ You requested to join <b>{chat.title}</b>.\n"
            f"Please press the button below within <b>{timeout}s</b> to be approved:"
        )
        keyboard = [
            [InlineKeyboardButton(colored_button("✅ I'm not a bot", "green"), callback_data=f"cpt_jr_btn_{chat_id}_{user_id}")]
        ]
        try:
            pm_msg = await context.bot.send_message(
                chat_id=user_id,
                text=text,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode=ParseMode.HTML
            )
        except Exception as e:
            logging.warning(f"Could not send join request button captcha DM to {user_id}: {e}")

    if not pm_msg:
        logging.warning(f"Failed to deliver join request captcha DM to user {user_id} for chat {chat_id}")
        return

    job = None
    if context.job_queue:
        try:
            job = context.job_queue.run_once(
                join_request_captcha_timeout_job,
                timeout,
                data={"chat_id": chat_id, "user_id": user_id},
                name=f"cpt_jr_timeout_{chat_id}_{user_id}"
            )
        except Exception as e:
            logging.error(f"Failed to schedule join request captcha timeout job: {e}")

    PENDING_CAPTCHAS[(chat_id, user_id)] = {
        "chat_id": chat_id,
        "user_id": user_id,
        "chat": chat,
        "user": user,
        "type": test_type,
        "answer": correct_ans,
        "attempts": 0,
        "max_attempts": 2,
        "message_id": pm_msg.message_id,
        "job": job,
        "timeout": timeout,
        "action": "decline",
        "created_at": time.time(),
        "is_vc_join": False,
        "is_join_request": True
    }


async def join_request_captcha_timeout_job(context: ContextTypes.DEFAULT_TYPE):
    """Timeout job for join request captchas."""
    data = context.job.data
    chat_id = data.get("chat_id")
    user_id = data.get("user_id")
    state = PENDING_CAPTCHAS.pop((chat_id, user_id), None)
    if not state:
        return
    try:
        await context.bot.decline_chat_join_request(chat_id=chat_id, user_id=user_id)
        msg_id = state.get("message_id")
        if msg_id:
            try:
                await context.bot.edit_message_text(
                    chat_id=user_id,
                    message_id=msg_id,
                    text="❌ <b>Join Request Expired</b>\n\nYou did not complete verification in time. Your join request was declined.",
                    parse_mode=ParseMode.HTML
                )
            except: pass
    except Exception as e:
        logging.warning(f"Error declining timed out join request for {user_id}: {e}")


async def resolve_join_request_captcha_success(chat_id, user_id, state, query_or_update, context: ContextTypes.DEFAULT_TYPE):
    """Approves newcomer's join request upon successful captcha solution in PM."""
    job = state.get("job")
    if job:
        try: job.schedule_removal()
        except: pass

    try:
        from features.join_request import record_approved_join_request
        record_approved_join_request(chat_id, user_id)
        await context.bot.approve_chat_join_request(chat_id=chat_id, user_id=user_id)
        logging.info(f"Approved join request via captcha for user {user_id} in {chat_id}")
    except Exception as e:
        logging.error(f"Failed to approve join request for {user_id} in {chat_id}: {e}")

    from features.join_request import get_group_link
    group_url = await get_group_link(chat_id, context)

    chat_obj = state.get("chat")
    chat_title = getattr(chat_obj, 'title', 'the group') if chat_obj else "the group"

    keyboard = None
    if group_url:
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton(colored_button("🚀 Enter Group", "blue"), url=group_url)]
        ])

    success_text = (
        f"🎉 <b>Join Request Approved!</b>\n\n"
        f"You have successfully passed the verification test.\n"
        f"Welcome to <b>{chat_title}</b>!"
    )

    try:
        if hasattr(query_or_update, "edit_message_text"):
            await query_or_update.edit_message_text(success_text, reply_markup=keyboard, parse_mode=ParseMode.HTML)
        elif hasattr(query_or_update, "message") and query_or_update.message:
            await query_or_update.message.reply_text(success_text, reply_markup=keyboard, parse_mode=ParseMode.HTML)
    except Exception as e:
        logging.warning(f"Error editing join request success message: {e}")

    user = state.get("user")
    if user:
        user_mention = user.mention_html() if hasattr(user, "mention_html") else f"<code>{user_id}</code>"
        from core.config import log_to_channel
        await log_to_channel(context, f"✅ <b>Join Request Approved via Captcha</b>\nUser: {user_mention} (<code>{user_id}</code>)\nGroup: <b>{chat_title}</b>")


async def captcha_jr_button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles button clicks for join request captcha verification."""
    query = update.callback_query
    if not query or not query.data:
        return
    data = query.data
    if not data.startswith("cpt_jr_btn_"):
        return
    parts = data.split("_")
    if len(parts) < 5:
        return
    try:
        chat_id = int(parts[3])
        target_user_id = int(parts[4])
    except ValueError:
        return

    clicker_id = query.from_user.id
    if clicker_id != target_user_id:
        await query.answer("❌ This button is not for you.", show_alert=True)
        return

    key = (chat_id, target_user_id)
    state = PENDING_CAPTCHAS.pop(key, None)
    if not state:
        await query.answer("⚠️ This request has expired or already been handled.", show_alert=True)
        try: await query.message.delete()
        except: pass
        return

    await query.answer("✅ Verification successful!")
    await resolve_join_request_captcha_success(chat_id, target_user_id, state, query, context)


async def captcha_jr_options_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles option clicks for join request digits and image captchas."""
    query = update.callback_query
    if not query or not query.data:
        return
    data = query.data
    if not data.startswith("cpt_jr_ans_"):
        return
    parts = data.split("_", 5)
    if len(parts) < 6:
        return
    try:
        chat_id = int(parts[3])
        target_user_id = int(parts[4])
        selected_val = parts[5]
    except (ValueError, IndexError):
        return

    clicker_id = query.from_user.id
    if clicker_id != target_user_id:
        await query.answer("❌ This option is not for you.", show_alert=True)
        return

    key = (chat_id, target_user_id)
    state = PENDING_CAPTCHAS.get(key)
    if not state:
        await query.answer("⚠️ This request has expired or already been handled.", show_alert=True)
        try: await query.message.delete()
        except: pass
        return

    correct_val = state.get("answer")
    if selected_val == correct_val:
        PENDING_CAPTCHAS.pop(key, None)
        await query.answer("✅ Correct answer! Verification successful.")
        await resolve_join_request_captcha_success(chat_id, target_user_id, state, query, context)
    else:
        state["attempts"] += 1
        if state["attempts"] >= state["max_attempts"]:
            PENDING_CAPTCHAS.pop(key, None)
            await query.answer("❌ Incorrect answer! Verification failed.", show_alert=True)
            try:
                await context.bot.decline_chat_join_request(chat_id=chat_id, user_id=target_user_id)
            except Exception as e:
                logging.error(f"Error declining join request on captcha failure: {e}")

            fail_text = "❌ <b>Verification Failed!</b>\n\nYour join request has been declined."
            try:
                await query.edit_message_text(fail_text, parse_mode=ParseMode.HTML)
            except: pass
        else:
            remaining = state["max_attempts"] - state["attempts"]
            await query.answer(f"❌ Incorrect answer! You have {remaining} attempt(s) remaining.", show_alert=True)


async def captcha_pm_simple_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles text message sent by newcomers in PM when simple captcha mode is active."""
    if not update.message or not update.effective_user or (update.effective_chat and update.effective_chat.type != "private"):
        return
    user_id = update.effective_user.id
    matching_keys = [k for k, s in PENDING_CAPTCHAS.items() if k[1] == user_id and s.get("type") == "simple"]
    if not matching_keys:
        return
    for key in matching_keys:
        state = PENDING_CAPTCHAS.pop(key, None)
        if not state:
            continue
        chat_id = key[0]
        if state.get("is_join_request"):
            await resolve_join_request_captcha_success(chat_id, user_id, state, update, context)
        elif state.get("is_pm_mode"):
            await resolve_captcha_pm_success(chat_id, user_id, state, update, context)


# ─────────────────────────────────────────────────────────────────────────────
# Admin Command: /captcha
# ─────────────────────────────────────────────────────────────────────────────

async def captcha_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Direct admin command to view and configure captcha settings."""
    if not update.effective_chat or not update.effective_user:
        return
        
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    
    if update.effective_chat.type == "private":
        await update.message.reply_text("This command can only be used inside a group by admins.")
        return
        
    from managers.user_manager_mongo import can_user_configure_settings
    if not await can_user_configure_settings(chat_id, user_id, context):
        await update.message.reply_text("❌ You must be a group administrator with Change Info and Ban rights to configure Captcha.")
        return
        
    from features.settings import get_captcha_settings_keyboard
    settings = get_chat_settings(chat_id)
    is_enabled = settings.get("captcha_enabled", False)
    test_type = settings.get("captcha_type", "button").capitalize()
    action = settings.get("captcha_action", "kick").capitalize()
    timeout = settings.get("captcha_timeout", 120)
    
    status_str = "✅ Enabled" if is_enabled else "❌ Disabled"
    vc_enabled = settings.get("captcha_vc_enabled", False)
    vc_status_str = "✅ Enabled" if vc_enabled else "❌ Disabled"
    loc_str = "Private (PM)" if settings.get("captcha_pm_mode", False) else "Group Chat"
    
    text = (
        f"🛡️ <b>Captcha Verification Configuration</b>\n\n"
        f"Group Join Captcha: <b>{status_str}</b>\n"
        f"VC Join Captcha: <b>{vc_status_str}</b>\n"
        f"Verification Location: <b>{loc_str}</b>\n"
        f"Test Type: <b>{test_type}</b>\n"
        f"Penalty on Failure: <b>{action}</b>\n"
        f"Timeout: <b>{timeout}s</b>\n\n"
        f"Newcomers must pass the verification test when joining the group or Voice Chat."
    )
    
    await update.message.reply_text(
        text,
        reply_markup=get_captcha_settings_keyboard(settings),
        parse_mode=ParseMode.HTML
    )


def get_captcha_handlers():
    """Returns handlers for newcomer captcha verification."""
    return [
        CommandHandler("captcha", captcha_command),
        CallbackQueryHandler(captcha_button_callback, pattern=r"^cpt_btn_"),
        CallbackQueryHandler(captcha_options_callback, pattern=r"^cpt_ans_"),
        CallbackQueryHandler(captcha_pm_button_callback, pattern=r"^cpt_pm_btn_"),
        CallbackQueryHandler(captcha_pm_options_callback, pattern=r"^cpt_pm_ans_"),
        CallbackQueryHandler(captcha_jr_button_callback, pattern=r"^cpt_jr_btn_"),
        CallbackQueryHandler(captcha_jr_options_callback, pattern=r"^cpt_jr_ans_"),
        MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.GROUPS, captcha_simple_message_handler),
        MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, captcha_pm_simple_message_handler),
    ]
