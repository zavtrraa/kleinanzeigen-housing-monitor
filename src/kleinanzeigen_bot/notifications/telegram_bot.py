import asyncio
import logging

from telegram.error import BadRequest

from ..core.models import CycleReport
from ..settings import safe_image_url
from .cards import listing_card
from .formatters import format_summary, summary_parts

log = logging.getLogger(__name__)


async def send_connection_test(bot, chat_id: str) -> dict:
    """Explicit CLI-only write action; never called by read-only diagnostics."""
    message = await bot.send_message(
        chat_id=chat_id,
        text="Housing bot: connection test requested with --telegram-test.",
        parse_mode=None,
    )
    return {"delivery_tested": True, "message_id": message.message_id}


async def inspect_connection(bot, chat_id: str) -> dict:
    """Read-only diagnostics; never sends, consumes updates, or changes webhook."""
    me = await bot.get_me()
    chat = await bot.get_chat(chat_id)
    webhook = await bot.get_webhook_info()
    return {
        "bot_authenticated": bool(me.is_bot),
        "bot_username": me.username,
        "configured_chat_accessible": True,
        "chat_type": str(chat.type),
        "webhook_configured": bool(webhook.url),
        "delivery_tested": False,
        "housing_commands_and_buttons_implemented": True,
        "housing_runner_active_verified": False,
    }


class ConsoleNotifier:
    async def listing(self, listing, decision) -> None:
        text, _ = listing_card(listing, decision)
        print(text + "\n" + listing.url)

    async def summary(self, report: CycleReport) -> None:
        print(format_summary(report))

    async def alert(self, message: str) -> None:
        log.warning("alert: %s", message)


class TelegramNotifier:
    def __init__(self, bot, chat_id: str, *, console: bool = False, rich=False, keyboard=None):
        self.bot, self.chat_id, self.console = bot, chat_id, console
        self.rich, self.keyboard = rich, keyboard

    async def listing(self, listing, decision) -> None:
        text, keyboard = listing_card(listing, decision)
        if photo := safe_image_url(listing.image_url):
            try:
                await self.bot.send_photo(
                    chat_id=self.chat_id,
                    photo=photo,
                    caption=text,
                    reply_markup=keyboard,
                    parse_mode=None,
                )
                return
            except BadRequest:
                # A removed source image must not hide an otherwise usable listing.
                pass
        await self.bot.send_message(
            chat_id=self.chat_id,
            text=text,
            reply_markup=keyboard,
            parse_mode=None,
            disable_web_page_preview=True,
        )

    async def _send(self, text: str) -> None:
        # Telegram counts UTF-16 code units. Split conservatively below 4096.
        chunks, chunk, units = [], "", 0
        for char in text:
            size = len(char.encode("utf-16-le")) // 2
            if units + size > 3800:
                chunks.append(chunk)
                chunk, units = "", 0
            chunk += char
            units += size
        if chunk:
            chunks.append(chunk)
        for part in chunks:
            await self.bot.send_message(
                chat_id=self.chat_id,
                text=part,
                parse_mode=None,
                disable_web_page_preview=True,
            )

    async def summary(self, report: CycleReport) -> None:
        text = format_summary(report)
        if self.console:
            print(text, flush=True)
        if self.rich:
            for index, part in enumerate(summary_parts(report)):
                if index:
                    await asyncio.sleep(3.2)
                await self.bot.send_message(
                    chat_id=self.chat_id,
                    text=part,
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )
        else:
            await self._send(text)

    async def alert(self, message: str) -> None:
        text = "Housing monitor | Review required\n" + message
        if self.console:
            print(text, flush=True)
        await self._send(text)
