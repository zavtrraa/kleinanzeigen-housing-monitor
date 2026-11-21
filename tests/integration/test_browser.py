"""Real Chromium, entirely intercepted fixture responses: no live-site traffic."""

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from playwright.async_api import async_playwright

from kleinanzeigen_bot.action.sender import PlaywrightMessageSender
from kleinanzeigen_bot.core.exceptions import ChallengeRequired, SelectorNotFound, SendUncertain
from kleinanzeigen_bot.core.models import Listing
from kleinanzeigen_bot.ingestion.search import PlaywrightSearchClient
from kleinanzeigen_bot.settings import Profiles, Runtime, load_yaml

ROOT = Path(__file__).resolve().parents[2]
URL = "https://www.kleinanzeigen.de/s-anzeige/helle-wohnung/123456-203-1"


class BrowserTests(unittest.IsolatedAsyncioTestCase):
    async def test_inspection_reads_details_without_a_contact_form(self):
        from kleinanzeigen_bot.ingestion.details import ListingInspector

        self.html = (
            '<html><body><div id="viewad-description-text">'
            "Quiet balcony apartment. Warmmiete 500 EUR.</div>"
            '<div id="viewad-details">42 m²</div></body></html>'
        )
        inspector = ListingInspector(self.context, self.runtime)
        detailed = await inspector.inspect(self.listing)
        self.assertIn("Warmmiete 500 EUR", detailed.description)
        self.assertEqual(detailed.cost_details, "42 m²")
        self.assertEqual(self.context.pages, [])

    async def asyncSetUp(self):
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(headless=True)
        self.context = await self.browser.new_context()
        self.runtime = load_yaml(ROOT / "config/runtime.yaml", Runtime)
        self.runtime.timeout_ms = 500
        self.runtime.confirmation_timeout_ms = 300
        self.html = (ROOT / "tests/fixtures/listing.html").read_text(encoding="utf-8")

        async def intercept(route):
            await route.fulfill(status=200, content_type="text/html; charset=utf-8", body=self.html)

        await self.context.route("**/*", intercept)
        self.sender = PlaywrightMessageSender(self.context, self.runtime)
        self.listing = Listing("123456", URL, "Helle Wohnung")

    async def asyncTearDown(self):
        await self.context.close()
        await self.browser.close()
        await self.playwright.stop()

    async def test_prepare_does_not_click_submit(self):
        prepared = await self.sender.prepare(self.listing, "Guten Tag!\nViele Grüße")
        self.assertEqual(
            await prepared.page.locator("textarea").input_value(), "Guten Tag!\nViele Grüße"
        )
        self.assertIsNone(await prepared.page.evaluate("window.submissions"))
        await prepared.close()

    async def test_submit_requires_new_confirmation_and_only_clicks_once(self):
        prepared = await self.sender.prepare(self.listing, "Test")
        await prepared.submit()
        self.assertEqual(await prepared.page.evaluate("window.submissions"), 1)
        with self.assertRaises(SendUncertain):
            await prepared.submit()
        self.assertEqual(await prepared.page.evaluate("window.submissions"), 1)
        await prepared.close()

    async def test_no_confirmation_is_uncertain_without_second_click(self):
        self.html = self.html.replace("document.getElementById('success').hidden=false", "void 0")
        prepared = await self.sender.prepare(self.listing, "Test")
        with self.assertRaises(SendUncertain):
            await prepared.submit()
        self.assertEqual(await prepared.page.evaluate("window.submissions"), 1)
        await prepared.close()

    async def test_stale_confirmation_blocks_preparation(self):
        self.html = self.html.replace('id="success" hidden', 'id="success"')
        with self.assertRaises(SelectorNotFound):
            await self.sender.prepare(self.listing, "Test")

    async def test_ip_range_block_is_detected_from_visible_heading(self):
        from kleinanzeigen_bot.core.exceptions import IPRangeBlocked

        self.html = (
            "<html><title>Kleinanzeigen</title><body>"
            "<h1>IP-Bereich vorübergehend gesperrt.</h1>"
            "<p>Bitte versuche es später erneut.</p></body></html>"
        )
        with self.assertRaises(IPRangeBlocked):
            await self.sender.prepare(self.listing, "Test")
        self.assertEqual(self.context.pages, [])

    async def test_ip_range_block_is_detected_from_page_title(self):
        from kleinanzeigen_bot.core.exceptions import IPRangeBlocked

        self.html = "<html><title>IP-Bereich gesperrt</title><body>Blocked</body></html>"
        with self.assertRaises(IPRangeBlocked):
            await self.sender.prepare(self.listing, "Test")

    async def test_challenge_stops_before_form(self):
        self.html = "<html><title>Just a moment</title><body>Challenge</body></html>"
        with self.assertRaises(ChallengeRequired):
            await self.sender.prepare(self.listing, "Test")

    async def test_ambiguous_submit_selector_refuses_to_send(self):
        self.html = self.html.replace("</section>", "<button>Nachricht senden</button></section>")
        with self.assertRaises(SelectorNotFound):
            await self.sender.prepare(self.listing, "Test")

    async def test_search_extracts_fixture_card(self):
        self.html = (ROOT / "tests/fixtures/search.html").read_text(encoding="utf-8")
        profiles = Profiles(
            profiles=[
                {
                    "name": "test",
                    "url": "https://www.kleinanzeigen.de/s-wohnung-mieten/c203",
                }
            ]
        )
        listings = await PlaywrightSearchClient(self.context, profiles, self.runtime).search()
        self.assertEqual(len(listings), 1)
        self.assertEqual(listings[0].id, "123456")
        self.assertEqual(listings[0].price_eur, "1250.50")

    async def test_current_react_search_cards_are_extracted(self):
        self.html = (ROOT / "tests/fixtures/search_current.html").read_text(encoding="utf-8")
        profiles = Profiles(
            profiles=[
                {
                    "name": "test",
                    "url": "https://www.kleinanzeigen.de/s-wohnung-mieten/c203",
                }
            ]
        )
        listings = await PlaywrightSearchClient(self.context, profiles, self.runtime).search()
        self.assertEqual([item.id for item in listings], ["3553580304", "3548645595"])
        self.assertEqual([item.price_eur for item in listings], ["446", "475"])
        self.assertEqual(listings[0].rooms, "2.5")
        self.assertEqual(listings[0].size_sqm, "49.80")
        self.assertIsNone(listings[0].published_at)

    async def test_unknown_search_markup_does_not_look_like_empty_results(self):
        self.html = "<html><title>Search</title><body>DOM changed</body></html>"
        profiles = Profiles(
            profiles=[
                {
                    "name": "test",
                    "url": "https://www.kleinanzeigen.de/s-wohnung-mieten/c203",
                }
            ]
        )
        with self.assertRaises(SelectorNotFound):
            await PlaywrightSearchClient(self.context, profiles, self.runtime).search()

    async def test_filter_rejects_before_opening_form(self):
        from kleinanzeigen_bot.core.eligibility import ListingFilter
        from kleinanzeigen_bot.core.exceptions import ListingFiltered
        from kleinanzeigen_bot.settings import Exclusions

        config = load_yaml(ROOT / "config/exclusions.yaml", Exclusions)
        sender = PlaywrightMessageSender(self.context, self.runtime, ListingFilter(config))
        opened = []
        await self.context.expose_function("recordOpen", lambda: opened.append(True))
        self.html = self.html.replace(
            'onclick="document', 'onclick="window.recordOpen();document', 1
        )
        self.html = self.html.replace("Warmmiete: 500 €.", "Nur an Frauen. Warmmiete: 500 €.")
        with self.assertRaises(ListingFiltered) as result:
            await sender.prepare(self.listing, "Test")
        self.assertEqual(result.exception.outcome, "excluded")
        self.assertEqual(opened, [])
        self.assertEqual(self.context.pages, [])

    async def test_unknown_warm_total_goes_to_review_without_submit(self):
        from kleinanzeigen_bot.core.eligibility import ListingFilter
        from kleinanzeigen_bot.core.exceptions import ListingFiltered
        from kleinanzeigen_bot.settings import Exclusions

        config = load_yaml(ROOT / "config/exclusions.yaml", Exclusions)
        sender = PlaywrightMessageSender(self.context, self.runtime, ListingFilter(config))
        self.html = self.html.replace("Warmmiete: 500 €.", "Kaltmiete: 450 €.")
        with self.assertRaises(ListingFiltered) as result:
            await sender.prepare(self.listing, "Test")
        self.assertEqual(result.exception.outcome, "review")
        self.assertEqual(self.context.pages, [])

    async def test_eligible_total_allows_dry_preparation(self):
        from kleinanzeigen_bot.core.eligibility import ListingFilter
        from kleinanzeigen_bot.settings import Exclusions

        config = load_yaml(ROOT / "config/exclusions.yaml", Exclusions)
        sender = PlaywrightMessageSender(self.context, self.runtime, ListingFilter(config))
        prepared = await sender.prepare(self.listing, "Test")
        self.assertIsNone(await prepared.page.evaluate("window.submissions"))
        await prepared.close()

    async def test_saved_query_and_area_rules_block_the_contact_form(self):
        from kleinanzeigen_bot.core.eligibility import ListingFilter
        from kleinanzeigen_bot.core.exceptions import ListingFiltered
        from kleinanzeigen_bot.runtime.preferences import SearchPreferences
        from kleinanzeigen_bot.settings import Exclusions

        opened = []
        await self.context.expose_function("recordOpen", lambda: opened.append(True))
        self.html = self.html.replace(
            'onclick="document.getElementById(\'compose\')',
            'onclick="window.recordOpen();document.getElementById(\'compose\')',
        )
        for preferences in (
            SearchPreferences(query="garden", min_area="20", max_rent="600"),
            SearchPreferences(query="balcony", min_area="50", max_rent="600"),
        ):
            sender = PlaywrightMessageSender(
                self.context,
                self.runtime,
                ListingFilter(Exclusions(max_warm_eur="600")),
                preferences=preferences,
            )
            with self.assertRaises(ListingFiltered) as result:
                await sender.prepare(replace(self.listing, size_sqm="42"), "Test")
            self.assertEqual(result.exception.outcome, "excluded")
            self.assertEqual(self.context.pages, [])
        self.assertEqual(opened, [])

    async def test_missing_description_fails_closed_with_filters_enabled(self):
        from kleinanzeigen_bot.core.eligibility import ListingFilter
        from kleinanzeigen_bot.settings import Exclusions

        config = load_yaml(ROOT / "config/exclusions.yaml", Exclusions)
        sender = PlaywrightMessageSender(self.context, self.runtime, ListingFilter(config))
        self.html = self.html.replace('id="viewad-description-text"', 'id="unknown"')
        with self.assertRaises(SelectorNotFound):
            await sender.prepare(self.listing, "Test")

    async def test_persistent_profile_lock(self):
        from kleinanzeigen_bot.core.browser_factory import BrowserFactory
        from kleinanzeigen_bot.core.exceptions import ProfileLocked

        with tempfile.TemporaryDirectory() as directory:
            factory = BrowserFactory(
                Path(directory) / "profile", headless=True, chromium_sandbox=False
            )
            async with factory.open():
                with self.assertRaises(ProfileLocked):
                    async with factory.open():
                        self.fail("A second context must not own the same profile")

    async def test_filtered_cards_each_wait_before_navigation(self):
        from unittest.mock import AsyncMock

        from kleinanzeigen_bot.core.eligibility import ListingFilter
        from kleinanzeigen_bot.core.exceptions import ListingFiltered
        from kleinanzeigen_bot.settings import Exclusions

        config = load_yaml(ROOT / "config/exclusions.yaml", Exclusions)
        events = []

        async def pause():
            events.append("pause")

        pause_mock = AsyncMock(side_effect=pause)
        sender = PlaywrightMessageSender(
            self.context, self.runtime, ListingFilter(config), pause_before_listing=pause_mock
        )
        original = self.html
        self.html = original.replace("Warmmiete: 500 €.", "Anfragestopp. Warmmiete: 500 €.")
        for _ in range(2):
            with self.assertRaises(ListingFiltered) as result:
                await sender.prepare(self.listing, "Test")
            self.assertEqual(result.exception.outcome, "excluded")
            self.assertEqual(self.context.pages, [])
        self.assertEqual(events, ["pause", "pause"])
        self.assertEqual(pause_mock.await_count, 2)
        self.html = original
        prepared = await sender.prepare(self.listing, "Test")
        self.assertEqual(pause_mock.await_count, 3)
        self.assertIsNone(await prepared.page.evaluate("window.submissions"))
        await prepared.close()

    async def test_disabled_submit_fails_without_sending(self):
        self.html = self.html.replace(
            '<button onclick="window.submissions', '<button disabled onclick="window.submissions'
        )
        with self.assertRaisesRegex(SelectorNotFound, "disabled"):
            await self.sender.prepare(self.listing, "Test")
        self.assertEqual(self.context.pages, [])

    async def test_message_field_mutation_is_detected(self):
        self.html = self.html.replace(
            'name="message"', 'name="message" oninput="this.value=String.fromCharCode(120)"'
        )
        with self.assertRaisesRegex(SelectorNotFound, "differs"):
            await self.sender.prepare(self.listing, "Exact text")
        self.assertEqual(self.context.pages, [])

    async def test_explicit_empty_search_is_successful(self):
        self.html = "<html><title>Search</title><body>Keine Anzeigen gefunden</body></html>"
        profiles = Profiles(
            profiles=[{"name": "test", "url": "https://www.kleinanzeigen.de/s-wohnung-mieten/c203"}]
        )
        results = await PlaywrightSearchClient(self.context, profiles, self.runtime).search()
        self.assertEqual(results, [])
        self.assertEqual(self.context.pages, [])

    async def test_search_http_failure_is_not_empty_and_captures_diagnostics(self):
        await self.context.unroute("**/*")

        async def unavailable(route):
            await route.fulfill(status=503, content_type="text/html", body="Unavailable")

        await self.context.route("**/*", unavailable)
        profiles = Profiles(
            profiles=[{"name": "test", "url": "https://www.kleinanzeigen.de/s-wohnung-mieten/c203"}]
        )
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(SelectorNotFound) as result:
                await PlaywrightSearchClient(
                    self.context, profiles, self.runtime, Path(directory)
                ).search()
            self.assertEqual(result.exception.stage, "search")
            self.assertIn("503", str(result.exception))
            self.assertTrue((Path(result.exception.diagnostic_path) / "metadata.json").is_file())
        self.assertEqual(self.context.pages, [])

    async def test_mutated_text_after_preparation_never_submits(self):
        prepared = await self.sender.prepare(self.listing, "Exact text")
        await prepared.page.locator("textarea").fill("Changed text")
        with self.assertRaisesRegex(SendUncertain, "Message changed"):
            await prepared.submit()
        self.assertIsNone(await prepared.page.evaluate("window.submissions"))
        with self.assertRaises(SendUncertain):
            await prepared.submit()
        await prepared.close()

    async def test_disabled_button_after_preparation_never_submits(self):
        prepared = await self.sender.prepare(self.listing, "Exact text")
        await prepared.submit_button.evaluate("button => button.disabled = true")
        with self.assertRaisesRegex(SendUncertain, "disabled"):
            await prepared.submit()
        self.assertIsNone(await prepared.page.evaluate("window.submissions"))
        await prepared.close()

    async def test_changed_listing_after_preparation_never_submits(self):
        prepared = await self.sender.prepare(self.listing, "Exact text")
        await prepared.page.goto("https://www.kleinanzeigen.de/s-anzeige/other/654321-203-1")
        with self.assertRaisesRegex(SendUncertain, "Listing changed"):
            await prepared.submit()
        self.assertIsNone(await prepared.page.evaluate("window.submissions"))
        await prepared.close()

    async def test_explicit_unknown_warm_policy_allows_reply_with_warning(self):
        from kleinanzeigen_bot.core.eligibility import ListingFilter
        from kleinanzeigen_bot.settings import Exclusions

        config = load_yaml(ROOT / "config/exclusions.yaml", Exclusions)
        self.html = self.html.replace("Warmmiete: 500 €.", "Kaltmiete: 450 €.")
        sender = PlaywrightMessageSender(
            self.context, self.runtime, ListingFilter(config), allow_unpriced=True
        )
        prepared = await sender.prepare(self.listing, "Test")
        self.assertIn("Warmmiete", prepared.warnings[0])
        await prepared.submit()
        self.assertEqual(await prepared.page.evaluate("window.submissions"), 1)
        await prepared.close()

    async def test_unknown_warm_policy_never_sends_known_over_budget(self):
        from kleinanzeigen_bot.core.eligibility import ListingFilter
        from kleinanzeigen_bot.core.exceptions import ListingFiltered
        from kleinanzeigen_bot.settings import Exclusions

        config = load_yaml(ROOT / "config/exclusions.yaml", Exclusions)
        self.html = self.html.replace("Warmmiete: 500 €.", "Warmmiete: 650 €.")
        sender = PlaywrightMessageSender(
            self.context, self.runtime, ListingFilter(config), allow_unpriced=True
        )
        with self.assertRaises(ListingFiltered):
            await sender.prepare(self.listing, "Test")
        self.assertEqual(self.context.pages, [])

    async def test_alternative_recommendations_do_not_replace_empty_search(self):
        self.html = (
            "<html><title>Search</title><body><h1>"
            "Es wurden keine Ergebnisse für Wohnung gefunden</h1>"
            '<ul id="srchrslt-adtable-altads"><li>'
            '<article data-adid="1" data-href="/s-anzeige/seek/1-203-1"><h3>'
            'Wohnung gesucht</h3><p class="text-title3">500 €</p></article></li></ul>'
            "</body></html>"
        )
        profiles = Profiles(
            profiles=[{"name": "test", "url": "https://www.kleinanzeigen.de/s-wohnung-mieten/c203"}]
        )
        self.assertEqual(
            await PlaywrightSearchClient(self.context, profiles, self.runtime).search(), []
        )

    async def test_gesuch_is_not_contacted_even_with_unknown_warm_enabled(self):
        from dataclasses import replace

        from kleinanzeigen_bot.core.exceptions import ListingFiltered

        sender = PlaywrightMessageSender(self.context, self.runtime, allow_unpriced=True)
        with self.assertRaisesRegex(ListingFiltered, "Housing request"):
            await sender.prepare(replace(self.listing, is_request=True), "Test")
        self.assertEqual(self.context.pages, [])

    async def test_card_price_over_budget_never_reaches_message_form(self):
        from dataclasses import replace

        from kleinanzeigen_bot.core.eligibility import ListingFilter
        from kleinanzeigen_bot.core.exceptions import ListingFiltered
        from kleinanzeigen_bot.settings import Exclusions

        sender = PlaywrightMessageSender(
            self.context,
            self.runtime,
            ListingFilter(load_yaml(ROOT / "config/exclusions.yaml", Exclusions)),
            allow_unpriced=True,
        )
        with self.assertRaisesRegex(ListingFiltered, "Listed rent"):
            await sender.prepare(replace(self.listing, price_eur="650"), "Test")
        self.assertEqual(self.context.pages, [])

    async def test_observed_native_dialog_with_dynamic_ids_can_prepare_and_submit(self):
        self.html = (ROOT / "tests/fixtures/listing_native_dialog.html").read_text(encoding="utf-8")
        prepared = await self.sender.prepare(self.listing, "Exact requested text")
        self.assertEqual(await prepared.field.input_value(), "Exact requested text")
        self.assertEqual(await prepared.page.locator("section textarea").input_value(), "")
        self.assertTrue(await prepared.field.evaluate("e => !!e.closest('dialog[open]')"))
        await prepared.submit()
        self.assertEqual(await prepared.page.evaluate("window.submissions"), 1)
        with self.assertRaises(SendUncertain):
            await prepared.submit()
        await prepared.close()

    async def test_pagination_collects_each_page_once(self):
        first = (ROOT / "tests/fixtures/search.html").read_text(encoding="utf-8")
        second = first.replace("123456", "654321")
        first += '<a aria-label="Nächste" href="/s-wohnung-mieten/seite:2/c203">Next</a>'
        await self.context.unroute("**/*")
        visited = []

        async def route(request):
            visited.append(request.request.url)
            await request.fulfill(
                status=200,
                content_type="text/html; charset=utf-8",
                body=second if "seite:2" in request.request.url else first,
            )

        await self.context.route("**/*", route)
        profiles = Profiles(
            profiles=[{"name": "test", "url": "https://www.kleinanzeigen.de/s-wohnung-mieten/c203"}]
        )
        search = PlaywrightSearchClient(self.context, profiles, self.runtime)
        result = await search.search()
        self.assertEqual({x.id for x in result}, {"123456", "654321"})
        self.assertEqual(len(visited), 2)
        self.assertFalse(search.profile_stats[0]["limited"])

    async def test_pagination_limit_is_reported(self):
        self.html = (ROOT / "tests/fixtures/search.html").read_text(encoding="utf-8")
        self.html += '<a aria-label="Nächste" href="/s-wohnung-mieten/seite:2/c203">Next</a>'
        self.runtime.max_pages_per_profile = 1
        profiles = Profiles(
            profiles=[{"name": "test", "url": "https://www.kleinanzeigen.de/s-wohnung-mieten/c203"}]
        )
        search = PlaywrightSearchClient(self.context, profiles, self.runtime)
        await search.search()
        self.assertTrue(search.profile_stats[0]["limited"])

    async def test_missing_confirmation_keeps_post_click_diagnostics(self):
        self.html = self.html.replace("document.getElementById('success').hidden=false", "void 0")
        with tempfile.TemporaryDirectory() as folder:
            sender = PlaywrightMessageSender(
                self.context, self.runtime, diagnostics_dir=Path(folder)
            )
            prepared = await sender.prepare(self.listing, "Test")
            with self.assertRaises(SendUncertain) as error:
                await prepared.submit()
            self.assertTrue((Path(error.exception.diagnostic_path) / "metadata.json").is_file())
            self.assertEqual(await prepared.page.evaluate("window.submissions"), 1)
            await prepared.close()

    async def test_observed_sent_toast_confirms_once(self):
        self.html = self.html.replace(
            '<p id="success" hidden>Deine Nachricht wurde erfolgreich gesendet</p>',
            '<div role="alert" id="success" hidden>Erfolgreich gesendet!</div>',
        )
        self.assertIn('role="alert"', self.html)
        prepared = await self.sender.prepare(self.listing, "Test")
        await prepared.submit()
        with self.assertRaises(SendUncertain):
            await prepared.submit()
        self.assertEqual(await prepared.page.evaluate("window.submissions"), 1)
        await prepared.close()

    async def test_stale_observed_toast_never_counts_as_new_send(self):
        self.html += '<div role="alert">Erfolgreich gesendet!</div>'
        with self.assertRaises(SelectorNotFound):
            await self.sender.prepare(self.listing, "Test")

    async def test_dialog_closing_without_positive_toast_remains_uncertain(self):
        self.html = (ROOT / "tests/fixtures/listing_native_dialog.html").read_text(encoding="utf-8")
        self.html = self.html.replace(
            "document.getElementById('success').hidden=false",
            "document.querySelector('dialog').close()",
        )
        prepared = await self.sender.prepare(self.listing, "Test")
        with self.assertRaises(SendUncertain):
            await prepared.submit()
        self.assertEqual(await prepared.page.evaluate("window.submissions"), 1)
        await prepared.close()
