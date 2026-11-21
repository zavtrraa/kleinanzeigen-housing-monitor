import hashlib
import json
import re
import time
from dataclasses import asdict
from datetime import UTC, datetime

from ..core.models import Listing


def utcnow():
    return datetime.now(UTC).isoformat()


def material(listing):
    value = asdict(listing)
    value.pop("profile", None)  # The same ad can occur in several city searches.
    return value


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def patch(before, after):
    return {
        key: {"before": before.get(key), "after": value}
        for key, value in after.items()
        if before.get(key) != value
    }


class ObservationStore:
    check_policy = "v0.7"

    def observe(self, listing, cycle_id=None):
        now = utcnow()
        old = self.db.execute("SELECT payload FROM listings WHERE id=?", (listing.id,)).fetchone()
        before = material(Listing(**json.loads(old[0]))) if old else {}
        after = material(listing)
        delta = patch(before, after)
        outcome = "new" if old is None else "changed" if delta else "unchanged"
        baseline = (
            old is not None
            and not self.db.execute(
                "SELECT 1 FROM observations WHERE listing_id=? LIMIT 1", (listing.id,)
            ).fetchone()
        )
        stored_patch = patch({}, after) if baseline else delta
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO listings(id,url,title,payload,first_seen,last_"
                "seen) VALUES(?,?,?,?,?,?)",
                (listing.id, listing.url, listing.title, json.dumps(asdict(listing)), now, now),
            )
            self.db.execute(
                "UPDATE listings SET url=?,title=?,payload=?,last_seen=? WHERE id=?",
                (
                    listing.url,
                    listing.title,
                    json.dumps(asdict(listing), ensure_ascii=False),
                    now,
                    listing.id,
                ),
            )
            self.db.execute(
                "INSERT OR IGNORE INTO observations(listing_id,cycle_id,observed_at,k"
                "ind,fingerprint,patch) VALUES(?,?,?,?,?,?)",
                (
                    listing.id,
                    cycle_id,
                    now,
                    "baseline" if baseline else outcome,
                    digest(after),
                    json.dumps(stored_patch, ensure_ascii=False),
                ),
            )
            if outcome == "changed":
                self.db.execute("DELETE FROM listing_checks WHERE listing_id=?", (listing.id,))
        return outcome

    def record_details(self, listing, fields):
        after = {key: re.sub(r"\s+", " ", value).strip() for key, value in fields.items()}
        old = self.db.execute(
            "SELECT payload FROM detail_observations WHERE listing_id=? ORDER BY id DESC LIMIT 1",
            (listing.id,),
        ).fetchone()
        before = json.loads(old[0]) if old else {}
        with self.db:
            self.db.execute(
                "INSERT INTO detail_observations(listing_id,cycle_id,observed_at,fing"
                "erprint,patch,payload) VALUES(?,?,?,?,?,?)",
                (
                    listing.id,
                    getattr(self, "active_cycle_id", None),
                    utcnow(),
                    digest(after),
                    json.dumps(patch(before, after), ensure_ascii=False),
                    json.dumps(after, ensure_ascii=False),
                ),
            )

    def record_check(self, listing_id, outcome, reason, *, seconds=21600):
        old = self.db.execute(
            "SELECT failures FROM listing_checks WHERE listing_id=?", (listing_id,)
        ).fetchone()
        failures = (old[0] if old else 0) + 1 if outcome == "prepare_error" else 0
        delay = min(seconds * 2 ** min(max(failures - 1, 0), 6), 21600) if failures else seconds
        with self.db:
            self.db.execute(
                "INSERT INTO listing_checks VALUES(?,?,?,?,?,?,?) ON CONFLICT(listing"
                "_id) DO UPDATE SET outcome=excluded.outcome,reason=excluded.reason,c"
                "hecked_at=excluded.checked_at,next_check_at=excluded.next_check_at,f"
                "ailures=excluded.failures,policy=excluded.policy",
                (
                    listing_id,
                    outcome,
                    reason,
                    utcnow(),
                    time.time() + delay,
                    failures,
                    self.check_policy,
                ),
            )

    def cached_check(self, listing_id):
        row = self.db.execute(
            "SELECT * FROM listing_checks WHERE listing_id=? AND next_check_at>? AND policy=?",
            (listing_id, time.time(), self.check_policy),
        ).fetchone()
        return dict(row) if row else None

    def history(self, listing_id, limit=20):
        rows = self.db.execute(
            "SELECT cycle_id,observed_at,kind,patch FROM observations WHERE listi"
            "ng_id=? ORDER BY id DESC LIMIT ?",
            (listing_id, limit),
        ).fetchall()
        return [{**dict(row), "patch": json.loads(row["patch"])} for row in rows]
