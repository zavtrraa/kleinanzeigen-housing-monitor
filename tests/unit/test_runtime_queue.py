import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from kleinanzeigen_bot.runtime.controls import handle_update
from kleinanzeigen_bot.runtime.queue import RunQueue


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.path = Path(self.folder.name) / "runner.sqlite3"
        self.q = RunQueue(self.path)

    def tearDown(self):
        self.q.close()
        self.folder.cleanup()

    def test_active_job_serializes_new_manual_request(self):
        self.q.request("scheduled", "s1")
        first = self.q.claim()
        ident, _ = self.q.request("manual", "u1")
        self.assertIsNone(self.q.claim())
        self.q.complete(first["id"])
        self.assertEqual(self.q.claim()["id"], ident)

    def test_redelivery_is_idempotent(self):
        self.assertEqual(self.q.request("manual", "u1"), (1, True))
        self.assertEqual(self.q.request("manual", "u1"), (1, False))

    def test_bipolar_schedule_and_no_catch_up_burst(self):
        for delay in (3000, 3600, 4200):
            choose = Mock(return_value=delay)
            self.q.set("next_due", 0)
            self.q.tick(now=10000, choose=choose)
            choose.assert_called_once_with(3000, 4200)
            self.assertEqual(self.q.get("next_due"), 10000 + delay)
        self.assertEqual(self.q.status()["jobs"]["queued"], 1)
        self.assertIsNone(self.q.tick(now=10001))

    def test_manual_priority_preserves_schedule(self):
        self.q.tick(now=100, choose=lambda a, b: 3600)
        self.q.request("manual", "u1")
        self.assertEqual(self.q.claim()["trigger"], "manual")
        self.assertEqual(self.q.get("next_due"), 3700)

    def test_restart_does_not_repeat_inflight_job(self):
        self.q.request("manual", "one")
        self.q.request("manual", "two")
        self.q.claim()
        self.q.close()
        self.q = RunQueue(self.path)
        self.assertEqual(self.q.status()["jobs"]["interrupted"], 1)
        self.assertEqual(self.q.claim()["id"], 2)

    def test_pause_and_queue_limit(self):
        self.q.limit = 1
        self.q.request("manual", "one")
        with self.assertRaises(ValueError):
            self.q.request("manual", "two")
        self.q.set("paused", True)
        self.assertIsNone(self.q.claim())
        self.assertIsNone(self.q.tick(now=9999))

    def test_telegram_checkpoint_accepts_a_lower_id_after_the_reset_window(self):
        self.q.advance_telegram_offset(1000, now=100)
        self.assertEqual(self.q.telegram_offset(now=100 + 60), 1001)
        self.assertEqual(self.q.telegram_offset(now=100 + 8 * 86400), 0)


class ControlTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.q = RunQueue(Path(self.folder.name) / "queue.sqlite3")
        self.bot = SimpleNamespace(send_message=AsyncMock())
        self.events = SimpleNamespace(emit=Mock())

    def tearDown(self):
        self.q.close()
        self.folder.cleanup()

    def update(self, chat=-1, actor=42):
        query = SimpleNamespace(
            message=SimpleNamespace(chat_id=chat),
            from_user=SimpleNamespace(id=actor),
            data="r:run",
            answer=AsyncMock(),
        )
        return SimpleNamespace(callback_query=query, update_id=1)

    async def test_log_chat_is_readonly_even_for_owner(self):
        await handle_update(
            self.update(chat=-1000000001), self.q, self.bot, "-1", [42], self.events
        )
        self.assertEqual(self.q.status()["jobs"], {})
        self.bot.send_message.assert_not_awaited()

    async def test_unknown_actor_cannot_enqueue(self):
        await handle_update(self.update(actor=999), self.q, self.bot, "-1", [42], self.events)
        self.assertEqual(self.q.status()["jobs"], {})

    async def test_accepts_manual_run_while_busy_without_duplicates(self):
        self.q.request("scheduled", "initial")
        self.q.claim()
        update = self.update()
        for _ in range(2):
            await handle_update(update, self.q, self.bot, "-1", [42], self.events)
        self.assertEqual(self.q.status()["jobs"]["queued"], 1)

    async def test_control_pause_and_resume_keep_current_job_intact(self):
        self.q.request("scheduled", "initial")
        active = self.q.claim()
        update = self.update()
        update.callback_query.data = "r:pause"
        await handle_update(update, self.q, self.bot, "-1", [42], self.events)
        self.assertTrue(self.q.get("paused"))
        self.assertEqual(self.q.status()["active"]["id"], active["id"])
        update.callback_query.data = "r:resume"
        await handle_update(update, self.q, self.bot, "-1", [42], self.events)
        self.assertFalse(self.q.get("paused"))

    async def test_scheduler_with_full_manual_queue_does_not_crash(self):
        self.q.limit = 1
        self.q.request("manual", "one")
        self.assertIsNone(self.q.tick(now=100, choose=lambda a, b: 3600))
        self.assertEqual(self.q.get("next_due"), 3700)
