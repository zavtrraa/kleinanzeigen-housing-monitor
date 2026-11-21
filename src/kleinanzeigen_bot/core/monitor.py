import asyncio
from collections import Counter
from datetime import UTC, datetime
from uuid import uuid4

from ..state.observations import digest, material
from .exceptions import AuthError, ChallengeRequired, ListingUnavailable, SelectorNotFound
from .matching import match_listing
from .models import CycleReport, RunItem, SkippedListing


class Monitor:
    """Collect and notify without creating or preparing a seller message."""

    def __init__(
        self,
        search,
        inspector,
        repo,
        notifier,
        preferences,
        listing_filter,
        *,
        destination,
        trigger="once",
        request_id="",
        recovered=0,
    ):
        self.search, self.inspector, self.repo = search, inspector, repo
        self.notifier, self.preferences, self.listing_filter = notifier, preferences, listing_filter
        self.destination, self.trigger, self.request_id = destination, trigger, request_id
        self.recovered = recovered

    async def run_cycle(self):
        report = CycleReport(
            str(uuid4()),
            True,
            monitoring=True,
            trigger=self.trigger,
            request_id=self.request_id,
            recovered=self.recovered,
        )
        self.repo.begin_cycle(report.id)
        self.repo.check_policy = digest(
            {
                "preferences": self.preferences.model_dump(mode="json"),
                "rules": [
                    (name, [p.pattern for p in patterns])
                    for name, patterns in self.listing_filter.rules
                ],
            }
        )
        try:
            if reason := self.repo.pause_reason():
                report.halted = True
                report.errors.append("Paused: " + reason)
                return report
            listings = {item.id: item for item in await self.search.search()}
            report.found, report.search_completed = len(listings), True
            report.sources = list(getattr(self.search, "profile_stats", []))
            inspection_errors = Counter()
            notification_errors = Counter()
            for listing in listings.values():
                kind = self.repo.observe(listing, report.id)
                setattr(report, kind, getattr(report, kind) + 1)
                item = RunItem(listing.id, listing.title, listing.url, change_kind=kind)
                report.items.append(item)
                cached = self.repo.cached_check(listing.id)
                if cached and cached["outcome"] == "excluded":
                    item.status, item.note = "cached", cached["reason"]
                    report.cached += 1
                    continue
                try:
                    detailed = await self.inspector.inspect(listing)
                except (ListingUnavailable, SelectorNotFound) as exc:
                    item.status, item.note = "inspection_failed", str(exc)[:240]
                    report.inspection_failed += 1
                    inspection_errors[type(exc).__name__] += 1
                    self.repo.record_check(listing.id, "inspection_failed", item.note)
                    continue
                self.repo.record_details(
                    listing,
                    {
                        "description": detailed.description,
                        "costs": detailed.cost_details,
                    },
                )
                decision = match_listing(detailed, self.preferences, self.listing_filter)
                item.status, item.note = decision.outcome, decision.reason
                if decision.outcome == "excluded":
                    report.excluded += 1
                    self.repo.record_check(listing.id, decision.outcome, decision.reason)
                    report.skipped.append(
                        SkippedListing(listing.id, listing.url, decision.outcome, decision.reason)
                    )
                    continue
                if decision.outcome == "review":
                    report.review += 1
                    report.skipped.append(
                        SkippedListing(listing.id, listing.url, decision.outcome, decision.reason)
                    )
                report.matched += decision.outcome == "allowed"
                fingerprint = digest(material(detailed))
                if self.repo.notification_sent(listing.id, self.destination, fingerprint):
                    item.status = "already_notified"
                    continue
                try:
                    await self.notifier.listing(detailed, decision)
                except Exception as exc:
                    item.status = "notification_failed"
                    notification_errors[type(exc).__name__] += 1
                else:
                    self.repo.mark_notified(listing.id, self.destination, fingerprint)
                    report.notified += 1
                    if decision.outcome == "allowed":
                        item.status = "notified"
            report.errors.extend(
                f"{count} listing inspection(s) failed: {kind}"
                for kind, count in sorted(inspection_errors.items())
            )
            report.errors.extend(
                f"{count} listing notification(s) failed: {kind}"
                for kind, count in sorted(notification_errors.items())
            )
        except asyncio.CancelledError:
            report.halted = True
            report.errors.append("Monitoring cancelled before completion")
            raise
        except (AuthError, ChallengeRequired) as exc:
            report.halted = True
            self.repo.pause(type(exc).__name__)
            report.errors.append(type(exc).__name__)
            try:
                await self.notifier.alert("Manual browser action required: " + type(exc).__name__)
            except Exception:
                report.errors.append("Alert delivery failed")
        except Exception as exc:
            report.halted = True
            report.errors.append("Monitoring failed: " + type(exc).__name__)
        finally:
            report.finished_at = datetime.now(UTC).isoformat()
            self.repo.finish_cycle(report)
            try:
                await self.notifier.summary(report)
            except Exception:
                report.errors.append("Summary delivery failed; inspect local cycle report")
                self.repo.finish_cycle(report)
        return report
