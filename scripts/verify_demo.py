"""Run the visible demo workflow and optionally record shareable local evidence."""

import argparse
import asyncio
import json
import tempfile
from pathlib import Path

from playwright.async_api import async_playwright, expect

from kleinanzeigen_bot.demo.server import create_server
from kleinanzeigen_bot.demo.session import DemoSession


async def verify(output: Path, record: bool):
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="housing-demo-") as temporary:
        session = DemoSession(Path(temporary))
        await session.start()
        server = create_server(session, asyncio.get_running_loop(), port=0)
        serving = asyncio.create_task(asyncio.to_thread(server.serve_forever, poll_interval=0.05))
        failures, external = [], []
        try:
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(headless=True)
                options = {"viewport": {"width": 1920, "height": 1080}, "device_scale_factor": 1}
                if record:
                    options.update(
                        record_video_dir=str(output / "video"),
                        record_video_size=options["viewport"],
                    )
                context = await browser.new_context(**options)

                async def local_only(route):
                    if route.request.url.startswith(f"http://127.0.0.1:{server.server_port}/"):
                        await route.continue_()
                    else:
                        external.append(route.request.url)
                        await route.abort()

                await context.route("**/*", local_only)
                page = await context.new_page()
                page.on("pageerror", lambda error: failures.append(str(error)))

                async def pause(seconds=3):
                    if record:
                        await page.wait_for_timeout(seconds * 1000)

                await page.goto(f"http://127.0.0.1:{server.server_port}")
                await expect(page.locator("#mode")).to_have_text("Monitoring ready")
                await pause(5)
                await page.screenshot(path=str(output / "01_main.png"))
                await page.select_option("#city", "hannover")
                await pause()
                await page.fill("#query", "balcony")
                await pause()
                await page.fill("#budget", "600")
                await page.fill("#area", "30")
                await pause()
                await page.get_by_role("button", name="Save criteria", exact=True).click()
                await expect(
                    page.get_by_role("button", name="Save criteria", exact=True)
                ).to_be_enabled()
                await page.screenshot(path=str(output / "02_search_settings.png"))
                await pause(5)
                await page.locator('[data-command="/run"]').click()
                await expect(page.locator("#notified")).to_have_text("2")
                await expect(page.locator("#matched")).to_have_text("1")
                first_photo = (
                    page.locator(".bubble").filter(has=page.locator(".listing-image")).first
                )
                await first_photo.scroll_into_view_if_needed()
                await pause(6)
                await page.screenshot(path=str(output / "03_listing_result.png"))
                await page.locator('[data-command="/status"]').click()
                await expect(page.locator('[data-command="/status"]')).to_be_enabled()
                await pause(5)
                await page.screenshot(path=str(output / "04_monitor_status.png"))
                await page.locator('[data-command="/template"]').click()
                await expect(page.locator('[data-command="/template"]')).to_be_enabled()
                await pause(5)
                await page.locator("#replies").check()
                await expect(page.locator('[data-command="/run"]')).to_be_enabled()
                await page.locator('[data-command="/run"]').click()
                await expect(page.locator("#notified")).to_have_text("0")
                await expect(page.locator("#messages")).to_contain_text("Demo reply preview")
                await pause(6)
                await page.locator('[data-command="/pause"]').click()
                await expect(page.locator("#mode")).to_have_text("Monitoring paused")
                await pause(3)
                await page.locator('[data-command="/run"]').click()
                await expect(page.locator("#queued")).to_have_text("1")
                await pause(3)
                await page.locator('[data-command="/resume"]').click()
                await expect(page.locator("#queued")).to_have_text("0")
                await expect(page.locator("#mode")).to_have_text("Monitoring ready")
                await pause(5)
                await page.set_viewport_size({"width": 390, "height": 844})
                assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                await page.screenshot(path=str(output / "mobile.png"), full_page=True)
                assert not failures, failures
                assert not external, external
                result = {
                    "workflow": (
                        "settings -> search -> cards -> status -> template "
                        "-> repeat -> pause -> resume"
                    ),
                    "browser_errors": failures,
                    "external_requests": external,
                    "seller_attempts": session.repo.db.execute(
                        "SELECT count(*) FROM attempts"
                    ).fetchone()[0],
                    "last_report": session.snapshot()["reports"][-1],
                }
                assert result["seller_attempts"] == 0
                video = page.video
                await context.close()
                if record and video:
                    await video.save_as(str(output / "housing_monitor_demo.webm"))
                await browser.close()
                (output / "verification.json").write_text(
                    json.dumps(result, indent=2), encoding="utf-8"
                )
                print(json.dumps({"passed": True, "output": str(output.resolve())}))
        finally:
            await asyncio.to_thread(server.shutdown)
            await serving
            server.server_close()
            session.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("docs/demo"))
    parser.add_argument("--record", action="store_true")
    args = parser.parse_args()
    asyncio.run(verify(args.output, args.record))
