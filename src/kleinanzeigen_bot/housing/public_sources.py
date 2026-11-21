import asyncio
import base64
import json
import re
from decimal import Decimal
from urllib.parse import urlsplit

import httpx
from bs4 import BeautifulSoup

from .models import Candidate, SourceBlocked, SourceRateLimited, SourceResult

HEIMKEHR_URL = "https://heimkehr-hannover.de/wohnungsangebote"
HANOVA_URL = "https://hanova.de/mieten/wohnungs-objektsuche/"


def amount(value) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return str(Decimal(str(value)))
    cleaned = str(value).replace("\u00a0", "").replace(" ", "")
    if "," in cleaned:
        cleaned = cleaned.replace(".", "").replace(",", ".")
    parsed = Decimal(cleaned)
    return str(parsed) if parsed.is_finite() else None


async def fetch_public(url: str, *, timeout: int = 25, etag: str | None = None):
    allowed = {"heimkehr-hannover.de", "hanova.de", "homepage.immomio.com"}
    headers = {"If-None-Match": etag} if etag else {}
    async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
        for _ in range(4):
            parsed = urlsplit(url)
            if (
                parsed.scheme != "https"
                or parsed.hostname not in allowed
                or parsed.port not in (None, 443)
            ):
                raise ValueError("Unexpected public source URL")
            response = await client.get(url, headers=headers)
            if response.status_code in (401, 403):
                raise SourceBlocked("Public source denied access")
            if response.status_code == 429:
                retry = response.headers.get("retry-after", "1800")
                raise SourceRateLimited(int(retry) if retry.isdigit() else 1800)
            if response.status_code in (301, 302, 303, 307, 308):
                from urllib.parse import urljoin

                url = urljoin(url, response.headers["location"])
                continue
            if response.status_code != 304:
                response.raise_for_status()
            return response
    raise ValueError("Too many public source redirects")


def parse_heimkehr(html: str) -> list[Candidate]:
    match = re.search(r"\b(?:var|const|let)\s+data\s*=\s*(?=\[)", html)
    if not match:
        raise ValueError("Heimkehr listing data missing; not a confirmed empty result")
    rows, _ = json.JSONDecoder().raw_decode(html[match.end() :])
    if not isinstance(rows, list):
        raise ValueError("Unexpected Heimkehr data shape")
    result = []
    for row in rows:
        external_id = str(row["linkurl"])
        if not external_id.isdigit():
            raise ValueError("Unexpected Heimkehr property ID")
        result.append(
            Candidate(
                source="heimkehr",
                external_id=external_id,
                url=f"https://heimkehr-hannover.de/wohnungsangebote/wohnung-id/{external_id}",
                title=row["titel"],
                city=row.get("ort"),
                address=" ".join(
                    filter(None, [row.get("strasse"), row.get("plz"), row.get("ort")])
                ),
                size_sqm=amount(row.get("wohnflaeche_num", row.get("wohnflaeche"))),
                rooms=amount(row.get("zimmer")),
                warm_eur=amount(row.get("warmmiete_num", row.get("warmmiete"))),
            )
        )
    return result


def hanova_embed(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    block = soup.select_one('[data-borlabs-cookie-content-blocker-id="immomio"]')
    if block is None:
        raise ValueError("Hanova public widget missing")
    decoded = base64.b64decode(block["data-borlabs-cookie-content"], validate=True).decode()
    iframe = BeautifulSoup(decoded, "html.parser").find("iframe")
    if not iframe:
        raise ValueError("Hanova public iframe missing")
    url = iframe["src"]
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname != "homepage.immomio.com":
        raise ValueError("Unexpected Hanova widget origin")
    return url


def parse_hanova(payload: dict) -> list[Candidate]:
    listing = payload.get("data", {}).get("propertyList")
    if not isinstance(listing, dict) or not isinstance(listing.get("nodes"), list):
        raise ValueError("Hanova listing response missing")
    result = []
    for row in listing["nodes"]:
        external_id = str(row["objectId"])
        url = row["applicationLink"]
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or parsed.hostname != "tenant.immomio.com"
            or not parsed.path.startswith("/apply/")
        ):
            raise ValueError("Unexpected Hanova application URL")
        address = row.get("address") or {}
        available = row.get("availableFrom") or {}
        result.append(
            Candidate(
                source="hanova",
                external_id=external_id,
                url=url,
                title=row["name"],
                city=address.get("city"),
                address=" ".join(
                    str(value)
                    for value in (
                        address.get("street"),
                        address.get("houseNumber"),
                        address.get("zipCode"),
                        address.get("city"),
                        address.get("district"),
                    )
                    if value
                ),
                size_sqm=amount(row.get("size")),
                rooms=amount(row.get("totalRooms")),
                warm_eur=amount(row.get("totalRentGross")),
                available_from=available.get("dateAvailable"),
                wbs_required=row.get("wbs"),
                kind="apartment"
                if row.get("propertyType") == "FLAT" and row.get("marketingType") == "RENT"
                else "other",
            )
        )
    return result


