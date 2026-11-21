import re
from datetime import datetime, timedelta
from urllib.parse import urljoin, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from ..core.models import Listing
from ..settings import safe_image_url, validate_site_url

BERLIN = ZoneInfo("Europe/Berlin")


def parse_published(text: str, now: datetime | None = None) -> str | None:
    now = (now or datetime.now(BERLIN)).astimezone(BERLIN)
    value = text.strip()
    relative = re.fullmatch(r"(Heute|Gestern),?\s+(\d{1,2}):(\d{2})", value, re.I)
    if relative:
        day = now.date() - timedelta(days=int(relative[1].casefold() == "gestern"))
        hour, minute = int(relative[2]), int(relative[3])
        try:
            return datetime(day.year, day.month, day.day, hour, minute, tzinfo=BERLIN).isoformat()
        except ValueError:
            return None
    try:
        # Date-only source precision stays date-only: no fabricated midnight timestamp.
        return datetime.strptime(value, "%d.%m.%Y").date().isoformat()
    except ValueError:
        return None


def parse_card(
    raw: dict, profile: str, now: datetime | None = None, *, city: str | None = None
) -> Listing:
    url = urljoin("https://www.kleinanzeigen.de", raw["href"])
    validate_site_url(url)
    parsed = urlsplit(url)
    match = re.search(r"/(\d+)-\d+-\d+/?$", parsed.path)
    if not parsed.path.startswith("/s-anzeige/") or not match:
        raise ValueError("Unrecognized listing URL")
    canonical = urlunsplit(("https", "www.kleinanzeigen.de", parsed.path, "", ""))
    title = raw.get("title", "").strip()
    if not title:
        raise ValueError("Listing has no title")
    price = re.search(r"(\d[\d. ]*(?:,\d{1,2})?)\s*€", raw.get("price", ""))
    details = raw.get("details", "")
    rooms = re.search(r"(\d+(?:[,.]\d+)?)\s*(?:Zi\.?|Zimmer)\b", details)
    size = re.search(r"(\d+(?:[,.]\d+)?)\s*(?:m²|m2|qm)", details)
    return Listing(
        id=match[1],
        url=canonical,
        title=title,
        price_eur=price[1].replace(".", "").replace(" ", "").replace(",", ".") if price else None,
        rooms=rooms[1].replace(",", ".") if rooms else None,
        size_sqm=size[1].replace(",", ".") if size else None,
        published_at=parse_published(raw.get("date", ""), now),
        profile=profile,
        city=city,
        is_request=bool(re.search(r"\bGesuch\b", details, re.I)),
        image_url=safe_image_url(raw.get("image")),
    )
