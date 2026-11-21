PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS listings (
    id TEXT PRIMARY KEY, url TEXT NOT NULL, title TEXT NOT NULL, payload TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'discovered'
        CHECK(status IN ('discovered','sending','sent','uncertain')),
    first_seen TEXT NOT NULL, last_seen TEXT NOT NULL, sent_at TEXT
);
CREATE TABLE IF NOT EXISTS cycles (
    id TEXT PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT, report TEXT
);
CREATE TABLE IF NOT EXISTS attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    listing_id TEXT NOT NULL REFERENCES listings(id),
    cycle_id TEXT NOT NULL REFERENCES cycles(id),
    text TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('sending','sent','uncertain')),
    created_at TEXT NOT NULL, finished_at TEXT, detail TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_attempt
    ON attempts(listing_id) WHERE status='sending';
CREATE TABLE IF NOT EXISTS previews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    listing_id TEXT NOT NULL REFERENCES listings(id),
    cycle_id TEXT NOT NULL REFERENCES cycles(id),
    text TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS control (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS resolutions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    listing_id TEXT NOT NULL REFERENCES listings(id),
    resolution TEXT NOT NULL, reason TEXT NOT NULL, created_at TEXT NOT NULL
);
PRAGMA user_version=1;

CREATE TABLE IF NOT EXISTS observations (
 id INTEGER PRIMARY KEY AUTOINCREMENT, listing_id TEXT NOT NULL REFERENCES listings(id),
 cycle_id TEXT REFERENCES cycles(id), observed_at TEXT NOT NULL, kind TEXT NOT NULL,
 fingerprint TEXT NOT NULL, patch TEXT NOT NULL, UNIQUE(listing_id,cycle_id)
);
CREATE TABLE IF NOT EXISTS listing_checks (
 listing_id TEXT PRIMARY KEY REFERENCES listings(id), outcome TEXT NOT NULL, reason TEXT NOT NULL,
 checked_at TEXT NOT NULL, next_check_at REAL NOT NULL, failures INTEGER NOT NULL, policy TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS detail_observations (
 id INTEGER PRIMARY KEY AUTOINCREMENT, listing_id TEXT NOT NULL REFERENCES listings(id),
 cycle_id TEXT REFERENCES cycles(id), observed_at TEXT NOT NULL, fingerprint TEXT NOT NULL,
 patch TEXT NOT NULL, payload TEXT NOT NULL
);
PRAGMA user_version=2;

CREATE TABLE IF NOT EXISTS listing_notifications (
 listing_id TEXT NOT NULL REFERENCES listings(id), destination TEXT NOT NULL,
 fingerprint TEXT NOT NULL, delivered_at TEXT NOT NULL,
 PRIMARY KEY (listing_id, destination, fingerprint)
);
PRAGMA user_version=3;
