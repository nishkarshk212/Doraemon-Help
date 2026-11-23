import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from features.assistant_login import (
    save_assistant_session,
    get_assistant_session,
    delete_assistant_session,
    login_session_command,
    login_phone_command,
    cancel_login_command,
    assistant_command,
    logout_assistant_command,
    interactive_login_message_handler,
    login_states,
    temp_login_clients,
    InLoginStateFilter,
    in_login_state_filter
)

from features.voice_chat import (
    vcbanall_command,
    vcmuteall_command,
    vcunmuteall_command,
    get_voice_chat_handlers
)


@pytest.mark.asyncio
async def test_assistant_session_storage():
    """Test saving, retrieving, and deleting assistant session."""
    mock_collection = MagicMock()
    stored_doc = {}

    def mock_replace(query, doc, upsert=False):
        stored_doc.update(doc)
        return MagicMock()

    def mock_find_one(query):
        return stored_doc if stored_doc.get("_id") == "active_assistant" else None

    def mock_delete_one(query):
        stored_doc.clear()
        return MagicMock()

    mock_collection.replace_one = MagicMock(side_effect=mock_replace)
    mock_collection.find_one = MagicMock(side_effect=mock_find_one)
    mock_collection.delete_one = MagicMock(side_effect=mock_delete_one)

    with patch("features.assistant_login._get_assistant_collection", return_value=mock_collection):
        # 1. Save session
        me_mock = MagicMock(id=11223344, first_name="Doraemon", last_name="Assistant", username="dora_ast", phone="919999999999")
        save_assistant_session("test_session_string_123", me_mock)

        # 2. Get session
        session_data = get_assistant_session()
        assert session_data is not None
        assert session_data.get("string_session") == "test_session_string_123"
        assert session_data.get("user_id") == 11223344
        assert session_data.get("username") == "dora_ast"

        # 3. Delete session
        delete_assistant_session()
        assert get_assistant_session() == {}


@pytest.mark.asyncio
async def test_in_login_state_filter():
    """Test message filter for interactive login state."""
    filter_obj = InLoginStateFilter()

    msg_active = MagicMock()
    msg_active.from_user.id = 99999

    msg_inactive = MagicMock()
    msg_inactive.from_user.id = 88888

    login_states[99999] = {"step": "WAITING_PHONE"}

    try:
        assert filter_obj.filter(msg_active) is True
        assert filter_obj.filter(msg_inactive) is False
        assert filter_obj.filter(None) is False
    finally:
        login_states.pop(99999, None)


@pytest.mark.asyncio
async def test_login_session_command_permissions():
    """Verify login command gates: private chat only & sudo only."""
    # 1. Non-private chat
    update_group = MagicMock()
    update_group.effective_chat.type = "group"
    update_group.effective_user.id = 12345
    update_group.message.reply_text = AsyncMock()
    context = MagicMock()

    await login_session_command(update_group, context)
    update_group.message.reply_text.assert_called_once()
    assert "private chat" in update_group.message.reply_text.call_args[0][0]

    # 2. Private chat, not sudo
    update_priv_non_sudo = MagicMock()
    update_priv_non_sudo.effective_chat.type = "private"
    update_priv_non_sudo.effective_user.id = 99999
    update_priv_non_sudo.message.reply_text = AsyncMock()

    with patch("features.assistant_login.is_sudo", return_value=False):
        await login_session_command(update_priv_non_sudo, context)
        update_priv_non_sudo.message.reply_text.assert_called_once()
        assert "Only bot owner and authorized sudo" in update_priv_non_sudo.message.reply_text.call_args[0][0]


@pytest.mark.asyncio
async def test_login_session_command_success():
    """Test successful string session login."""
    update = MagicMock()
    update.effective_chat.type = "private"
    update.effective_user.id = 7408854697
    status_msg = MagicMock()
    status_msg.edit_text = AsyncMock()
    update.message.reply_text = AsyncMock(return_value=status_msg)

    context = MagicMock()
    context.args = ["1BVtsOLgBu...test_session..."]

    mock_client = AsyncMock()
    mock_client.connect = AsyncMock()
    mock_client.is_user_authorized = AsyncMock(return_value=True)
    mock_client.get_me = AsyncMock(return_value=MagicMock(id=55555, first_name="Dora", last_name="Userbot", username="dora_userbot", phone="123456789"))
    mock_client.disconnect = AsyncMock()
    mock_client.is_connected = MagicMock(return_value=False)

    with patch("features.assistant_login.is_sudo", return_value=True), \
         patch("features.assistant_login.TelegramClient", return_value=mock_client), \
         patch("features.assistant_login.save_assistant_session") as mock_save, \
         patch("features.voice_chat.reload_telethon_client", AsyncMock(return_value=(True, "OK"))):

        await login_session_command(update, context)

        mock_save.assert_called_once()
        status_msg.edit_text.assert_called_once()
        res_text = status_msg.edit_text.call_args[0][0]
        assert "ASSISTANT CONNECTED SUCCESSFULLY" in res_text
        assert "55555" in res_text


