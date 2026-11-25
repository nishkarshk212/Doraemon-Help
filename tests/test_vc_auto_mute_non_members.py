import unittest
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from features.settings import get_vc_settings_keyboard
from features.voice_chat import (
    vcautomute_command,
    check_and_mute_non_members_in_vc
)


class TestVCAutoMuteNonMembers(unittest.TestCase):

    def test_vc_settings_keyboard_contains_auto_mute(self):
        """Verify that get_vc_settings_keyboard renders the Auto-Mute Non-Members button."""
        # 1. Disabled setting
        settings_off = {"vc_auto_mute_non_members_enabled": False}
        kb_off = get_vc_settings_keyboard(settings_off)
        found_off = False
        for row in kb_off.inline_keyboard:
            for btn in row:
                if btn.callback_data == "set_toggle_vc_auto_mute_non_members_enabled":
                    found_off = True
                    self.assertIn("❌", btn.text)
        self.assertTrue(found_off)

        # 2. Enabled setting
        settings_on = {"vc_auto_mute_non_members_enabled": True}
        kb_on = get_vc_settings_keyboard(settings_on)
        found_on = False
        for row in kb_on.inline_keyboard:
            for btn in row:
                if btn.callback_data == "set_toggle_vc_auto_mute_non_members_enabled":
                    found_on = True
                    self.assertIn("✅", btn.text)
        self.assertTrue(found_on)

    def test_vcautomute_command_permissions_and_toggle(self):
        """Verify /vcautomute permissions and toggle handling."""
        async def _run():
            # 1. Non-admin test
            update_non_admin = MagicMock()
            update_non_admin.effective_chat.type = "supergroup"
            update_non_admin.effective_chat.id = -1001234567890
            update_non_admin.effective_user.id = 99999
            update_non_admin.message.reply_text = AsyncMock()
            context = MagicMock()
            context.args = ["on"]

            with patch("features.voice_chat.is_user_admin", AsyncMock(return_value=False)):
                await vcautomute_command(update_non_admin, context)
                update_non_admin.message.reply_text.assert_called_once()
                self.assertIn("Only group admins", update_non_admin.message.reply_text.call_args[0][0])

            # 2. Admin test: /vcautomute on
            update_admin = MagicMock()
            update_admin.effective_chat.type = "supergroup"
            update_admin.effective_chat.id = -1001234567890
            update_admin.effective_user.id = 11111
            status_msg = MagicMock()
            status_msg.edit_text = AsyncMock()
            update_admin.message.reply_text = AsyncMock(return_value=status_msg)

            with patch("features.voice_chat.is_user_admin", AsyncMock(return_value=True)), \
                 patch("managers.settings_manager_mongo.update_chat_setting") as mock_update, \
                 patch("features.voice_chat.check_and_mute_non_members_in_vc", AsyncMock(return_value=2)):

                await vcautomute_command(update_admin, context)
                mock_update.assert_called_with(-1001234567890, "vc_auto_mute_non_members_enabled", True)
                update_admin.message.reply_text.assert_called()
                status_msg.edit_text.assert_called_once()
                self.assertIn("Auto-muted 2 non-members", status_msg.edit_text.call_args[0][0])

        asyncio.run(_run())

    def test_check_and_mute_non_members_in_vc(self):
        """Verify check_and_mute_non_members_in_vc mutes only non-members."""
        async def _run():
            chat_id = -1001234567890

            p_assistant = MagicMock(peer=MagicMock(user_id=22222), left=False)
            p_admin = MagicMock(peer=MagicMock(user_id=33333), left=False)
            p_member = MagicMock(peer=MagicMock(user_id=44444), left=False)
            p_non_member = MagicMock(peer=MagicMock(user_id=55555), left=False)

            mock_tele_client = AsyncMock()
            mock_tele_client.is_connected = MagicMock(return_value=True)
            mock_tele_client.get_me = AsyncMock(return_value=MagicMock(id=22222))

            full_chat_mock = MagicMock()
            full_chat_mock.full_chat.call = MagicMock(id=777)

            call_info_mock = MagicMock()
            call_info_mock.participants = [p_assistant, p_admin, p_member, p_non_member]

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

            mock_ptb = MagicMock()
            mock_ptb.bot.id = 999999

            async def mock_get_chat_member(chat_id, user_id):
                m = MagicMock()
                if user_id == 44444:
                    m.status = "member"
                elif user_id == 33333:
                    m.status = "administrator"
                else:
                    m.status = "left" # Non-member
                return m

            mock_ptb.bot.get_chat_member = AsyncMock(side_effect=mock_get_chat_member)

            async def mock_is_admin(c_id, u_id, app):
                return u_id == 33333

            with patch("features.voice_chat.telethon_client", mock_tele_client), \
                 patch("features.voice_chat.ptb_application", mock_ptb), \
                 patch("features.voice_chat.is_user_admin", side_effect=mock_is_admin):

                muted_count = await check_and_mute_non_members_in_vc(chat_id)

                self.assertEqual(muted_count, 1)
                self.assertEqual(len(muted_calls), 1)
                self.assertTrue(muted_calls[0].muted)

        asyncio.run(_run())


if __name__ == "__main__":
    unittest.main()
