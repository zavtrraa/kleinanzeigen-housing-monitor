import json
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from email.message import EmailMessage
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from telegram.error import Forbidden, RetryAfter

from kleinanzeigen_bot.core.eligibility import ListingFilter
from kleinanzeigen_bot.housing.email_sources import GmailSource, canonical_offer, parse_alert
from kleinanzeigen_bot.housing.models import (
    Candidate,
    Decision,
    HousingConfig,
    SourceBlocked,
    SourceResult,
)
from kleinanzeigen_bot.housing.policy import evaluate
from kleinanzeigen_bot.housing.public_sources import hanova_embed, parse_hanova, parse_heimkehr
from kleinanzeigen_bot.housing.service import collect_sources
from kleinanzeigen_bot.housing.store import HousingStore
from kleinanzeigen_bot.housing.telegram import card, drain_outbox, process_update
from kleinanzeigen_bot.settings import Exclusions, load_yaml

ROOT = Path(__file__).resolve().parents[2]


def candidate(**changes):
    return replace(
        Candidate(
            "immowelt",
            "fixture123",
            "https://www.immowelt.de/expose/fixture123",
            "Fixture Wohnung",
            city="Hannover",
            size_sqm="25",
            warm_eur="500",
            available_from="2027-02-01",
            wbs_required=False,
        ),
        **changes,
    )


def alert_html(html, sender="alerts@immowelt.de"):
    message = EmailMessage()
    message["From"] = sender
    message["Subject"] = "Fixture Suchauftrag"
    message.set_content("HTML offer")
    message.add_alternative(html, subtype="html")
    return message.as_bytes()


class HousingPolicyTests(unittest.TestCase):
    def setUp(self):
        self.config = HousingConfig()
        self.filters = ListingFilter(load_yaml(ROOT / "config/exclusions.yaml", Exclusions))

    def test_full_match_is_eligible(self):
        self.assertEqual(evaluate(candidate(), self.config, self.filters).outcome, "eligible")

    def test_personal_requirements(self):
        cases = [
            ({"city": "Langenhagen"}, "excluded"),
            ({"warm_eur": "521"}, "excluded"),
            ({"kind": "parking"}, "excluded"),
            ({"title": "WG-Zimmer"}, "excluded"),
            ({"title": "Tauschwohnung"}, "excluded"),
            ({"title": "Nur an Frauen"}, "excluded"),
            ({"city": None}, "review"),
            ({"warm_eur": None}, "review"),
            ({"size_sqm": None}, "review"),
            ({"wbs_required": True}, "review"),
            ({"available_from": None}, "review"),
            ({"available_from": "2026-12-01"}, "review"),
            ({"description": "Warmmiete 550 €"}, "review"),
        ]
        for fields, expected in cases:
            with self.subTest(fields=fields):
                self.assertEqual(
                    evaluate(candidate(**fields), self.config, self.filters).outcome, expected
                )

    def test_wbs_known_and_negated_exchange(self):
        self.config.wbs_status = "yes"
        self.assertEqual(
            evaluate(candidate(wbs_required=True), self.config, self.filters).outcome, "eligible"
        )
        self.assertEqual(
            evaluate(candidate(title="Keine Tauschwohnung"), self.config, self.filters).outcome,
            "eligible",
        )


class PublicParsingTests(unittest.TestCase):
    def test_heimkehr_structure_and_decimals(self):
        rows = parse_heimkehr((ROOT / "tests/fixtures/heimkehr_public.html").read_text())
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].warm_eur, "500.00")
        self.assertEqual(rows[0].size_sqm, "25.50")
        self.assertIn("/wohnung-id/123456", rows[0].url)

    def test_unknown_markup_is_not_zero_results(self):
        with self.assertRaises(ValueError):
            parse_heimkehr("<p>Captcha</p>")
        self.assertEqual(parse_heimkehr("<script>var data = [];</script>"), [])

    def test_hanova_schema(self):
        payload = json.loads((ROOT / "tests/fixtures/hanova_public.json").read_text())
        rows = parse_hanova(payload)
        self.assertEqual(rows[0].city, "Hannover")
        self.assertEqual(rows[0].available_from, "2027-02-01")
        self.assertEqual(rows[0].warm_eur, "500")

    def test_hanova_embed_cannot_point_to_arbitrary_host(self):
        import base64

        content = base64.b64encode(b'<iframe src="https://evil.test/"></iframe>').decode()
        html = (
            '<div data-borlabs-cookie-content-blocker-id="immomio" '
            f'data-borlabs-cookie-content="{content}"></div>'
        )
        with self.assertRaises(ValueError):
            hanova_embed(html)


