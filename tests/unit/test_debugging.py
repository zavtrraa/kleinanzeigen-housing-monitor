import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from bs4 import BeautifulSoup

from kleinanzeigen_bot.core.diagnostics import capture_failure
from kleinanzeigen_bot.core.exceptions import SelectorNotFound
from kleinanzeigen_bot.housing.runtime import new_runtime, read_runtime, save_runtime
from kleinanzeigen_bot.ingestion.extractors import parse_card
from kleinanzeigen_bot.settings import Runtime, load_yaml

ROOT = Path(__file__).resolve().parents[2]


class CurrentMarkupTests(unittest.TestCase):
    def test_runtime_selectors_match_observed_live_dom_and_old_dom(self):
        config = load_yaml(ROOT / "config/runtime.yaml", Runtime)
        for name, expected_count in [("search_current.html", 2), ("search.html", 1)]:
            with self.subTest(name=name):
                soup = BeautifulSoup(
                    (ROOT / "tests/fixtures" / name).read_text(encoding="utf-8"), "html.parser"
                )
                cards = soup.select(config.search.cards)
                self.assertEqual(len(cards), expected_count)
                for card in cards:

                    def text(selector, current_card=card):
                        element = current_card.select_one(selector)
                        return element.get_text(" ", strip=True) if element else ""

                    link = card.select_one(config.search.link)
                    listing = parse_card(
                        {
                            "href": card.get("data-href") or link["href"],
                            "title": text(config.search.title),
                            "price": text(config.search.price),
                            "details": text(config.search.details),
                            "date": text(config.search.date),
                        },
                        "fixture",
                    )
                    self.assertTrue(listing.title)
                    self.assertIsNotNone(listing.price_eur)
                    self.assertIsNotNone(listing.size_sqm)

    def test_runtime_snapshot_reports_stop_and_staleness(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            report = new_runtime()
            save_runtime(directory, report)
            self.assertEqual(read_runtime(directory)["state"], "running")
            self.assertFalse(read_runtime(directory)["heartbeat_stale"])
            report["state"] = "stopped"
            report["stop_reason"] = "cancelled"
            save_runtime(directory, report)
            self.assertEqual(read_runtime(directory)["stop_reason"], "cancelled")


class DiagnosticTests(unittest.IsolatedAsyncioTestCase):
    async def test_failure_artifacts_include_stage_and_safe_reason(self):
        page = SimpleNamespace(
            url="https://www.kleinanzeigen.de/s-test",
            title=AsyncMock(return_value="Fixture"),
            content=AsyncMock(return_value="<html><p>fixture</p></html>"),
            screenshot=AsyncMock(return_value=b"fixture"),
        )
        with tempfile.TemporaryDirectory() as folder:
            error = SelectorNotFound("No cards found", stage="search")
            target = await capture_failure(page, Path(folder), error, stage=error.stage)
            metadata = json.loads((Path(target) / "metadata.json").read_text())
            self.assertEqual(metadata["stage"], "search")
            self.assertEqual(metadata["reason"], "No cards found")
            self.assertTrue((Path(target) / "page.html").exists())

    async def test_diagnostics_do_not_expose_untrusted_exception_message(self):
        page = SimpleNamespace(
            url="https://www.kleinanzeigen.de/s-test",
            title=AsyncMock(return_value="Fixture"),
            content=AsyncMock(return_value="<p>fixture</p>"),
            screenshot=AsyncMock(side_effect=RuntimeError("private-token")),
        )
        with tempfile.TemporaryDirectory() as folder:
            target = await capture_failure(
                page, Path(folder), RuntimeError("private-token"), stage="search"
            )
            text = (Path(target) / "metadata.json").read_text()
            self.assertNotIn("private-token", text)
            self.assertIn("RuntimeError", text)
