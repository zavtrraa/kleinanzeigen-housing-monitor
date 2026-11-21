import asyncio
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx

from kleinanzeigen_bot.core.models import CycleReport, Preview, SkippedListing
from kleinanzeigen_bot.demo import DemoPrepared, run_demo
from kleinanzeigen_bot.housing.models import HousingConfig, SourceBlocked, SourceRateLimited
from kleinanzeigen_bot.housing.public_sources import HEIMKEHR_URL, HeimkehrSource, fetch_public
from kleinanzeigen_bot.main import parser, run
from kleinanzeigen_bot.notifications.formatters import format_summary
from kleinanzeigen_bot.notifications.telegram_bot import TelegramNotifier
from kleinanzeigen_bot.scheduler import run_scheduled

ROOT = Path(__file__).resolve().parents[2]


class PublicTransportTests(unittest.IsolatedAsyncioTestCase):
    async def call(self, responses, **kwargs):
        get = AsyncMock(side_effect=responses)
        manager = Mock()
        manager.__aenter__ = AsyncMock(return_value=SimpleNamespace(get=get))
        manager.__aexit__ = AsyncMock(return_value=False)
        with patch(
            "kleinanzeigen_bot.housing.public_sources.httpx.AsyncClient", return_value=manager
        ):
            result = await fetch_public(HEIMKEHR_URL, **kwargs)
        return result, get

    def response(self, status=200, **kwargs):
        return httpx.Response(status, request=httpx.Request("GET", HEIMKEHR_URL), **kwargs)

    async def test_conditional_get_preserves_etag_and_not_modified(self):
        response, get = await self.call([self.response(304)], etag='"version1"')
        self.assertEqual(response.status_code, 304)
        self.assertEqual(get.call_args.kwargs["headers"], {"If-None-Match": '"version1"'})

    async def test_denied_and_rate_limited_are_never_retried(self):
        for status in (401, 403):
            with self.subTest(status=status), self.assertRaises(SourceBlocked):
                await self.call([self.response(status)])
        for retry, expected in (("120", 120), ("tomorrow", 1800), ("0", 60)):
            with self.subTest(retry=retry), self.assertRaises(SourceRateLimited) as error:
                await self.call([self.response(429, headers={"retry-after": retry})])
            self.assertEqual(error.exception.retry_after, expected)

    async def test_server_errors_are_not_silently_empty_results(self):
        with self.assertRaises(httpx.HTTPStatusError):
            await self.call([self.response(503)])

    async def test_allowed_redirect_and_redirect_limit(self):
        response, get = await self.call(
            [
                self.response(302, headers={"location": "/wohnungsangebote?seite=1"}),
                self.response(200),
            ]
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(get.await_count, 2)
        with self.assertRaisesRegex(ValueError, "Too many"):
            await self.call([self.response(302, headers={"location": "/again"})] * 4)

    async def test_redirect_to_external_host_is_rejected_before_request(self):
        with self.assertRaisesRegex(ValueError, "Unexpected"):
            await self.call([self.response(302, headers={"location": "https://example.com/"})])

    async def test_unsafe_initial_urls_do_not_make_requests(self):
        for url in ("http://hanova.de/", "https://example.com/", "https://hanova.de:8443/"):
            get = AsyncMock()
            manager = Mock()
            manager.__aenter__ = AsyncMock(return_value=SimpleNamespace(get=get))
            manager.__aexit__ = AsyncMock(return_value=False)
            with (
                self.subTest(url=url),
                patch(
                    "kleinanzeigen_bot.housing.public_sources.httpx.AsyncClient",
                    return_value=manager,
                ),
                self.assertRaises(ValueError),
            ):
                await fetch_public(url)
            get.assert_not_awaited()

    async def test_heimkehr_304_preserves_checkpoint_without_notifications(self):
        checkpoint = {"etag": "v1"}
        with patch(
            "kleinanzeigen_bot.housing.public_sources.fetch_public", new_callable=AsyncMock
        ) as fetch:
            fetch.return_value = self.response(304)
            result = await HeimkehrSource(HousingConfig()).collect(checkpoint)
        self.assertEqual(result.candidates, [])
        self.assertEqual(result.checkpoint, checkpoint)
        self.assertTrue(result.details["unchanged"])

    async def test_heimkehr_changed_feed_updates_checkpoint(self):
        html = (ROOT / "tests/fixtures/heimkehr_public.html").read_text(encoding="utf-8")
        with patch(
            "kleinanzeigen_bot.housing.public_sources.fetch_public", new_callable=AsyncMock
        ) as fetch:
            fetch.return_value = self.response(200, text=html, headers={"etag": "v2"})
            result = await HeimkehrSource(HousingConfig()).collect({"etag": "v1"})
        self.assertTrue(result.candidates)
        self.assertEqual(result.checkpoint, {"etag": "v2"})
        self.assertEqual(result.details["count"], len(result.candidates))


class ReportingTests(unittest.IsolatedAsyncioTestCase):
    def test_failed_search_is_not_reported_as_zero_results(self):
        report = CycleReport("12345678-test", True, halted=True, errors=["SelectorNotFound"])
        text = format_summary(report)
        self.assertIn("result count is unknown", text)
        self.assertNotIn("Found: 0", text)
        self.assertIn("SelectorNotFound", text)

    def test_success_summary_contains_preview_and_exclusion_reason(self):
        report = CycleReport("cycle", True, search_completed=True, found=2)
        report.previews = [Preview("1", "https://example.com/1", "Exact message")]
        report.skipped = [SkippedListing("2", "https://example.com/2", "excluded", "Anfragestopp")]
        text = format_summary(report)
        self.assertIn("Found: 2", text)
        self.assertIn("Exact message", text)
        self.assertIn("Anfragestopp", text)

    async def test_long_unicode_telegram_messages_split_without_loss(self):
        bot = SimpleNamespace(send_message=AsyncMock())
        notifier = TelegramNotifier(bot, "fixture-chat")
        text = "🏠" * 2500 + "End"
        await notifier._send(text)
        parts = [call.kwargs["text"] for call in bot.send_message.await_args_list]
        self.assertEqual("".join(parts), text)
        self.assertGreater(len(parts), 1)
        for call in bot.send_message.await_args_list:
            self.assertLessEqual(len(call.kwargs["text"].encode("utf-16-le")) // 2, 3800)
            self.assertIsNone(call.kwargs["parse_mode"])

    async def test_telegram_failure_propagates_without_local_retry(self):
        bot = SimpleNamespace(send_message=AsyncMock(side_effect=RuntimeError("offline")))
        with self.assertRaises(RuntimeError):
            await TelegramNotifier(bot, "fixture").alert("Check")
        bot.send_message.assert_awaited_once()

    async def test_cli_demo_has_isolated_state_and_never_submits(self):
        with tempfile.TemporaryDirectory() as folder, redirect_stdout(io.StringIO()) as output:
            directory = Path(folder)
            report = await run_demo(directory)
            code = await run(parser().parse_args(["--demo", "--data-dir", folder]))
            self.assertEqual(code, 0)
            self.assertTrue(report.dry_run)
            self.assertEqual(report.sent, 0)
            self.assertEqual(report.found, 2)
            self.assertTrue((directory / "demo.sqlite3").is_file())
            self.assertFalse((directory / "state.sqlite3").exists())
            self.assertIn("DRY RUN", output.getvalue())
        with self.assertRaises(AssertionError):
            await DemoPrepared().submit()


class SchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def test_halted_initial_cycle_does_not_start_scheduler(self):
        bot = SimpleNamespace(run_cycle=AsyncMock(return_value=SimpleNamespace(halted=True)))
        with patch("kleinanzeigen_bot.scheduler.AsyncIOScheduler") as scheduler:
            await run_scheduled(bot, SimpleNamespace())
        scheduler.assert_not_called()

    async def test_scheduler_has_no_overlap_and_stops_after_halted_job(self):
        bot = SimpleNamespace(
            run_cycle=AsyncMock(
                side_effect=[SimpleNamespace(halted=False), SimpleNamespace(halted=True)]
            )
        )
        scheduler = Mock()
        tasks = []

        def start():
            tasks.append(asyncio.create_task(scheduler.add_job.call_args.args[0]()))

        scheduler.start.side_effect = start
        with patch("kleinanzeigen_bot.scheduler.AsyncIOScheduler", return_value=scheduler):
            await asyncio.wait_for(
                run_scheduled(bot, SimpleNamespace(interval_seconds=7200, jitter_seconds=300)), 2
            )
        await asyncio.gather(*tasks)
        self.assertEqual(bot.run_cycle.await_count, 2)
        options = scheduler.add_job.call_args.kwargs
        self.assertEqual(options["max_instances"], 1)
        self.assertTrue(options["coalesce"])
        self.assertEqual(options["seconds"], 7200)
        scheduler.shutdown.assert_called_once_with(wait=False)
