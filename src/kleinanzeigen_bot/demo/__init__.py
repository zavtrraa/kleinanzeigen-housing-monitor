from pathlib import Path

from ..action.text_variator import TextVariator
from ..core.models import Listing
from ..core.orchestrator import Orchestrator
from ..notifications.telegram_bot import ConsoleNotifier
from ..state.sqlite_repo import SQLiteRepository


class DemoSearch:
    async def search(self):
        return [
            Listing(
                "1000000001",
                "https://www.kleinanzeigen.de/s-anzeige/demo/1000000001-203-1",
                "Demo: 2-Zimmer-Wohnung",
                "850",
                "2",
                "55",
                profile="offline",
            ),
            Listing(
                "1000000002",
                "https://www.kleinanzeigen.de/s-anzeige/demo/1000000002-203-1",
                "Demo: Helle Wohnung",
                "920",
                "2",
                "60",
                profile="offline",
            ),
        ]


class DemoPrepared:
    async def submit(self):
        raise AssertionError("Offline demo must never submit")

    async def close(self):
        pass


class DemoSender:
    async def prepare(self, listing, text):
        return DemoPrepared()


async def run_demo(data_dir: Path):
    # Isolated from the real account, real state and Telegram; no network/browser imports.
    repo = SQLiteRepository(data_dir / "demo.sqlite3")
    try:
        texts = TextVariator(
            "{greeting}\n\nIch interessiere mich für „{title}“.\n\n{closing}\n{signature}",
            ["Guten Tag,"],
            ["Viele Grüße"],
            "DEMO",
        )
        bot = Orchestrator(DemoSearch(), repo, DemoSender(), texts, ConsoleNotifier())
        return await bot.run_cycle()
    finally:
        repo.close()
