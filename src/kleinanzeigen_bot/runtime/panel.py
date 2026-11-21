import asyncio

from telegram.error import BadRequest

from .controls import keyboard, status_text


class ControlPanel:
    """One persistent inline panel; ordinary notifications have no global keyboard."""

    def __init__(self, queue, bot, chat_id):
        self.queue, self.bot, self.chat_id = queue, bot, str(chat_id)
        self.lock = asyncio.Lock()

    def note_message(self, chat_id, message_id):
        if str(chat_id) == self.chat_id and isinstance(message_id, int):
            self.queue.set(
                "last_chat_message_id", max(message_id, self.queue.get("last_chat_message_id", 0))
            )

    async def retire(self, message_id):
        if not message_id:
            return
        try:
            await self.bot.edit_message_reply_markup(
                chat_id=self.chat_id, message_id=message_id, reply_markup=None
            )
        except BadRequest as exc:
            # Deleted messages and keyboards already removed are harmless.
            if not any(
                s in str(exc).lower()
                for s in (
                    "message is not modified",
                    "message to edit not found",
                    "message can't be edited",
                )
            ):
                raise

    async def refresh(self, notice="", clicked_id=None):
        async with self.lock:
            panel_id = self.queue.get("panel_message_id")
            if clicked_id and clicked_id != panel_id:
                await self.retire(clicked_id)
            text = status_text(self.queue)
            if notice:
                text += "\n\n" + notice
            if panel_id and panel_id == self.queue.get("last_chat_message_id"):
                try:
                    await self.bot.edit_message_text(
                        chat_id=self.chat_id,
                        message_id=panel_id,
                        text=text,
                        reply_markup=keyboard(),
                    )
                    return
                except BadRequest as exc:
                    if "message is not modified" in str(exc).lower():
                        return
                    if "message to edit not found" not in str(exc).lower():
                        raise
            await self.retire(panel_id)
            message = await self.bot.send_message(
                chat_id=self.chat_id, text=text, reply_markup=keyboard()
            )
            if isinstance(message.message_id, int):
                self.queue.set("panel_message_id", message.message_id)
                self.note_message(self.chat_id, message.message_id)
