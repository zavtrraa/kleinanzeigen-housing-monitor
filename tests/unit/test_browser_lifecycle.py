import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from filelock import FileLock
from pydantic import ValidationError

from kleinanzeigen_bot.action.humanizer import between_messages
from kleinanzeigen_bot.action.sender import PlaywrightMessageSender
from kleinanzeigen_bot.core.browser_factory import BrowserFactory
from kleinanzeigen_bot.core.exceptions import ProfileLocked
from kleinanzeigen_bot.core.models import Listing
from kleinanzeigen_bot.settings import Runtime, Settings, load_yaml

ROOT = Path(__file__).resolve().parents[2]


class BrowserLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.profile = Path(self.temp.name) / "profile"
        self.context = SimpleNamespace(set_default_timeout=Mock(), close=AsyncMock())
        self.launch = AsyncMock(return_value=self.context)
        self.driver = SimpleNamespace(
            chromium=SimpleNamespace(launch_persistent_context=self.launch)
        )
        manager = SimpleNamespace(
            __aenter__=AsyncMock(return_value=self.driver), __aexit__=AsyncMock(return_value=False)
        )
        # Async special methods are resolved on the type, not instance attributes.
        self.manager = type(
            "Manager", (), dict(__aenter__=manager.__aenter__, __aexit__=manager.__aexit__)
        )()
        self.api = SimpleNamespace(async_playwright=Mock(return_value=self.manager))
        self.importer = patch(
            "kleinanzeigen_bot.core.browser_factory.import_module", return_value=self.api
        )
        self.import_mock = self.importer.start()

    def tearDown(self):
        self.importer.stop()
        self.temp.cleanup()

    async def test_launch_preserves_native_values_and_enables_sandbox(self):
        factory = BrowserFactory(self.profile, engine="patchright", channel="chrome")
        async with factory.open() as context:
            self.assertIs(context, self.context)
            with self.assertRaises(ProfileLocked):
                async with factory.open():
                    self.fail("Overlapping profile owner")
        options = self.launch.call_args.kwargs
        self.assertTrue(options["chromium_sandbox"])
        self.assertTrue(options["no_viewport"])
        self.assertFalse(options["accept_downloads"])
        self.assertNotIn("user_agent", options)
        self.assertNotIn("locale", options)
        self.assertIn("--window-size=1000,720", options["args"])
        self.assertIn("--force-dark-mode", options["args"])
        self.assertEqual(options["color_scheme"], "dark")
        self.context.close.assert_awaited_once()

    async def test_explicit_sandbox_opt_out_is_forwarded(self):
        factory = BrowserFactory(self.profile, chromium_sandbox=False)
        async with factory.open():
            pass
        self.assertFalse(self.launch.call_args.kwargs["chromium_sandbox"])

    async def test_context_and_lock_released_when_consumer_fails(self):
        factory = BrowserFactory(self.profile)
        with self.assertRaisesRegex(RuntimeError, "consumer"):
            async with factory.open():
                raise RuntimeError("consumer")
        self.context.close.assert_awaited_once()
        async with factory.open():
            pass
        self.assertEqual(self.launch.await_count, 2)

    async def test_hook_failure_closes_context_and_releases_lock(self):
        hook = AsyncMock(side_effect=RuntimeError("hook"))
        with self.assertRaisesRegex(RuntimeError, "hook"):
            async with BrowserFactory(self.profile, context_hook=hook).open():
                self.fail("Failed hook must not yield")
        self.context.close.assert_awaited_once()
        async with BrowserFactory(self.profile).open():
            pass

    async def test_launch_failure_releases_lock(self):
        self.launch.side_effect = RuntimeError("launch")
        factory = BrowserFactory(self.profile)
        with self.assertRaisesRegex(RuntimeError, "launch"):
            async with factory.open():
                self.fail("Failed launch must not yield")
        self.launch.side_effect = None
        async with factory.open():
            pass
        self.context.close.assert_awaited_once()

    async def test_locked_profile_never_imports_or_launches_engine(self):
        with FileLock(str(self.profile) + ".lock"):
            with self.assertRaises(ProfileLocked):
                async with BrowserFactory(self.profile).open():
                    self.fail("Lock ignored")
        self.import_mock.assert_not_called()

    async def test_invalid_engine_channel_and_locale_do_not_launch(self):
        for options in (
            {"engine": "unknown"},
            {"channel": "unknown"},
            {"engine": "patchright", "locale": "de-DE"},
        ):
            with self.subTest(options=options), self.assertRaises(ValueError):
                async with BrowserFactory(self.profile, **options).open():
                    self.fail("Invalid settings launched a browser")
        self.import_mock.assert_not_called()


class PacingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.runtime = load_yaml(ROOT / "config/runtime.yaml", Runtime)
        self.listing = Listing("1", "https://www.kleinanzeigen.de/s-anzeige/a/1-203-1", "A")

    async def test_pacing_sleeps_for_selected_duration(self):
        with (
            patch("kleinanzeigen_bot.action.humanizer.random.uniform", return_value=47) as choose,
            patch(
                "kleinanzeigen_bot.action.humanizer.asyncio.sleep", new_callable=AsyncMock
            ) as sleep,
        ):
            await between_messages(30, 60)
        choose.assert_called_once_with(30, 60)
        sleep.assert_awaited_once_with(47)

    async def test_cancel_during_pause_does_not_open_page(self):
        context = SimpleNamespace(new_page=AsyncMock())
        pause = AsyncMock(side_effect=asyncio.CancelledError())
        sender = PlaywrightMessageSender(context, self.runtime, pause_before_listing=pause)
        with self.assertRaises(asyncio.CancelledError):
            await sender.prepare(self.listing, "Text")
        context.new_page.assert_not_awaited()

    async def test_invalid_url_is_rejected_before_pause_or_browser(self):
        context = SimpleNamespace(new_page=AsyncMock())
        pause = AsyncMock()
        sender = PlaywrightMessageSender(context, self.runtime, pause_before_listing=pause)
        listing = Listing("1", "https://example.com/", "A")
        with self.assertRaises(ValueError):
            await sender.prepare(listing, "Text")
        pause.assert_not_awaited()
        context.new_page.assert_not_awaited()

    async def test_navigation_failure_closes_page_after_pause(self):
        events = []

        async def pause():
            events.append("pause")

        async def navigate(*args, **kwargs):
            events.append("navigate")
            raise TimeoutError("Navigation timed out")

        page = SimpleNamespace(goto=AsyncMock(side_effect=navigate), close=AsyncMock())
        context = SimpleNamespace(new_page=AsyncMock(return_value=page))
        sender = PlaywrightMessageSender(context, self.runtime, pause_before_listing=pause)
        with self.assertRaises(TimeoutError):
            await sender.prepare(self.listing, "Text")
        self.assertEqual(events, ["pause", "navigate"])
        page.close.assert_awaited_once()

    def test_listing_pause_settings_validate_ranges(self):
        settings = Settings(_env_file=None)
        self.assertEqual(
            (settings.min_listing_pause_seconds, settings.max_listing_pause_seconds), (30, 60)
        )
        for lower, upper in ((60, 30), (-1, 30), (30, -1)):
            with self.subTest(lower=lower, upper=upper), self.assertRaises(ValidationError):
                Settings(
                    _env_file=None, min_listing_pause_seconds=lower, max_listing_pause_seconds=upper
                )

    async def test_visible_countdown_preserves_full_pause_duration(self):
        events = []
        with (
            patch("kleinanzeigen_bot.action.humanizer.random.uniform", return_value=47),
            patch(
                "kleinanzeigen_bot.action.humanizer.asyncio.sleep", new_callable=AsyncMock
            ) as sleep,
        ):
            await between_messages(30, 60, progress=events.append)
        self.assertEqual([call.args[0] for call in sleep.await_args_list], [10, 10, 10, 10, 7])
        self.assertIn("47", events[0])
        self.assertIn("Delay complete", events[-1])

    async def test_cancelled_countdown_does_not_claim_navigation_started(self):
        events = []
        with (
            patch("kleinanzeigen_bot.action.humanizer.random.uniform", return_value=30),
            patch(
                "kleinanzeigen_bot.action.humanizer.asyncio.sleep",
                side_effect=asyncio.CancelledError(),
            ),
        ):
            with self.assertRaises(asyncio.CancelledError):
                await between_messages(30, 60, progress=events.append)
        self.assertEqual(len(events), 1)
        self.assertNotIn("Opening", events[0])


class CompactWindowTests(unittest.IsolatedAsyncioTestCase):
    async def test_saved_maximized_state_is_reset_and_size_fits_screen(self):
        page = SimpleNamespace(evaluate=AsyncMock(return_value={"width": 900, "height": 700}))
        session = SimpleNamespace(send=AsyncMock(return_value={"windowId": 17}), detach=AsyncMock())
        context = SimpleNamespace(pages=[page], new_cdp_session=AsyncMock(return_value=session))
        await BrowserFactory(Path("unused"))._fit_window(context)
        calls = session.send.await_args_list
        self.assertEqual(calls[1].args[1]["bounds"], {"windowState": "normal"})
        self.assertEqual(calls[2].args[1]["bounds"], {"width": 840, "height": 640})
        session.detach.assert_awaited_once()

    async def test_resize_error_does_not_block_normal_browser_work(self):
        page = SimpleNamespace(evaluate=AsyncMock(side_effect=RuntimeError("no screen")))
        await BrowserFactory(Path("unused"))._fit_window(SimpleNamespace(pages=[page]))
