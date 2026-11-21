from types import SimpleNamespace

from telegram.error import BadRequest


class DemoTransport:
    """Capture Telegram payloads locally; this transport has no network client."""

    def __init__(self, queue):
        self.queue = queue
        self.messages = queue.get("demo_messages", [])
        self.on_message = None

    def _save(self):
        self.messages = self.messages[-120:]
        self.queue.set("demo_messages", self.messages)

    def _append(self, payload, kind):
        ident = self.queue.get("demo_message_id", 0) + 1
        self.queue.set("demo_message_id", ident)
        item = {"message_id": ident, "kind": kind, **payload}
        markup = item.get("reply_markup")
        if markup is not None and hasattr(markup, "to_dict"):
            item["reply_markup"] = markup.to_dict()
        self.messages.append(item)
        self._save()
        if self.on_message:
            self.on_message(str(payload.get("chat_id", "-1")), ident)
        return SimpleNamespace(message_id=ident)

    async def send_message(self, **payload):
        return self._append(payload, "message")

    async def send_photo(self, **payload):
        # Synthetic source images have one local illustration in this demo.
        return self._append({**payload, "demo_image": "/assets/apartment.svg"}, "photo")

    async def edit_message_text(self, *, message_id, **payload):
        for item in self.messages:
            if item["message_id"] == message_id:
                markup = payload.get("reply_markup")
                item.update(payload)
                if markup is not None:
                    item["reply_markup"] = markup.to_dict()
                self._save()
                return
        raise BadRequest("Message to edit not found")

    async def edit_message_reply_markup(self, *, message_id, reply_markup, **_):
        for item in self.messages:
            if item["message_id"] == message_id:
                item["reply_markup"] = reply_markup.to_dict() if reply_markup else None
                self._save()
                return
        raise BadRequest("Message to edit not found")

    def user_message(self, text):
        return self._append({"chat_id": "-1", "text": text}, "user")
