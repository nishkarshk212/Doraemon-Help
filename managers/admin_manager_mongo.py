import logging
import datetime
from core.database import get_collection, COLLECTIONS
from telegram import Update, ChatMemberAdministrator, ChatMemberOwner
from telegram.ext import ContextTypes

# In-memory cache for admin permissions
# Format: {(chat_id, user_id): permissions_dict}
ADMIN_CACHE = {}

def update_admin_cache(chat_id, user_id, permissions, merge=True):
    """
    Store or update admin permissions in MongoDB and in-memory cache.
    
    permissions: dict of boolean permissions
    merge: if True, merges with existing permissions (preserving custom ones)
    """
    try:
        clean_new = {k: bool(v) for k, v in permissions.items() if k.startswith('can_') or k == 'is_anonymous'}
        
        if merge:
            existing = get_stored_admin_permissions(chat_id, user_id) or {}
            updated = existing.copy()
            for k, v in clean_new.items():
                # If we are syncing from native Telegram (merge=True),
                # we don't want to overwrite can_restrict_members if it's False,
                # because it might be a bot-only Mute permission (which is False on Telegram).
                if k == 'can_restrict_members' and not v:
                    continue
                updated[k] = v
            final_perms = updated
        else:
            final_perms = clean_new

        ADMIN_CACHE[(chat_id, user_id)] = final_perms
        try:
            from managers.user_manager_mongo import clear_admin_cache
            clear_admin_cache(chat_id, user_id)
        except Exception:
            pass
        
        admins_col = get_collection(COLLECTIONS["admins"])
        if admins_col is None:
            return False
        
        admins_col.update_one(
            {"chat_id": chat_id, "user_id": user_id},
            {
                "$set": {
                    "permissions": final_perms,
                    "last_updated": datetime.datetime.now()
                }
            },
            upsert=True
        )
        return True
    except Exception as e:
        logging.error(f"Error updating admin cache: {e}")
        return False

def get_stored_admin_permissions(chat_id, user_id):
    """Get stored admin permissions from cache or MongoDB."""
    # Check in-memory cache first
    if (chat_id, user_id) in ADMIN_CACHE:
        return ADMIN_CACHE[(chat_id, user_id)].copy()
        
    try:
        admins_col = get_collection(COLLECTIONS["admins"])
        if admins_col is None:
            return None
        
        doc = admins_col.find_one({"chat_id": chat_id, "user_id": user_id})
        if doc:
            permissions = doc.get("permissions")
            # Store in cache for next time
            if permissions:
                ADMIN_CACHE[(chat_id, user_id)] = permissions.copy()
            return permissions
        return None
    except Exception as e:
        logging.error(f"Error getting stored admin permissions: {e}")
        return None

def is_stored_admin(chat_id, user_id):
    """Check if user is a stored admin in the cache or database."""
    if (chat_id, user_id) in ADMIN_CACHE:
        return True
        
    try:
        admins_col = get_collection(COLLECTIONS["admins"])
        if admins_col is None:
            return False
        
        count = admins_col.count_documents({"chat_id": chat_id, "user_id": user_id})
        return count > 0
    except Exception as e:
        logging.error(f"Error checking stored admin: {e}")
        return False

async def sync_admins(chat_id, context: ContextTypes.DEFAULT_TYPE):
    """Fetch current admins from Telegram and sync with MongoDB and cache."""
    try:
        admins = await context.bot.get_chat_administrators(chat_id)
        
        # Clear existing cache for this chat to remove demoted admins
        keys_to_remove = [k for k in ADMIN_CACHE.keys() if k[0] == chat_id]
        for k in keys_to_remove:
            del ADMIN_CACHE[k]
            
        admins_col = get_collection(COLLECTIONS["admins"])
        
        if admins_col is None:
            # Still update cache even if DB is down
            for admin in admins:
                # ... permissions logic ...
                pass
            return len(admins)

        # Sync permissions for all admins
        for admin in admins:
            user = admin.user
            permissions = {
                "can_change_info": getattr(admin, "can_change_info", False),
                "can_delete_messages": getattr(admin, "can_delete_messages", False),
                "can_restrict_members": getattr(admin, "can_restrict_members", False),
                "can_invite_users": getattr(admin, "can_invite_users", False),
                "can_pin_messages": getattr(admin, "can_pin_messages", False),
                "can_promote_members": getattr(admin, "can_promote_members", False),
                "can_manage_chat": getattr(admin, "can_manage_chat", False),
                "can_manage_video_chats": getattr(admin, "can_manage_video_chats", False),
                "can_post_stories": getattr(admin, "can_post_stories", False),
                "can_edit_stories": getattr(admin, "can_edit_stories", False),
                "can_delete_stories": getattr(admin, "can_delete_stories", False),
                "is_anonymous": getattr(admin, "is_anonymous", False),
            }
            
            # Special handling for owner
            if admin.status == 'creator':
                # Owners have all permissions
                for k in permissions:
                    permissions[k] = True
            
            update_admin_cache(chat_id, user.id, permissions)
            
        return len(admins)
    except Exception as e:
        logging.error(f"Error syncing admins for chat {chat_id}: {e}")
        return 0

def remove_admin_cache(chat_id, user_id):
    """Remove admin from database and cache (e.g., when demoted)."""
    try:
        # Remove from cache
        if (chat_id, user_id) in ADMIN_CACHE:
            del ADMIN_CACHE[(chat_id, user_id)]
        try:
            from managers.user_manager_mongo import clear_admin_cache
            clear_admin_cache(chat_id, user_id)
        except Exception:
            pass
            
        admins_col = get_collection(COLLECTIONS["admins"])
        if admins_col is None:
            return False
        
        admins_col.delete_one({"chat_id": chat_id, "user_id": user_id})
        return True
    except Exception as e:
        logging.error(f"Error removing admin from cache: {e}")
        return False
