import hashlib
from dataclasses import asdict, dataclass, field
from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


@dataclass(frozen=True)
class Candidate:
    source: str
    external_id: str
    url: str
    title: str
    city: str | None = None
    address: str = ""
    size_sqm: str | None = None
    rooms: str | None = None
    warm_eur: str | None = None
    description: str = ""
    available_from: str | None = None
    wbs_required: bool | None = None
    kind: str = "apartment"

    @property
    def key(self) -> str:
        return f"{self.source}:{self.external_id}"

    @property
    def short_id(self) -> str:
        return hashlib.sha256(self.key.encode()).hexdigest()[:20]

    def payload(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class Decision:
    outcome: Literal["eligible", "review", "excluded"]
    reasons: tuple[str, ...] = ()

    def payload(self) -> dict:
        return {"outcome": self.outcome, "reasons": list(self.reasons)}


class HousingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    city: str = "Hannover"
    allowed_cities: list[str] = Field(default_factory=list)
    min_size_sqm: Decimal = Field(Decimal("20"), gt=0)
    max_warm_eur: Decimal = Field(Decimal("520"), gt=0)
    move_in_from: date = date(2027, 2, 1)
    wbs_status: Literal["unknown", "yes", "no"] = "unknown"
    notify_review: bool = True
    feed_interval_seconds: int = Field(1800, ge=300)
    gmail_interval_seconds: int = Field(60, ge=30)
    gmail_folder: str = "Housing"
    gmail_initial_messages: int = Field(30, ge=1, le=100)
    max_pages_per_source: int = Field(3, ge=1, le=5)
    request_timeout_seconds: int = Field(25, ge=5, le=60)
    hanova_enabled: bool = True
    heimkehr_enabled: bool = True
    gmail_enabled: bool = True


@dataclass
class SourceResult:
    candidates: list[Candidate] = field(default_factory=list)
    checkpoint: dict | None = None
    details: dict = field(default_factory=dict)


class SourceBlocked(Exception):
    pass


class SourceRateLimited(Exception):
    def __init__(self, retry_after: int = 1800):
        self.retry_after = max(60, retry_after)
        super().__init__("Source requested a cooldown")
