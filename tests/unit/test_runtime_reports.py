import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from kleinanzeigen_bot.core.models import CycleReport, RunItem
from kleinanzeigen_bot.notifications.formatters import format_summary, summary_parts
from kleinanzeigen_bot.runtime.events import EventConsole


class SummaryTests(unittest.TestCase):
    def test_many_notification_errors_keep_every_summary_part_within_limit(self):
        report = CycleReport("many-errors", True, monitoring=True)
        report.errors = ["Listing notification failed: TimeoutError"] * 125
        parts = summary_parts(report)
        self.assertTrue(all(len(part.encode("utf-16-le")) // 2 <= 3600 for part in parts))

    def test_links_dates_status_warning(self):
        report = CycleReport(
            "cycle",
            False,
            search_completed=True,
            started_at="2026-10-28T19:00:00+00:00",
            finished_at="2026-10-28T19:05:00+00:00",
        )
        report.items = [
            RunItem(
                "1",
                "A < B",
                "https://www.kleinanzeigen.de/s-anzeige/a/1-203-1",
                "sent",
                warnings=["Warmmiete is not specified reliably"],
            )
        ]
        text = format_summary(report)
        self.assertIn("28.10.2026 20:00:00 CET", text)
        self.assertIn("28.10.2026 20:05:00 CET", text)
        self.assertIn("! Warmmiete", text)
        html = summary_parts(report)[0]
        self.assertIn("1. <a href=", html)
        self.assertIn("A &lt; B", html)
        self.assertIn("Reply sent", html)

    def test_large_report_keeps_whole_html_links(self):
        report = CycleReport("cycle", False)
        report.items = [
            RunItem(
                str(i),
                "🏠" * 100,
                f"https://www.kleinanzeigen.de/s-anzeige/a/{i}-203-1",
                "deferred",
            )
            for i in range(70)
        ]
        parts = summary_parts(report)
        self.assertGreater(len(parts), 1)
        for part in parts:
            self.assertLessEqual(len(part.encode("utf-16-le")) // 2, 3600)
            self.assertEqual(part.count("<a "), part.count("</a>"))


class EventTests(unittest.IsolatedAsyncioTestCase):
    async def test_delivers_only_to_readonly_chat_and_persists(self):
        with tempfile.TemporaryDirectory() as directory:
            bot = SimpleNamespace(send_message=AsyncMock())
            sink = EventConsole(Path(directory), bot, "-1000000001", pace=0)
            sink.start()
            sink.emit("Открыто объявление", "sender.prepare", "--live", "goto listing")
            await sink.close()
            kwargs = bot.send_message.call_args.kwargs
            self.assertEqual(kwargs["chat_id"], "-1000000001")
            self.assertNotIn("reply_markup", kwargs)
            self.assertIn("| sender.prepare", kwargs["text"])
            self.assertEqual(
                json.loads(sink.path.read_text(encoding="utf-8"))["function"], "sender.prepare"
            )

    async def test_delivery_failure_does_not_break_business_logic(self):
        with tempfile.TemporaryDirectory() as directory:
            bot = SimpleNamespace(send_message=AsyncMock(side_effect=RuntimeError("private-token")))
            sink = EventConsole(Path(directory), bot, "-1", pace=0)
            sink.start()
            sink.emit("Проверка", "test")
            await sink.close()
            self.assertEqual(sink.failures, 1)
            self.assertNotIn("private-token", sink.path.read_text(encoding="utf-8"))


class PacerTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_senders_share_per_chat_limit_without_blocking_other_chat(self):
        from kleinanzeigen_bot.notifications.pacing import ChatPacer

        tick = [100.0]
        waits = []

        async def sleep(delay):
            waits.append(delay)
            tick[0] += delay

        bot = SimpleNamespace(send_message=AsyncMock())
        paced = ChatPacer(bot, clock=lambda: tick[0], sleep=sleep)
        await paced.send_message(chat_id="main", text="one")
        await paced.send_message(chat_id="events", text="independent")
        self.assertEqual(waits, [])
        await paced.send_message(chat_id="main", text="two")
        self.assertAlmostEqual(waits[0], 3.2)
        self.assertEqual(bot.send_message.await_count, 3)


class DeltaSummaryTests(unittest.TestCase):
    def test_unchanged_backlog_is_not_repeated_but_checked_error_is_visible(self):
        report = CycleReport("cycle", False)
        report.items = [
            RunItem(
                "1",
                "OLD WAITING",
                "https://www.kleinanzeigen.de/s-anzeige/old/1-203-1",
                "deferred",
                change_kind="unchanged",
            ),
            RunItem(
                "2",
                "FAILED NOW",
                "https://www.kleinanzeigen.de/s-anzeige/fail/2-203-1",
                "prepare_failed",
                change_kind="unchanged",
            ),
        ]
        text = format_summary(report)
        self.assertNotIn("OLD WAITING", text)
        self.assertIn("FAILED NOW", text)
        self.assertIn("Known pending listings", text)
