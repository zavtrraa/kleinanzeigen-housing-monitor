import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from kleinanzeigen_bot.housing.runner import run_housing
from kleinanzeigen_bot.main import parser
from kleinanzeigen_bot.settings import Settings


class HubLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def bot(self):
        bot = AsyncMock()
        bot.__aenter__.return_value = bot
        bot.get_chat.return_value = SimpleNamespace(id=-123)
        bot.get_webhook_info.return_value = SimpleNamespace(url="")
        return bot

    async def test_watch_stays_alive_after_initial_scan_and_records_cancellation(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings(
                _env_file=None,
                data_dir=Path(directory),
                telegram_bot_token="fixture",
                telegram_chat_id="-123",
            )
            bot = self.bot()
            original_sleep = asyncio.sleep

            async def controls(*args):
                await original_sleep(0.01)

            with (
                patch("kleinanzeigen_bot.housing.runner.Bot", return_value=bot),
                patch("kleinanzeigen_bot.housing.runner.build_sources", return_value=[]),
                patch(
                    "kleinanzeigen_bot.housing.runner.collect_sources",
                    new_callable=AsyncMock,
                    return_value={},
                ) as collect,
                patch("kleinanzeigen_bot.housing.runner.drain_outbox", new_callable=AsyncMock),
                patch("kleinanzeigen_bot.housing.runner.poll_controls", side_effect=controls),
            ):
                with self.assertRaises(TimeoutError):
                    await asyncio.wait_for(
                        run_housing(parser().parse_args(["--hub-watch"]), settings), timeout=0.12
                    )
            self.assertGreaterEqual(collect.await_count, 1)
            report = json.loads((Path(directory) / "hub-runtime.json").read_text())
            self.assertEqual(report["state"], "stopped")
            self.assertEqual(report["stop_reason"], "cancelled")
            bot.send_message.assert_not_awaited()

    async def test_watch_failure_records_error_type_without_secret_message(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings(
                _env_file=None,
                data_dir=Path(directory),
                telegram_bot_token="fixture",
                telegram_chat_id="-123",
            )
            bot = self.bot()
            with (
                patch("kleinanzeigen_bot.housing.runner.Bot", return_value=bot),
                patch("kleinanzeigen_bot.housing.runner.build_sources", return_value=[]),
                patch(
                    "kleinanzeigen_bot.housing.runner.collect_sources",
                    side_effect=RuntimeError("secret-value"),
                ),
                patch("kleinanzeigen_bot.housing.runner.poll_controls", new_callable=AsyncMock),
            ):
                with self.assertRaises(RuntimeError):
                    await run_housing(parser().parse_args(["--hub-watch"]), settings)
            report = (Path(directory) / "hub-runtime.json").read_text()
            self.assertNotIn("secret-value", report)
            self.assertEqual(json.loads(report)["error_type"], "RuntimeError")
