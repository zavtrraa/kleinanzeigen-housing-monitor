import random
from string import Formatter

from ..core.models import Listing


class TextVariator:
    def __init__(self, template: str, greetings: list[str], closings: list[str], signature: str):
        allowed = {"greeting", "closing", "signature", "title", "url", "price_eur"}
        fields = set()
        for _, name, spec, conversion in Formatter().parse(template):
            if name is not None:
                if name not in allowed or spec or conversion:
                    raise ValueError(f"Unsupported template field: {name}")
                fields.add(name)
        if not greetings or not closings or any(not s.strip() for s in greetings + closings):
            raise ValueError("Message variants must not be empty")
        self.template, self.greetings, self.closings = template, greetings, closings
        self.signature = signature.strip()

    def render(self, listing: Listing) -> str:
        text = self.template.format(
            greeting=random.choice(self.greetings),
            closing=random.choice(self.closings),
            signature=self.signature,
            title=listing.title,
            url=listing.url,
            price_eur=listing.price_eur or "",
        ).strip()
        if not text or len(text) > 4000:
            raise ValueError("Message must contain 1–4000 characters")
        return text
