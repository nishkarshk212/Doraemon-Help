import logging
import re
from telegram import Update
from telegram.ext import ContextTypes, MessageHandler, filters
from managers.settings_manager_mongo import get_chat_settings
from managers.user_manager_mongo import is_user_admin
from core.config import OWNER_ID

# Regular expression to detect URLs
URL_PATTERN = re.compile(
    r'(https?://[^\s]+|'  # http:// or https://
    r'www\.[^\s]+|'  # www.
    r't\.me/[^\s]+|'  # t.me links
    r'telegram\.me/[^\s]+)',  # telegram.me links
    re.IGNORECASE
)

def contains_link(text):
    """Check if text contains a URL."""
    if not text:
        return False
    return bool(URL_PATTERN.search(text))

async def check_link_spam(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """MessageHandler to check for links in messages and delete them if link spam protection is enabled."""
    if not update.message or not update.effective_chat:
        return
        
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    
    # Skip private chats
    if update.effective_chat.type == "private":
        return
    
    # Get settings
    settings = get_chat_settings(chat_id)
    enabled = settings.get("link_spam_protection_enabled", False)
    logging.info(f"[LINK_SPAM] Checking chat {chat_id}, user {user_id}. enabled={enabled}")
    
    # Check if link spam protection is enabled
    if not enabled:
        return

    # Check "Apply On" setting
    apply_on = settings.get("link_spam_apply_on", "members")
    is_admin = await is_user_admin(chat_id, user_id, context)
    is_owner = user_id == OWNER_ID
    
    # Check for specific high-level permissions (Ban or Change Info)
    is_high_level_admin = False
    if is_admin and not is_owner:
        try:
            # For anonymous admins, treat them as high-level to be safe
            if user_id == 1087968824:
                is_high_level_admin = True
            else:
                member = await context.bot.get_chat_member(chat_id, user_id)
                is_high_level_admin = member.can_restrict_members or member.can_change_info
        except: pass

    # Logic for "Apply On"
    if apply_on == "members":
        # Apply ONLY on members (Exempt Admins and Owner)
        if is_admin or is_owner:
            return
    elif apply_on == "admins":
        # Apply ONLY on admins who are NOT high-level admins (Exempt high-level admins and Owner)
        # Also exempt regular members
        if not is_admin or is_high_level_admin or is_owner:
            return
    elif apply_on == "everyone":
        # Apply on EVERYONE including owner
        pass
    
    # Check if message contains a link (text regex + entities check for stealth hyperlinks)
    message_text = update.message.text or update.message.caption or ""
    has_link = contains_link(message_text)
    
    if not has_link:
        entities = (update.message.entities or ()) + (update.message.caption_entities or ())
        for ent in entities:
            if ent.type in ("url", "text_link"):
                has_link = True
                break

    if has_link:
        logging.info(f"[LINK_SPAM] Link detected in chat {chat_id} from user {user_id}. apply_on={apply_on}, is_admin={is_admin}")
        try:
            # Delete the message with link
            await update.message.delete()
            logging.info(f"Link Spam Protection: Deleted message from user {user_id} in chat {chat_id}")
            
            # Notify the user
            from core.config import send_bot_response
            await send_bot_response(
                update, context,
                f"⚠️ User <code>{update.effective_user.id}</code>, links are not allowed in this group.",
                parse_mode='HTML'
            )
        except Exception as e:
            logging.error(f"Link Spam Protection: Failed to delete message from user {user_id} in chat {chat_id}: {e}")

def get_link_spam_handlers():
    """Returns the handlers for link spam protection."""
    return [
        MessageHandler(
            filters.ALL & ~filters.COMMAND,
            check_link_spam
        )
    ]
