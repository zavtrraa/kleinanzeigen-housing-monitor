import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from telegram.error import BadRequest

from kleinanzeigen_bot.core.models import CycleReport
from kleinanzeigen_bot.notifications.pacing import ChatPacer
from kleinanzeigen_bot.notifications.telegram_bot import TelegramNotifier
from kleinanzeigen_bot.runtime.controls import handle_update
from kleinanzeigen_bot.runtime.panel import ControlPanel
from kleinanzeigen_bot.runtime.queue import RunQueue


class PanelTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.queue = RunQueue(Path(self.folder.name) / "queue.sqlite3")
        self.ident = 100

        async def send(**kwargs):
            self.ident += 1
            return SimpleNamespace(message_id=self.ident)

        self.raw = SimpleNamespace(
            send_message=AsyncMock(side_effect=send),
            edit_message_text=AsyncMock(),
            edit_message_reply_markup=AsyncMock(),
        )
        self.bot = ChatPacer(self.raw, interval=0)
        self.panel = ControlPanel(self.queue, self.bot, "-1")
        self.bot.on_message = self.panel.note_message

    def tearDown(self):
        self.queue.close()
        self.folder.cleanup()

    async def test_last_panel_is_edited_without_another_message(self):
        await self.panel.refresh()
        await self.panel.refresh("Updated", clicked_id=101)
        self.assertEqual(self.raw.send_message.await_count, 1)
        self.assertEqual(self.raw.edit_message_text.call_args.kwargs["message_id"], 101)

    async def test_summary_has_no_buttons_and_next_click_moves_panel(self):
        await self.panel.refresh()
        notifier = TelegramNotifier(self.bot, "-1", rich=True, keyboard="legacy")
        await notifier.summary(CycleReport("test", False))
        self.assertNotIn("reply_markup", self.raw.send_message.call_args.kwargs)
        await self.panel.refresh(clicked_id=101)
        self.assertEqual(self.queue.get("panel_message_id"), 103)
        self.assertEqual(self.raw.edit_message_reply_markup.call_args.kwargs["message_id"], 101)
        self.assertIsNone(self.raw.edit_message_reply_markup.call_args.kwargs["reply_markup"])

    async def test_stale_report_keyboard_is_retired_without_duplicate_panel(self):
        await self.panel.refresh()
        await self.panel.refresh(clicked_id=12)
        self.raw.edit_message_reply_markup.assert_awaited_once_with(
            chat_id="-1", message_id=12, reply_markup=None
        )
        self.assertEqual(self.raw.send_message.await_count, 1)

    async def test_restart_reuses_persisted_panel(self):
        await self.panel.refresh()
        restarted = ControlPanel(self.queue, self.bot, "-1")
        await restarted.refresh()
        self.assertEqual(self.raw.send_message.await_count, 1)

    async def test_user_message_moves_panel_but_other_chat_does_not(self):
        await self.panel.refresh()
        self.panel.note_message("-2", 999)
        await self.panel.refresh()
        self.assertEqual(self.raw.send_message.await_count, 1)
        self.panel.note_message("-1", 102)
        self.ident = 102
        await self.panel.refresh()
        self.assertEqual(self.queue.get("panel_message_id"), 103)

    async def test_unchanged_text_is_harmless(self):
        await self.panel.refresh()
        self.raw.edit_message_text.side_effect = BadRequest("Message is not modified")
        await self.panel.refresh()
        self.assertEqual(self.raw.send_message.await_count, 1)

    async def test_failed_edit_does_not_create_duplicate_buttons(self):
        await self.panel.refresh()
        self.raw.edit_message_text.side_effect = RuntimeError("Network failure")
        with self.assertRaises(RuntimeError):
            await self.panel.refresh()
        self.assertEqual(self.raw.send_message.await_count, 1)

    async def test_callback_enqueues_once_and_updates_panel(self):
        await self.panel.refresh()
        query = SimpleNamespace(
            message=SimpleNamespace(chat_id=-1, message_id=101),
            from_user=SimpleNamespace(id=42),
            data="r:run",
            answer=AsyncMock(),
        )
        update = SimpleNamespace(callback_query=query, update_id=44)
        for _ in range(2):
            await handle_update(
                update,
                self.queue,
                self.bot,
                "-1",
                [42],
                SimpleNamespace(emit=Mock()),
                panel=self.panel,
            )
        self.assertEqual(self.queue.status()["jobs"]["queued"], 1)
        self.assertEqual(self.raw.send_message.await_count, 1)
