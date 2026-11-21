import json
import sqlite3
import tempfile
import unittest
from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import patch

from kleinanzeigen_bot.core.models import Listing
from kleinanzeigen_bot.state.sqlite_repo import SQLiteRepository

AD = Listing(
    "123",
    "https://www.kleinanzeigen.de/s-anzeige/test/123-203-1",
    "Wohnung",
    "335",
    profile="hannover",
)


class ObservationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "state.sqlite3"
        self.repo = SQLiteRepository(self.path)
        self.repo.begin_cycle("one")

    def tearDown(self):
        self.repo.close()
        self.temp.cleanup()

    def test_micro_patches_record_only_material_changes(self):
        self.assertEqual(self.repo.observe(AD, "one"), "new")
        self.repo.begin_cycle("two")
        self.assertEqual(self.repo.observe(replace(AD, profile="seelze"), "two"), "unchanged")
        self.repo.begin_cycle("three")
        self.assertEqual(self.repo.observe(replace(AD, price_eur="400"), "three"), "changed")
        history = self.repo.history("123")
        self.assertEqual(history[0]["patch"], {"price_eur": {"before": "335", "after": "400"}})
        self.assertEqual(history[1]["patch"], {})
        self.assertEqual(len(history), 3)

    def test_unchanged_cards_cached_but_changed_cards_reopen(self):
        self.repo.observe(AD, "one")
        self.repo.record_check("123", "excluded", "WG")
        self.assertEqual(self.repo.pending(), [])
        self.repo.observe(AD)
        self.assertEqual(self.repo.pending(), [])
        self.repo.observe(replace(AD, title="Changed flat"))
        self.assertEqual(len(self.repo.pending()), 1)

    def test_changed_sent_listing_is_never_resent(self):
        self.repo.observe(AD)
        self.repo.complete(self.repo.claim("123", "one", "test"), True)
        self.repo.observe(replace(AD, price_eur="400"))
        self.assertEqual(self.repo.pending(), [])
        self.assertIsNone(self.repo.claim("123", "one", "again"))

    def test_ttl_backoff_and_policy_change(self):
        self.repo.observe(AD)
        with patch("time.time", return_value=1000):
            self.repo.record_check("123", "prepare_error", "field", seconds=3600)
            self.assertEqual(self.repo.cached_check("123")["next_check_at"], 4600)
            self.repo.record_check("123", "prepare_error", "field", seconds=3600)
            self.assertEqual(self.repo.cached_check("123")["next_check_at"], 8200)
            self.assertEqual(self.repo.pending(), [])
            self.repo.check_policy = "new-selectors"
            self.assertEqual(len(self.repo.pending()), 1)
            self.repo.check_policy = "v0.7"
        with patch("time.time", return_value=8201):
            self.assertEqual(len(self.repo.pending()), 1)

    def test_detail_patches_normalize_whitespace(self):
        self.repo.observe(AD)
        self.repo.record_details(AD, {"description": "Flat  available", "costs": "Warmmiete 500"})
        self.repo.record_details(AD, {"description": "Flat\navailable", "costs": "Warmmiete 500"})
        row = self.repo.db.execute(
            "SELECT patch FROM detail_observations ORDER BY id DESC LIMIT 1"
        ).fetchone()
        self.assertEqual(json.loads(row[0]), {})

    def test_v1_migration_keeps_state_and_creates_backup(self):
        self.repo.close()
        old = Path(self.temp.name) / "old.sqlite3"
        schema = Path(__file__).resolve().parents[2] / "src/kleinanzeigen_bot/state/schema.sql"
        legacy = schema.read_text().split("CREATE TABLE IF NOT EXISTS observations")[0]
        with sqlite3.connect(old) as db:
            db.executescript(legacy)
            payload = asdict(AD)
            payload.pop("is_request")
            db.execute(
                "INSERT INTO listings(id,url,title,payload,status,first_seen,last_see"
                "n) VALUES(?,?,?,?,?,?,?)",
                (AD.id, AD.url, AD.title, json.dumps(payload), "sent", "2026-10-01", "2026-10-01"),
            )
            db.execute("PRAGMA user_version=1")
        db.close()
        self.repo = SQLiteRepository(old)
        self.assertEqual(self.repo.db.execute("PRAGMA user_version").fetchone()[0], 3)
        self.assertEqual(self.repo.pending(), [])
        self.assertTrue(list((old.parent / "backups").glob("*.sqlite3")))
