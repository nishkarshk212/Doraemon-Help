import logging
import datetime
from core.database import get_collection, COLLECTIONS

# In-memory cache for chat settings
SETTINGS_CACHE = {}

DEFAULT_CHAT_SETTINGS = {
    "welcome_enabled": True,
    "welcome_media_enabled": True,
    "welcome_button_enabled": True,
    "welcome_rejoin_enabled": True,
    "welcome_text": "Welcome {NAME} to the group!",
    "welcome_media": None,
    "welcome_media_type": "photo",
    "welcome_delete_time": 60,
    "welcome_delete_enabled": True,
    "welcome_clean_enabled": True,
    "welcome_buttons": [],
    "welcome_button_text": "Join Channel",
    "welcome_button_url": None,
    "last_welcome_id": None,
    "clean_service_enabled": True,
    "clean_join": True,
    "clean_left": True,
    "clean_video_chat_started": True,
    "clean_video_chat_ended": True,
    "clean_video_chat_invited": True,
    "clean_video_chat_scheduled": True,
    "clean_pinned_message": True,
    "clean_title": True,
    "clean_photo": True,
    "vc_user_join_enabled": True,
    "vc_user_leave_enabled": True,
    "vc_invite_notification_enabled": True,
    "auto_delete_enabled": False,
    "auto_delete_time": 60,
    "auto_delete_stickers": True,
    "auto_delete_media": True,
    "auto_delete_text": True,
    "warn_limit": 3,
    "warn_penalty": "ban",
    "block_warn_limit": 3,
    "block_warn_penalty": "warn",
    "msg_length_limit": 0, # 0 means disabled
    "command_deletion": False,
    "command_access": "all",
    "bot_protection_enabled": False,
    "bot_protection_apply_on": "members", # members, admins, everyone
    "link_spam_protection_enabled": False,
    "link_spam_apply_on": "members", # members, admins, everyone
    "forward_protection_enabled": False,
    "forward_protection_apply_on": "members", # members, admins, everyone
    "nightmode_restrict_links": False,
    "nightmode_apply_on": "members", # members, admins, everyone
    "nightmode_global_silence": False,
    # Language filter settings
    "language_filter_enabled": False,
    "allowed_languages": ["en", "hi", "hinglish"],
    # NSFW filter settings
    "nsfw_filter_enabled": False,
    "nsfw_action": "delete", # delete, warn, mute, ban, kick
    "nsfw_threshold": 0.7,   # Relevance threshold for NSFW match
    # Emoji and special character blocking
    "emoji_block_enabled": False,
    "block_emoji_only": True,
    "block_punctuation_only": True,
    # Manager module settings
    "manager_ban_enabled": True,
    "manager_unban_enabled": True,
    "manager_mute_enabled": True,
    "manager_unmute_enabled": True,
    "manager_kick_enabled": True,
    "manager_promote_enabled": True,
    "manager_demote_enabled": True,
    "manager_purge_enabled": True,
    "manager_pin_enabled": True,
    "manager_mass_actions_enabled": True,
    "manager_zombie_enabled": True,
    "manager_sg_enabled": True,
    "manager_id_enabled": True,
    "manager_info_enabled": True,
    "vc_safety_enabled": False,
    "vc_panic_mode_enabled": False,
    "tagger_enabled": True,
    # Captcha verification settings
    "captcha_enabled": False,
    "captcha_vc_enabled": False,
    "captcha_type": "button", # "simple", "button", "digits", "image"
    "captcha_timeout": 120,    # seconds before action
    "captcha_action": "kick",  # "kick", "mute", "ban"
    # Premium user boost gate
    "premium_boost_enabled": True,
    "premium_boost_count": 4,
    "boost_url": "https://t.me/boost/Titanic_World_Chatting_Group",
    # Recurring messages settings
    "recurring_messages": [
        {
            "id": 1,
            "active": False,
            "type": "time", # "time" or "messages"
            "interval": 1440, # minutes (default 24 hours)
            "message_interval": 100, # every 100 messages
            "text": None,
            "media": None,
            "media_type": None,
            "last_sent_at": None,
            "current_message_count": 0
        },
        {
            "id": 2,
            "active": False,
            "type": "time",
            "interval": 1440,
            "message_interval": 100,
            "text": None,
            "media": None,
            "media_type": None,
            "last_sent_at": None,
            "current_message_count": 0
        },
        {
            "id": 3,
            "active": False,
            "type": "time",
            "interval": 1440,
            "message_interval": 100,
            "text": None,
            "media": None,
            "media_type": None,
            "last_sent_at": None,
            "current_message_count": 0
        }
    ],
    # Group Link settings
    "group_link": None,
    # Banned Words settings
    "banned_words": [],
    "banned_words_penalty": "off", # "off", "warn", "kick", "mute", "ban"
    "banned_words_deletion": True,
    "banned_words_target": "members", # "members" or "everyone"
    "banned_words_warning_delete_time": 30,
    # Rules/Regulations settings
    "rules_text": None,
    "rules_media": None,
    "rules_media_type": None,
    "rules_buttons": [],
    # Command access levels (nobody, staff, all, private)
    "command_permissions": {
        "staff": "all",
        "rules": "staff",
        "me": "private",
        "translate": "all",
        "link": "all"
    },
    # Command access levels (admin, member, owner) - OLD STYLE (kept for compat)
    "cmd_access_start": "all",
    "cmd_access_help": "all",
    "cmd_access_id": "all",
    "cmd_access_info": "all",
    "cmd_access_report": "all",
    "cmd_access_settings": "admin",
    "cmd_access_ban": "admin",
    "cmd_access_unban": "admin",
    "cmd_access_mute": "admin",
    "cmd_access_unmute": "admin",
    "cmd_access_warn": "admin",
    "cmd_access_unwarn": "admin",
    "cmd_access_kick": "admin",
    "cmd_access_purge": "admin",
    "cmd_access_pin": "admin",
    "cmd_access_unpin": "admin",
    "cmd_access_promote": "admin",
    "cmd_access_demote": "admin",
    "cmd_access_staff": "all",
    "cmd_access_bots": "all",
    "cmd_access_zombies": "admin",
    "cmd_access_mass_actions": "admin",
    "cmd_access_psend": "admin",
    # Blocking settings
    "blocking_enabled": True,
    "block_stickers": False,
    "block_premium_sticker": False,
    "block_link": False,
    "block_embed_link": False,
    "block_media": False,
    "block_documents": False,
    "block_forward": False,
    "block_channel_post": False,
    "block_command": False,
    "block_contact": False,
    "block_location": False,
    "block_voice": False,
    "block_audio": False,
    "block_video_note": False,
    "block_poll": False,
    "block_dice": False,
    "block_game": False,
    "block_reactions": False,
    "msg_length_min": 0,
    "msg_length_max": 2000,
    "msg_length_delete": False,
    "msg_length_penalty": "off",
    # User permissions for blocking exemptions
    "user_permissions": {},
    # Anti-Flood settings
    "antiflood_enabled": False,
    "antiflood_limit": 5,
    "antiflood_window": 3,
    "antiflood_penalty": "mute", # warn, mute, ban, kick
    "antiflood_apply_on": "members", # members, everyone
    # Edit Check settings
    "edit_checks_enabled": False,
    "edit_checks_target": "members",
    "edit_checks_penalty": "off",
    "edit_checks_warn_limit": 3,
    # Bio Link Check settings
    "bio_link_check_enabled": False,
    "bio_link_target": "members",
    "bio_link_penalty": "warn",
    "bio_link_warn_limit": 3,
    # Emergency settings
    "emergency_enabled": False,
    "emergency_block_stickers": False,
    "emergency_block_text": False,
    "emergency_block_media": False,
    "emergency_block_links": False,
    "emergency_block_premium": False,
    "emergency_block_contact": False,
    "emergency_block_location": False,
    "emergency_block_voice": False,
    "emergency_block_audio": False,
    "emergency_block_forward": False,
    "emergency_block_poll": False,
    "emergency_start_time": "00:00",
    "emergency_end_time": "23:59",
    "emergency_mode": "daily", # daily, today
    "emergency_apply_on": "members",
    "blocking_delete_notifications": True,
    "blocking_notification_timer": 30,
    "blocking_custom_text": None,
    "ui_layout_type": 1, # 1 (Default/Large), 2 (Small/Compact)
    # High Protection & Shield Suite settings
    "shield_mode_enabled": False,
    "shield_backup_settings": None,
    "anti_raid_enabled": True,
    "anti_raid_threshold": 5,
    "anti_raid_window": 10,
    "anti_raid_action": "mute", # mute, kick, ban
    "anti_raid_mute_time": 900, # 15 minutes
    "anti_channel_enabled": False,
    "anti_channel_action": "delete_ban", # delete, delete_ban
    "mention_spam_enabled": True,
    "mention_spam_limit": 5,
    "mention_spam_action": "delete_warn", # delete, delete_warn, delete_mute
    "duplicate_spam_enabled": True,
    "duplicate_spam_limit": 3,
    "duplicate_spam_window": 10,
    "anti_zalgo_enabled": True,
    # Ranking, Chat Score, Level-Up & Daily Top 10 settings
    "ranking_enabled": True,
    "daily_leaderboard_auto_post": True,
    "level_up_alerts": True
}

