# Kleinanzeigen Housing Monitor

Local-first apartment search monitoring for Kleinanzeigen. It stores search criteria,
checks public listing details against explicit rules, sends Telegram cards for relevant
listings, and keeps reply delivery behind a separate opt-in workflow.

The project is designed for one local operator. SQLite, the browser profile and optional
Telegram credentials stay on that operator's machine.

## What it does

- Persists cities, search terms, maximum rent, minimum area and reply mode in SQLite.
- Searches the selected Hannover-area cities and validates the final listing details,
  including the configured warm-rent limit and exclusion rules.
- Sends a photo card when a safe source image is available; otherwise it sends a text card.
- Deduplicates unchanged listing notifications and retains a per-cycle report.
- Provides Telegram commands and controls for search, status, pause/resume, criteria and
  template preview.
- Defaults to monitoring. Reply preparation and submission are explicit, guarded paths;
  a potentially submitted seller message is never retried automatically.
- Includes a fully local demo with fictional data. It uses the real controls, preferences,
  matching, cards and SQLite flow, but it has no Telegram client and cannot contact sellers.

## Project tree

```text
kleinanzeigen-bot/
├── config/                 # Versioned selectors, filters and disabled optional sources
├── docs/                   # Design, plan, verification notes and demo evidence
├── scripts/verify_demo.py  # Local browser walkthrough and evidence capture
├── src/kleinanzeigen_bot/
│   ├── action/             # Explicit reply preparation and one-click confirmation
│   ├── core/               # Models, matching, browser and monitoring orchestration
│   ├── demo/               # Isolated local interface and synthetic fixtures
│   ├── housing/            # Optional additional source collectors
│   ├── ingestion/          # Kleinanzeigen search and detail extraction
│   ├── notifications/      # Telegram cards, summaries and pacing
│   ├── runtime/            # Queue, controls, preferences and worker lifecycle
│   └── state/              # SQLite observations and delivery receipts
└── tests/                  # Unit, integration and intercepted-browser tests
```

## Quick start

Use Python 3.11 or newer from this directory.

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m playwright install chromium
Copy-Item .env.example .env
```

The safe configuration is already the default:

```ini
DRY_RUN=true
AUTO_REPLY=false
```

Validate local configuration without opening a browser or contacting a site:

```powershell
.\.venv\Scripts\kleinbot.exe --check
```

## Local portfolio demo

Start the local interface:

```powershell
.\.venv\Scripts\kleinbot.exe --demo-ui
```

Open `http://127.0.0.1:8765`. The demo is intentionally local-only and contains synthetic
apartments. A useful walkthrough is: set locations and filters, run a search, inspect the
card and report, show the template, enable reply previews, then pause and resume.

To reproduce its browser evidence without external traffic:

```powershell
.\.venv\Scripts\python.exe scripts\verify_demo.py --output docs\demo --record
```

This writes PNG screenshots, a WebM recording when Playwright video recording is available,
and `verification.json`. It proves the isolated demonstration only; it does not attest to
live Kleinanzeigen selectors, Telegram delivery or seller-message delivery.

## Telegram controls

After setting `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` and
`TELEGRAM_ALLOWED_USER_IDS` locally, start the worker with `--watch`. The private control
chat accepts these commands:

```text
/city Hannover,Garbsen
/search Balkon
/budget 600
/area 30
/replies on
/settings
/template
/run
/status
/pause
/resume
/whoami
```

Settings are applied to the next queued run. A callback that Telegram has already expired is
acknowledged safely and cannot stop later commands. Telegram update checkpoints reset after a
week of inactivity so a documented Telegram update-ID reset cannot permanently skip controls.

## Auto-reply safety

`/replies on` enables the local reply workflow for the next run; it does not by itself permit
live sending. Live sending also requires `DRY_RUN=false`, valid local credentials and the
existing visible browser validation. The sender validates the configured query, city, area,
rent and exclusion rules before it opens a contact form. It submits exactly once and requires a
new success signal; an unclear result becomes a manual-review state.

## Tests and verification

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check src tests
```

Browser tests use intercepted local fixture pages rather than the live site. See
[docs/verification.md](docs/verification.md) for the current evidence, boundaries and known
live-verification limitations.

## Publication hygiene

The repository contains no runtime credentials, browser profile, SQLite state or Telegram
exports. Copy `.env.example` to a private `.env` only on the operator's machine, and run
`python scripts/verify_publication.py` before staging changes. The default configuration remains
monitoring-only (`DRY_RUN=true`, `AUTO_REPLY=false`).
