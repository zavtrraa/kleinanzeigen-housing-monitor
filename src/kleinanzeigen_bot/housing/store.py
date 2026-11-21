import hashlib
import json
import sqlite3
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .models import Candidate, Decision


class HousingStore:
    """Independent DB so legacy sending states and pause are never rewritten."""

    TELEGRAM_RESET_SECONDS = 7 * 24 * 60 * 60

    def __init__(self, path: Path, *, recover: bool = False):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS candidates(
          key TEXT PRIMARY KEY, short_id TEXT UNIQUE NOT NULL, payload TEXT NOT NULL,
          decision TEXT NOT NULL, revision TEXT NOT NULL, workflow TEXT NOT NULL DEFAULT 'new',
          first_seen REAL NOT NULL, last_seen REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS outbox(
          id INTEGER PRIMARY KEY, event_key TEXT UNIQUE NOT NULL, payload TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
          next_attempt REAL NOT NULL DEFAULT 0, telegram_message_id INTEGER,
          last_error TEXT, created_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sources(
          name TEXT PRIMARY KEY, next_poll REAL NOT NULL DEFAULT 0,
          paused INTEGER NOT NULL DEFAULT 0, last_status TEXT, last_error TEXT,
          last_success REAL, details TEXT, checkpoint TEXT
        );
        CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """)
        # Delivery can be retried after a crash. Telegram has no idempotency key:
        # rare duplicate notifications after an ambiguous network failure are possible.
        if recover:
            with self.db:
                self.db.execute("UPDATE outbox SET status='pending' WHERE status='sending'")

    def close(self):
        self.db.close()

    def get_meta(self, key: str, default=None):
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_meta(self, key: str, value):
        with self.db:
            self.db.execute(
                "INSERT INTO meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value)),
            )

    def telegram_offset(self, *, now=None):
        checkpoint = self.get_meta("telegram_checkpoint")
        if not checkpoint:
            return 0
        now = time.time() if now is None else now
        if now - checkpoint.get("updated_at", 0) >= self.TELEGRAM_RESET_SECONDS:
            return 0
        return checkpoint.get("offset", 0)

    def advance_telegram_offset(self, update_id, *, now=None):
        self.set_meta(
            "telegram_checkpoint",
            {"offset": update_id + 1, "updated_at": time.time() if now is None else now},
        )

    def source(self, name: str) -> dict:
        row = self.db.execute("SELECT * FROM sources WHERE name=?", (name,)).fetchone()
        if row is None:
            return {"name": name, "next_poll": 0, "paused": 0, "checkpoint": None}
        result = dict(row)
        result["checkpoint"] = json.loads(result["checkpoint"]) if result["checkpoint"] else None
        return result

    def update_source(
        self,
        name: str,
        *,
        status: str,
        next_poll: float,
        paused: bool = False,
        error: str | None = None,
        details: dict | None = None,
        checkpoint=None,
    ):
        old = self.source(name)
        with self.db:
            self.db.execute(
                "INSERT INTO sources(name,next_poll,paused,last_status,last_error,last_success,"
                "details,checkpoint) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET "
                "next_poll=excluded.next_poll,paused=excluded.paused,last_status=excluded.last_status,"
                "last_error=excluded.last_error,last_success=excluded.last_success,"
                "details=excluded.details,checkpoint=excluded.checkpoint",
                (
                    name,
                    next_poll,
                    int(paused),
                    status,
                    error,
                    time.time() if status == "ok" else old.get("last_success"),
                    json.dumps(details or {}, ensure_ascii=False),
                    json.dumps(checkpoint if checkpoint is not None else old.get("checkpoint")),
                ),
            )

    def upsert(self, candidate: Candidate, decision: Decision, notify_review: bool = True) -> bool:
        payload = {"candidate": candidate.payload(), "decision": decision.payload()}
        serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        from ..core.eligibility import warm_amounts

        material = {"candidate": candidate.payload(), "decision": decision.payload()}
        material["candidate"].pop("description", None)
        for name in ("warm_eur", "size_sqm", "rooms"):
            value = material["candidate"].get(name)
            if value is not None:
                try:
                    parsed = Decimal(value)
                    if parsed.is_finite():
                        material["candidate"][name] = format(parsed.normalize(), "f")
                except InvalidOperation:
                    pass
        material["warm_values"] = sorted(
            format(value.normalize(), "f") for value in warm_amounts(candidate.description)
        )
        revision = hashlib.sha256(
            json.dumps(material, ensure_ascii=False, sort_keys=True).encode()
        ).hexdigest()
        now = time.time()
        with self.db:
            row = self.db.execute(
                "SELECT revision,workflow FROM candidates WHERE key=?", (candidate.key,)
            ).fetchone()
            changed = row is None or row["revision"] != revision
            self.db.execute(
                "INSERT INTO candidates(key,short_id,payload,decision,revision,"
                "first_seen,last_seen)"
                " VALUES(?,?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET payload=excluded.payload,"
                "decision=excluded.decision,revision=excluded.revision,"
                "last_seen=excluded.last_seen",
                (
                    candidate.key,
                    candidate.short_id,
                    json.dumps(candidate.payload(), ensure_ascii=False),
                    json.dumps(decision.payload(), ensure_ascii=False),
                    revision,
                    now,
                    now,
                ),
            )
            notify = decision.outcome == "eligible" or (
                notify_review and decision.outcome == "review"
            )
            if changed and notify and (row is None or row["workflow"] != "dismissed"):
                self.db.execute(
                    "INSERT OR IGNORE INTO outbox(event_key,payload,created_at) VALUES(?,?,?)",
                    (candidate.key + ":" + revision, serialized, now),
                )
        return changed

    def queue_alert(self, event_key: str, text: str):
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO outbox(event_key,payload,created_at) VALUES(?,?,?)",
                (event_key, json.dumps({"alert": text}, ensure_ascii=False), time.time()),
            )

    def claim_notification(self):
        with self.db:
            row = self.db.execute(
                "SELECT * FROM outbox WHERE status='pending' AND next_attempt<=? "
                "ORDER BY id LIMIT 1",
                (time.time(),),
            ).fetchone()
            if row is None:
                return None
            changed = self.db.execute(
                "UPDATE outbox SET status='sending',attempts=attempts+1 "
                "WHERE id=? AND status='pending'",
                (row["id"],),
            )
            if changed.rowcount != 1:
                return None
        result = dict(row)
        result["payload"] = json.loads(result["payload"])
        result["attempts"] += 1
        return result

    def delivered(self, event_id: int, message_id: int):
        with self.db:
            self.db.execute(
                "UPDATE outbox SET status='sent',telegram_message_id=?,last_error=NULL WHERE id=?",
                (message_id, event_id),
            )

    def delivery_failed(self, event: dict, error: str, delay: float, fatal: bool = False):
        terminal = fatal or event["attempts"] >= 5
        with self.db:
            self.db.execute(
                "UPDATE outbox SET status=?,last_error=?,next_attempt=? WHERE id=?",
                ("failed" if terminal else "pending", error, time.time() + delay, event["id"]),
            )

    def set_workflow(self, short_id: str, workflow: str) -> bool:
        if workflow not in {"new", "shortlisted", "contacted", "viewing", "dismissed"}:
            raise ValueError("Invalid workflow")
        with self.db:
            return (
                self.db.execute(
                    "UPDATE candidates SET workflow=? WHERE short_id=?", (workflow, short_id)
                ).rowcount
                == 1
            )

    def retry_failed_notifications(self) -> int:
        with self.db:
            return self.db.execute(
                "UPDATE outbox SET status='pending',attempts=0,next_attempt=0 WHERE status='failed'"
            ).rowcount

    def status(self) -> dict:
        return {
            "paused": self.get_meta("paused", False),
            "matches": {
                outcome: sum(
                    json.loads(row[0])["outcome"] == outcome
                    for row in self.db.execute("SELECT decision FROM candidates")
                )
                for outcome in ("eligible", "review", "excluded")
            },
            "candidates": {
                row[0]: row[1]
                for row in self.db.execute(
                    "SELECT workflow,count(*) FROM candidates GROUP BY workflow"
                )
            },
            "outbox": {
                row[0]: row[1]
                for row in self.db.execute("SELECT status,count(*) FROM outbox GROUP BY status")
            },
            "sources": [
                dict(row)
                for row in self.db.execute(
                    "SELECT name,paused,last_status,last_error,last_success "
                    "FROM sources ORDER BY name"
                )
            ],
        }