class EmailParsingTests(unittest.TestCase):
    def test_multi_listing_message_keeps_prices_separate(self):
        raw = alert_html("""
          <article><h2><a href="https://www.immowelt.de/expose/abcdef12">Wohnung A</a></h2>
          Hannover 25 m², Warmmiete 500 €, verfügbar ab 01.02.2027
          <a href="https://www.immowelt.de/expose/abcdef12?tracking=1">Ansehen</a></article>
          <article><h2><a href="https://www.immowelt.de/expose/abcdef13">Wohnung B</a></h2>
          Hannover 30 m², Warmmiete 600 €, verfügbar ab 01.02.2027</article>
        """)
        rows = parse_alert(raw)
        self.assertEqual(len(rows), 2)
        self.assertNotIn("600", rows[0].description)
        self.assertNotIn("500", rows[1].description)
        self.assertEqual(rows[0].city, "Hannover")
        self.assertEqual(rows[0].available_from, "2027-02-01")
        self.assertNotIn("tracking", rows[0].url)

    def test_trusted_unsupported_email_is_not_silently_discarded(self):
        raw = alert_html('<a href="https://tracker.example/opaque-id">Ansehen</a>')
        with self.assertRaisesRegex(ValueError, "inspect manually"):
            parse_alert(raw)

    def test_scout_link_and_tracking_unwrap(self):
        url = (
            "https://tracker.example/r?url=https%3A%2F%2Fwww.immobilienscout24.de%2Fexpose%2F12345"
        )
        self.assertEqual(
            canonical_offer(url),
            ("immoscout24", "12345", "https://www.immobilienscout24.de/expose/12345"),
        )

    def test_spoofed_hosts_and_invalid_urls_are_ignored(self):
        for url in [
            "https://www.immowelt.de.evil.test/expose/abcdef12",
            "https://www.immowelt.de:bad/expose/abcdef12",
            "javascript:alert(1)",
            "http://www.immowelt.de/expose/abcdef12",
            "https://user@www.immowelt.de/expose/abcdef12",
        ]:
            with self.subTest(url=url):
                self.assertIsNone(canonical_offer(url))
        raw = alert_html(
            '<a href="https://www.immowelt.de/expose/abcdef12">A</a>',
            sender="alerts@immowelt.de.evil.test",
        )
        self.assertEqual(parse_alert(raw), [])

    def test_gmail_auth_failure_stops_automatic_login_retries(self):
        import imaplib

        connection = MagicMock()
        connection.login.side_effect = imaplib.IMAP4.error("fixture auth failure")
        source = GmailSource(HousingConfig(), "fixture@gmail.com", "invalid")
        with patch(
            "kleinanzeigen_bot.housing.email_sources.imaplib.IMAP4_SSL", return_value=connection
        ):
            with self.assertRaises(SourceBlocked):
                source._read(None)
        connection.select.assert_not_called()
        connection.logout.assert_called_once()

    def test_gmail_is_read_only_and_peeks_body(self):
        raw = alert_html('<a href="https://www.immowelt.de/expose/abcdef12">A</a>')
        connection = MagicMock()
        connection.select.return_value = ("OK", [])
        connection.response.return_value = ("UIDVALIDITY", [b"42"])
        connection.uid.side_effect = [
            ("OK", [b"11"]),
            ("OK", [b"11 (RFC822.SIZE 1000)"]),
            ("OK", [(b"meta", raw)]),
        ]
        source = GmailSource(HousingConfig(), "fixture@gmail.com", "fixture-password")
        with patch(
            "kleinanzeigen_bot.housing.email_sources.imaplib.IMAP4_SSL", return_value=connection
        ):
            result = source._read({"uidvalidity": "42", "uid": 10})
        connection.select.assert_called_once_with('"Housing"', readonly=True)
        connection.uid.assert_any_call("fetch", "11", "(BODY.PEEK[])")
        connection.store.assert_not_called()
        self.assertEqual(result.checkpoint, {"uidvalidity": "42", "uid": 11})
        self.assertEqual(len(result.candidates), 1)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "housing.sqlite3"
        self.store = HousingStore(self.path)

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def test_all_candidates_queued_independently_of_message_budget(self):
        for index in range(3):
            self.store.upsert(candidate(external_id=str(index)), Decision("eligible"))
        self.assertEqual(self.store.status()["outbox"], {"pending": 3})

    def test_repeat_and_cosmetic_description_change_do_not_notify_again(self):
        self.store.upsert(candidate(description="Warmmiete 500 €."), Decision("eligible"))
        self.store.upsert(
            candidate(description="Warmmiete 500 €. Unsubscribe footer changed"),
            Decision("eligible"),
        )
        self.assertEqual(self.store.status()["outbox"], {"pending": 1})

    def test_numeric_formatting_change_does_not_repeat_notification(self):
        self.store.upsert(candidate(warm_eur="500.00", size_sqm="25.00"), Decision("eligible"))
        self.store.upsert(candidate(warm_eur="500", size_sqm="25"), Decision("eligible"))
        self.assertEqual(self.store.status()["outbox"], {"pending": 1})

    def test_price_change_and_review_to_eligible_queue_new_event(self):
        row = candidate(warm_eur=None)
        self.store.upsert(row, Decision("review", ("Unknown price",)))
        self.store.upsert(candidate(), Decision("eligible"))
        self.store.upsert(candidate(warm_eur="510"), Decision("eligible"))
        self.assertEqual(self.store.status()["outbox"], {"pending": 3})

    def test_source_ids_do_not_collide(self):
        self.store.upsert(candidate(source="immowelt"), Decision("eligible"))
        self.store.upsert(candidate(source="immoscout24"), Decision("eligible"))
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM candidates").fetchone()[0], 2)

    def test_crashed_delivery_recovered_but_status_read_does_not_reset_claim(self):
        self.store.upsert(candidate(), Decision("eligible"))
        self.store.claim_notification()
        other = HousingStore(self.path, recover=False)
        self.assertEqual(other.status()["outbox"], {"sending": 1})
        other.close()
        self.store.close()
        self.store = HousingStore(self.path, recover=True)
        self.assertEqual(self.store.status()["outbox"], {"pending": 1})

    def test_candidate_and_queue_commit_atomically(self):
        self.store.db.execute(
            "CREATE TRIGGER fail_queue BEFORE INSERT ON outbox BEGIN "
            "SELECT RAISE(ABORT,'fixture failure'); END;"
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.upsert(candidate(), Decision("eligible"))
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM candidates").fetchone()[0], 0)

    def test_notification_claim_is_atomic_across_connections(self):
        import threading
        from concurrent.futures import ThreadPoolExecutor

        self.store.upsert(candidate(), Decision("eligible"))
        barrier = threading.Barrier(2)

        def worker():
            other = HousingStore(self.path)
            try:
                barrier.wait(timeout=5)
                return other.claim_notification()
            finally:
                other.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            calls = [pool.submit(worker) for _ in range(2)]
            results = [call.result(timeout=10) for call in calls]
        self.assertEqual(sum(row is not None for row in results), 1)

    def test_dismissed_listing_does_not_resurface(self):
        self.store.upsert(candidate(), Decision("eligible"))
        self.store.set_workflow(candidate().short_id, "dismissed")
        self.store.upsert(candidate(warm_eur="510"), Decision("eligible"))
        self.assertEqual(self.store.status()["outbox"], {"pending": 1})


class AsyncHousingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = HousingStore(Path(self.directory.name) / "housing.sqlite3")
        self.config = HousingConfig()
        self.filters = ListingFilter(load_yaml(ROOT / "config/exclusions.yaml", Exclusions))

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    async def test_one_blocked_source_does_not_block_other_sources(self):
        blocked = SimpleNamespace(
            name="blocked", interval=1800, collect=AsyncMock(side_effect=SourceBlocked())
        )
        good = SimpleNamespace(
            name="good", interval=1800, collect=AsyncMock(return_value=SourceResult([candidate()]))
        )
        await collect_sources(self.store, [blocked, good], self.config, self.filters, force=True)
        self.assertEqual(self.store.source("blocked")["paused"], 1)
        self.assertEqual(self.store.source("good")["last_status"], "ok")
        self.assertEqual(self.store.status()["outbox"], {"pending": 2})
        await collect_sources(self.store, [blocked, good], self.config, self.filters, force=True)
        self.assertEqual(blocked.collect.await_count, 1)
        self.assertEqual(self.store.status()["outbox"], {"pending": 2})

    async def test_rate_limited_source_respects_cooldown_even_in_forced_collection(self):
        from kleinanzeigen_bot.housing.models import SourceRateLimited

        source = SimpleNamespace(
            name="limited", interval=60, collect=AsyncMock(side_effect=SourceRateLimited(3600))
        )
        await collect_sources(self.store, [source], self.config, self.filters, force=True)
        await collect_sources(self.store, [source], self.config, self.filters, force=True)
        self.assertEqual(source.collect.await_count, 1)
        self.assertEqual(self.store.source("limited")["last_status"], "rate_limited")

    async def test_notifications_survive_telegram_error_and_retry(self):
        self.store.upsert(candidate(), Decision("eligible"))
        bot = SimpleNamespace(send_message=AsyncMock(side_effect=RuntimeError("network")))
        await drain_outbox(self.store, bot, "-1", "Guten Tag", pace=AsyncMock())
        self.assertEqual(self.store.status()["outbox"], {"pending": 1})
        self.store.db.execute("UPDATE outbox SET next_attempt=0")
        self.store.db.commit()
        bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=11))
        await drain_outbox(self.store, bot, "-1", "Guten Tag", pace=AsyncMock())
        self.assertEqual(self.store.status()["outbox"], {"sent": 1})

    async def test_telegram_rate_limit_is_persisted(self):
        self.store.upsert(candidate(), Decision("eligible"))
        bot = SimpleNamespace(send_message=AsyncMock(side_effect=RetryAfter(60)))
        await drain_outbox(self.store, bot, "-1", "Guten Tag", pace=AsyncMock())
        self.assertEqual(self.store.status()["outbox"], {"pending": 1})
        self.assertIsNone(self.store.claim_notification())

    async def test_forbidden_chat_does_not_retry_forever(self):
        self.store.upsert(candidate(), Decision("eligible"))
        bot = SimpleNamespace(send_message=AsyncMock(side_effect=Forbidden("blocked")))
        await drain_outbox(self.store, bot, "-1", "Guten Tag", pace=AsyncMock())
        self.assertEqual(self.store.status()["outbox"], {"failed": 1})

    async def test_unapproved_group_member_cannot_change_workflow(self):
        self.store.upsert(candidate(), Decision("eligible"))
        query = SimpleNamespace(
            message=SimpleNamespace(chat_id=-1),
            from_user=SimpleNamespace(id=999),
            data=f"h:{candidate().short_id}:dismissed",
            answer=AsyncMock(),
        )
        update = SimpleNamespace(callback_query=query, message=None)
        await process_update(update, self.store, SimpleNamespace(), "-1", {123})
        self.assertEqual(self.store.status()["candidates"], {"new": 1})

    async def test_allowed_user_can_mark_contact_without_external_contact(self):
        self.store.upsert(candidate(), Decision("eligible"))
        query = SimpleNamespace(
            message=SimpleNamespace(chat_id=-1),
            from_user=SimpleNamespace(id=123),
            data=f"h:{candidate().short_id}:contacted",
            answer=AsyncMock(),
        )
        await process_update(
            SimpleNamespace(callback_query=query, message=None),
            self.store,
            SimpleNamespace(),
            "-1",
            {123},
        )
        self.assertEqual(self.store.status()["candidates"], {"contacted": 1})

    async def test_wrong_chat_is_rejected_even_for_allowed_user(self):
        self.store.upsert(candidate(), Decision("eligible"))
        query = SimpleNamespace(
            message=SimpleNamespace(chat_id=-2),
            from_user=SimpleNamespace(id=123),
            data=f"h:{candidate().short_id}:dismissed",
            answer=AsyncMock(),
        )
        await process_update(
            SimpleNamespace(callback_query=query, message=None),
            self.store,
            SimpleNamespace(),
            "-1",
            {123},
        )
        self.assertEqual(self.store.status()["candidates"], {"new": 1})

    async def test_global_pause_stops_collection_and_delivery(self):
        self.store.upsert(candidate(), Decision("eligible"))
        self.store.set_meta("paused", True)
        source = SimpleNamespace(name="x", interval=60, collect=AsyncMock())
        await collect_sources(self.store, [source], self.config, self.filters, force=True)
        source.collect.assert_not_awaited()
        bot = SimpleNamespace(send_message=AsyncMock())
        await drain_outbox(self.store, bot, "-1", "text")
        bot.send_message.assert_not_awaited()

    def test_card_has_open_copy_and_bookkeeping_buttons(self):
        payload = {"candidate": candidate().payload(), "decision": Decision("eligible").payload()}
        text, keyboard = card(payload, "Guten Tag,\nLG")
        self.assertIn("500", text)
        self.assertEqual(keyboard.inline_keyboard[0][0].url, candidate().url)
        self.assertEqual(keyboard.inline_keyboard[0][1].copy_text.text, "Guten Tag,\nLG")
