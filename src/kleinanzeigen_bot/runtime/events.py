import asyncio
import json
from datetime import UTC, datetime
from zoneinfo import ZoneInfo


class EventConsole:
    """Application events only. No raw HTTP logs, tokens or page contents."""

    def __init__(self, directory, bot=None, chat_id="", *, pace=3.2):
        self.path = directory / "events.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.bot, self.chat_id, self.pace = bot, chat_id, pace
        self.queue = asyncio.Queue(maxsize=2000)
        self.task = None
        self.failures = 0
        self.delivered = 0

    def emit(self, description, function, flags="", command=""):
        event = {
            "at": datetime.now(UTC).isoformat(),
            "description": description[:900],
            "function": function[:100],
            "flags": flags[:150],
            "command": command[:400],
        }
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event, ensure_ascii=False) + "\n")
        if self.bot and self.chat_id:
            try:
                self.queue.put_nowait(event)
            except asyncio.QueueFull:
                self.failures += 1

    def start(self):
        if self.bot and self.chat_id:
            self.task = asyncio.create_task(self._deliver())

    async def _deliver(self):
        while True:
            event = await self.queue.get()
            if event is None:
                return
            batch = [event]
            stopping = False
            while len(batch) < 3 and not self.queue.empty():
                next_event = self.queue.get_nowait()
                if next_event is None:
                    stopping = True
                    break
                batch.append(next_event)
            text = []
            for item in batch:
                stamp = datetime.fromisoformat(item["at"]).astimezone(ZoneInfo("Europe/Berlin"))
                text.append(
                    f"{stamp:%d.%m %H:%M:%S} · {item['description']}\n"
                    f"| {item['function']} — {item['flags'] or 'no flags'} — "
                    f"{item['command'] or 'internal call'} |"
                )
            try:
                await self.bot.send_message(
                    chat_id=self.chat_id,
                    text="\n\n".join(text)
                    .encode("utf-16-le")[:7200]
                    .decode("utf-16-le", errors="ignore"),
                    parse_mode=None,
                    disable_web_page_preview=True,
                    disable_notification=True,
                )
                self.delivered += len(batch)
            except Exception as exc:
                self.failures += 1
                with self.path.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps({"event_delivery_error": type(exc).__name__}) + "\n")
            if stopping:
                return
            await asyncio.sleep(self.pace)

    async def close(self):
        if self.task:
            # Drain best-effort; local JSONL always retains the complete event inventory.
            await self.queue.put(None)
            try:
                await asyncio.wait_for(self.task, 12)
            except TimeoutError:
                self.task.cancel()
                try:
                    await self.task
                except asyncio.CancelledError:
                    pass
