import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from kleinanzeigen_bot.core.exceptions import StateConflict
from kleinanzeigen_bot.core.models import Listing
from kleinanzeigen_bot.state.sqlite_repo import SQLiteRepository

LISTING = Listing("123", "https://www.kleinanzeigen.de/s-anzeige/test/123-203-1", "Wohnung")


class StateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "state.sqlite3"
        self.repo = SQLiteRepository(self.path)
        self.repo.begin_cycle("cycle")
        self.repo.discover(LISTING)

    def tearDown(self):
        self.repo.close()
        self.temp.cleanup()

    def status(self):
        return self.repo.db.execute("SELECT status FROM listings WHERE id='123'").fetchone()[0]

    def test_discovery_is_idempotent_and_does_not_reset_sent(self):
        attempt = self.repo.claim("123", "cycle", "text")
        self.repo.complete(attempt, True)
        self.assertFalse(self.repo.discover(LISTING))
        self.assertEqual(self.status(), "sent")
        self.assertEqual(self.repo.pending(), [])

    def test_preview_leaves_listing_sendable(self):
        self.repo.preview("123", "cycle", "exact draft")
        self.assertEqual(self.status(), "discovered")
        self.assertEqual(len(self.repo.pending()), 1)
        text = self.repo.db.execute("SELECT text FROM previews").fetchone()[0]
        self.assertEqual(text, "exact draft")

    def test_claim_rolls_back_when_attempt_insert_fails(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.repo.claim("123", "nonexistent-cycle", "text")
        self.assertEqual(self.status(), "discovered")

    def test_concurrent_claim_only_one_wins(self):
        barrier = threading.Barrier(2)

        def worker():
            repo = SQLiteRepository(self.path)
            try:
                barrier.wait(timeout=5)
                return repo.claim("123", "cycle", "text")
            finally:
                repo.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(worker) for _ in range(2)]
            results = [future.result(timeout=10) for future in futures]
        self.assertEqual(sum(value is not None for value in results), 1)
        self.assertEqual(self.status(), "sending")

    def test_crash_recovery_never_requeues(self):
        self.repo.claim("123", "cycle", "saved text")
        self.repo.close()
        self.repo = SQLiteRepository(self.path)
        self.assertEqual(self.repo.recover_interrupted(), 1)
        self.assertEqual(self.repo.recover_interrupted(), 0)
        self.assertEqual(self.status(), "uncertain")
        self.assertIsNone(self.repo.claim("123", "cycle", "again"))
        self.assertEqual(self.repo.pending(), [])

    def test_manual_resolution_is_audited(self):
        attempt = self.repo.claim("123", "cycle", "text")
        self.repo.complete(attempt, False)
        self.repo.resolve("123", "discovered", "Checked conversation: message absent")
        self.assertEqual(self.status(), "discovered")
        self.assertEqual(self.repo.db.execute("SELECT count(*) FROM resolutions").fetchone()[0], 1)

    def test_invalid_resolution_does_not_change_state(self):
        with self.assertRaises(StateConflict):
            self.repo.resolve("123", "sent", "not uncertain")
        self.assertEqual(self.status(), "discovered")

    def test_pause_survives_reopen(self):
        self.repo.pause("ChallengeRequired")
        self.repo.close()
        self.repo = SQLiteRepository(self.path)
        self.assertEqual(self.repo.pause_reason(), "ChallengeRequired")
        self.repo.resume()
        self.assertIsNone(self.repo.pause_reason())
