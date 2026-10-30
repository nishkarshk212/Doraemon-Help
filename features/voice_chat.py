import logging
import os
import asyncio
import datetime
import html
from telethon import TelegramClient, events
from telethon.tl.functions.phone import GetGroupCallRequest, EditGroupCallParticipantRequest
from telethon.tl.types import UpdateGroupCallParticipants, PeerChat, PeerChannel, PeerUser, UpdateGroupCall, InputGroupCall
from telethon.tl.functions.channels import GetFullChannelRequest
from telethon.tl.functions.messages import GetFullChatRequest
from telethon.sessions import StringSession
from core.config import colored_button
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, ChatMemberBanned
from telegram.constants import ParseMode
import core.config as config
from core.config import BOT_TOKEN, to_small_caps, LOG_CHANNEL_ID, log_to_channel
from features.font import to_mono
from telegram.ext import Application, CallbackQueryHandler, ContextTypes, MessageHandler, filters, CommandHandler
from managers.settings_manager_mongo import get_chat_settings
from managers.user_manager_mongo import is_user_admin
from collections import defaultdict
from core.database import get_collection, COLLECTIONS

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Telethon Credentials
def _safe_int_env(name, default=0):
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default

API_ID = _safe_int_env("API_ID", 0)
API_HASH = os.getenv("API_HASH", "")
STRING_SESSION = os.getenv("STRING_SESSION", "")

# Initialize Telethon Client
telethon_client = None
ptb_application = None

# Cache to prevent duplicate notifications
notification_cache = {}

# Map call IDs to chat entities
call_to_chat = {}

# Locks for processing VC events to avoid race conditions
processing_locks = defaultdict(asyncio.Lock)

# Glitch/Dedox Protection
vc_glitch_cache = {} # (chat_id, user_id) -> timestamp
vc_flood_counter = {} # chat_id -> (count, timestamp)

# Pending invites: (chat_id, user_id) -> list of message_ids to delete upon join
pending_invites = {}

# Users invited to VC by group admins: chat_id -> set(user_id)
admin_vc_invites = defaultdict(set)

# Timestamp when the monitor started — ignore UpdateGroupCallParticipants for first 15s
_vc_monitor_start_ts = None

# Calls we've already resolved but the bot isn't a member — skip resolution next time
_failed_resolutions = {} # call_id -> timestamp

# Track current participants per call to detect actual joins/leaves
# call_id -> set(user_id)
current_participants_map = {}

def get_peer_id(peer):
    """Helper to get ID from PeerUser, PeerChannel, or PeerChat."""
    if isinstance(peer, PeerUser):
        return peer.user_id
    elif isinstance(peer, PeerChannel):
        return peer.channel_id
    elif isinstance(peer, PeerChat):
        return peer.chat_id
    return None

async def start_voice_chat_monitor(application: Application):
    """Starts the Telethon client to monitor voice chat events."""
    global telethon_client, ptb_application
    ptb_application = application
    
    logger.info(f"🎙 VC Monitor: LOG_CHANNEL_ID is {LOG_CHANNEL_ID}")
    
    if not API_ID or not API_HASH or not STRING_SESSION:
        logger.warning("⚠️ Telethon credentials missing. Voice chat join notifications disabled.")
        return

    logger.info("🎙 Starting Voice Chat Monitor...")
    try:
        telethon_client = TelegramClient(StringSession(STRING_SESSION), API_ID, API_HASH)
    except Exception as e:
        logger.warning(f"⚠️ Telethon StringSession invalid or incompatible: {e}. VC join notifications disabled.")
        return

    # Register Telethon event handlers BEFORE connecting to avoid missing events
    @telethon_client.on(events.Raw(UpdateGroupCall))
    async def handle_group_call_update(event):
        """Handles updates to group calls to map call IDs to chat entities."""
        try:
            call = event.call
            peer = getattr(event, 'peer', None)
            if peer is None:
                logger.info(f"📞 UpdateGroupCall: call={call.id}, no peer (flags missing)")
                return
            peer_id = getattr(peer, 'channel_id', None) or getattr(peer, 'chat_id', None) or getattr(peer, 'user_id', None)
            if peer_id is None:
                logger.info(f"📞 UpdateGroupCall: call={call.id}, peer={type(peer).__name__} with no id")
                return
            logger.info(f"📞 UpdateGroupCall: call_id={call.id}, peer_id={peer_id}")
            try:
                # Resolve the PTB-compatible chat ID
                # 1. First try Telethon entity resolution
                try:
                    entity = await telethon_client.get_entity(peer_id)
                    from telethon.tl.types import Channel
                    if isinstance(entity, Channel) or getattr(entity, 'broadcast', False) or getattr(entity, 'megagroup', False):
                        peer_full = int(f"-100{peer_id}")
                    else:
                        peer_full = -peer_id
                except:
                    # Fallback 1: Assume it's a channel/supergroup
                    peer_full = int(f"-100{peer_id}")
                
                # 2. Check if bot is in the chat
                try:
                    await ptb_application.bot.get_chat(peer_full)
                    logger.info(f"✅ Bot is in chat {peer_full}. Mapping call {call.id}.")
                except Exception as e:
                    # Fallback 2: Try without -100 prefix for legacy groups
                    if str(peer_full).startswith("-100"):
                        legacy_id = -peer_id
                        try:
                            await ptb_application.bot.get_chat(legacy_id)
                            peer_full = legacy_id
                            logger.info(f"✅ Bot is in legacy chat {peer_full}. Mapping call {call.id}.")
                        except:
                            logger.info(f"⚠️ Bot might not be in chat {peer_full} or {-peer_id} ({e}). Mapping anyway per user request.")
                    else:
                        logger.info(f"⚠️ Bot might not be in chat {peer_full} ({e}). Mapping anyway per user request.")
                
                # If we reached here, we map the call regardless of get_chat success
                if 'entity' in locals():
                    call_to_chat[call.id] = entity
                    logger.info(f"📞 Mapped call {call.id} to chat {entity.id} (entity)")
                else:
                    call_to_chat[call.id] = peer_full
                    logger.info(f"📞 Mapped call {call.id} to chat {peer_full} (ID only)")
            except Exception as e:
                logger.warning(f"Could not get entity for call {call.id} peer {peer_id}: {e}")
        except Exception as e:
            logger.error(f"Error in handle_group_call_update: {e}")

    @telethon_client.on(events.Raw(UpdateGroupCallParticipants))
    async def handle_voice_chat_join(event):
        """Handles voice chat join events for Users and Channels."""
        logger.info(f"📥 Received UpdateGroupCallParticipants for call {event.call.id} with {len(event.participants)} participants")
        asyncio.create_task(_process_vc_join_event(event))
    
    # Register PTB handlers for invite notifications and VC commands
    # We do this OUTSIDE the try block so they work even if Telethon fails
    handlers = get_voice_chat_handlers()
    for handler in handlers:
        ptb_application.add_handler(handler)
    logger.info(f"✅ Voice Chat handlers registered: {[type(h) for h in handlers]}")

    # Connect and start the client properly
    try:
        await telethon_client.start()
        global _vc_monitor_start_ts
        _vc_monitor_start_ts = datetime.datetime.now()
        logger.info("✅ Telethon client started successfully!")
        
        # Load dialogs to populate cache
        logger.info("📂 Loading dialogs for Telethon client...")
        dialogs = await telethon_client.get_dialogs(limit=50)
        logger.info(f"✅ Loaded {len(dialogs)} dialogs.")
        
        # Start automatic ghost cleaner
        asyncio.create_task(auto_ghost_cleaner_task())
        
        # Pre-scan active VCs to populate call_to_chat mapping
        asyncio.create_task(scan_active_vcs())
    except Exception as e:
        logger.error(f"❌ Failed to start Telethon client: {e}")
        
        # Notify the log channel if possible
        if LOG_CHANNEL_ID:
            try:
                error_text = f"❌ <b>Voice Chat Monitor Failed to Start</b>\n\nError: <code>{e}</code>"
                if "AuthKeyDuplicatedError" in str(e):
                    error_text += "\n\n⚠️ <b>Root Cause:</b> Your <code>STRING_SESSION</code> is being used elsewhere. Please generate a new session string."
                await ptb_application.bot.send_message(chat_id=LOG_CHANNEL_ID, text=error_text, parse_mode='HTML')
            except: pass
        return

