# Kleinanzeigen Bot Implementation Plan

> **For agentic workers:** Use the executing-plans workflow to implement this plan task by task. Preserve the full user objective across continuations.

**Goal:** Complete and verify the existing housing bot's English monitoring, search configuration and demonstration workflows.

**Architecture:** Retain the existing Python package, SQLite state and transport boundaries. Add persisted search preferences shared by real Telegram controls and an isolated local demo. Keep browser delivery logic independently testable.

**Tech Stack:** Python 3.11+, Playwright, python-telegram-bot, Pydantic, SQLite, pytest, Ruff; standard-library HTTP for the local demo if sufficient.

**Spec:** [design.md](design.md)

## Global constraints

- Original sources are read-only inputs. No credentials, browser state or private data in the new tree.
- English project text, minimal dependencies and structure. German site selectors and reply copy are functional exceptions.
- No live messages during tests or demonstrations. No uncertain-delivery retries.
- One immutable preference snapshot per run. Filter changes do not reset delivery records.

## Review focus

1. Unauthorized or replayed Telegram updates must not alter preferences or duplicate jobs (task 2).
2. Empty, malformed, non-finite or out-of-range numeric filters must not corrupt persisted settings (task 2).
3. A listing failing local query/area/rent filters must never reach reply preparation (task 3).
4. Failed notification delivery must not mark a listing as successfully notified or roll back seller delivery (task 3).
5. Demo requests must stay isolated from real credentials, hosts and state, including repeated/concurrent commands (task 4).

## Task 1: Clean copy and baseline

- [x] Inventory every source/config/test/script file and record keep/change/remove decisions in `docs/verification.md`.
- [x] Copy source and tests; create sanitized examples and a neutral message template. Exclude runtime data, caches, virtualenvs and secrets.
- [x] Run `python -m pytest -q` in the new folder with its src path. Record failures before changing behavior.
- [x] Consolidate source documentation into an English README and focused docs. Keep independent runnable packaging.

## Task 2: Persistent settings and controls

Files: `runtime/preferences.py`, `runtime/controls.py`, `runtime/panel.py`, `runtime/daemon.py`, `tests/unit/test_preferences.py`, `tests/integration/test_search_controls.py`.

Interfaces: `SearchPreferences` validates city/query/max_rent/min_area/auto_reply; `PreferenceStore.load()` and `update(**changes)` use the existing run queue's durable metadata. `build_profiles(preferences)` yields existing `Profiles`. Control handlers accept optional preference services so old integrations remain testable.

- [x] Write and run failing tests for persistence, invalid values, city/query URL encoding and authorization/replay handling.
- [x] Implement validated preferences, configuration commands/buttons, current settings and template preview. New defaults monitor only.
- [x] Capture a preferences snapshot at worker claim, retain current cycle semantics and show actual schedule settings.
- [x] Run focused tests then the full suite; record results.

## Task 3: Matching, cards and reports

Files: `core/models.py`, `core/orchestrator.py`, `ingestion/extractors.py`, `ingestion/search.py`, `runtime/live_cycle.py`, `notifications/cards.py`, `notifications/telegram_bot.py`, `notifications/formatters.py`, relevant tests.

Interfaces: optional `Listing.image_url`; a matcher returning an explicit eligibility decision; notifier listing-card method with retry-safe durable notification identity. Existing `CycleReport` remains the shared report data.

- [x] Write failing tests for query/rent/area matching, unknown fields, safe image handling, card content, repeat-run behavior and notification failure recovery.
- [x] Implement extraction/matching/card delivery without disturbing sent/uncertain states or requiring reply-form access for monitoring.
- [x] Translate controls, reports and operational messages; update assertions that intentionally depend on translated product text.
- [x] Run full tests, formatter/linter and isolated browser send probe. Investigate any failures from root cause.

## Task 4: Interactive demo and evidence

Files: `demo/`, `main.py`, `tests/integration/test_demo.py`, `docs/demo/`, `README.md`.

Interfaces: local demo transport captures actual Telegram payloads; the demo drives shared commands, preference storage, matcher and report/card formatters. Synthetic source fixtures return clearly labeled fictional apartments and images; no credentials are loaded.

- [x] Write failing end-to-end tests for settings -> run -> cards/report, no duplicates on repeat, pause/resume and template preview.
- [x] Implement the interactive local demo and one documented launch command.
- [x] Inspect rendered screens and complete the entire demo workflow. Capture screenshots/recording using supported tools.
- [x] Run final tests and checks, inspect packaging for sensitive data and document live-verification limitations.
- [x] Obtain a fresh code review, fix material findings with regression coverage, then finalize tree and verification evidence.

The clinic project starts after this project's implementation and verification, retaining the same audit -> plan/tree -> rebuild -> demo -> tests sequence.
