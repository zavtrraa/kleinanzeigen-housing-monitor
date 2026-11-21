import asyncio
import logging
from datetime import UTC, datetime
from uuid import uuid4

from ..action.rate_limiter import CycleBudget
from ..state.repository import ListingRepository
from .exceptions import AuthError, ChallengeRequired, ListingFiltered, SelectorNotFound
from .models import CycleReport, Preview, RunItem, SkippedListing
from .ports import MessageSender, Notifier, SearchClient, TextBuilder

log = logging.getLogger(__name__)


class Orchestrator:
    def __init__(
        self,
        search: SearchClient,
        repo: ListingRepository,
        sender: MessageSender,
        texts: TextBuilder,
        notifier: Notifier,
        *,
        dry_run: bool = True,
        maximum: int = 1,
        pause_between=None,
        recovered: int = 0,
        progress=None,
        trigger="once",
        request_id="",
        events=None,
        started_at=None,
        fresh_first=False,
        excluded_recheck_seconds=21600,
        prepare_retry_seconds=3600,
        prepare_failure_limit=2,
    ):
        self.search, self.repo, self.sender = search, repo, sender
        self.texts, self.notifier = texts, notifier
        self.dry_run, self.maximum = dry_run, maximum
        self.pause_between, self.recovered = pause_between, recovered
        self.progress = progress
        self.trigger, self.request_id, self.events = trigger, request_id, events
        self.started_at = started_at
        self.fresh_first = fresh_first
        self.excluded_recheck_seconds = excluded_recheck_seconds
        self.prepare_retry_seconds = prepare_retry_seconds
        self.prepare_failure_limit = prepare_failure_limit

    def _progress(self, text: str, *, emit=True) -> None:
        if self.progress is not None:
            self.progress(text)
        if self.events is not None and emit:
            self.events.emit(text, "orchestrator.run_cycle", f"--{self.trigger}", "")

    async def _alert(self, report: CycleReport, message: str) -> None:
        try:
            await self.notifier.alert(message)
        except Exception:
            report.errors.append("Alert delivery failed; inspect local cycle report")
            log.error("alert_delivery_failed")

    async def run_cycle(self) -> CycleReport:
        report = CycleReport(
            str(uuid4()),
            self.dry_run,
            recovered=self.recovered,
            trigger=self.trigger,
            request_id=self.request_id,
        )
        if self.started_at:
            report.started_at = self.started_at
        current_item = None
        self.recovered = 0
        self.repo.begin_cycle(report.id)
        budget = CycleBudget(self.maximum)
        try:
            reason = self.repo.pause_reason()
            if reason:
                report.halted = True
                report.errors.append(f"Paused: {reason}")
                return report
            found = {listing.id: listing for listing in await self.search.search()}
            report.found = len(found)
            report.search_completed = True
            log.info("search_completed found=%s cycle=%s", report.found, report.id)
            observations = {}
            report.sources = list(getattr(self.search, "profile_stats", []))
            for listing in found.values():
                kind = self.repo.observe(listing, report.id)
                observations[listing.id] = kind
                if kind == "new":
                    report.new += 1
                elif kind == "changed":
                    report.changed += 1
                else:
                    report.unchanged += 1
            # Never send from a stale backlog: listing must still match current search.
            candidates = [listing for listing in self.repo.pending() if listing.id in found]
            if self.fresh_first:
                rank = {ident: index for index, ident in enumerate(found)}
                priority = {"new": 0, "changed": 1, "unchanged": 2}
                candidates.sort(key=lambda item: (priority[observations[item.id]], rank[item.id]))
            pending_ids = {listing.id for listing in candidates}
            report.items = [
                RunItem(
                    listing.id,
                    listing.title,
                    listing.url,
                    "not_checked" if listing.id in pending_ids else "already_processed",
                    change_kind=observations[listing.id],
                )
                for listing in found.values()
            ]
            by_id = {item.listing_id: item for item in report.items}
            for item in report.items:
                cached = self.repo.cached_check(item.listing_id)
                if item.listing_id not in pending_ids and cached:
                    item.status, item.note = "cached", cached["reason"]
                    report.cached += 1
                    if self.events:
                        self.events.emit(
                            f"Cached: #{item.listing_id}; no repeat inspection. {cached['reason']}",
                            "repository.cached_check",
                            f"--outcome={cached['outcome']}",
                            item.url,
                        )
            self._progress(
                f"Search complete: {len(found)} listings; "
                f"remaining to check: {len(candidates)}. Sending requires eligibility checks."
            )
            for index, listing in enumerate(candidates, 1):
                if not budget.available:
                    for item in report.items:
                        if item.status == "not_checked":
                            item.status, item.note = "deferred", "Reply limit for this run"
                    break
                current_item = by_id[listing.id]
                if listing.id not in found:
                    continue
                if budget.used and self.pause_between is not None:
                    await self.pause_between()
                self._progress(
                    f"\n[{index}/{len(candidates)}] Checking: {listing.title}\n{listing.url}"
                )
                text = self.texts.render(listing)
                try:
                    prepared = await self.sender.prepare(listing, text)
                except ListingFiltered as exc:
                    log.info("listing_skipped id=%s reason=%s", listing.id, exc.reason)
                    self._progress(f"Skipped: {exc.reason}. No message sent.", emit=False)
                    if self.events:
                        self.events.emit(
                            f"#{listing.id}: {exc.reason}. No message sent.",
                            "eligibility.check",
                            f"--outcome={exc.outcome}",
                            listing.url,
                        )
                    current_item.status, current_item.note = exc.outcome, exc.reason
                    self.repo.record_check(
                        listing.id, exc.outcome, exc.reason, seconds=self.excluded_recheck_seconds
                    )
                    report.skipped.append(
                        SkippedListing(listing.id, listing.url, exc.outcome, exc.reason)
                    )
                    continue
                except SelectorNotFound as exc:
                    current_item.status, current_item.note = "prepare_failed", f"{exc.stage}: {exc}"
                    report.preparation_failed += 1
                    if self.events:
                        self.events.emit(
                            f"#{listing.id}: form unavailable: {current_item.note}",
                            "sender.prepare",
                            "--prepare-failed --no-send",
                            listing.url,
                        )
                    self.repo.record_check(
                        listing.id,
                        "prepare_error",
                        current_item.note,
                        seconds=self.prepare_retry_seconds,
                    )
                    if exc.diagnostic_path and exc.diagnostic_path not in report.diagnostics:
                        report.diagnostics.append(exc.diagnostic_path)
                    self._progress("Form unavailable; listing deferred, checking the next one.")
                    if report.preparation_failed >= self.prepare_failure_limit:
                        raise
                    continue
                current_item.warnings = list(getattr(prepared, "warnings", []))
                try:
                    if self.dry_run:
                        self.repo.preview(listing.id, report.id, text)
                        report.previews.append(Preview(listing.id, listing.url, text))
                        current_item.status = "preview"
                        budget.reserve()
                        continue
                    attempt = self.repo.claim(listing.id, report.id, text)
                    if attempt is None:
                        continue
                    budget.reserve()
                    self._progress("Eligibility and form checked. Submitting one reply.")
                    try:
                        await prepared.submit()
                    except BaseException as exc:
                        # Cancellation is also ambiguous after the durable claim.
                        self.repo.complete(attempt, False, type(exc).__name__)
                        if getattr(exc, "diagnostic_path", None):
                            report.diagnostics.append(exc.diagnostic_path)
                        report.uncertain += 1
                        current_item.status = "uncertain"
                        report.halted = True
                        self.repo.pause(f"Ambiguous send for listing {listing.id}")
                        await self._alert(report, f"uncertain: {listing.url}; check conversation")
                        raise
                    self.repo.complete(attempt, True)
                    report.sent += 1
                    current_item.status = "sent"
                    log.info("message_confirmed id=%s cycle=%s", listing.id, report.id)
                    self._progress("Site confirmed delivery. Reply sent.")
                finally:
                    await prepared.close()
        except asyncio.CancelledError:
            if current_item and current_item.status == "not_checked":
                current_item.status = "cancelled"
                current_item.note = "Interrupted before inspection completed"
            report.halted = True
            report.errors.append("Run interrupted before completion.")
            raise
        except (ChallengeRequired, AuthError) as exc:
            if current_item and current_item.status == "not_checked":
                current_item.status, current_item.note = "blocked", type(exc).__name__
            self.repo.pause(type(exc).__name__)
            report.halted = True
            report.errors.append(type(exc).__name__)
            await self._alert(report, f"{type(exc).__name__}: manual browser action required")
        except SelectorNotFound as exc:
            report.halted = True
            detail = f"SelectorNotFound[{exc.stage}]: {exc}"
            report.errors.append(detail)
            if exc.diagnostic_path and exc.diagnostic_path not in report.diagnostics:
                report.diagnostics.append(exc.diagnostic_path)
            log.error(
                "cycle_selector_failed stage=%s reason=%s cycle=%s", exc.stage, str(exc), report.id
            )
            await self._alert(
                report,
                detail
                + (
                    "\nScreenshot and HTML saved locally in data/diagnostics."
                    if exc.diagnostic_path
                    else ""
                ),
            )
        except Exception as exc:
            if current_item and current_item.status == "not_checked":
                current_item.status, current_item.note = "error", type(exc).__name__
            report.halted = True
            # Do not dump browser/HTTP exception strings which may contain credentials.
            report.errors.append(type(exc).__name__)
            log.error("cycle_failed type=%s cycle=%s", type(exc).__name__, report.id)
            if not report.uncertain:
                await self._alert(report, f"Cycle failed: {type(exc).__name__}")
        finally:
            report.finished_at = datetime.now(UTC).isoformat()
            # Telegram availability never gates durable state.
            self.repo.finish_cycle(report)
            try:
                await self.notifier.summary(report)
            except Exception:
                report.errors.append("Summary delivery failed; inspect local cycle report")
                self.repo.finish_cycle(report)
                log.error("summary_delivery_failed cycle=%s", report.id)
        return report
