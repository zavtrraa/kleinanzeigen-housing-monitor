# Verification record

## Scope and source handling

The original bot was retained as a read-only input. This rebuilt tree excludes `.env` files,
SQLite state, browser profiles, diagnostics, logs, virtual environments and private message
exports. The file-by-file keep/change/consolidate decisions are in
`source-inventory.json`.

The rebuilt application keeps the existing Python package boundaries where they represent a
real operational responsibility. It adds an isolated demo, durable preferences, a monitoring
path that never opens a seller form, and English documentation/controls. No web service,
database server or AI component was added.

## Automated checks

Run from the project root after installing the development dependencies:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check src tests scripts
.\.venv\Scripts\kleinbot.exe --check
.\.venv\Scripts\python.exe scripts\verify_publication.py
```

The verification set covers:

- preference validation, restart persistence, replay protection and Telegram's delayed
  update-ID reset window;
- filtering by city, query, area, card rent and confirmed warm rent;
- no reply-form opening for rejected criteria, and single-click send confirmation rules;
- monitoring card delivery, retryability, deduplication, manual-review counts and expired
  listing isolation;
- queue serialization, pause/recovery behavior and local-only demo workflow;
- card formatting, report chunk limits and safe source-image handling.

Final verification on 21 November 2026:

```text
pytest -q
263 passed, 2 skipped, 4 warnings, 84 subtests passed in 76.60s

ruff check src tests scripts
All checks passed!

kleinbot --check
Configuration valid. Mode: MONITORING; form validation: per_listing.

python scripts/verify_publication.py
Publication boundary verified.
```

The four warnings are an upstream `python-telegram-bot` deprecation notice about the future
type of `RetryAfter.retry_after`; they come from the compatibility test for persisted rate-limit
handling and do not fail the suite.

The browser walkthrough completed the settings → search → cards → status → template → repeat
→ pause → resume flow with no browser errors, no external requests and zero seller attempts.
Its evidence is under `docs/demo/`: four desktop PNGs, a mobile PNG,
`housing_monitor_demo.webm` and `verification.json`.

## Demonstration boundary

`--demo-ui` runs only on `127.0.0.1` with fictional listings and a local transport. It can
prove the interface and the shared application workflow, including reply preview generation. It
cannot prove current production selectors, a logged-in browser, Telegram availability or a
seller-message result. Those actions require the operator's local credentials and a deliberate
manual run.

Live site changes, login challenges, rate limits and access blocks remain operational conditions
that the bot detects and pauses for rather than trying to evade.

## Publication safety

The tracked tree deliberately excludes `.env` files, SQLite state, browser profiles, runtime
logs, diagnostics and virtual environments. `.env.example` contains placeholders only. Before
staging changes, run `python scripts/verify_publication.py`; it confirms the required ignored
paths and the hardened `filelock` dependency range.