@pytest.mark.asyncio
async def test_login_phone_and_cancel():
    """Test starting phone login and cancelling it."""
    update = MagicMock()
    update.effective_chat.type = "private"
    user_id = 7408854697
    update.effective_user.id = user_id
    update.message.reply_text = AsyncMock()
    context = MagicMock()

    with patch("features.assistant_login.is_sudo", return_value=True):
        # Start phone login
        await login_phone_command(update, context)
        assert user_id in login_states
        assert login_states[user_id]["step"] == "WAITING_PHONE"
        assert "ASSISTANT PHONE LOGIN WIZARD" in update.message.reply_text.call_args[0][0]

        # Cancel login
        update.message.reply_text.reset_mock()
        await cancel_login_command(update, context)
        assert user_id not in login_states
        assert "cancelled" in update.message.reply_text.call_args[0][0]


@pytest.mark.asyncio
async def test_vcbanall_command():
    """Test /vcbanall command targets only active VC users."""
    update = MagicMock()
    update.effective_chat.type = "supergroup"
    chat_id = -1001234567890
    update.effective_chat.id = chat_id
    admin_id = 11111
    update.effective_user.id = admin_id

    status_msg = MagicMock()
    status_msg.edit_text = AsyncMock()
    update.message.reply_text = AsyncMock(return_value=status_msg)

    context = MagicMock()
    context.bot.id = 999999 # Bot ID
    context.bot.ban_chat_member = AsyncMock()

    # VC Participants:
    # 1. Assistant: id=22222
    # 2. Bot: id=999999
    # 3. Admin: id=33333
    # 4. Normal VC User 1: id=44444 -> Should be banned!
    # 5. Normal VC User 2: id=55555 -> Should be banned!
    # 6. User who already left VC: left=True -> Should NOT be touched!
    p_assistant = MagicMock(peer=MagicMock(user_id=22222), left=False)
    p_bot = MagicMock(peer=MagicMock(user_id=999999), left=False)
    p_admin = MagicMock(peer=MagicMock(user_id=33333), left=False)
    p_user1 = MagicMock(peer=MagicMock(user_id=44444), left=False)
    p_user2 = MagicMock(peer=MagicMock(user_id=55555), left=False)
    p_left = MagicMock(peer=MagicMock(user_id=66666), left=True)

    mock_tele_client = AsyncMock()
    mock_tele_client.is_connected = MagicMock(return_value=True)
    mock_tele_client.get_me = AsyncMock(return_value=MagicMock(id=22222))

    full_chat_mock = MagicMock()
    full_chat_mock.full_chat.call = MagicMock(id=777)

    call_info_mock = MagicMock()
    call_info_mock.participants = [p_assistant, p_bot, p_admin, p_user1, p_user2, p_left]

    async def mock_tele_call(req):
        from telethon.tl.functions.channels import GetFullChannelRequest
        from telethon.tl.functions.phone import GetGroupCallRequest, EditGroupCallParticipantRequest
        if isinstance(req, GetFullChannelRequest):
            return full_chat_mock
        elif isinstance(req, GetGroupCallRequest):
            return call_info_mock
        elif isinstance(req, EditGroupCallParticipantRequest):
            return True
        return None

    mock_tele_client.side_effect = mock_tele_call

    async def mock_is_admin(c_id, u_id, ctx):
        return u_id in (admin_id, 33333)

    with patch("features.voice_chat.telethon_client", mock_tele_client), \
         patch("features.voice_chat.is_user_admin", side_effect=mock_is_admin):

        # Context bot member check
        async def mock_get_chat_member(c_id, u_id):
            m = MagicMock()
            m.user.is_bot = False
            return m

        context.bot.get_chat_member = AsyncMock(side_effect=mock_get_chat_member)

        await vcbanall_command(update, context)

        # Ban should be called only for normal active VC users (44444 and 55555)
        assert context.bot.ban_chat_member.call_count == 2
        banned_ids = [call[0][1] for call in context.bot.ban_chat_member.call_args_list]
        assert 44444 in banned_ids
        assert 55555 in banned_ids
        assert 22222 not in banned_ids  # Assistant skipped
        assert 999999 not in banned_ids # Bot skipped
        assert 33333 not in banned_ids  # Admin skipped
        assert 66666 not in banned_ids  # User who left skipped

        status_msg.edit_text.assert_called()
        last_msg = status_msg.edit_text.call_args_list[-1][0][0]
        assert "VOICE CHAT BAN ALL COMPLETED" in last_msg
        assert "Successfully Banned:</b> <code>2</code>" in last_msg


