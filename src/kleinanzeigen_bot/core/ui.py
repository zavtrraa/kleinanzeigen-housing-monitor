import asyncio
import re
import time
from urllib.parse import urlsplit

from ..settings import Runtime, Selector
from .exceptions import AuthError, ChallengeRequired, IPRangeBlocked, SelectorNotFound


def locator_for(page, spec: Selector):
    value = re.compile(spec.value, re.IGNORECASE) if spec.regex else spec.value
    if spec.kind == "role":
        return page.get_by_role(spec.role, name=value, exact=spec.exact)
    if spec.kind == "text":
        return page.get_by_text(value, exact=spec.exact)
    return page.locator(spec.value)


async def visible_matches(page, specs: list[Selector]):
    for spec in specs:
        locator = locator_for(page, spec)
        for index in range(await locator.count()):
            item = locator.nth(index)
            if await item.is_visible():
                yield item


async def any_visible(page, specs: list[Selector]) -> bool:
    async for _ in visible_matches(page, specs):
        return True
    return False


IP_RANGE_BLOCKED = re.compile(
    r"^\s*IP[-\s]?Bereich\b.{0,60}\b(?:gesperrt|eingeschränkt)\b[.!]?\s*$",
    re.IGNORECASE,
)


async def ensure_not_ip_blocked(page) -> str:
    title = (await page.title()).strip()
    if IP_RANGE_BLOCKED.search(title):
        raise IPRangeBlocked(
            "Kleinanzeigen temporarily blocked this IP range; wait and check manually"
        )
    warning = page.get_by_text(IP_RANGE_BLOCKED)
    for index in range(await warning.count()):
        if await warning.nth(index).is_visible():
            raise IPRangeBlocked(
                "Kleinanzeigen temporarily blocked this IP range; wait and check manually"
            )
    return title.casefold()


async def ensure_access(page, runtime: Runtime) -> None:
    title = await ensure_not_ip_blocked(page)
    if await any_visible(page, runtime.message.challenge):
        raise ChallengeRequired("Challenge requires manual action")
    if any(
        text in title for text in ("just a moment", "security verification", "zugriff verweigert")
    ):
        raise ChallengeRequired("Access check requires manual action")
    parsed = urlsplit(page.url)
    if (
        "login" in (parsed.hostname or "")
        or re.search(r"/(?:m-einloggen|einloggen|login)(?:[/.]|$)", parsed.path)
        or await any_visible(page, runtime.message.auth)
    ):
        raise AuthError("Manual sign-in required")
    if parsed.scheme != "https" or parsed.hostname not in {
        "www.kleinanzeigen.de",
        "kleinanzeigen.de",
    }:
        raise AuthError("Unexpected navigation destination")


async def select_one(page, specs: list[Selector], runtime: Runtime, label: str):
    deadline = time.monotonic() + runtime.timeout_ms / 1000
    while time.monotonic() < deadline:
        await ensure_access(page, runtime)
        for spec in specs:
            items = []
            async for item in visible_matches(page, [spec]):
                items.append(item)
            if len(items) > 1:
                raise SelectorNotFound(f"Ambiguous selector for {label}", stage=label)
            if items:
                return items[0]
        await asyncio.sleep(0.1)
    raise SelectorNotFound(f"No visible selector for {label}", stage=label)