async def cleanup_vc_caches():
    """Clean up expired entries from glitch/flood/notification caches."""
    now = datetime.datetime.now()
    
    # Clean vc_glitch_cache entries older than 30 seconds
    expired_glitch = [k for k, ts in vc_glitch_cache.items() if (now - ts).total_seconds() > 30]
    for k in expired_glitch:
        del vc_glitch_cache[k]
    if expired_glitch:
        logger.debug(f"Cleaned {len(expired_glitch)} expired glitch cache entries")
    
    # Clean notification_cache entries older than 6 minutes (buffer above 5min cooldown)
    expired_notif = [k for k, ts in notification_cache.items() if (now - ts).total_seconds() > 360]
    for k in expired_notif:
        del notification_cache[k]
    
    # Clean flood counter entries older than 90 seconds
    expired_flood = [k for k, (c, ts) in vc_flood_counter.items() if (now - ts).total_seconds() > 90]
    for k in expired_flood:
        del vc_flood_counter[k]

async def auto_ghost_cleaner_task():
    """Periodically cleans up ghost participants and VC caches."""
    await asyncio.sleep(60) # Initial delay
    
    while True:
        try:
            # Clean up expired cache entries first
            await cleanup_vc_caches()
            
            # Find all chats with vc_safety_enabled = True
            collection = get_collection(COLLECTIONS['settings'])
            if collection is None:
                await asyncio.sleep(1800)
                continue
                
            chats_with_safety = list(collection.find({"vc_safety_enabled": True}))
            
            for chat_doc in chats_with_safety:
                chat_id = chat_doc.get("chat_id")
                if chat_id:
                    logger.info(f"🧹 Auto-cleaning ghosts for chat {chat_id}")
                    await clean_ghost_participants(chat_id)
                    await asyncio.sleep(5) # Delay between groups to avoid flood
                    
        except Exception as e:
            logger.error(f"Error in auto_ghost_cleaner_task: {e}")
            
        await asyncio.sleep(3600) # Run every hour

async def scan_active_vcs():
    """Pre-scan all groups for active VCs to populate call_to_chat mapping."""
    try:
        logger.info("🔍 Scanning groups for active VCs...")
        collection = get_collection(COLLECTIONS['settings'])
        if collection is None:
            return
        all_chats = list(collection.find({}))
        count = 0
        for chat_doc in all_chats:
            cid = chat_doc.get("chat_id")
            if not cid:
                continue
            try:
                clean_id = int(str(cid).replace('-100', ''))
                try:
                    full = await telethon_client(GetFullChannelRequest(channel=clean_id))
                except Exception as e:
                    # logger.debug(f"Could not get full channel {clean_id}: {e}")
                    try:
                        full = await telethon_client(GetFullChatRequest(chat_id=clean_id))
                    except:
                        continue
                    
                if hasattr(full, 'full_chat') and hasattr(full.full_chat, 'call') and full.full_chat.call:
                    call_id = full.full_chat.call.id
                    if call_id not in call_to_chat:
                        try:
                            # Verify if the bot is actually in the group before mapping
                            try:
                                await ptb_application.bot.get_chat(cid)
                            except Exception as e:
                                # Try legacy ID fallback
                                if str(cid).startswith("-100"):
                                    legacy_id = -int(str(cid).replace("-100", ""))
                                    await ptb_application.bot.get_chat(legacy_id)
                                    cid = legacy_id
                                else:
                                    raise e

                            entity = await telethon_client.get_entity(clean_id)
                            
                            # Populate Telethon's entity cache for this group
                            logger.info(f"👥 Populating entity cache for group {cid}...")
                            await telethon_client.get_participants(entity, limit=200)
                            
                            # Populate initial participant map to avoid ghost joins
                            try:
                                call_info = await telethon_client(GetGroupCallRequest(call=full.full_chat.call, limit=100))
                                current_participants_map[call_id] = set()
                                for p in call_info.participants:
                                    if not getattr(p, 'left', False):
                                        uid = get_peer_id(p.peer)
                                        if uid: current_participants_map[call_id].add(uid)
                                logger.info(f"👥 Initialized map for call {call_id} with {len(current_participants_map[call_id])} users")
                            except Exception as ce:
                                logger.warning(f"Could not get initial participants for call {call_id}: {ce}")

                            call_to_chat[call_id] = entity
                            count += 1
                            logger.info(f"🔍 Pre-scanned active VC: call {call_id} in group {cid}")
                        except Exception as e:
                            # logger.debug(f"Skipping call {call_id} in group {cid}: bot not a member or {e}")
                            continue
                await asyncio.sleep(0.5) # Faster scan on startup
            except:
                continue
        logger.info(f"🔍 Pre-scan complete: mapped {count} active VCs")
    except Exception as e:
        logger.error(f"Error in scan_active_vcs: {e}")

