from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum


class ListingStatus(StrEnum):
    DISCOVERED = "discovered"
    SENDING = "sending"
    SENT = "sent"
    UNCERTAIN = "uncertain"


@dataclass(frozen=True)
class Listing:
    id: str
    url: str
    title: str
    price_eur: str | None = None
    rooms: str | None = None
    size_sqm: str | None = None
    published_at: str | None = None
    profile: str = ""
    city: str | None = None
    is_request: bool = False
    image_url: str | None = None
    description: str = ""
    cost_details: str = ""


@dataclass
class Preview:
    listing_id: str
    url: str
    text: str


@dataclass
class SkippedListing:
    listing_id: str
    url: str
    outcome: str
    reason: str


@dataclass
class RunItem:
    listing_id: str
    title: str
    url: str
    status: str = "not_checked"
    note: str = ""
    warnings: list[str] = field(default_factory=list)
    change_kind: str = ""


@dataclass
class CycleReport:
    id: str
    dry_run: bool
    found: int = 0
    search_completed: bool = False
    diagnostics: list[str] = field(default_factory=list)
    new: int = 0
    changed: int = 0
    unchanged: int = 0
    cached: int = 0
    preparation_failed: int = 0
    sources: list[dict] = field(default_factory=list)
    sent: int = 0
    uncertain: int = 0
    recovered: int = 0
    previews: list[Preview] = field(default_factory=list)
    skipped: list[SkippedListing] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    halted: bool = False
    started_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    finished_at: str | None = None
    trigger: str = "once"
    request_id: str = ""
    items: list[RunItem] = field(default_factory=list)
    monitoring: bool = False
    matched: int = 0
    notified: int = 0
    excluded: int = 0
    review: int = 0
    inspection_failed: int = 0

    def to_dict(self) -> dict:
        return asdict(self)
