import unittest
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from telegram.error import Forbidden

from handlers.welcome import send_goodbye, on_chat_member_update


class TestNoUserbotWelcome(unittest.TestCase):

    def test_goodbye_does_not_call_telethon_on_bot_api_failure(self):
        """Verify that when Bot API fails to send goodbye DM, Telethon assistant is NOT used."""
        async def _run():
            chat = MagicMock()
            chat.id = -1001234567890
            chat.title = "Test Group"
            chat.username = "testgroup"

            user = MagicMock()
            user.id = 998877
            user.first_name = "TestUser"
            user.last_name = "LastName"
            user.username = "testuser"
            user.is_bot = False
            user.language_code = "en"
            user.mention_html.return_value = "<a href='tg://user?id=998877'>TestUser</a>"

            context = MagicMock()
            context.bot.send_message = AsyncMock(side_effect=Forbidden("Forbidden: bot was blocked by the user"))

            mock_settings = {
                "goodbye_enabled": True,
                "goodbye_text": "Goodbye {MENTION}!"
            }

            with patch("handlers.welcome.get_chat_settings", return_value=mock_settings), \
                 patch("features.voice_chat.telethon_client", MagicMock()) as mock_telethon:

                # Call send_goodbye
                await send_goodbye(chat, user, context)

                # context.bot.send_message was attempted
                context.bot.send_message.assert_called_once()
                # telethon_client should NEVER be called to send_message
                self.assertEqual(mock_telethon.send_message.call_count, 0)

        asyncio.run(_run())


if __name__ == "__main__":
    unittest.main()
