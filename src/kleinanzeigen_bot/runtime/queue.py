import json
import random
import sqlite3
import time
from datetime import UTC, datetime


def stamp():
    return datetime.now(UTC).isoformat()


class RunQueue:
    TELEGRAM_RESET_SECONDS = 7 * 24 * 60 * 60
    def __init__(self, path, *, limit=20):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.limit = limit
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS jobs(
                id INTEGER PRIMARY KEY AUTOINCREMENT, cause_key TEXT UNIQUE NOT NULL,
                trigger TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'queued',
                requested_at TEXT NOT NULL, started_at TEXT, finished_at TEXT,
                cycle_id TEXT, error_type TEXT
            );
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        """)
        with self.db:
            self.db.execute(
                "UPDATE jobs SET status='interrupted',finished_at=? WHERE status='running'",
                (stamp(),),
            )

    def close(self):
        self.db.close()

    def get(self, key, default=None):
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key, value):
        with self.db:
            self.db.execute(
                "INSERT INTO meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value)),
            )

    def request(self, trigger, cause_key):
        row = self.db.execute("SELECT id FROM jobs WHERE cause_key=?", (cause_key,)).fetchone()
        if row:
            return row[0], False
        if (
            self.db.execute("SELECT count(*) FROM jobs WHERE status='queued'").fetchone()[0]
            >= self.limit
        ):
            raise ValueError("Queue is full; wait for existing requests to finish")
        with self.db:
            row = self.db.execute(
                "INSERT INTO jobs(cause_key,trigger,requested_at) VALUES(?,?,?)",
                (cause_key, trigger, stamp()),
            )
        return row.lastrowid, True

    def claim(self):
        if self.get("paused", False):
            return None
        if self.db.execute("SELECT count(*) FROM jobs WHERE status='running'").fetchone()[0]:
            return None
        with self.db:
            # One worker, durable transitions; scheduled requests cannot starve manual ones.
            row = self.db.execute(
                "SELECT * FROM jobs WHERE status='queued' "
                "ORDER BY (trigger='manual') DESC,id LIMIT 1"
            ).fetchone()
            if row:
                self.db.execute(
                    "UPDATE jobs SET status='running',started_at=? WHERE id=? AND status='queued'",
                    (stamp(), row["id"]),
                )
        return dict(row) if row else None

    def complete(self, ident, *, cycle_id=None, error=None):
        with self.db:
            self.db.execute(
                "UPDATE jobs SET status=?,finished_at=?,cycle_id=?,error_type=? "
                "WHERE id=? AND status='running'",
                ("failed" if error else "done", stamp(), cycle_id, error, ident),
            )

    def tick(self, interval=3600, jitter=600, *, now=None, choose=random.uniform):
        now = time.time() if now is None else now
        due = self.get("next_due", now)
        if due > now or self.get("paused", False):
            return None
        pending = self.db.execute(
            "SELECT count(*) FROM jobs WHERE trigger='scheduled' AND status='queued'"
        ).fetchone()[0]
        queued = None
        if not pending:
            try:
                queued = self.request("scheduled", f"schedule:{due}")[0]
            except ValueError:
                pass  # A full manual queue must not crash the scheduler.
        # Never replay a burst of missed hourly jobs after sleep/restart.
        self.set("next_due", now + choose(interval - jitter, interval + jitter))
        return queued

    def telegram_offset(self, *, now=None):
        """Return a safe polling offset after Telegram's seven-day update-ID reset window."""
        checkpoint = self.get("telegram_checkpoint")
        if not checkpoint:
            return 0
        now = time.time() if now is None else now
        if now - checkpoint.get("updated_at", 0) >= self.TELEGRAM_RESET_SECONDS:
            return 0
        return checkpoint.get("offset", 0)

    def advance_telegram_offset(self, update_id, *, now=None):
        self.set(
            "telegram_checkpoint",
            {"offset": update_id + 1, "updated_at": time.time() if now is None else now},
        )

    def status(self):
        counts = dict(
            self.db.execute("SELECT status,count(*) FROM jobs GROUP BY status").fetchall()
        )
        active = self.db.execute(
            "SELECT id,trigger,started_at FROM jobs WHERE status='running' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return {
            "paused": self.get("paused", False),
            "jobs": counts,
            "active": dict(active) if active else None,
            "next_due": self.get("next_due"),
        }