class HeimkehrSource:
    name = "heimkehr"

    def __init__(self, config):
        self.config = config
        self.interval = config.feed_interval_seconds

    async def collect(self, checkpoint=None):
        checkpoint = checkpoint or {}
        response = await fetch_public(
            HEIMKEHR_URL,
            timeout=self.config.request_timeout_seconds,
            etag=checkpoint.get("etag"),
        )
        if response.status_code == 304:
            return SourceResult(checkpoint=checkpoint, details={"unchanged": True})
        candidates = parse_heimkehr(response.text)
        return SourceResult(
            candidates, {"etag": response.headers.get("etag")}, {"count": len(candidates)}
        )


class HanovaSource:
    name = "hanova"

    def __init__(self, config, browser_factory):
        self.config, self.browser_factory = config, browser_factory
        self.interval = config.feed_interval_seconds

    async def collect(self, checkpoint=None):
        response = await fetch_public(HANOVA_URL, timeout=self.config.request_timeout_seconds)
        url = hanova_embed(response.text)
        found, pages, response_errors = {}, {}, []
        tasks = set()
        async with self.browser_factory.open() as context:
            page = await context.new_page()

            async def consume(response):
                host = urlsplit(response.url).hostname or ""
                if not (host == "immomio.com" or host.endswith(".immomio.com")):
                    return
                if response.status in (401, 403):
                    response_errors.append(SourceBlocked("Hanova widget denied access"))
                    return
                if response.status == 429:
                    response_errors.append(SourceRateLimited())
                    return
                if "application/json" not in response.headers.get("content-type", ""):
                    return
                try:
                    data = await response.json()
                except Exception:
                    return
                listing = (
                    data.get("data", {}).get("propertyList") if isinstance(data, dict) else None
                )
                if listing is None:
                    return
                for candidate in parse_hanova(data):
                    found[candidate.key] = candidate
                pagination = listing["page"]
                pages[int(pagination["page"])] = pagination

            def on_response(response):
                task = asyncio.create_task(consume(response))
                tasks.add(task)

                def finished(completed):
                    tasks.discard(completed)
                    if not completed.cancelled() and completed.exception() is not None:
                        response_errors.append(completed.exception())

                task.add_done_callback(finished)

            page.on("response", on_response)
            try:
                navigation = await page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=self.config.request_timeout_seconds * 1000,
                )
                if navigation and navigation.status in (401, 403):
                    raise SourceBlocked("Hanova widget denied access")
                for _ in range(self.config.max_pages_per_source):
                    for _ in range(50):
                        if pages or response_errors:
                            break
                        await asyncio.sleep(0.1)
                    if tasks:
                        await asyncio.gather(*tasks)
                    if response_errors:
                        raise response_errors[0]
                    if not pages:
                        raise ValueError("Hanova widget returned no listing data")
                    latest = pages[max(pages)]
                    if not latest.get("hasNext"):
                        break
                    previous = len(pages)
                    await page.mouse.move(600, 400)
                    await page.mouse.wheel(0, 10000)
                    for _ in range(40):
                        if len(pages) > previous or response_errors:
                            break
                        await asyncio.sleep(0.1)
                if tasks:
                    await asyncio.gather(*tasks)
                if response_errors:
                    raise response_errors[0]
                latest = pages[max(pages)]
                if latest.get("hasNext"):
                    raise ValueError(
                        "Hanova pagination incomplete; refusing an incomplete inventory"
                    )
            finally:
                await page.close()
        return SourceResult(
            list(found.values()), details={"count": len(found), "pages": len(pages)}
        )
