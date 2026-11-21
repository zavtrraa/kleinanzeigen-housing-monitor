import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from kleinanzeigen_bot.core.models import CycleReport
from kleinanzeigen_bot.runtime.daemon import run_daemon
from kleinanzeigen_bot.settings import Settings


class DaemonTests(unittest.IsolatedAsyncioTestCase):
    async def test_manual_request_is_accepted_during_active_run_and_executes_serially(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings(
                _env_file=None,
                data_dir=Path(directory),
                telegram_bot_token="fixture",
                telegram_chat_id="-1",
                telegram_allowed_user_ids=[42],
            )
            bot = AsyncMock()
            bot.__aenter__.return_value = bot
            bot.get_chat.return_value = SimpleNamespace(id=-1)
            bot.get_webhook_info.return_value = SimpleNamespace(url="")
            first_started, release, second_started = (
                asyncio.Event(),
                asyncio.Event(),
                asyncio.Event(),
            )
            calls = []
            active = 0
            max_active = 0

            async def cycle(*args, trigger, **kwargs):
                nonlocal active, max_active
                active += 1
                max_active = max(active, max_active)
                calls.append(trigger)
                if len(calls) == 1:
                    first_started.set()
                    await release.wait()
                else:
                    second_started.set()
                active -= 1
                return CycleReport("fixture-cycle", False)

            sent = False

            async def updates(**kwargs):
                nonlocal sent
                await asyncio.sleep(0.01)
                if first_started.is_set() and not sent:
                    sent = True
                    query = SimpleNamespace(
                        message=SimpleNamespace(chat_id=-1),
                        from_user=SimpleNamespace(id=42),
                        data="r:run",
                        answer=AsyncMock(),
                    )
                    return [SimpleNamespace(callback_query=query, update_id=17)]
                return []

            bot.get_updates.side_effect = updates
            sink = SimpleNamespace(start=Mock(), emit=Mock(), close=AsyncMock())
            with (
                patch("kleinanzeigen_bot.runtime.daemon.Bot", return_value=bot),
                patch(
                    "kleinanzeigen_bot.runtime.daemon.ChatPacer", side_effect=lambda value: value
                ),
                patch("kleinanzeigen_bot.runtime.daemon.EventConsole", return_value=sink),
                patch("kleinanzeigen_bot.runtime.daemon.build_sources", return_value=[]),
                patch("kleinanzeigen_bot.runtime.daemon.drain_outbox", new_callable=AsyncMock),
                patch("kleinanzeigen_bot.runtime.daemon.run_live_cycle", side_effect=cycle),
            ):
                task = asyncio.create_task(run_daemon(settings))
                try:
                    await asyncio.wait_for(first_started.wait(), 3)
                    for _ in range(100):
                        if sent and any(
                            "queued" in call.kwargs.get("text", "")
                            for call in bot.send_message.await_args_list
                        ):
                            break
                        await asyncio.sleep(0.01)
                    else:
                        self.fail("Manual request not accepted while busy")
                    self.assertEqual(calls, ["scheduled"])
                    release.set()
                    await asyncio.wait_for(second_started.wait(), 3)
                    (settings.data_dir / "runner-stop.request").write_text("stop")
                    self.assertEqual(await asyncio.wait_for(task, 4), 0)
                finally:
                    if not task.done():
                        task.cancel()
                        try:
                            await task
                        except asyncio.CancelledError:
                            pass
            self.assertEqual(calls[:2], ["scheduled", "manual"])
            self.assertEqual(max_active, 1)