async def _process_vc_join_event(event):
    """Internal function to process VC join events asynchronously."""
    call_id = event.call.id
    
    # Use a lock per call_id to prevent race conditions during state updates
    async with processing_locks[call_id]:
        try:
            # Skip initial state dump events during the first few seconds after startup
            # increased to 10s to be safe
            if _vc_monitor_start_ts and (datetime.datetime.now() - _vc_monitor_start_ts).total_seconds() < 10:
                if call_id not in current_participants_map:
                    current_participants_map[call_id] = set()
                    for p in event.participants:
                        if not getattr(p, 'left', False):
                            uid = get_peer_id(p.peer)
                            if uid: current_participants_map[call_id].add(uid)
                    logger.info(f"Ignoring startup sync for call {call_id} (initialized with {len(current_participants_map[call_id])} users)")
                return
            
            chat_entity = call_to_chat.get(call_id)
            
            # If call_to_chat missing, try resolving the chat from the call itself
            now = datetime.datetime.now()
            last_failed = _failed_resolutions.get(call_id)
            
            # Try resolution if missing, and haven't failed recently (cooldown 5 mins)
            if not chat_entity and (not last_failed or (now - last_failed).total_seconds() > 300):
                try:
                    logger.info(f"🔍 Attempting to resolve call {call_id} via GetGroupCallRequest...")
                    result = await telethon_client(GetGroupCallRequest(call=event.call, limit=1))
                    if result and result.chats:
                        raw_entity = result.chats[0]
                        raw_id = raw_entity.id
                        logger.info(f"📡 Found raw_entity {raw_id} for call {call_id}")
                        
                        # Determine PTB compatible ID
                        from telethon.tl.types import Channel
                        if isinstance(raw_entity, Channel) or getattr(raw_entity, 'broadcast', False) or getattr(raw_entity, 'megagroup', False):
                            bot_check_id = int(f"-100{raw_id}")
                        else:
                            bot_check_id = -raw_id
                            
                        try:
                            await ptb_application.bot.get_chat(bot_check_id)
                            chat_entity = raw_entity
                            call_to_chat[call_id] = chat_entity
                            _failed_resolutions.pop(call_id, None)
                            logger.info(f"✅ Successfully resolved call {call_id} -> chat {bot_check_id} ({type(raw_entity).__name__})")
                        except Exception as e:
                            # Fallback: Try without -100 prefix for legacy groups
                            if str(bot_check_id).startswith("-100"):
                                legacy_id = -raw_id
                                try:
                                    await ptb_application.bot.get_chat(legacy_id)
                                    bot_check_id = legacy_id
                                    chat_entity = raw_entity
                                    call_to_chat[call_id] = chat_entity
                                    _failed_resolutions.pop(call_id, None)
                                    logger.info(f"✅ Successfully resolved call {call_id} -> legacy chat {bot_check_id}")
                                except:
                                    _failed_resolutions[call_id] = now
                                    logger.info(f"❌ Skipping call {call_id}: bot not in chat {bot_check_id} or error: {e}")
                            else:
                                _failed_resolutions[call_id] = now
                                logger.info(f"❌ Skipping call {call_id}: bot not in chat {bot_check_id} or error: {e}")
                    else:
                        # Fallback: Deep scan all groups if GetGroupCallRequest returns nothing
                        logger.info(f"❓ GetGroupCallRequest empty for {call_id}. Starting deep scan fallback...")
                        
                        # 1. First try scanning dialogs the account is already in
                        logger.info(f"🔎 Scanning active dialogs for call {call_id}...")
                        dialogs = await telethon_client.get_dialogs(limit=100)
                        found = False
                        for dialog in dialogs:
                            if dialog.is_group or dialog.is_channel:
                                try:
                                    full = await telethon_client(GetFullChannelRequest(channel=dialog.entity)) if dialog.is_channel else await telethon_client(GetFullChatRequest(chat_id=dialog.id))
                                    if hasattr(full, 'full_chat') and hasattr(full.full_chat, 'call') and full.full_chat.call and full.full_chat.call.id == call_id:
                                        call_to_chat[call_id] = dialog.entity
                                        _failed_resolutions.pop(call_id, None)
                                        logger.info(f"🎯 Dialog scan found call {call_id} in chat {dialog.id}")
                                        
                                        # Populate cache
                                        try: await telethon_client.get_participants(dialog.entity, limit=100)
                                        except: pass
                                        
                                        found = True
                                        chat_entity = dialog.entity
                                        break
                                except: continue
                        
                        # 2. If still not found, scan all groups in DB (backup)
                        if not found:
                            collection = get_collection(COLLECTIONS['settings'])
                            if collection is not None:
                                all_chats = list(collection.find({}))
                                logger.info(f"🔎 Scanning {len(all_chats)} database groups for call {call_id}...")
                                for chat_doc in all_chats:
                                    cid = chat_doc.get("chat_id")
                                    if not cid: continue
                                    try:
                                        clean_id = int(str(cid).replace('-100', ''))
                                        try:
                                            full = await telethon_client(GetFullChannelRequest(channel=clean_id))
                                        except Exception as e:
                                            full = await telethon_client(GetFullChatRequest(chat_id=clean_id))
                                            
                                        if hasattr(full, 'full_chat') and hasattr(full.full_chat, 'call') and full.full_chat.call:
                                            if full.full_chat.call.id == call_id:
                                                entity = await telethon_client.get_entity(clean_id)
                                                chat_entity = entity
                                                call_to_chat[call_id] = chat_entity
                                                _failed_resolutions.pop(call_id, None)
                                                logger.info(f"🎯 DB scan found call {call_id} in chat {cid}")
                                                found = True
                                                break
                                    except Exception as e:
                                        continue
                            
                        if not found:
                            _failed_resolutions[call_id] = now
                            logger.info(f"❌ Resolution fallback failed for call {call_id}")
                except Exception as resolve_err:
                    _failed_resolutions[call_id] = now
                    logger.warning(f"⚠️ Could not resolve chat for call {call_id}: {resolve_err}")
            
            # Glitch/Dedox Safety: Massive join events
            if len(event.participants) > 20: 
                logger.warning(f"⚠️ Massive VC join event ({len(event.participants)} users).")
                if chat_entity:
                    cid = chat_entity.id if not isinstance(chat_entity, int) else chat_entity
                    asyncio.create_task(clean_ghost_participants(cid))

            # Track participants to avoid ghost joins
            if call_id not in current_participants_map:
                # If this is the first event for a call, and it has multiple participants, 
                # it's likely an existing call we just discovered.
                # If it has only 1, it's likely a new join we should notify.
                is_state_sync = len(event.participants) > 2
                
                current_participants_map[call_id] = set()
                if is_state_sync:
                    for p in event.participants:
                        if not getattr(p, 'left', False):
                            uid = get_peer_id(p.peer)
                            if uid: current_participants_map[call_id].add(uid)
                    logger.info(f"Initialized participant map for call {call_id} (sync mode, {len(current_participants_map[call_id])} users)")
                    return
                else:
                    logger.info(f"Initialized empty participant map for NEW call {call_id}")

            # Analyze batch for actual changes
            leaves = []
            joins = []
            
            current_set = current_participants_map[call_id]
            
            for p in event.participants:
                uid = get_peer_id(p.peer)
                if not uid: continue
                
                is_left = getattr(p, 'left', False)
                
                if is_left:
                    if uid in current_set:
                        leaves.append(p)
                        current_set.discard(uid)
                else:
                    if uid not in current_set:
                        joins.append(p)
                        current_set.add(uid)

            # Identity Switch Detection:
            # If someone leaves and joins in the same update, it's often a switch.
            # We look for participants with different peer types but possibly related.
            # Simplified: if len(leaves) == 1 and len(joins) == 1, we treat it as a potential switch.
            is_switch_event = len(leaves) == 1 and len(joins) == 1
            
            # Process participants
            for participant in leaves:
                asyncio.create_task(_process_single_participant(call_id, participant, chat_entity, is_join=False, is_switch=is_switch_event))
                
            for participant in joins:
                asyncio.create_task(_process_single_participant(call_id, participant, chat_entity, is_join=True, is_switch=is_switch_event))
        except Exception as e:
            logger.error(f"❌ Error in _process_vc_join_event: {e}")

