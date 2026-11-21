import json
import tempfile
from pathlib import Path

from playwright.async_api import async_playwright

from ..action.sender import PlaywrightMessageSender
from ..action.text_variator import TextVariator
from ..core.eligibility import ListingFilter
from ..core.exceptions import SendUncertain
from ..core.models import Listing
from ..core.orchestrator import Orchestrator
from ..notifications.telegram_bot import ConsoleNotifier
from ..settings import Exclusions, Runtime, load_yaml
from ..state.sqlite_repo import SQLiteRepository

HTML = """<!doctype html><html><head><title>Isolated send test</title></head><body>
<p id="viewad-description-text">Kaltmiete 400 €. Die Warmmiete bitte erfragen.</p>
<button onclick="document.getElementById('compose').hidden=false">Nachricht schreiben</button>
<section id="compose" hidden><label>Nachricht<textarea name="message"></textarea></label>
<button onclick="submitFixture()">Nachricht senden</button></section>
<p id="success" hidden>Deine Nachricht wurde erfolgreich gesendet</p>
<script>async function submitFixture(){await window.recordTestSend();
document.getElementById('success').hidden=false;}</script>
</body></html>"""


async def run_send_probe(settings):
    print("Isolated test: fixture page and database, no account or real sellers.", flush=True)
    runtime = load_yaml(settings.runtime_path, Runtime)
    counts = {"submissions": 0, "intercepted_requests": 0}
    async with async_playwright() as driver:
        browser = await driver.chromium.launch(headless=True)
        try:
            context = await browser.new_context()

            async def intercept(route):
                counts["intercepted_requests"] += 1
                await route.fulfill(status=200, content_type="text/html; charset=utf-8", body=HTML)

            await context.route("**/*", intercept)

            def record():
                counts["submissions"] += 1

            await context.expose_function("recordTestSend", record)
            sender = PlaywrightMessageSender(
                context,
                runtime,
                ListingFilter(load_yaml(settings.exclusions_path, Exclusions)),
                allow_unpriced=True,
            )
            listing = Listing(
                "9000000001",
                "https://www.kleinanzeigen.de/s-anzeige/isolated-test/9000000001-203-1",
                "Fixture listing",
            )

            class Search:
                async def search(self):
                    return [listing]

            with tempfile.TemporaryDirectory(prefix="kleinbot-send-test-") as folder:
                repo = SQLiteRepository(Path(folder) / "state.sqlite3")
                try:
                    runner = Orchestrator(
                        Search(),
                        repo,
                        sender,
                        TextVariator("Guten Tag!", ["Hallo"], ["LG"], ""),
                        ConsoleNotifier(),
                        dry_run=False,
                        trigger="isolated-test",
                    )
                    first = await runner.run_cycle()
                    second = await runner.run_cycle()
                    assert first.sent == 1 and second.sent == 0 and counts["submissions"] == 1
                    assert first.items[0].warnings
                    prepared = await sender.prepare(listing, "Test")
                    await prepared.submit()
                    try:
                        await prepared.submit()
                    except SendUncertain:
                        pass
                    else:
                        raise AssertionError("Repeated submit was not blocked")
                    await prepared.close()
                    assert counts["submissions"] == 2
                    result = {
                        "isolated": True,
                        "external_requests": 0,
                        "fixture_submissions": 2,
                        "database_dedup": True,
                        "double_click_blocked": True,
                        "unknown_warm_warning": True,
                        "real_landlord_delivery_tested": False,
                    }
                finally:
                    repo.close()
            target = settings.data_dir / "send-test-result.json"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps(result))
            return 0
        finally:
            await browser.close()
