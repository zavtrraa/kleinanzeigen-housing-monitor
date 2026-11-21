# Kleinanzeigen bot rebuild

## Purpose

Turn the existing personal housing assistant into a maintainable English-language project with a working, repeatable portfolio demonstration. Preserve the useful collection, queue, deduplication and delivery logic. The user explicitly requested both planning and implementation, with tests and demonstration evidence, in new project folders.

## Current implementation

The source is a Python 3.11+ src-layout package with Playwright, python-telegram-bot, Pydantic and SQLite. It has roughly fifty production modules, a persistent run queue, listing observations, eligibility rules, a message delivery state machine and additional housing sources. Its Telegram controls expose run/status/pause/resume only. The console demo returns two hard-coded listings and does not demonstrate filter configuration or listing cards. Most user-facing text is Russian. Deployment scripts and examples are tied to one workstation and personal search criteria.

The supplied Telegram snapshot shows historical failures finding the message input and confirming delivery. Those reports are evidence to investigate, not proof that the latest implementation still has each failure. Current code and fresh tests determine that.

## Approach

The implementation choice is an incremental rebuild in a separate clean folder. Retain useful existing modules and tests, repair defects, add the missing product paths and consolidate documentation. A complete rewrite would discard valuable delivery and recovery logic. Cosmetic changes alone cannot satisfy the demonstrated workflow.

No new queue service, frontend framework, distributed deployment or AI component is needed. SQLite remains sufficient for one local user and one worker. Extra public sources remain optional and are disabled in the example configuration.

## Requirements

- English code, documentation, controls, diagnostics and reports. German source text and the landlord message template remain German where that is functional.
- Authorized Telegram users can inspect and change search city, query, maximum rent, minimum area and reply mode. Validate changes and persist them across restart. Apply one consistent configuration snapshot per cycle; do not mutate a running cycle.
- Use the existing region catalog for Hannover, Laatzen, Langenhagen, Garbsen and Seelze. Advanced arbitrary search URLs remain available through configuration. Query and numeric values are encoded and validated.
- Local filters enforce configured rent and area as well as existing exclusion rules. Unknown values are explicitly marked for review. Changes must never reset sent or uncertain delivery records.
- Provide a main panel, search settings, reply template preview, listing cards (image when supplied by the source, price, area, rooms, source link), monitoring controls and a clear cycle report.
- Persist manual requests, avoid duplicate Telegram updates, serialize browser runs and retain pause/recovery behavior. Surface actual configured schedule intervals.
- Separate monitoring from automatic replies. New installations default to monitoring with no seller contact. A real auto-reply requires explicit configuration plus existing delivery safeguards; changing filters must not enable it.
- Keep one-click delivery, exact text validation, sent deduplication and uncertain-result handling. Never retry a possibly submitted message automatically.
- Provide an interactive local demo using the same command handlers, search preferences, filters, SQLite state and formatters as the Telegram integration. Clearly label fictional listings and local transport. It must work without Telegram credentials or contacting any real seller. It is not evidence of live delivery.
- Prove query -> filters -> run -> matching result, pause/resume, repeat-run deduplication and reply-template preview. Save screenshots and a short recording if the available capture tools support it. Include a fixture-browser test for message preparation and delivery confirmation.
- Keep original projects untouched and exclude secrets, private databases, logs and browser profiles from the new tree.

## Structure

```text
kleinanzeigen-bot/
  pyproject.toml
  .env.example
  .gitignore
  README.md
  config/
    search_profiles.yaml
    runtime.yaml
    exclusions.yaml
    housing.yaml
    message_templates/
  src/kleinanzeigen_bot/
    main.py
    settings.py
    scheduler.py
    core/
    ingestion/
    action/
    state/
    runtime/
      preferences.py
      controls.py
      panel.py
      queue.py
      daemon.py
      live_cycle.py
      events.py
      send_probe.py
    notifications/
      cards.py
      formatters.py
      telegram_bot.py
      pacing.py
    housing/
    observability/
    demo/
  tests/
    unit/
    integration/
    fixtures/
  docs/
    design.md
    plan.md
    progress.md
    verification.md
    demo/
```

Modules are retained only when they own a real responsibility. The complete file inventory and decisions will be recorded during the audit; the tree above groups unchanged internals for readability.

## Verification

Run the existing suite from the clean copy first. For each new behavior or reproduced defect, write a meaningful regression test before the change. Use real SQLite and local browser fixtures; mock only external transports. Run the complete suite, static checks and the interactive demo before claiming completion. Record precisely what is and is not verified live. Do not send real Telegram or seller messages as part of automated checks.
