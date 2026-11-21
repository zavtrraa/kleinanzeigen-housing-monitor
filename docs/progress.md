# Implementation progress

Plan: `docs/plan.md`.

- Goal created for both projects; Kleinanzeigen is first.
- Original bot and clinic source paths verified. Working folder was empty except for Git metadata.
- Reviewed bot topology, settings, controls/panel, daemon, models, extraction, notifier, eligibility and current console demo.
- Confirmed missing interactive search configuration and incomplete demonstration path.
- Ruling: proceed with the requested plan-and-implementation sequence without another design approval round; the user already authorized rebuilding both projects and delegated implementation choices.
- Ruling: preserve optional additional housing sources, disabled by default in examples, because they are existing functionality; no new sources are being added.
- Ruling: use an explicitly labeled local Telegram transport for reproducible public demo evidence; this does not prove live Telegram or seller delivery.
- Clean copy prepared; original projects remain untouched. Inventory: `source-inventory.json`.
- Baseline verified outside the Windows sandbox: 201 tests and 84 subtests passed in 71.62 seconds; four existing Telegram timedelta deprecation warnings. The sandbox blocked asyncio socketpair before test execution; no product workaround was added for that environment restriction.
- Added immutable validated preferences, atomic persistence/update watermark, English controls, photo cards/text fallback, public detail inspection, local matching and a monitor that creates no seller attempts.
- New test groups were observed failing before implementation. Latest focused verification: 22 integration/preference/control tests passed; seven monitor tests passed after fixing deduplication order; five card/browser-inspection tests passed.
- Ruling: a separate monitoring service is clearer than forcing notification-only behavior through message preparation. Existing sender orchestration remains for explicit auto replies, with shared filters and data models.
- Ruling: schema v3 adds notification receipts and backs up existing v1/v2 databases before migration. Tests verify preserved delivery state.
- Connected a preference snapshot to each daemon job and real cycle. Read-only preference access does not invoke queue recovery or interrupt a worker.
- Original example personal message replaced with neutral German copy. Additional sources disabled by default. Playwright is the default browser dependency; optional Patchright remains supported.
- Completed bot verification: 265 tests and 84 subtests passed; Ruff passed; offline CLI
  configuration validation passed. The expected upstream Telegram compatibility warning is
  documented in `verification.md`.
- Completed local browser walkthrough: criteria, cards, status, template, reply preview,
  deduplication and pause/resume. It made no external request and created zero seller attempts.
  PNGs, a mobile capture, WebM recording and machine-readable evidence are in `docs/demo/`.
- Stop point: the Kleinanzeigen project is complete. Do not begin the clinic project until the
  user explicitly confirms after reviewing this handoff.
