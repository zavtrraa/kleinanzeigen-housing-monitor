import io
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from kleinanzeigen_bot.core.models import CycleReport
from kleinanzeigen_bot.notifications.telegram_bot import TelegramNotifier
from kleinanzeigen_bot.settings import Profiles, Runtime, Settings, load_yaml, validate_ready

ROOT = Path(__file__).resolve().parents[2]


class LivePolicyTests(unittest.TestCase):
    def setUp(self):
        self.runtime = load_yaml(ROOT / "config/runtime.yaml", Runtime)
        self.profiles = Profiles(
            profiles=[{"name": "test", "url": "https://www.kleinanzeigen.de/s-test"}]
        )
        self.settings = Settings(
            _env_file=None, dry_run=False, telegram_bot_token="fixture", telegram_chat_id="fixture"
        )

    def test_explicit_per_listing_policy_allows_one_visible_live_reply(self):
        self.assertFalse(self.runtime.selectors_verified)
        self.assertEqual(self.runtime.live_validation, "per_listing")
        validate_ready(self.settings, self.profiles, self.runtime)

    def test_unverified_selectors_with_default_policy_still_block_live(self):
        self.runtime.live_validation = "verified"
        with self.assertRaisesRegex(ValueError, "selectors"):
            validate_ready(self.settings, self.profiles, self.runtime)

    def test_per_listing_policy_cannot_expand_budget_or_hide_browser(self):
        for changes in ({"max_messages_per_cycle": 2}, {"headless": True}):
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, "one message"):
                validate_ready(
                    self.settings.model_copy(update=changes), self.profiles, self.runtime
                )

    def test_live_requires_configured_telegram(self):
        self.settings.telegram_bot_token = Settings(_env_file=None).telegram_bot_token
        with self.assertRaisesRegex(ValueError, "Telegram"):
            validate_ready(self.settings, self.profiles, self.runtime)

    def test_no_enabled_search_cannot_send(self):
        self.profiles.profiles[0].enabled = False
        with self.assertRaisesRegex(ValueError, "enable"):
            validate_ready(self.settings, self.profiles, self.runtime)

    def test_dry_run_does_not_require_live_credentials(self):
        validate_ready(Settings(_env_file=None), self.profiles, self.runtime)


class ConsoleDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_live_summary_is_visible_in_terminal_and_telegram(self):
        bot = SimpleNamespace(send_message=AsyncMock())
        report = CycleReport("fixture", False, search_completed=True, found=1, sent=1)
        with redirect_stdout(io.StringIO()) as output:
            await TelegramNotifier(bot, "fixture", console=True).summary(report)
        self.assertIn("LIVE", output.getvalue())
        self.assertIn("sent: 1", output.getvalue())
        self.assertEqual(bot.send_message.call_args.kwargs["text"], output.getvalue().strip())

    async def test_delivery_error_does_not_hide_terminal_status(self):
        bot = SimpleNamespace(send_message=AsyncMock(side_effect=RuntimeError("offline")))
        with redirect_stdout(io.StringIO()) as output, self.assertRaises(RuntimeError):
            await TelegramNotifier(bot, "fixture", console=True).alert("Check manually")
        self.assertIn("Check manually", output.getvalue())
