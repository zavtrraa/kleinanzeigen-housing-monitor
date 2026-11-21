import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from kleinanzeigen_bot.action.rate_limiter import CycleBudget
from kleinanzeigen_bot.action.text_variator import TextVariator
from kleinanzeigen_bot.core.models import Listing
from kleinanzeigen_bot.ingestion.extractors import parse_card, parse_published
from kleinanzeigen_bot.settings import (
    Profiles,
    Runtime,
    Settings,
    load_yaml,
    validate_ready,
    validate_site_url,
)

ROOT = Path(__file__).resolve().parents[2]


class ConfigAndParsingTests(unittest.TestCase):
    def test_safe_defaults(self):
        settings = Settings(_env_file=None)
        self.assertTrue(settings.dry_run)
        self.assertEqual(settings.max_messages_per_cycle, 1)
        self.assertFalse(settings.headless)

    def test_live_requires_verified_selectors(self):
        settings = Settings(_env_file=None, dry_run=False)
        profiles = Profiles(
            profiles=[{"name": "test", "url": "https://www.kleinanzeigen.de/s-test"}]
        )
        runtime = load_yaml(ROOT / "config/runtime.yaml", Runtime)
        runtime.live_validation = "verified"
        with self.assertRaisesRegex(ValueError, "selectors"):
            validate_ready(settings, profiles, runtime)

    def test_rejects_unknown_yaml_keys(self):
        with self.assertRaises(ValidationError):
            Profiles(profiles=[], typo=True)

    def test_invalid_pause_order(self):
        with self.assertRaises(ValidationError):
            Settings(_env_file=None, min_message_pause_seconds=4, max_message_pause_seconds=1)

    def test_external_and_spoofed_urls_rejected(self):
        for url in [
            "http://www.kleinanzeigen.de/x",
            "https://kleinanzeigen.de.evil.test/x",
            "https://www.kleinanzeigen.de@evil.test/x",
            "https://evil.test/x",
            "https://user@www.kleinanzeigen.de/x",
            "https://www.kleinanzeigen.de:8443/x",
        ]:
            with self.subTest(url=url), self.assertRaises(ValueError):
                validate_site_url(url)

    def test_parse_card_canonical_id_and_decimal_fields(self):
        card = parse_card(
            {
                "href": "/s-anzeige/wohnung/123456-203-1?tracking=abc",
                "title": "  Helle Wohnung  ",
                "price": "1.250,50 €",
                "details": "65,5 m² · 2,5 Zimmer",
                "date": "Heute, 09:05",
            },
            "berlin",
            datetime(2026, 10, 27, 12, tzinfo=ZoneInfo("Europe/Berlin")),
        )
        self.assertEqual(card.id, "123456")
        self.assertEqual(card.price_eur, "1250.50")
        self.assertEqual(card.rooms, "2.5")
        self.assertEqual(card.size_sqm, "65.5")
        self.assertNotIn("?", card.url)
        self.assertEqual(card.published_at, "2026-10-27T09:05:00+01:00")

    def test_relative_date_crosses_year_boundary(self):
        now = datetime(2026, 1, 1, 1, tzinfo=ZoneInfo("Europe/Berlin"))
        self.assertEqual(parse_published("Gestern, 23:55", now), "2025-12-31T23:55:00+01:00")
        self.assertEqual(parse_published("20.10.2026", now), "2026-10-20")
        self.assertIsNone(parse_published("Heute, 99:99", now))
        self.assertIsNone(parse_published("unknown", now))

    def test_rate_budget_counts_attempts(self):
        budget = CycleBudget(1)
        budget.reserve()
        self.assertFalse(budget.available)
        with self.assertRaises(RuntimeError):
            budget.reserve()

    def test_template_formats_literal_listing_braces_safely(self):
        text = TextVariator(
            "{greeting}\n{title}\n{closing}\n{signature}",
            ["Hallo,"],
            ["Viele Grüße"],
            "Max",
        ).render(Listing("1", "url", "{unexpected} Wohnung"))
        self.assertIn("{unexpected} Wohnung", text)
        self.assertTrue(text.endswith("Max"))

    def test_template_rejects_attribute_traversal_and_unknown_fields(self):
        for field in ["title.__class__", "unknown", "title!r", "title:>10"]:
            with self.subTest(field=field), self.assertRaises(ValueError):
                TextVariator("{greeting}{closing}{signature}{" + field + "}", ["A"], ["B"], "C")
