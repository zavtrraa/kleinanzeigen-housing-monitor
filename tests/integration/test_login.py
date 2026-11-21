import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from kleinanzeigen_bot.core.exceptions import IPRangeBlocked
from kleinanzeigen_bot.main import parser, run
from kleinanzeigen_bot.settings import Settings
from kleinanzeigen_bot.state.sqlite_repo import SQLiteRepository


class LoginTests(unittest.IsolatedAsyncioTestCase):
    async def check_blocked_login(self, after_input=False):
        with tempfile.TemporaryDirectory() as directory:
            page = SimpleNamespace(goto=AsyncMock())
            context = SimpleNamespace(new_page=AsyncMock(return_value=page), pages=[page])
            closed = []

            @asynccontextmanager
            async def open_context():
                try:
                    yield context
                finally:
                    closed.append(True)

            factory = SimpleNamespace(open=open_context)
            settings = Settings(_env_file=None, data_dir=Path(directory))
            args = parser().parse_args(["--login"])
            failures = [None, IPRangeBlocked()] if after_input else IPRangeBlocked()
            with (
                patch("kleinanzeigen_bot.settings.Settings", return_value=settings),
                patch(
                    "kleinanzeigen_bot.core.browser_factory.BrowserFactory", return_value=factory
                ),
                patch("kleinanzeigen_bot.core.ui.ensure_not_ip_blocked", side_effect=failures),
                patch("kleinanzeigen_bot.main.asyncio.to_thread", new_callable=AsyncMock) as prompt,
            ):
                code = await run(args)
            self.assertEqual(code, 2)
            self.assertEqual(closed, [True])
            self.assertEqual(prompt.await_count, int(after_input))
            repo = SQLiteRepository(Path(directory) / "state.sqlite3")
            try:
                self.assertEqual(repo.pause_reason(), "IPRangeBlocked")
            finally:
                repo.close()

    async def test_initial_block_saves_pause_without_prompting_for_login(self):
        await self.check_blocked_login()

    async def test_block_after_manual_login_saves_pause(self):
        await self.check_blocked_login(after_input=True)
