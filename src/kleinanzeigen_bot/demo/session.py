import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from ..action.text_variator import TextVariator
from ..core.eligibility import ListingFilter
from ..core.matching import match_listing
from ..core.models import Listing, Preview
from ..core.monitor import Monitor
from ..notifications.telegram_bot import TelegramNotifier
from ..runtime.controls import handle_update
from ..runtime.events import EventConsole
from ..runtime.panel import ControlPanel
from ..runtime.preferences import PreferenceStore
from ..runtime.queue import RunQueue
from ..settings import ExclusionRule, Exclusions
from ..state.sqlite_repo import SQLiteRepository
from .transport import DemoTransport

TEMPLATE = (
    "Guten Tag,\n\nich interessiere mich für die Wohnung. Ist eine Besichtigung möglich?"
    "\n\nMit freundlichen Grüßen"
)


class DemoSource:
    def __init__(self):
        data = json.loads(Path(__file__).with_name("listings.json").read_text(encoding="utf-8"))
        self.listings = [Listing(**item) for item in data]

    async def search(self):
        return self.listings

    async def inspect(self, listing):
        return listing


class DemoSession:
    def __init__(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True)
        self.queue = RunQueue(directory / "demo-queue.sqlite3")
        self.queue.set("manual_only", True)
        self.repo = SQLiteRepository(directory / "demo-listings.sqlite3")
        self.preferences = PreferenceStore(self.queue)
        self.transport = DemoTransport(self.queue)
        self.panel = ControlPanel(self.queue, self.transport, "-1")
        self.transport.on_message = self.panel.note_message
        self.notifier = TelegramNotifier(self.transport, "-1", rich=True)
        self.events = EventConsole(directory)
        self.source = DemoSource()
        self.lock = asyncio.Lock()

    async def start(self):
        await self.panel.refresh(
            "Local demo. All listings are fictional; no messages leave this app."
        )

    def snapshot(self):
        reports = self.repo.db.execute(
            "SELECT report FROM cycles WHERE report IS NOT NULL ORDER BY rowid DESC LIMIT 10"
        ).fetchall()
        return {
            "preferences": self.preferences.load().model_dump(mode="json"),
            "status": self.queue.status(),
            "messages": self.transport.messages,
            "reports": [json.loads(row[0]) for row in reversed(reports)],
            "template": TEMPLATE,
        }

    async def command(self, text: str, message_id=None):
        if not isinstance(text, str) or len(text) > 500:
            raise ValueError("Command must contain at most 500 characters")
        async with self.lock:
            ident = self.queue.get("demo_update_id", 0) + 1
            self.queue.set("demo_update_id", ident)
            message = SimpleNamespace(chat_id=-1, from_user=SimpleNamespace(id=42), text=text)
            if text.startswith("r:"):

                async def answer(_):
                    return None

                message.message_id = message_id or self.queue.get("panel_message_id")
                query = SimpleNamespace(
                    message=message, from_user=message.from_user, data=text, answer=answer
                )
            else:
                message.message_id = self.transport.user_message(text).message_id
                query = None
            update = SimpleNamespace(update_id=ident, callback_query=query, message=message)
            await handle_update(
                update,
                self.queue,
                self.transport,
                "-1",
                [42],
                self.events,
                panel=self.panel,
                preferences=self.preferences,
                template=TEMPLATE,
            )
            await self._run_queued()
            return self.snapshot()

    async def _run_queued(self):
        while job := self.queue.claim():
            preferences = self.preferences.load()
            filters = ListingFilter(
                Exclusions(
                    max_warm_eur=preferences.max_rent,
                    rules=[
                        ExclusionRule(
                            name="shared", label="Shared housing", patterns=[r"\bWG-Zimmer\b"]
                        )
                    ],
                )
            )
            runner = Monitor(
                self.source,
                self.source,
                self.repo,
                self.notifier,
                preferences,
                filters,
                destination="demo",
                trigger=job["trigger"],
                request_id=str(job["id"]),
            )
            report = await runner.run_cycle()
            if preferences.auto_reply and not report.halted:
                # Show the exact template result without introducing a seller transport.
                for listing in self.source.listings:
                    if match_listing(listing, preferences, filters).outcome == "allowed":
                        text = TextVariator(TEMPLATE, ["Guten Tag"], ["Viele Grüße"], "").render(
                            listing
                        )
                        self.repo.preview(listing.id, report.id, text)
                        report.previews.append(Preview(listing.id, listing.url, text))
                        await self.transport.send_message(
                            chat_id="-1",
                            text=f"Demo reply preview | {listing.title}\n\n{text}"
                            "\n\nPreview only. No seller was contacted.",
                        )
                        break
                self.repo.finish_cycle(report)
            self.queue.complete(
                job["id"], cycle_id=report.id, error="CycleHalted" if report.halted else None
            )
            if report.halted:
                self.queue.set("paused", True)
            await self.panel.refresh("Demo run finished. No seller messages were sent.")

    def close(self):
        self.repo.close()
        self.queue.close()
