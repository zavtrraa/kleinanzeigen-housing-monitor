from typing import Protocol

from .models import CycleReport, Listing


class SearchClient(Protocol):
    async def search(self) -> list[Listing]: ...


class PreparedMessage(Protocol):
    async def submit(self) -> None:
        """One click, then positive confirmation; ambiguity must raise."""
        ...

    async def close(self) -> None: ...


class MessageSender(Protocol):
    async def prepare(self, listing: Listing, text: str) -> PreparedMessage: ...


class TextBuilder(Protocol):
    def render(self, listing: Listing) -> str: ...


class Notifier(Protocol):
    async def summary(self, report: CycleReport) -> None: ...
    async def alert(self, message: str) -> None: ...