@pytest.mark.asyncio
async def test_vcmuteall_command():
    """Test /vcmuteall command mutes only active VC users."""
    update = MagicMock()
    update.effective_chat.type = "supergroup"
    chat_id = -1001234567890
    update.effective_chat.id = chat_id
    admin_id = 11111
    update.effective_user.id = admin_id

    status_msg = MagicMock()
    status_msg.edit_text = AsyncMock()
    update.message.reply_text = AsyncMock(return_value=status_msg)

    context = MagicMock()
    context.bot.id = 999999

    p_assistant = MagicMock(peer=MagicMock(user_id=22222), left=False)
    p_admin = MagicMock(peer=MagicMock(user_id=33333), left=False)
    p_user1 = MagicMock(peer=MagicMock(user_id=44444), left=False)
    p_user2 = MagicMock(peer=MagicMock(user_id=55555), left=False)

    mock_tele_client = AsyncMock()
    mock_tele_client.is_connected = MagicMock(return_value=True)
    mock_tele_client.get_me = AsyncMock(return_value=MagicMock(id=22222))

    full_chat_mock = MagicMock()
    full_chat_mock.full_chat.call = MagicMock(id=777)

    call_info_mock = MagicMock()
    call_info_mock.participants = [p_assistant, p_admin, p_user1, p_user2]

    muted_calls = []

    async def mock_tele_call(req):
        from telethon.tl.functions.channels import GetFullChannelRequest
        from telethon.tl.functions.phone import GetGroupCallRequest, EditGroupCallParticipantRequest
        if isinstance(req, GetFullChannelRequest):
            return full_chat_mock
        elif isinstance(req, GetGroupCallRequest):
            return call_info_mock
        elif isinstance(req, EditGroupCallParticipantRequest):
            muted_calls.append(req)
            return True
        return None

    mock_tele_client.side_effect = mock_tele_call

    async def mock_is_admin(c_id, u_id, ctx):
        return u_id in (admin_id, 33333)

    with patch("features.voice_chat.telethon_client", mock_tele_client), \
         patch("features.voice_chat.is_user_admin", side_effect=mock_is_admin):

        await vcmuteall_command(update, context)

        # Mute should be called for user1 and user2
        assert len(muted_calls) == 2
        for req in muted_calls:
            assert req.muted is True

        last_msg = status_msg.edit_text.call_args_list[-1][0][0]
        assert "VOICE CHAT MUTE ALL COMPLETED" in last_msg
        assert "Successfully Muted:</b> <code>2</code>" in last_msg


@pytest.mark.asyncio
async def test_vcunmuteall_command():
    """Test /vcunmuteall command un-mutes active VC users."""
    update = MagicMock()
    update.effective_chat.type = "supergroup"
    chat_id = -1001234567890
    update.effective_chat.id = chat_id
    admin_id = 11111
    update.effective_user.id = admin_id

    status_msg = MagicMock()
    status_msg.edit_text = AsyncMock()
    update.message.reply_text = AsyncMock(return_value=status_msg)

    context = MagicMock()
    context.bot.id = 999999

    p_user1 = MagicMock(peer=MagicMock(user_id=44444), left=False)
    p_user2 = MagicMock(peer=MagicMock(user_id=55555), left=False)

    mock_tele_client = AsyncMock()
    mock_tele_client.is_connected = MagicMock(return_value=True)

    full_chat_mock = MagicMock()
    full_chat_mock.full_chat.call = MagicMock(id=777)

    call_info_mock = MagicMock()
    call_info_mock.participants = [p_user1, p_user2]

    unmuted_calls = []

    async def mock_tele_call(req):
        from telethon.tl.functions.channels import GetFullChannelRequest
        from telethon.tl.functions.phone import GetGroupCallRequest, EditGroupCallParticipantRequest
        if isinstance(req, GetFullChannelRequest):
            return full_chat_mock
        elif isinstance(req, GetGroupCallRequest):
            return call_info_mock
        elif isinstance(req, EditGroupCallParticipantRequest):
            unmuted_calls.append(req)
            return True
        return None

    mock_tele_client.side_effect = mock_tele_call

    with patch("features.voice_chat.telethon_client", mock_tele_client), \
         patch("features.voice_chat.is_user_admin", AsyncMock(return_value=True)):

        await vcunmuteall_command(update, context)

        assert len(unmuted_calls) == 2
        for req in unmuted_calls:
            assert req.muted is False

        last_msg = status_msg.edit_text.call_args_list[-1][0][0]
        assert "VOICE CHAT UNMUTE ALL COMPLETED" in last_msg
        assert "Successfully Unmuted:</b> <code>2</code>" in last_msg
