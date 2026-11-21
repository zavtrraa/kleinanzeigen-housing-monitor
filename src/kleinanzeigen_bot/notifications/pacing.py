import asyncio
import time
from collections import defaultdict


class ChatPacer:
    """One shared limiter per destination for summaries, controls and source cards."""

    def __init__(self, bot, *, interval=3.2, clock=time.monotonic, sleep=asyncio.sleep):
        self.bot, self.interval, self.clock, self.sleep = bot, interval, clock, sleep
        self.locks = defaultdict(asyncio.Lock)
        self.next_send = defaultdict(float)
        self.on_message = None

    def __getattr__(self, name):
        return getattr(self.bot, name)

    async def send_message(self, *args, **kwargs):
        return await self._send(self.bot.send_message, args, kwargs)

    async def send_photo(self, *args, **kwargs):
        return await self._send(self.bot.send_photo, args, kwargs)

    async def _send(self, method, args, kwargs):
        chat = str(kwargs.get("chat_id", args[0] if args else ""))
        async with self.locks[chat]:
            wait = self.next_send[chat] - self.clock()
            if wait > 0:
                await self.sleep(wait)
            try:
                message = await method(*args, **kwargs)
                if self.on_message is not None:
                    self.on_message(chat, message.message_id)
                return message
            finally:
                self.next_send[chat] = self.clock() + self.interval