def get_chat_settings(chat_id):
    """Get chat settings from MongoDB with in-memory caching."""
    chat_id_str = str(chat_id)
    
    # Return from cache if available
    if chat_id_str in SETTINGS_CACHE:
        return SETTINGS_CACHE[chat_id_str].copy()
        
    try:
        settings_col = get_collection(COLLECTIONS["settings"])
        if settings_col is None:
            logging.error("MongoDB not connected - returning default settings")
            return DEFAULT_CHAT_SETTINGS.copy()
        
        settings_doc = settings_col.find_one({"chat_id": chat_id_str})
        
        if settings_doc:
            # Merge with defaults to ensure all keys exist
            current_settings = settings_doc.get("settings", {})
            updated = False
            for key, default_value in DEFAULT_CHAT_SETTINGS.items():
                if key not in current_settings:
                    current_settings[key] = default_value
                    updated = True
            
            # Update the document with any new defaults if needed
            if updated:
                settings_col.update_one(
                    {"chat_id": chat_id_str},
                    {"$set": {"settings": current_settings, "updated_at": datetime.datetime.now()}}
                )
            
            # Store in cache
            SETTINGS_CACHE[chat_id_str] = current_settings.copy()
            return current_settings
        else:
            # Create new settings with defaults
            new_settings = DEFAULT_CHAT_SETTINGS.copy()
            settings_col.insert_one({
                "chat_id": chat_id_str,
                "settings": new_settings,
                "created_at": datetime.datetime.now(),
                "updated_at": datetime.datetime.now()
            })
            # Store in cache
            SETTINGS_CACHE[chat_id_str] = new_settings.copy()
            return new_settings
    except Exception as e:
        logging.error(f"Error getting chat settings: {e}")
        return DEFAULT_CHAT_SETTINGS.copy()