async def _process_single_participant(call_id, participant, chat_entity, is_join=True, is_switch=False):
    """Process a single VC participant join or leave."""
    try:
        peer = participant.peer
        user_id = None
        peer_type = "user"
        
        if isinstance(peer, PeerUser):
            user_id = peer.user_id
            peer_type = "user"
        elif isinstance(peer, PeerChannel):
            user_id = peer.channel_id
            peer_type = "channel"
        elif isinstance(peer, PeerChat):
            user_id = peer.chat_id
            peer_type = "chat"
        
        if not user_id: 
            logger.info(f"⚠️ Skipping participant in call {call_id}: no user_id found in peer {type(peer).__name__}")
            return
        
        logger.info(f"👤 [START] Processing {user_id} in call {call_id} (Join: {is_join}, Switch: {is_switch})")
        
        # Use timeout and handle "Entity not found" gracefully
        try:
            # 1. Check Chat Entity
            if not chat_entity:
                logger.info(f"❌ [STOP] No chat_entity for {user_id} in call {call_id}")
                return

            # Determine the PTB-compatible chat ID
            if isinstance(chat_entity, int):
                settings_chat_id = chat_entity
            else:
                # chat_entity is a Telethon entity
                raw_id = chat_entity.id
                from telethon.tl.types import Channel
                if isinstance(chat_entity, Channel) or getattr(chat_entity, 'broadcast', False) or getattr(chat_entity, 'megagroup', False):
                    settings_chat_id = int(f"-100{raw_id}")
                else:
                    settings_chat_id = -raw_id

            # 2. Check Cooldowns/Glitches
            now = datetime.datetime.now()
            if is_join:
                # Glitch check
                glitch_key = f"{user_id}_{call_id}_{participant.date}"
                if glitch_key in vc_glitch_cache:
                    if (now - vc_glitch_cache[glitch_key]).total_seconds() < 10:
                        logger.warning(f"🚫 [STOP] Glitch detected for {user_id}")
                        return
                vc_glitch_cache[glitch_key] = now

                # Cooldown check
                notif_key = f"{user_id}_{call_id}"
                if notif_key in notification_cache:
                    if (now - notification_cache[notif_key]).total_seconds() < 300:
                        logger.info(f"⏸ [STOP] Cooldown active for {user_id}")
                        return
            else:
                leave_cache_key = f"leave_{user_id}_{call_id}"
                if leave_cache_key in notification_cache:
                    if (now - notification_cache[leave_cache_key]).total_seconds() < 30: # Increased to 30s
                        logger.info(f"⏸ [STOP] Leave cooldown active for {user_id}")
                        return
                notification_cache[leave_cache_key] = now
                
                # DO NOT clear join cache immediately to prevent Join-Leave-Join spam glitch
                # join_notif_key = f"{user_id}_{call_id}"
                # notification_cache.pop(join_notif_key, None)
                
                if is_switch:
                    logger.info(f"🔄 [STOP] Identity switch for {user_id} - suppressing 'Left' notification")
                    return

            # 3. Get Entity
            name = "User" # Default name
            entity = None
            
            try:
                logger.info(f"🔍 [GET_ENTITY] Fetching {peer_type} {user_id}...")
                entity = await asyncio.wait_for(telethon_client.get_entity(peer), timeout=10.0)
                name = getattr(entity, 'first_name', getattr(entity, 'title', "User"))
                logger.info(f"✅ [ENTITY_FOUND] Name: {name}")
            except Exception as e:
                # Fallback: try to find the user in the group participants
                try:
                    logger.info(f"🔍 [GET_ENTITY_FALLBACK] Searching for {user_id} in group participants...")
                    participants = await telethon_client.get_participants(chat_entity, search=str(user_id))
                    if participants:
                        entity = participants[0]
                        name = getattr(entity, 'first_name', getattr(entity, 'title', "User"))
                        logger.info(f"✅ [ENTITY_FOUND_FALLBACK] Name: {name}")
                    else:
                        # Final attempt: use PTB fallback for {user_id}...
                        try:
                            logger.info(f"🔍 [GET_ENTITY_FINAL] Using PTB fallback for {user_id}...")
                            chat_member = await ptb_application.bot.get_chat_member(chat_id=settings_chat_id, user_id=user_id)
                            user = chat_member.user
                            name = user.first_name
                            # Create a dummy entity object for the rest of the logic
                            class DummyEntity:
                                def __init__(self, u):
                                    self.username = u.username
                                    self.first_name = u.first_name
                            entity = DummyEntity(user)
                            logger.info(f"✅ [ENTITY_FOUND_FINAL] Name: {name}")
                        except Exception as ptb_err:
                            logger.info(f"ℹ️ [ENTITY_NOT_FOUND] {user_id} in {settings_chat_id}: {ptb_err}. Proceeding with default name.")
                            # Create a minimal dummy entity if we still don't have one
                            if not entity:
                                class MinimalEntity:
                                    def __init__(self):
                                        self.username = None
                                        self.first_name = "User"
                                entity = MinimalEntity()
                except Exception as fallback_err:
                    logger.warning(f"⚠️ [ENTITY_RESOLUTION_FAILED] {user_id}: {fallback_err}. Proceeding with default name.")
                    if not entity:
                        class MinimalEntity:
                            def __init__(self):
                                self.username = None
                                self.first_name = "User"
                        entity = MinimalEntity()

            # 4. Check Settings
            settings = get_chat_settings(settings_chat_id)

            # VC Captcha Check for users who join VC without joining the group
            if is_join and settings.get("captcha_vc_enabled", False):
                try:
                    is_admin = await is_user_admin(settings_chat_id, user_id, ptb_application)
                except:
                    is_admin = False

                if not is_admin:
                    # Check if user has joined the group
                    is_group_member = False
                    try:
                        cm = await ptb_application.bot.get_chat_member(chat_id=settings_chat_id, user_id=user_id)
                        if cm and cm.status in ['member', 'administrator', 'creator', 'restricted']:
                            is_group_member = True
                    except Exception:
                        is_group_member = False

                    if not is_group_member:
                        # Check if user was invited to VC by a group admin
                        if user_id in admin_vc_invites.get(settings_chat_id, set()):
                            logger.info(f"User {user_id} was invited to VC by an admin in {settings_chat_id}, bypassing VC captcha.")
                        else:
                            logger.info(f"🛡️ Triggering VC Captcha for user {user_id} in chat {settings_chat_id} (joined VC without group join)")
                            # Mute in VC immediately
                            await mute_vc_participant(settings_chat_id, user_id, muted=True)

                            # Trigger VC captcha challenge
                            from features.captcha import start_vc_captcha_verification
                            asyncio.create_task(start_vc_captcha_verification(
                                chat_id=settings_chat_id,
                                user_id=user_id,
                                user_entity=entity,
                                call_id=call_id,
                                ptb_app=ptb_application
                            ))
                            # Suppress regular join notification while verification is pending
                            return

            if is_join and not settings.get("vc_user_join_enabled", True):
                logger.info(f"🔇 [STOP] Join notifs disabled in {settings_chat_id}")
                return
            if not is_join and not settings.get("vc_user_leave_enabled", True):
                logger.info(f"🔇 [STOP] Leave notifs disabled in {settings_chat_id}")
                return

            # 5. Flood Protection (Join only)
            if is_join:
                chat_flood_key = settings_chat_id
                count, last_ts = vc_flood_counter.get(chat_flood_key, (0, now))
                if (now - last_ts).total_seconds() < 60: count += 1
                else: count = 1
                vc_flood_counter[chat_flood_key] = (count, now)
                
                if count > 15: # Standard threshold
                    logger.warning(f"🚨 [FLOOD] chat {settings_chat_id} count {count}")
                    asyncio.create_task(clean_ghost_participants(settings_chat_id))
                    return

        # 6. Send Message
            username_str = f"@{entity.username}" if getattr(entity, 'username', None) else "—"
            display_id = user_id
            
            # For Channels/Chats, ensure we show the -100 prefix if missing
            if peer_type in ["channel", "chat"] and not str(user_id).startswith("-100"):
                display_id = f"-100{user_id}"
            
            # Formatting mention based on peer type
            if peer_type == "channel":
                mention = f'<a href="https://t.me/{entity.username}">{name}</a>' if getattr(entity, 'username', None) else f"<b>{name}</b>"
            elif peer_type == "chat":
                mention = f"<b>{name}</b>"
            else:
                mention = f'<a href="tg://user?id={user_id}">{name}</a>'

            # Status logic and header
            if is_join:
                header = "#𝛁𝐂_𝐉𝐎𝚰𝚴_𝚴𝐎𝚻𝚰𝐅𝚰𝐂𝚨𝚻𝚰𝐎𝚴"
                if is_switch:
                    status_text = "Switched Identity 🔄"
                else:
                    status_text = "Joined ✅"
            else:
                header = "#𝛁𝐂_𝐋𝐄𝚨𝐕𝚵_𝚴𝐎𝚻𝚰𝐅𝚰𝐂𝚨𝚻𝚰𝐎𝚴"
                status_text = "Left ❌"
            
            bot_info = await ptb_application.bot.get_me()
            
            # Dynamic Join Link
            chat_obj = None
            try:
                chat_obj = await ptb_application.bot.get_chat(settings_chat_id)
            except: pass
            
            if chat_obj and chat_obj.username:
                join_link = f"https://t.me/{chat_obj.username}?videochat"
            else:
                clean_id = str(settings_chat_id).replace("-100", "")
                join_link = f"https://t.me/c/{clean_id}?videochat"

            notification_text = (
                f"<b>{header}</b>\n\n"
                f"<blockquote>\n"
                f"𝚴𝛂ϻ𝛆 ➛ {mention}\n"
                f"𝚰𝛛 ➛ <code>{display_id}</code>\n"
                f"𝐔𝛅𝛆𝛑𝛈𝛂ϻ𝛆 ➛ {username_str}\n"
                f"Status ➛ {status_text}\n"
                f"</blockquote>"
            )
            
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton(colored_button("𝐉𝐨𝐢𝐧 𝐕𝐨𝐢𝐜𝐞 𝐂𝐡𝐚𝐭", "default"), url=join_link)],
                [InlineKeyboardButton(to_small_caps("+ ᴀᴅᴅ ᴍᴇ ɪɴ ɢʀᴏᴜᴘ +"), url=f"https://t.me/{bot_info.username}?startgroup=true")]
            ])
            
            sent_message = await ptb_application.bot.send_message(
                chat_id=settings_chat_id,
                text=notification_text,
                reply_markup=keyboard,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True
            )
            logger.info(f"📤 [SENT] {user_id} to group {settings_chat_id}")
            
            # Log to log channel
            if config.LOG_CHANNEL_ID:
                try:
                    # Ensure LOG_CHANNEL_ID is an integer
                    try:
                        target_log_id = int(str(config.LOG_CHANNEL_ID))
                    except:
                        target_log_id = config.LOG_CHANNEL_ID

                    chat_title = "Unknown Group"
                    if chat_obj:
                        chat_title = getattr(chat_obj, 'title', str(settings_chat_id))
                    
                    log_msg = (
                        f"🎙 <b>VC {'Join' if is_join else 'Leave'}</b>\n"
                        f"👤 <b>User:</b> {mention} (<code>{user_id}</code>)\n"
                        f"📍 <b>Group:</b> {html.escape(chat_title)} (<code>{settings_chat_id}</code>)\n"
                        f"🕒 <b>Time:</b> {now.strftime('%Y-%m-%d %H:%M:%S')}"
                    )
                    await ptb_application.bot.send_message(chat_id=target_log_id, text=log_msg, parse_mode=ParseMode.HTML)
                    logger.info(f"📤 [LOGGED] {user_id} to log channel {target_log_id}")
                except Exception as le:
                    logger.error(f"❌ Failed to log VC event to channel {config.LOG_CHANNEL_ID}: {le}")
            else:
                logger.info(f"ℹ️ [SKIP_LOG] No LOG_CHANNEL_ID configured for {user_id}")

            if is_join:
                notification_cache[f"{user_id}_{call_id}"] = datetime.datetime.now()
            
            # Auto delete
            async def auto_delete_notification(c_id, m_id):
                await asyncio.sleep(15) # Increased to 15s
                try: await ptb_application.bot.delete_message(chat_id=c_id, message_id=m_id)
                except: pass
            
            asyncio.create_task(auto_delete_notification(settings_chat_id, sent_message.message_id))
            
        except Exception as e:
            if "Chat not found" in str(e):
                logger.info(f"🔇 [SKIP] {user_id} in {call_id}: Bot not in chat")
            else:
                logger.error(f"❌ [CRASH] _process_single_participant: {e}")
    except Exception as e:
        if "Chat not found" not in str(e):
            logger.error(f"❌ [CRASH] _process_single_participant outer: {e}")

