from dataclasses import replace

import pytest

from kleinanzeigen_bot.core.eligibility import ListingFilter
from kleinanzeigen_bot.core.exceptions import SelectorNotFound
from kleinanzeigen_bot.core.models import Listing
from kleinanzeigen_bot.core.monitor import Monitor
from kleinanzeigen_bot.runtime.preferences import SearchPreferences
from kleinanzeigen_bot.settings import Exclusions
from kleinanzeigen_bot.state.sqlite_repo import SQLiteRepository


class Source:
    def __init__(self):
        self.listings = [
            Listing(
                "1",
                "https://www.kleinanzeigen.de/s-anzeige/example/1-203-1",
                "Balcony apartment",
                "450",
                "2",
                "42",
                profile="hannover",
                description="Warmmiete 500 EUR",
            )
        ]

    async def search(self):
        return self.listings

    async def inspect(self, listing):
        return listing


class Notifications:
    def __init__(self):
        self.cards, self.reports, self.alerts = [], [], []
        self.fail = False

    async def listing(self, listing, decision):
        if self.fail:
            raise OSError("Simulated transport failure")
        self.cards.append((listing, decision))

    async def summary(self, report):
        self.reports.append(report)

    async def alert(self, message):
        self.alerts.append(message)


@pytest.fixture
def monitor(tmp_path):
    repo = SQLiteRepository(tmp_path / "state.sqlite3")
    source, notifications = Source(), Notifications()
    runner = Monitor(
        source,
        source,
        repo,
        notifications,
        SearchPreferences(),
        ListingFilter(Exclusions(max_warm_eur="520")),
        destination="demo",
    )
    yield runner, repo, source, notifications
    repo.close()


@pytest.mark.asyncio
async def test_monitor_notifies_without_preparing_a_reply_and_deduplicates(monitor):
    runner, repo, _, notifications = monitor
    first = await runner.run_cycle()
    second = await runner.run_cycle()
    assert first.found == 1
    assert first.matched == 1
    assert first.notified == 1
    assert first.monitoring
    assert second.notified == 0
    assert len(notifications.cards) == 1
    assert repo.db.execute("SELECT count(*) FROM attempts").fetchone()[0] == 0
    assert repo.db.execute("SELECT status FROM listings").fetchone()[0] == "discovered"


@pytest.mark.asyncio
async def test_failed_notification_remains_retryable(monitor):
    runner, _, _, notifications = monitor
    notifications.fail = True
    first = await runner.run_cycle()
    assert first.notified == 0
    assert first.errors
    notifications.fail = False
    second = await runner.run_cycle()
    assert second.notified == 1
    assert len(notifications.cards) == 1


@pytest.mark.asyncio
async def test_changed_listing_is_notified_again_but_sent_state_is_preserved(monitor):
    runner, repo, source, notifications = monitor
    await runner.run_cycle()
    with repo.db:
        repo.db.execute("UPDATE listings SET status='sent'")
    source.listings = [replace(source.listings[0], price_eur="400")]
    report = await runner.run_cycle()
    assert report.changed == 1
    assert report.notified == 1
    assert repo.db.execute("SELECT status FROM listings").fetchone()[0] == "sent"
    assert len(notifications.cards) == 2


@pytest.mark.asyncio
async def test_excluded_results_do_not_produce_cards_and_unknown_rent_is_explicit(monitor):
    runner, _, source, notifications = monitor
    source.listings = [replace(source.listings[0], size_sqm="10")]
    report = await runner.run_cycle()
    assert report.notified == 0
    assert report.items[0].status == "excluded"
    source.listings = [replace(source.listings[0], size_sqm="40", description="Ask for rent")]
    report = await runner.run_cycle()
    assert report.notified == 1
    assert notifications.cards[0][1].outcome == "review"


@pytest.mark.asyncio
async def test_monitor_honors_persisted_pause(monitor):
    runner, repo, _, notifications = monitor
    repo.pause("Manual review required")
    report = await runner.run_cycle()
    assert report.halted
    assert not report.search_completed
    assert not notifications.cards


@pytest.mark.asyncio
async def test_new_filters_are_checked_even_after_a_notification(monitor):
    runner, _, _, _ = monitor
    await runner.run_cycle()
    runner.preferences = SearchPreferences(min_area="80")
    report = await runner.run_cycle()
    assert report.items[0].status == "excluded"


@pytest.mark.asyncio
async def test_changed_description_is_not_hidden_by_unchanged_search_card(monitor):
    runner, _, source, _ = monitor
    await runner.run_cycle()

    class UpdatedDetails:
        async def inspect(self, listing):
            return replace(listing, description="Warmmiete 510 EUR")

    runner.inspector = UpdatedDetails()
    report = await runner.run_cycle()
    assert report.notified == 1


@pytest.mark.asyncio
async def test_expired_listing_does_not_halt_remaining_results(monitor):
    runner, repo, source, notifications = monitor
    source.listings.append(replace(source.listings[0], id="2", title="Second apartment"))

    class Inspector:
        async def inspect(self, listing):
            if listing.id == "1":
                raise SelectorNotFound("Listing HTTP status 404", stage="inspection")
            return listing

    runner.inspector = Inspector()
    report = await runner.run_cycle()
    assert not report.halted
    assert not repo.pause_reason()
    assert report.items[0].status == "inspection_failed"
    assert notifications.cards[0][0].id == "2"


@pytest.mark.asyncio
async def test_report_counts_manual_reviews_independently_of_notification_delivery(monitor):
    runner, _, source, notifications = monitor
    source.listings[0] = replace(source.listings[0], description="Ask for costs")
    source.listings.append(replace(source.listings[0], id="2", size_sqm="10"))
    notifications.fail = True
    report = await runner.run_cycle()
    assert report.review == 1
    assert report.notified == 0
