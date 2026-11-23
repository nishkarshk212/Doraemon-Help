#!/usr/bin/env python3
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from handlers.blocking_handler import DEFAULT_BLOCKING_SETTINGS, send_blocking_notification
from managers.settings_manager_mongo import DEFAULT_CHAT_SETTINGS
from features.settings import get_blocking_notification_settings_keyboard

class TestBlockContentSilent(unittest.TestCase):
    def test_default_settings_no_warning_message(self):
        """Verify defaults are set to silent deletion with no warning notifications."""
        self.assertFalse(DEFAULT_BLOCKING_SETTINGS.get("blocking_send_notifications", True))
        self.assertFalse(DEFAULT_CHAT_SETTINGS.get("blocking_send_notifications", True))
        self.assertEqual(DEFAULT_CHAT_SETTINGS.get("block_warn_penalty"), "off")

    async def test_send_blocking_notification_is_silent_by_default(self):
        """Verify send_blocking_notification does not send any message when blocking_send_notifications is False."""
        update = MagicMock()
        update.effective_chat.id = -1001234567890
        update.effective_message = MagicMock()
        update.effective_message.reply_text = AsyncMock()
        context = MagicMock()
        
        # Test default settings (blocking_send_notifications: False)
        settings = {"blocking_send_notifications": False}
        await send_blocking_notification(update, context, settings, "⚠️ Content blocked!")
        
        # Verify reply_text was NOT called
        update.effective_message.reply_text.assert_not_called()

    async def test_send_blocking_notification_when_enabled(self):
        """Verify send_blocking_notification sends message only when explicitly enabled."""
        update = MagicMock()
        update.effective_chat.id = -1001234567890
        update.effective_user.first_name = "TestUser"
        update.effective_user.id = 12345
        update.effective_message = MagicMock()
        update.effective_message.reply_text = AsyncMock()
        context = MagicMock()
        
        # Settings with blocking_send_notifications: True
        settings = {
            "blocking_send_notifications": True,
            "blocking_delete_notifications": False
        }
        await send_blocking_notification(update, context, settings, "⚠️ Content blocked!")
        
        # Verify reply_text was called
        update.effective_message.reply_text.assert_called_once()

    def test_settings_keyboard_includes_warning_toggle(self):
        """Verify settings keyboard includes the toggle for warning messages."""
        settings = {"blocking_send_notifications": False}
        keyboard = get_blocking_notification_settings_keyboard(settings)
        buttons = [btn for row in keyboard.inline_keyboard for btn in row]
        toggle_btn = next((b for b in buttons if b.callback_data == "set_toggle_blocking_send_notifications"), None)
        self.assertIsNotNone(toggle_btn)
        self.assertIn("Disabled", toggle_btn.text)

if __name__ == '__main__':
    import asyncio
    suite = unittest.TestLoader().loadTestsFromTestCase(TestBlockContentSilent)
    runner = unittest.TextTestRunner()
    # Support running async tests cleanly in unittest
    test_case = TestBlockContentSilent()
    asyncio.run(test_case.test_send_blocking_notification_is_silent_by_default())
    asyncio.run(test_case.test_send_blocking_notification_when_enabled())
    unittest.main()
