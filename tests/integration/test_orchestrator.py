import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from kleinanzeigen_bot.core.exceptions import ChallengeRequired, SelectorNotFound, SendUncertain
from kleinanzeigen_bot.core.models import Listing
from kleinanzeigen_bot.core.orchestrator import Orchestrator
from kleinanzeigen_bot.state.sqlite_repo import SQLiteRepository

A = Listing("1", "https://www.kleinanzeigen.de/s-anzeige/a/1-203-1", "A")
B = Listing("2", "https://www.kleinanzeigen.de/s-anzeige/b/2-203-1", "B")


class Search:
    def __init__(self, listings=None, failure=None):
        self.listings = listings if listings is not None else [A, B]
        self.failure = failure
        self.calls = 0

    async def search(self):
        self.calls += 1
        if self.failure:
            raise self.failure
        return self.listings


class Texts:
    def render(self, listing):
        return "Exact message for " + listing.id


class Prepared:
    def __init__(self, sender):
        self.sender = sender

    async def submit(self):
        self.sender.clicks += 1
        if self.sender.failure:
            raise self.sender.failure

    async def close(self):
        self.sender.closed += 1


class Sender:
    def __init__(self, failure=None, prepare_failure=None):
        self.failure, self.prepare_failure = failure, prepare_failure
        self.clicks = self.closed = self.prepared = 0

    async def prepare(self, listing, text):
        self.prepared += 1
        if self.prepare_failure:
            raise self.prepare_failure
        return Prepared(self)


class Notifications:
    def __init__(self, fail=False):
        self.fail, self.reports, self.alerts = fail, [], []

    async def summary(self, report):
        if self.fail:
            raise RuntimeError("Telegram unavailable")
        self.reports.append(report)

    async def alert(self, text):
        if self.fail:
            raise RuntimeError("Telegram unavailable")
        self.alerts.append(text)


class OrchestratorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.temp.name) / "state.sqlite3")
        self.search, self.sender, self.notifier = Search(), Sender(), Notifications()

    def tearDown(self):
        self.repo.close()
        self.temp.cleanup()

    def bot(self, **kwargs):
        return Orchestrator(self.search, self.repo, self.sender, Texts(), self.notifier, **kwargs)

    def status(self, listing_id="1"):
        return self.repo.db.execute(
            "SELECT status FROM listings WHERE id=?", (listing_id,)
        ).fetchone()[0]

    async def test_dry_run_never_submits_and_stays_eligible_for_live(self):
        report = await self.bot().run_cycle()
        self.assertEqual(self.sender.clicks, 0)
        self.assertEqual(len(report.previews), 1)
        self.assertEqual(report.previews[0].text, "Exact message for 1")
        self.assertEqual(self.status(), "discovered")
        live = await self.bot(dry_run=False).run_cycle()
        self.assertEqual(live.sent, 1)
        self.assertEqual(self.status(), "sent")
        self.assertEqual(self.sender.closed, 2)

    async def test_live_is_limited_and_duplicate_discovery_does_not_resend(self):
        first = await self.bot(dry_run=False).run_cycle()
        second = await self.bot(dry_run=False).run_cycle()
        third = await self.bot(dry_run=False).run_cycle()
        self.assertEqual([first.sent, second.sent, third.sent], [1, 1, 0])
        self.assertEqual(self.sender.clicks, 2)

    async def test_ambiguous_send_blocks_retry_and_pauses(self):
        self.sender.failure = SendUncertain("timeout after delivery")
        report = await self.bot(dry_run=False).run_cycle()
        self.assertEqual(report.uncertain, 1)
        self.assertTrue(report.halted)
        self.assertEqual(self.status(), "uncertain")
        self.assertEqual(self.status("2"), "discovered")
        await self.bot(dry_run=False).run_cycle()
        self.assertEqual(self.sender.clicks, 1)

    async def test_cancellation_after_claim_is_uncertain(self):
        self.sender.failure = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.bot(dry_run=False).run_cycle()
        self.assertEqual(self.status(), "uncertain")
        self.assertIsNotNone(self.repo.pause_reason())

    async def test_prepare_failure_has_no_send_attempt(self):
        self.sender.prepare_failure = SelectorNotFound("changed DOM")
        report = await self.bot(dry_run=False).run_cycle()
        self.assertTrue(report.halted)
        self.assertEqual(self.status(), "discovered")
        self.assertEqual(self.repo.db.execute("SELECT count(*) FROM attempts").fetchone()[0], 0)

    async def test_ip_block_pauses_without_retry_and_alerts(self):
        from kleinanzeigen_bot.core.exceptions import IPRangeBlocked

        self.search.failure = IPRangeBlocked("Network range restricted")
        report = await self.bot().run_cycle()
        self.assertTrue(report.halted)
        self.assertEqual(self.repo.pause_reason(), "IPRangeBlocked")
        self.assertEqual(self.sender.prepared, 0)
        self.assertTrue(any("IPRangeBlocked" in alert for alert in self.notifier.alerts))
        self.search.failure = None
        await self.bot().run_cycle()
        self.assertEqual(self.search.calls, 1)

    async def test_challenge_persists_pause_before_any_send(self):
        self.search.failure = ChallengeRequired()
        report = await self.bot().run_cycle()
        self.assertTrue(report.halted)
        self.assertEqual(self.sender.prepared, 0)
        self.assertEqual(self.repo.pause_reason(), "ChallengeRequired")
        self.search.failure = None
        await self.bot().run_cycle()
        self.assertEqual(self.search.calls, 1)

    async def test_telegram_failure_does_not_lose_confirmed_state(self):
        self.notifier.fail = True
        report = await self.bot(dry_run=False).run_cycle()
        self.assertEqual(report.sent, 1)
        self.assertEqual(self.status(), "sent")
        row = self.repo.db.execute("SELECT report FROM cycles WHERE id=?", (report.id,)).fetchone()
        self.assertIn("Summary delivery failed", json.loads(row[0])["errors"][0])

    async def test_stale_backlog_not_in_current_results_is_not_sent(self):
        self.repo.discover(A)
        self.search.listings = [B]
        report = await self.bot(dry_run=False).run_cycle()
        self.assertEqual(report.sent, 1)
        self.assertEqual(self.status(), "discovered")
        self.assertEqual(self.status("2"), "sent")

    async def test_overlapping_search_profiles_are_deduplicated(self):
        self.search.listings = [A, A]
        report = await self.bot(dry_run=False, maximum=2).run_cycle()
        self.assertEqual(report.found, 1)
        self.assertEqual(self.sender.clicks, 1)

    async def test_excluded_listing_does_not_consume_budget_or_abort_next_candidate(self):
        from kleinanzeigen_bot.core.exceptions import ListingFiltered

        class SelectiveSender(Sender):
            async def prepare(self, listing, text):
                if listing.id == "1":
                    raise ListingFiltered("excluded", "Apartment exchange")
                return await super().prepare(listing, text)

        self.sender = SelectiveSender()
        report = await self.bot(dry_run=False).run_cycle()
        self.assertEqual(report.sent, 1)
        self.assertFalse(report.halted)
        self.assertEqual(self.status("1"), "discovered")
        self.assertEqual(self.status("2"), "sent")
        self.assertEqual(report.skipped[0].reason, "Apartment exchange")
        saved = json.loads(
            self.repo.db.execute("SELECT report FROM cycles WHERE id=?", (report.id,)).fetchone()[0]
        )
        self.assertEqual(saved["skipped"][0]["outcome"], "excluded")

    async def test_review_candidate_never_claimed(self):
        from kleinanzeigen_bot.core.exceptions import ListingFiltered

        self.sender.prepare_failure = ListingFiltered("review", "Warmmiete неизвестна")
        report = await self.bot(dry_run=False).run_cycle()
        self.assertEqual(report.sent, 0)
        self.assertFalse(report.halted)
        self.assertEqual(len(report.skipped), 2)
        self.assertEqual(self.repo.db.execute("SELECT count(*) FROM attempts").fetchone()[0], 0)

    async def test_pause_between_attempts_only(self):
        pauses = []

        async def pause():
            pauses.append(True)

        report = await self.bot(dry_run=False, maximum=2, pause_between=pause).run_cycle()
        self.assertEqual(report.sent, 2)
        self.assertEqual(pauses, [True])

    async def test_cancel_during_preparation_reports_halted_without_send_attempt(self):
        self.sender.prepare_failure = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.bot(dry_run=False).run_cycle()
        report = self.notifier.reports[-1]
        self.assertTrue(report.halted)
        self.assertIn("Run interrupted", report.errors[0])
        self.assertEqual(report.sent, 0)
        self.assertEqual(report.uncertain, 0)
        self.assertEqual(self.repo.db.execute("SELECT count(*) FROM attempts").fetchone()[0], 0)
        saved = json.loads(
            self.repo.db.execute("SELECT report FROM cycles WHERE id=?", (report.id,)).fetchone()[0]
        )
        self.assertTrue(saved["halted"])
        self.assertIsNone(self.repo.pause_reason())

    async def test_progress_explains_each_skip_without_claiming_send(self):
        from kleinanzeigen_bot.core.exceptions import ListingFiltered

        progress = []
        self.sender.prepare_failure = ListingFiltered("excluded", "Applications closed")
        report = await self.bot(dry_run=False, progress=progress.append).run_cycle()
        self.assertEqual(report.sent, 0)
        self.assertTrue(any("[1/2]" in text for text in progress))
        self.assertTrue(any("[2/2]" in text for text in progress))
        self.assertEqual(sum("Skipped" in text for text in progress), 2)
        self.assertFalse(
            any("Отправляю" in text or "Доставка подтверждена" in text for text in progress)
        )

    async def test_progress_confirms_delivery_only_after_successful_submit(self):
        progress = []
        self.sender.failure = SendUncertain("No confirmation")
        report = await self.bot(dry_run=False, progress=progress.append).run_cycle()
        self.assertEqual(report.uncertain, 1)
        self.assertFalse(any("Доставка подтверждена" in text for text in progress))

    async def test_single_bad_form_is_cached_and_does_not_block_other_listing(self):
        class OneBad(Sender):
            async def prepare(self, listing, text):
                if listing.id == "1":
                    raise SelectorNotFound("No field", stage="message input")
                return await super().prepare(listing, text)

        self.sender = OneBad()
        report = await self.bot(dry_run=False).run_cycle()
        self.assertEqual(report.sent, 1)
        self.assertFalse(report.halted)
        self.assertEqual(report.preparation_failed, 1)
        self.assertEqual(report.items[0].status, "prepare_failed")
        second = await self.bot(dry_run=False).run_cycle()
        self.assertEqual(second.preparation_failed, 0)
        self.assertEqual(second.cached, 1)
        self.assertEqual(second.sent, 0)

    async def test_new_listing_precedes_older_known_backlog(self):
        self.repo.discover(A)
        report = await self.bot(fresh_first=True).run_cycle()
        self.assertEqual(report.previews[0].listing_id, "2")

    async def test_event_chat_gets_reject_reason_and_cached_skip_reason(self):
        from types import SimpleNamespace
        from unittest.mock import Mock

        from kleinanzeigen_bot.core.exceptions import ListingFiltered

        events = SimpleNamespace(emit=Mock())
        self.sender.prepare_failure = ListingFiltered("excluded", "Apartment exchange")
        await self.bot(events=events).run_cycle()
        rejected = [
            call for call in events.emit.call_args_list if call.args[1] == "eligibility.check"
        ]
        self.assertEqual(len(rejected), 2)
        self.assertIn("Apartment exchange", rejected[0].args[0])
        self.assertEqual(rejected[0].args[3], A.url)
        events.emit.reset_mock()
        await self.bot(events=events).run_cycle()
        cached = [
            call for call in events.emit.call_args_list if call.args[1] == "repository.cached_check"
        ]
        self.assertEqual(len(cached), 2)
        self.assertIn("Apartment exchange", cached[0].args[0])