def update_chat_setting(chat_id, key, value):
    """Update a specific chat setting in MongoDB and update cache."""
    chat_id_str = str(chat_id)
    try:
        settings_col = get_collection(COLLECTIONS["settings"])
        if settings_col is None:
            logging.error("MongoDB not connected - cannot update setting")
            return False
        
        # Ensure the document exists
        settings_doc = settings_col.find_one({"chat_id": chat_id_str})
        if not settings_doc:
            # Create the document first with defaults
            current_settings = DEFAULT_CHAT_SETTINGS.copy()
            current_settings[key] = value
            settings_col.insert_one({
                "chat_id": chat_id_str,
                "settings": current_settings,
                "created_at": datetime.datetime.now(),
                "updated_at": datetime.datetime.now()
            })
            SETTINGS_CACHE[chat_id_str] = current_settings.copy()
            return True
        
        # Update the specific setting in DB
        result = settings_col.update_one(
            {"chat_id": chat_id_str},
            {
                "$set": {
                    f"settings.{key}": value,
                    "updated_at": datetime.datetime.now()
                }
            }
        )
        
        # Update cache if it exists
        if chat_id_str in SETTINGS_CACHE:
            SETTINGS_CACHE[chat_id_str][key] = value
        else:
            # Refresh full settings into cache
            get_chat_settings(chat_id)
            
        return result.modified_count > 0 or result.upserted_id is not None
    except Exception as e:
        logging.error(f"Error updating chat setting: {e}")
        return False

def delete_chat_settings(chat_id):
    """Delete all settings for a chat."""
    try:
        settings_col = get_collection(COLLECTIONS["settings"])
        if settings_col is None:
            return False
        
        chat_id_str = str(chat_id)
        result = settings_col.delete_one({"chat_id": chat_id_str})
        return result.deleted_count > 0
    except Exception as e:
        logging.error(f"Error deleting chat settings: {e}")
        return False

def get_all_chat_settings():
    """Get all chat settings (useful for admin functions)."""
    try:
        settings_col = get_collection(COLLECTIONS["settings"])
        if settings_col is None:
            return {}
        
        all_settings = {}
        cursor = settings_col.find({})
        
        for doc in cursor:
            all_settings[doc["chat_id"]] = doc.get("settings", {})
        
        return all_settings
    except Exception as e:
        logging.error(f"Error getting all chat settings: {e}")
        return {}

async def check_command_access(chat_id, user_id, command_name, context):
    """
    Check if a user has access to use a specific command.
    
    Args:
        chat_id: The chat ID
        user_id: The user ID trying to use the command
        command_name: The command name (e.g., 'ban', 'help', 'settings')
        context: The bot context
    
    Returns:
        bool: True if user has access, False otherwise
    """
    from managers.user_manager_mongo import is_user_admin
    
    settings = get_chat_settings(chat_id)
    cmd_key = f"cmd_access_{command_name.lower()}"
    access_level = settings.get(cmd_key, "admin")  # Default to admin if not set
    
    # Owner can always use all commands
    try:
        chat = await context.bot.get_chat(chat_id)
        if chat.type in ['group', 'supergroup']:
            admins = await context.bot.get_chat_administrators(chat_id)
            for admin in admins:
                if admin.user.id == user_id and admin.status == 'creator':
                    return True  # User is the creator/owner
    except:
        pass
    
    # Check access level
    if access_level == "all":
        return True  # Everyone can use it
    elif access_level == "admin":
        return await is_user_admin(chat_id, user_id, context)  # Only admins
    elif access_level == "owner":
        # Only group creator
        try:
            admins = await context.bot.get_chat_administrators(chat_id)
            for admin in admins:
                if admin.user.id == user_id and admin.status == 'creator':
                    return True
            return False
        except:
            return False
    
    return False  # Default deny
