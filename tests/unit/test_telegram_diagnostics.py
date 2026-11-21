import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from kleinanzeigen_bot.notifications.telegram_bot import inspect_connection, send_connection_test


class TelegramDiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    async def test_explicit_test_sends_one_message_to_configured_chat(self):
        bot = SimpleNamespace(
            send_message=AsyncMock(return_value=SimpleNamespace(message_id=42)),
            get_updates=AsyncMock(),
        )
        report = await send_connection_test(bot, "-123")
        self.assertTrue(report["delivery_tested"])
        self.assertEqual(report["message_id"], 42)
        bot.send_message.assert_awaited_once()
        self.assertEqual(bot.send_message.await_args.kwargs["chat_id"], "-123")
        bot.get_updates.assert_not_awaited()

    async def test_inspection_is_read_only_and_does_not_reveal_chat_or_webhook(self):
        bot = SimpleNamespace(
            get_me=AsyncMock(return_value=SimpleNamespace(is_bot=True, username="test_bot")),
            get_chat=AsyncMock(return_value=SimpleNamespace(type="private", id=123456)),
            get_webhook_info=AsyncMock(
                return_value=SimpleNamespace(url="https://private.example/secret")
            ),
            send_message=AsyncMock(),
            get_updates=AsyncMock(),
            delete_webhook=AsyncMock(),
        )
        report = await inspect_connection(bot, "123456")
        self.assertTrue(report["bot_authenticated"])
        self.assertTrue(report["configured_chat_accessible"])
        self.assertTrue(report["webhook_configured"])
        self.assertFalse(report["delivery_tested"])
        self.assertNotIn("123456", str(report))
        self.assertNotIn("secret", str(report))
        bot.send_message.assert_not_awaited()
        bot.get_updates.assert_not_awaited()
        bot.delete_webhook.assert_not_awaited()