async def vc_join_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the 'Join Voice Chat' button click for old messages."""
    query = update.callback_query
    
    try:
        # Answer immediately to stop the spinning icon on the button
        await query.answer("Checking voice chat status...")
        
        data_parts = query.data.split("_")
        if len(data_parts) < 3:
            return
            
        chat_id = int(data_parts[2])
        
        # Use a dynamic join link
        chat_obj = None
        try:
            chat_obj = await context.bot.get_chat(chat_id)
        except: pass
        
        if chat_obj and chat_obj.username:
            join_link = f"https://t.me/{chat_obj.username}?videochat"
        else:
            clean_id = str(chat_id).replace("-100", "")
            join_link = f"https://t.me/c/{clean_id}?videochat"

        # Check if call is active using Telethon (if available)
        is_active = True # Default to True to allow users to try joining
        if telethon_client and telethon_client.is_connected():
            try:
                tele_chat_id = int(str(chat_id).replace('-100', ''))
                full = await telethon_client(GetFullChannelRequest(channel=tele_chat_id))
                if hasattr(full, 'full_chat') and hasattr(full.full_chat, 'call'):
                    if not full.full_chat.call:
                        is_active = False
            except Exception as te:
                logger.warning(f"Telethon check failed in callback: {te}")

        if is_active:
            # Edit the message to provide the direct join button since answer(url=) is unreliable
            await query.edit_message_reply_markup(
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(colored_button("✅ ᴄʟɪᴄᴋ ᴛᴏ ᴊᴏɪɴ", "default"), url=join_link)],
                    [InlineKeyboardButton(to_small_caps("+ ᴀᴅᴅ ᴍᴇ ɪɴ ɢʀᴏᴜᴘ +"), url=f"https://t.me/{(await context.bot.get_me()).username}?startgroup=true")]
                ])
            )
        else:
            await query.edit_message_text(
                text=f"{query.message.text}\n\n❌ **ɴᴏ ᴀᴄᴛɪᴠᴇ ᴠᴏɪᴄᴇ ᴄʜᴀᴛ ғᴏᴜɴᴅ.**",
                parse_mode=ParseMode.HTML
            )
            
    except Exception as e:
        logger.error(f"Error in vc_join_callback: {e}")
        try:
            await query.answer("Error checking voice chat status.", show_alert=True)
        except: pass

async def voice_chat_invite_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles notifications when users are invited to a voice chat."""
    logger.info(f"Checking VC invite notification in {update.effective_chat.id}")
    
    if not update.message or not update.message.video_chat_participants_invited:
        logger.info("No video_chat_participants_invited in message")
        return
    
    chat_id = update.effective_chat.id
    settings = get_chat_settings(chat_id)
    
    # Pre-populate call_to_chat mapping for this chat
    if telethon_client and telethon_client.is_connected():
        try:
            tele_chat_id = int(str(chat_id).replace('-100', ''))
            full = await telethon_client(GetFullChannelRequest(channel=tele_chat_id))
            if hasattr(full, 'full_chat') and hasattr(full.full_chat, 'call') and full.full_chat.call:
                call_id = full.full_chat.call.id
                if call_id not in call_to_chat:
                    chat_entity = await telethon_client.get_entity(tele_chat_id)
                    call_to_chat[call_id] = chat_entity
                    logger.info(f"📞 Pre-mapped call {call_id} to chat {chat_id} via invite handler")
        except Exception as e:
            logger.warning(f"Could not pre-map call for chat {chat_id}: {e}")
    
    enabled = settings.get("vc_invite_notification_enabled", True)
    logger.info(f"VC invite notification enabled: {enabled}")
    
    if not enabled:
        return

    inviter = update.effective_user
    invited_users = update.message.video_chat_participants_invited.users
    
    logger.info(f"Processing {len(invited_users)} invited users")

    # If inviter is an admin, remember invited users to bypass VC captcha
    if inviter:
        try:
            if await is_user_admin(chat_id, inviter.id, context):
                for u in invited_users:
                    admin_vc_invites[chat_id].add(u.id)
                logger.info(f"Admin {inviter.id} invited {len(invited_users)} users to VC in {chat_id}, whitelisting from VC captcha.")
        except Exception as e:
            logger.warning(f"Error checking if VC inviter is admin: {e}")
    
    # Static parts in mono
    invited_mono = to_mono("invited")
    to_mono_str = to_mono("to")
    vc_mono = to_mono("'s voice chat")
    
    for user in invited_users:
        # Format: {inviter_mention} 𝚒𝚗𝚟𝚒𝚝𝚎𝚍 {invitee_mention}𝚝𝚘 ˹ {chat_title} ˼ 🪽'𝚜 𝚟𝚘𝚒𝚌𝚎 𝚌𝚑𝚊𝚝
        inviter_mention = inviter.mention_html()
        invitee_mention = user.mention_html()
        chat_title = update.effective_chat.title
        
        # Constructing the message with requested styling
        message_text = (
            f"<blockquote>\n"
            f"{inviter_mention} {invited_mono} {invitee_mention} {to_mono_str} "
            f"˹ {chat_title} ˼ 🪽{vc_mono}\n"
            f"</blockquote>"
        )
        
        try:
            sent_msg = await context.bot.send_message(
                chat_id=chat_id,
                text=message_text,
                parse_mode=ParseMode.HTML
            )
            logger.info(f"Sent VC invite notification for {user.first_name} in {chat_id}")
            
            # Log to log channel
            if config.LOG_CHANNEL_ID:
                try:
                    # Ensure LOG_CHANNEL_ID is an integer
                    try:
                        target_log_id = int(str(config.LOG_CHANNEL_ID))
                    except:
                        target_log_id = config.LOG_CHANNEL_ID

                    log_msg = (
                        f"🎙 <b>VC Invite Notification</b>\n"
                        f"👤 <b>Inviter:</b> {inviter.mention_html()} (<code>{inviter.id}</code>)\n"
                        f"👤 <b>Invited:</b> {user.mention_html()} (<code>{user.id}</code>)\n"
                        f"📍 <b>Group:</b> {html.escape(update.effective_chat.title)} (<code>{chat_id}</code>)\n"
                        f"🕒 <b>Time:</b> {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                    )
                    await context.bot.send_message(chat_id=target_log_id, text=log_msg, parse_mode=ParseMode.HTML)
                    logger.info(f"📤 [LOGGED_INVITE] {user.id} to log channel {target_log_id}")
                except Exception as le:
                    logger.error(f"❌ Failed to log VC invite to channel {config.LOG_CHANNEL_ID}: {le}")

            # Store message IDs for deletion upon join
            # We store the original service message ID AND the bot's notification ID
            invite_key = (chat_id, user.id)
            if invite_key not in pending_invites:
                pending_invites[invite_key] = []
            
            # Add service message ID (if not already added)
            service_msg_id = update.message.message_id
            if service_msg_id not in pending_invites[invite_key]:
                pending_invites[invite_key].append(service_msg_id)
            
            # Add bot's notification message ID
            pending_invites[invite_key].append(sent_msg.message_id)
            
            # Optional: auto-cleanup pending_invites after some time (e.g., 1 hour)
            async def cleanup_invite(key, msg_id):
                await asyncio.sleep(3600)
                if key in pending_invites and msg_id in pending_invites[key]:
                    pending_invites[key].remove(msg_id)
                    if not pending_invites[key]:
                        pending_invites.pop(key, None)
            
            asyncio.create_task(cleanup_invite(invite_key, sent_msg.message_id))

        except Exception as e:
            logger.error(f"Error sending invite notification: {e}")

async def clean_ghost_participants(chat_id):
    """
    Cleans up ghost participants from a voice chat.
    A 'ghost' is someone in the VC list who is not actually in the group
    or is causing a glitch.
    """
    if not telethon_client or not telethon_client.is_connected():
        return False, "Voice monitor (Telethon) not connected."

    try:
        # Normalize chat_id for Telethon
        tele_chat_id = int(str(chat_id).replace('-100', ''))
        
        # Get full chat/channel info to find the call
        try:
            if str(chat_id).startswith('-100'):
                full = await telethon_client(GetFullChannelRequest(channel=tele_chat_id))
            else:
                full = await telethon_client(GetFullChatRequest(chat_id=tele_chat_id))
        except Exception as e:
            logger.error(f"Failed to get full chat info: {e}")
            return False, "Could not fetch group info. Is the bot in the group?"
            
        if not hasattr(full, 'full_chat') or not hasattr(full.full_chat, 'call') or not full.full_chat.call:
            return False, "No active voice chat found."
            
        call = full.full_chat.call
        
        # Get participant list
        call_info = await telethon_client(GetGroupCallRequest(call=call, limit=100))
        participants = call_info.participants
        
        # Sync current_participants_map with reality
        new_set = set()
        for p in participants:
            if not getattr(p, 'left', False):
                uid = get_peer_id(p.peer)
                if uid: new_set.add(uid)
        current_participants_map[call.id] = new_set
        logger.info(f"🔄 Synced participant map for call {call.id} with {len(new_set)} users")

        if not participants:
            return True, "Voice chat is empty."
            
        cleaned_count = 0
        for p in participants:
            user_peer = p.peer
            user_id = getattr(user_peer, 'user_id', getattr(user_peer, 'channel_id', None))
            
            if not user_id: continue
            
            # Skip if it's the bot itself or the Telethon account
            me = await telethon_client.get_me()
            if user_id == me.id: continue

            try:
                # Check if user is still in the group using PTB
                member = await ptb_application.bot.get_chat_member(chat_id, user_id)
                if member.status in ['left', 'kicked']:
                    # Remove from VC
                    await telethon_client(EditGroupCallParticipantRequest(
                        call=call,
                        participant=user_peer,
                        left=True
                    ))
                    cleaned_count += 1
                    await asyncio.sleep(0.5) # Avoid flood
            except Exception as e:
                # If member not found, it's likely a ghost
                if "Chat not found" in str(e) or "User not found" in str(e) or "Member not found" in str(e):
                    await telethon_client(EditGroupCallParticipantRequest(
                        call=call,
                        participant=user_peer,
                        left=True
                    ))
                    cleaned_count += 1
                    await asyncio.sleep(0.5)
                else:
                    logger.warning(f"Error checking member {user_id}: {e}")
                
        return True, f"✅ Cleaned {cleaned_count} ghost participants."
        
    except Exception as e:
        logger.error(f"Error in clean_ghost_participants: {e}")
        return False, f"Error: {str(e)}"

async def mute_vc_participant(chat_id, user_id, muted=True):
    """Mutes or unmutes a specific user in the voice chat via Telethon."""
    if not telethon_client or not telethon_client.is_connected():
        return False
    try:
        tele_chat_id = int(str(chat_id).replace('-100', ''))
        if str(chat_id).startswith('-100'):
            full = await telethon_client(GetFullChannelRequest(channel=tele_chat_id))
        else:
            full = await telethon_client(GetFullChatRequest(chat_id=tele_chat_id))
            
        if not hasattr(full, 'full_chat') or not hasattr(full.full_chat, 'call') or not full.full_chat.call:
            return False
            
        call = full.full_chat.call
        try:
            peer = await telethon_client.get_input_entity(int(user_id))
        except:
            peer = PeerUser(user_id=int(user_id))
            
        await telethon_client(EditGroupCallParticipantRequest(
            call=call,
            participant=peer,
            muted=muted
        ))
        logger.info(f"🎙 User {user_id} set muted={muted} in VC for chat {chat_id}")
        return True
    except Exception as e:
        logger.warning(f"Failed to set muted={muted} for user {user_id} in VC: {e}")
        return False

async def kick_user_from_vc(chat_id, user_id, context=None, action="kick"):
    """Forcefully removes a user from VC by muting in VC and ejecting via Bot API."""
    # First mute in VC
    await mute_vc_participant(chat_id, user_id, muted=True)
    # Eject from chat/VC via Bot API
    bot = context.bot if context else (ptb_application.bot if ptb_application else None)
    if bot:
        try:
            await bot.ban_chat_member(chat_id, int(user_id))
            if action != "ban":
                await bot.unban_chat_member(chat_id, int(user_id))
            logger.info(f"✅ User {user_id} kicked from VC in chat {chat_id}")
            return True
        except Exception as e:
            logger.warning(f"Failed to kick user {user_id} from chat/VC {chat_id}: {e}")
            return False
    return False

async def remove_user_from_vc(chat_id, user_id):
    """Forcefully removes a specific user from the voice chat."""
    return await kick_user_from_vc(chat_id, user_id, action="kick")

async def vcclean_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command to clean ghost participants."""
    logger.info(f"vcclean_command triggered by {update.effective_user.id} in chat {update.effective_chat.id}")
    if not update.effective_chat or update.effective_chat.type == 'private':
        return
        
    if not await is_user_admin(update.effective_chat.id, update.effective_user.id, context):
        return await update.message.reply_text("❌ Only admins can use this command.")
        
    status_msg = await update.message.reply_text("🔍 Scanning voice chat for ghost participants...")
    
    try:
        success, result = await clean_ghost_participants(update.effective_chat.id)
        await status_msg.edit_text(result)
    except Exception as e:
        logger.error(f"Error executing vcclean: {e}")
        await status_msg.edit_text(f"❌ Error: {str(e)}")

async def vcstatus_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command to show VC monitor status and active calls with live participants."""
    if not update.effective_chat or update.effective_chat.type == 'private':
        return
        
    if not await is_user_admin(update.effective_chat.id, update.effective_user.id, context):
        return await update.message.reply_text("❌ Only admins can use this command.")
        
    status_msg = (
        f"🎙 <b>Voice Chat Monitor Status</b>\n\n"
        f"<b>Connected:</b> {'✅ Yes' if telethon_client and telethon_client.is_connected() else '❌ No'}\n"
        f"<b>Active Mappings:</b> {len(call_to_chat)}\n"
        f"<b>Notification Cache:</b> {len(notification_cache)}\n"
        f"<b>Failed Resolutions:</b> {len(_failed_resolutions)}\n\n"
    )

    if call_to_chat and telethon_client and telethon_client.is_connected():
        status_msg += "<b>Live Active Voice Chats:</b>\n"
        for call_id, chat_entity in list(call_to_chat.items()):
            try:
                # Determine chat ID for display
                if hasattr(chat_entity, 'id'):
                    cid = chat_entity.id
                    clean_id = int(str(cid).replace('-100', ''))
                    display_name = getattr(chat_entity, 'title', f"Chat {cid}")
                    
                    # Fetch participants for this call
                    try:
                        # Get full chat to ensure we have the correct call reference
                        if isinstance(chat_entity, PeerChannel) or "Channel" in type(chat_entity).__name__:
                            full = await telethon_client(GetFullChannelRequest(channel=clean_id))
                        else:
                            full = await telethon_client(GetFullChatRequest(chat_id=clean_id))
                        
                        if hasattr(full, 'full_chat') and hasattr(full.full_chat, 'call') and full.full_chat.call:
                            call_info = await telethon_client(GetGroupCallRequest(call=full.full_chat.call, limit=100))
                            participants = call_info.participants
                            
                            p_list = []
                            for p in participants:
                                if not getattr(p, 'left', False):
                                    p_id = getattr(p.peer, 'user_id', getattr(p.peer, 'channel_id', getattr(p.peer, 'chat_id', 'Unknown')))
                                    
                                    # Try to resolve name from Telethon cache
                                    try:
                                        p_entity = await telethon_client.get_entity(p.peer)
                                        p_name = getattr(p_entity, 'first_name', getattr(p_entity, 'title', "Unknown"))
                                    except:
                                        p_name = "Unknown"
                                        
                                    p_list.append(f"• {p_name} (<code>{p_id}</code>)")
                            
                            if p_list:
                                status_msg += f"<b>{display_name}</b>: {len(p_list)} users\n"
                                status_msg += "\n".join(p_list) + "\n\n"
                            else:
                                status_msg += f"<b>{display_name}</b>: No active participants\n\n"
                        else:
                            # Call might have ended
                            status_msg += f"• <b>{display_name}</b>: Voice chat ended\n"
                    except Exception as e:
                        status_msg += f"• <b>{display_name}</b>: (Error fetching users)\n"
                else:
                    status_msg += f"• <b>Unknown Chat ({call_id})</b>\n"
            except: continue
    else:
        status_msg += "<i>No active voice chats currently tracked.</i>\n"

    status_msg += f"\n<i>Use /vcrefresh to force a global scan of all groups.</i>"
    
    # Split message if it's too long
    if len(status_msg) > 4000:
        parts = [status_msg[i:i+4000] for i in range(0, len(status_msg), 4000)]
        for part in parts:
            await update.message.reply_text(part, parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text(status_msg, parse_mode=ParseMode.HTML)

async def vcrefresh_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command to force a global scan of all groups for active VCs."""
    if not update.effective_chat or update.effective_chat.type == 'private':
        return
        
    if not await is_user_admin(update.effective_chat.id, update.effective_user.id, context):
        return await update.message.reply_text("❌ Only admins can use this command.")
        
    msg = await update.message.reply_text("🔄 Starting global VC scan... this may take a minute.")
    
    # Reset failed resolutions to allow retrying everything
    _failed_resolutions.clear()
    
    # Run scan
    asyncio.create_task(scan_active_vcs())
    
    await msg.edit_text("✅ Global VC scan started in background. Check /vcstatus in a moment.")

def get_voice_chat_handlers():
    """Returns handlers for voice chat events."""
    return [
        CallbackQueryHandler(vc_join_callback, pattern="^join_vc_"),
        MessageHandler(filters.StatusUpdate.VIDEO_CHAT_PARTICIPANTS_INVITED, voice_chat_invite_handler),
        CommandHandler("vcclean", vcclean_command),
        CommandHandler("vcstatus", vcstatus_command),
        CommandHandler("vcrefresh", vcrefresh_command)
    ]

async def stop_voice_chat_monitor():
    """Stops the Telethon client."""
    if telethon_client:
        await telethon_client.disconnect()
        logger.info("🎙 Voice Chat Monitor stopped.")
