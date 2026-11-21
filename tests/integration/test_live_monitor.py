from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from playwright.async_api import async_playwright

from kleinanzeigen_bot.runtime.live_cycle import run_live_cycle
from kleinanzeigen_bot.runtime.preferences import SearchPreferences
from kleinanzeigen_bot.settings import Settings


class LocalTransport:
    def __init__(self):
        self.messages = []

    async def send_message(self, **payload):
        self.messages.append(payload)
        return SimpleNamespace(message_id=len(self.messages))


@pytest.mark.asyncio
async def test_real_cycle_uses_snapshot_and_monitor_never_needs_a_contact_form(tmp_path):
    visited = []
    search = """<html><body><article class="aditem" data-adid="123456">
        <h3><a href="/s-anzeige/apartment/123456-203-1">Balkon apartment</a></h3>
        <p class="text-title3">450 €</p><p class="font-strong">42 m² · 2 Zi.</p>
        </article></body></html>"""
    details = """<html><body><div id="viewad-description-text">
        Balkon apartment. Warmmiete 500 EUR.</div><div id="viewad-details">42 m²</div>
        </body></html>"""

    @asynccontextmanager
    async def browser_context(_):
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            context = await browser.new_context()

            async def respond(route):
                visited.append(route.request.url)
                body = details if "/s-anzeige/" in route.request.url else search
                await route.fulfill(status=200, content_type="text/html; charset=utf-8", body=body)

            await context.route("**/*", respond)
            try:
                yield context
            finally:
                await browser.close()

    settings = Settings(
        _env_file=None,
        data_dir=tmp_path,
        headless=True,
        min_listing_pause_seconds=0,
        max_listing_pause_seconds=0,
    )
    preferences = SearchPreferences(
        cities=["hannover"], query="Balkon", max_rent="600", min_area="30"
    )
    transport = LocalTransport()
    with patch("kleinanzeigen_bot.runtime.live_cycle.BrowserFactory.open", browser_context):
        report = await run_live_cycle(settings, bot=transport, preferences=preferences)
    assert not report.halted, report.errors
    assert report.monitoring
    assert report.notified == 1
    assert report.sent == 0
    assert "/preis::600/Balkon/" in visited[0]
    assert "Balkon apartment" in transport.messages[0]["text"]
