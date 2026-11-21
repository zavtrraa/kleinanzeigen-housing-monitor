"""Validated search preferences shared by Telegram, the worker and the demo."""

import json
import sqlite3
import time
import unicodedata
from decimal import Decimal
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..settings import Profiles, SearchProfile

CITIES = {
    "hannover": ("Hannover", 3155),
    "laatzen": ("Laatzen", 2892),
    "langenhagen": ("Langenhagen", 3041),
    "garbsen": ("Garbsen", 3067),
    "seelze": ("Seelze", 2920),
}


class SearchPreferences(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    cities: tuple[str, ...] = tuple(CITIES)
    query: str = Field(default="", max_length=120)
    max_rent: Decimal = Field(default=Decimal("520"), gt=0, le=10000, decimal_places=2)
    min_area: Decimal = Field(default=Decimal("20"), ge=0, le=1000, decimal_places=2)
    auto_reply: bool = False

    @field_validator("cities", mode="before")
    @classmethod
    def validate_cities(cls, value):
        if isinstance(value, str):
            value = value.split(",")
        cities = tuple(dict.fromkeys(city.strip().casefold() for city in value))
        if not cities or any(city not in CITIES for city in cities):
            raise ValueError("Choose cities from: " + ", ".join(CITIES))
        return cities

    @field_validator("query")
    @classmethod
    def validate_query(cls, value):
        if any(unicodedata.category(char).startswith("C") for char in value):
            raise ValueError("Query must not contain control characters")
        return " ".join(value.split())


class PreferenceStore:
    """Persist values and their update watermark in one SQLite metadata write."""

    def __init__(self, queue, defaults: SearchPreferences | None = None):
        self.queue = queue
        self.defaults = defaults or SearchPreferences()

    def load(self) -> SearchPreferences:
        record = self.queue.get("search_preferences", {})
        return SearchPreferences.model_validate(
            record.get("values", self.defaults.model_dump(mode="json"))
        )

    def update(self, *, update_id: int | None = None, **changes) -> bool:
        record = self.queue.get("search_preferences", {})
        last_update_id = record.get("last_update_id", -1)
        last_updated_at = record.get("last_updated_at")
        is_recent = (
            last_updated_at is not None
            and time.time() - float(last_updated_at) < 7 * 24 * 60 * 60
        )
        if update_id is not None and update_id <= last_update_id and is_recent:
            return False
        values = self.load().model_dump(mode="json") | changes
        validated = SearchPreferences.model_validate(values)
        self.queue.set(
            "search_preferences",
            {
                "values": validated.model_dump(mode="json"),
                "last_update_id": update_id if update_id is not None else last_update_id,
                "last_updated_at": time.time(),
            },
        )
        return True


def read_preferences(path, defaults: SearchPreferences | None = None) -> SearchPreferences:
    defaults = defaults or SearchPreferences()
    if not path.is_file():
        return defaults
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        row = connection.execute("SELECT value FROM meta WHERE key='search_preferences'").fetchone()
        return SearchPreferences.model_validate(json.loads(row[0])["values"]) if row else defaults
    finally:
        connection.close()


def build_profiles(preferences: SearchPreferences) -> Profiles:
    profiles = []
    query = quote(preferences.query, safe="")
    rent = format(preferences.max_rent.normalize(), "f")
    area = format(preferences.min_area.normalize(), "f")
    for city in preferences.cities:
        location_id = CITIES[city][1]
        query_path = f"{query}/k0" if query else ""
        url = (
            f"https://www.kleinanzeigen.de/s-wohnung-mieten/{city}/"
            f"sortierung:neuste/anzeige:angebote/preis::{rent}/"
            f"{query_path}c203l{location_id}+wohnung_mieten.qm_d:{area}%2C"
        )
        profiles.append(SearchProfile(name=city, city=city, url=url))
    return Profiles(profiles=profiles)


def settings_text(preferences: SearchPreferences) -> str:
    cities = ", ".join(CITIES[city][0] for city in preferences.cities)
    return (
        "Search settings\n"
        f"Cities: {cities}\n"
        f"Query: {preferences.query or 'Any apartment'}\n"
        f"Maximum rent: EUR {preferences.max_rent:,.2f}\n"
        f"Minimum area: {preferences.min_area:g} m²\n"
        f"Automatic replies: {'On' if preferences.auto_reply else 'Off'}\n\n"
        "Change with /city Hannover,Garbsen, /search Balkon, /budget 600 or /area 30.\n"
        "Use /search - to clear the query, /replies on|off to change replies, "
        "and /template to inspect the message.\n"
        "Changes apply to the next run. Listed rent may exclude service or heating costs."
    )
