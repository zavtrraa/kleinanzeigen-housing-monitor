import asyncio
import re
import time
from urllib.parse import urljoin

from ..core.diagnostics import capture_failure
from ..core.exceptions import SelectorNotFound
from ..core.ui import any_visible, ensure_access, visible_matches
from ..settings import Profiles, Runtime, validate_site_url
from .extractors import parse_card


class PlaywrightSearchClient:
    def __init__(
        self,
        context,
        profiles: Profiles,
        runtime: Runtime,
        diagnostics_dir=None,
        pause_between_profiles=None,
        events=None,
    ):
        self.context, self.profiles, self.runtime = context, profiles, runtime
        self.diagnostics_dir, self.pause_between_profiles = diagnostics_dir, pause_between_profiles
        self.events = events
        self.profile_stats = []

    async def search(self):
        found, searched = {}, False
        self.profile_stats = []
        for profile in self.profiles.profiles:
            if not profile.enabled:
                continue
            url, visited, local_ids = profile.url, set(), set()
            stats = {"profile": profile.name, "pages": 0, "found": 0, "limited": False}
            for number in range(1, self.runtime.max_pages_per_profile + 1):
                validate_site_url(url)
                if url in visited:
                    raise SelectorNotFound(
                        "Pagination returned an already visited URL", stage="pagination"
                    )
                visited.add(url)
                if searched and self.pause_between_profiles is not None:
                    await self.pause_between_profiles()
                searched = True
                page = await self.context.new_page()
                try:
                    if self.events:
                        self.events.emit(
                            f"Searching {profile.name}, page {number}",
                            "search.search",
                            f"--page={number} --newest",
                            url,
                        )
                    response = await page.goto(
                        url, wait_until="domcontentloaded", timeout=self.runtime.timeout_ms
                    )
                    await ensure_access(page, self.runtime)
                    if response is not None and response.status >= 400:
                        raise SelectorNotFound(
                            f"Search HTTP status {response.status}", stage="search"
                        )
                    deadline = time.monotonic() + self.runtime.timeout_ms / 1000
                    cards = page.locator(self.runtime.search.cards)
                    while time.monotonic() < deadline:
                        await ensure_access(page, self.runtime)
                        if await cards.count() or await any_visible(
                            page, self.runtime.search.empty
                        ):
                            break
                        await asyncio.sleep(0.1)
                    count = await cards.count()
                    empty = await any_visible(page, self.runtime.search.empty)
                    if not count and not empty:
                        raise SelectorNotFound(
                            "Neither listings nor an explicit empty-result marker found",
                            stage="search",
                        )
                    for index in range(count):
                        if len(local_ids) >= self.runtime.max_listings_per_profile:
                            stats["limited"] = True
                            break
                        raw = await cards.nth(index).evaluate(
                            """(card, s) => {
                                const text = selector =>
                                    card.querySelector(selector)?.textContent?.trim() || '';
                                return {
                                    href: card.getAttribute('data-href') ||
                                        card.querySelector(s.link)?.getAttribute('href') || '',
                                    title: text(s.title), price: text(s.price),
                                    details: text(s.details), date: text(s.date),
                                    image: card.querySelector('img')?.currentSrc ||
                                        card.querySelector('img')?.getAttribute('data-src') ||
                                        card.querySelector('img')?.getAttribute('src') || ''
                                };
                            }""",
                            self.runtime.search.model_dump(exclude={"empty", "next_page"}),
                        )
                        listing = parse_card(raw, profile.name, city=profile.city)
                        found.setdefault(listing.id, listing)
                        local_ids.add(listing.id)
                    stats["pages"] = number
                    stats["found"] = len(local_ids)
                    headers = page.locator("h1")
                    if await headers.count():
                        text = await headers.first.inner_text()
                        total = re.search(r"\bvon\s+([\d.]+)", text)
                        if total:
                            stats["site_total"] = int(total[1].replace(".", ""))
                    next_urls = set()
                    if not empty:
                        async for link in visible_matches(page, self.runtime.search.next_page):
                            href = await link.get_attribute("href")
                            if href:
                                next_urls.add(urljoin(page.url, href))
                    if len(next_urls) > 1:
                        raise SelectorNotFound("Conflicting next-page URLs", stage="pagination")
                    if next_urls:
                        if (
                            number >= self.runtime.max_pages_per_profile
                            or len(local_ids) >= self.runtime.max_listings_per_profile
                        ):
                            stats["limited"] = True
                            break
                        url = next_urls.pop()
                    else:
                        break
                except Exception as exc:
                    diagnostic = await capture_failure(
                        page,
                        self.diagnostics_dir,
                        exc,
                        stage="search",
                        extra={
                            "profile": profile.name,
                            "page": number,
                            "card_selector": self.runtime.search.cards,
                        },
                    )
                    if isinstance(exc, SelectorNotFound):
                        exc.diagnostic_path = diagnostic
                    raise
                finally:
                    await page.close()
            self.profile_stats.append(stats)
        return list(found.values())
