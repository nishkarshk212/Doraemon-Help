import unittest
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from features.captcha import (
    PENDING_CAPTCHAS,
    start_captcha_verification,
    handle_pm_captcha_start,
    captcha_pm_button_callback,
    captcha_pm_options_callback,
    start_join_request_captcha,
    captcha_jr_button_callback,
    captcha_jr_options_callback
)
from features.settings import get_captcha_settings_keyboard


class TestPMAndJoinRequestCaptcha(unittest.TestCase):

    def setUp(self):
        PENDING_CAPTCHAS.clear()

    def tearDown(self):
        PENDING_CAPTCHAS.clear()

    def test_settings_keyboard_has_location_toggle(self):
        """Verify get_captcha_settings_keyboard contains verification location."""
        settings_group = {"captcha_pm_mode": False}
        kb_group = get_captcha_settings_keyboard(settings_group)
        found_group = any("Group Chat" in btn.text for row in kb_group.inline_keyboard for btn in row)
        self.assertTrue(found_group)

        settings_pm = {"captcha_pm_mode": True}
        kb_pm = get_captcha_settings_keyboard(settings_pm)
        found_pm = any("Private (PM)" in btn.text for row in kb_pm.inline_keyboard for btn in row)
        self.assertTrue(found_pm)

    def test_direct_join_pm_verification_flow(self):
        """
        Verify Option 2:
        1. User joins group -> restricted and group prompt with verify URL sent.
        2. User opens verify link in PM -> challenge sent in PM.
        3. User clicks button -> unmuted in group, group prompt deleted, PM success confirmation.
        """
        async def _run():
            chat_id = -1001234567890
            user_id = 55555

            chat = MagicMock(id=chat_id, title="Test Group")
            user = MagicMock(id=user_id, first_name="Alice", is_bot=False)

            context = MagicMock()
            context.bot.get_me = AsyncMock(return_value=MagicMock(username="TestHelpBot"))
            context.bot.restrict_chat_member = AsyncMock()
            group_msg = MagicMock(message_id=101)
            context.bot.send_message = AsyncMock(return_value=group_msg)
            context.bot.delete_message = AsyncMock()
            context.job_queue = MagicMock()

            settings = {
                "captcha_enabled": True,
                "captcha_pm_mode": True,
                "captcha_type": "button",
                "captcha_timeout": 60,
                "captcha_action": "kick"
            }

            with patch("features.captcha.get_chat_settings", return_value=settings):
                # 1. User joins group
                await start_captcha_verification(chat, user, context)

                # Check user restricted in group
                context.bot.restrict_chat_member.assert_called_once()
                self.assertFalse(context.bot.restrict_chat_member.call_args[1]["permissions"].can_send_messages)

                # Check group prompt sent with verify URL
                context.bot.send_message.assert_called_once()
                sent_text = context.bot.send_message.call_args[1]["text"]
                self.assertIn("Verify in Private", str(context.bot.send_message.call_args[1]["reply_markup"]))
                self.assertIn("t.me/TestHelpBot?start=verify_-1001234567890_55555", str(context.bot.send_message.call_args[1]["reply_markup"]))

                # Check state saved
                self.assertIn((chat_id, user_id), PENDING_CAPTCHAS)
                state = PENDING_CAPTCHAS[(chat_id, user_id)]
                self.assertTrue(state["is_pm_mode"])

                # 2. User taps Start in PM
                update_pm = MagicMock()
                update_pm.effective_user.id = user_id
                update_pm.effective_user.first_name = "Alice"
                pm_challenge_msg = MagicMock(message_id=202)
                update_pm.message.reply_text = AsyncMock(return_value=pm_challenge_msg)

                await handle_pm_captcha_start(update_pm, context, f"verify_{chat_id}_{user_id}")
                update_pm.message.reply_text.assert_called_once()
                self.assertEqual(state["pm_message_id"], 202)

                # 3. User clicks verification button in PM
                query_cb = MagicMock()
                query_cb.data = f"cpt_pm_btn_{chat_id}_{user_id}"
                query_cb.from_user.id = user_id
                query_cb.answer = AsyncMock()
                query_cb.edit_message_text = AsyncMock()
                update_cb = MagicMock(callback_query=query_cb)

                with patch("features.join_request.get_group_link", AsyncMock(return_value="https://t.me/TestGroup")), \
                     patch("handlers.welcome.send_welcome", AsyncMock()) as mock_welcome:
                    await captcha_pm_button_callback(update_cb, context)

                    # Group prompt deleted
                    context.bot.delete_message.assert_called_with(chat_id=chat_id, message_id=101)

                    # User un-restricted in group (permissions.can_send_messages=True)
                    self.assertTrue(context.bot.restrict_chat_member.call_args[1]["permissions"].can_send_messages)

                    # PM message updated with success
                    query_cb.edit_message_text.assert_called_once()
                    self.assertIn("Verification Successful", query_cb.edit_message_text.call_args[0][0])

                    # Removed from pending
                    self.assertNotIn((chat_id, user_id), PENDING_CAPTCHAS)

        asyncio.run(_run())

    def test_join_request_pm_captcha_flow(self):
        """
        Verify Option 1:
        1. Join request arrives -> captcha DM sent to user in PM (zero group messages).
        2. User solves captcha in PM -> join request approved and success DM sent.
        """
        async def _run():
            chat_id = -1009876543210
            user_id = 77777

            chat = MagicMock(id=chat_id, title="Secret VIP Club")
            user = MagicMock(id=user_id, first_name="Bob", is_bot=False)
            join_req = MagicMock(chat=chat, from_user=user)

            context = MagicMock()
            dm_challenge_msg = MagicMock(message_id=303)
            context.bot.send_message = AsyncMock(return_value=dm_challenge_msg)
            context.bot.approve_chat_join_request = AsyncMock()
            context.bot.decline_chat_join_request = AsyncMock()
            context.job_queue = MagicMock()

            settings = {
                "captcha_type": "button",
                "captcha_timeout": 90
            }

            with patch("features.captcha.get_chat_settings", return_value=settings):
                # 1. Join request initiated
                await start_join_request_captcha(join_req, context)

                # Challenge sent to user in DM
                context.bot.send_message.assert_called_once()
                self.assertEqual(context.bot.send_message.call_args[1]["chat_id"], user_id)
                self.assertIn("Secret VIP Club", context.bot.send_message.call_args[1]["text"])

                # Check state
                self.assertIn((chat_id, user_id), PENDING_CAPTCHAS)
                state = PENDING_CAPTCHAS[(chat_id, user_id)]
                self.assertTrue(state["is_join_request"])

                # 2. User solves captcha in PM
                query_cb = MagicMock()
                query_cb.data = f"cpt_jr_btn_{chat_id}_{user_id}"
                query_cb.from_user.id = user_id
                query_cb.answer = AsyncMock()
                query_cb.edit_message_text = AsyncMock()
                update_cb = MagicMock(callback_query=query_cb)

                with patch("features.join_request.get_group_link", AsyncMock(return_value="https://t.me/SecretVIP")), \
                     patch("features.join_request.record_approved_join_request") as mock_record:
                    await captcha_jr_button_callback(update_cb, context)

                    # Join request approved
                    context.bot.approve_chat_join_request.assert_called_once_with(chat_id=chat_id, user_id=user_id)
                    mock_record.assert_called_once_with(chat_id, user_id)

                    # PM message edited with approval confirmation
                    query_cb.edit_message_text.assert_called_once()
                    self.assertIn("Join Request Approved", query_cb.edit_message_text.call_args[0][0])

                    # Removed from pending
                    self.assertNotIn((chat_id, user_id), PENDING_CAPTCHAS)

        asyncio.run(_run())


if __name__ == "__main__":
    unittest.main()
