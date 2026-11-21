import json
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path

from ..core.exceptions import StateConflict
from ..core.models import CycleReport, Listing
from .observations import ObservationStore


def utcnow() -> str:
    return datetime.now(UTC).isoformat()


class SQLiteRepository(ObservationStore):
    """Atomic transitions; recovery requires exclusive ownership of the process lock."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA busy_timeout=10000")
        self.db.execute("PRAGMA synchronous=FULL")
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1, 2, 3):
            self.db.close()
            raise StateConflict("Unsupported database schema version")
        if version in (1, 2):
            backup_dir = path.parent / "backups"
            backup_dir.mkdir(exist_ok=True)
            target = backup_dir / (
                "state-before-v3-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f") + ".sqlite3"
            )
            destination = sqlite3.connect(target)
            try:
                self.db.backup(destination)
            finally:
                destination.close()
        self.db.executescript(Path(__file__).with_name("schema.sql").read_text())

    def close(self) -> None:
        self.db.close()

    def notification_sent(self, listing_id: str, destination: str, fingerprint: str) -> bool:
        return (
            self.db.execute(
                "SELECT 1 FROM listing_notifications "
                "WHERE listing_id=? AND destination=? AND fingerprint=?",
                (listing_id, destination, fingerprint),
            ).fetchone()
            is not None
        )

    def mark_notified(self, listing_id: str, destination: str, fingerprint: str) -> None:
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO listing_notifications VALUES(?,?,?,?)",
                (listing_id, destination, fingerprint, utcnow()),
            )

    def begin_cycle(self, cycle_id: str) -> None:
        with self.db:
            self.db.execute("INSERT INTO cycles(id,started_at) VALUES(?,?)", (cycle_id, utcnow()))
        self.active_cycle_id = cycle_id

    def finish_cycle(self, report: CycleReport) -> None:
        with self.db:
            self.db.execute(
                "UPDATE cycles SET finished_at=?,report=? WHERE id=?",
                (utcnow(), json.dumps(report.to_dict(), ensure_ascii=False), report.id),
            )

    def discover(self, listing: Listing) -> bool:
        return self.observe(listing) == "new"

    def pending(self) -> list[Listing]:
        rows = self.db.execute(
            "SELECT l.payload FROM listings l LEFT JOIN listing_checks c ON c.listing_id=l.id "
            "WHERE l.status='discovered' AND (c.listing_id IS NULL OR c.next_chec"
            "k_at<=? OR c.policy!=?) "
            "ORDER BY l.first_seen,l.id",
            (time.time(), self.check_policy),
        ).fetchall()
        return [Listing(**json.loads(row[0])) for row in rows]

    def preview(self, listing_id: str, cycle_id: str, text: str) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO previews(listing_id,cycle_id,text,created_at) VALUES(?,?,?,?)",
                (listing_id, cycle_id, text, utcnow()),
            )

    def claim(self, listing_id: str, cycle_id: str, text: str) -> int | None:
        # Status and attempt commit together BEFORE the UI click.
        with self.db:
            cursor = self.db.execute(
                "UPDATE listings SET status='sending' WHERE id=? AND status='discovered'",
                (listing_id,),
            )
            if cursor.rowcount != 1:
                return None
            attempt = self.db.execute(
                "INSERT INTO attempts(listing_id,cycle_id,text,status,created_at)"
                " VALUES(?,?,?,'sending',?)",
                (listing_id, cycle_id, text, utcnow()),
            )
        return int(attempt.lastrowid)

    def complete(self, attempt_id: int, confirmed: bool, detail: str = "") -> None:
        status, now = ("sent" if confirmed else "uncertain"), utcnow()
        with self.db:
            row = self.db.execute(
                "SELECT listing_id FROM attempts WHERE id=? AND status='sending'", (attempt_id,)
            ).fetchone()
            if row is None:
                raise StateConflict("Attempt is no longer sending")
            changed = self.db.execute(
                "UPDATE listings SET status=?,sent_at=? WHERE id=? AND status='sending'",
                (status, now if confirmed else None, row[0]),
            )
            if changed.rowcount != 1:
                raise StateConflict("Listing is no longer sending")
            self.db.execute(
                "UPDATE attempts SET status=?,finished_at=?,detail=? WHERE id=?",
                (status, now, detail, attempt_id),
            )

    def recover_interrupted(self) -> int:
        with self.db:
            changed = self.db.execute(
                "UPDATE listings SET status='uncertain' WHERE status='sending'"
            ).rowcount
            self.db.execute(
                "UPDATE attempts SET status='uncertain',finished_at=?,detail=?"
                " WHERE status='sending'",
                (utcnow(), "Process stopped before confirmation"),
            )
        return changed

    def uncertain(self) -> list[dict]:
        return [
            dict(r)
            for r in self.db.execute(
                "SELECT id,url,title,status FROM listings "
                "WHERE status='uncertain' ORDER BY first_seen"
            ).fetchall()
        ]

    def resolve(self, listing_id: str, resolution: str, reason: str) -> None:
        if resolution not in ("sent", "discovered") or not reason.strip():
            raise ValueError("Resolution must be sent/discovered with an audit reason")
        with self.db:
            changed = self.db.execute(
                "UPDATE listings SET status=?,sent_at=? WHERE id=? AND status='uncertain'",
                (resolution, utcnow() if resolution == "sent" else None, listing_id),
            )
            if changed.rowcount != 1:
                raise StateConflict("Only uncertain listings can be manually resolved")
            self.db.execute(
                "INSERT INTO resolutions(listing_id,resolution,reason,created_at) VALUES(?,?,?,?)",
                (listing_id, resolution, reason.strip(), utcnow()),
            )

    def pause(self, reason: str) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO control(key,value) VALUES('paused',?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (reason,),
            )

    def pause_reason(self) -> str | None:
        row = self.db.execute("SELECT value FROM control WHERE key='paused'").fetchone()
        return row[0] if row else None

    def resume(self) -> None:
        with self.db:
            self.db.execute("DELETE FROM control WHERE key='paused'")
