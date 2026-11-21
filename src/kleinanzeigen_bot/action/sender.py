import asyncio
import time
from dataclasses import replace
from decimal import Decimal
from urllib.parse import urlsplit

from ..core.diagnostics import capture_failure
from ..core.exceptions import ListingFiltered, SelectorNotFound, SendUncertain
from ..core.matching import match_listing
from ..core.ui import any_visible, ensure_access, select_one, visible_matches
from ..settings import Runtime, validate_site_url


class UIPreparedMessage:
    def __init__(
        self,
        page,
        submit_button,
        runtime: Runtime,
        *,
        field,
        text,
        listing_url,
        events=None,
        diagnostics_dir=None,
    ):
        self.page, self.submit_button, self.runtime = page, submit_button, runtime
        self.field, self.text, self.listing_url = field, text, listing_url
        self.attempted = False
        self.events = events
        self.diagnostics_dir = diagnostics_dir

    async def submit(self) -> None:
        if self.attempted:
            raise SendUncertain("A prepared message cannot be submitted twice")
        self.attempted = True
        await ensure_access(self.page, self.runtime)
        if urlsplit(self.page.url).path.rstrip("/") != urlsplit(self.listing_url).path.rstrip("/"):
            raise SendUncertain("Listing changed after preparation; refusing to send")
        if await self.field.input_value() != self.text:
            raise SendUncertain("Message changed after preparation; refusing to send")
        if not await self.submit_button.is_enabled():
            raise SendUncertain("Submit disabled after preparation; refusing to send")
        if await any_visible(self.page, self.runtime.message.success):
            raise SendUncertain("Success marker was visible before sending")
        # No retries around this click. Even a click timeout can follow successful delivery.
        if self.events:
            self.events.emit(
                "Submitting message once",
                "UIPreparedMessage.submit",
                "--single-click --no-retry",
                "submit_button.click",
            )
        try:
            await self.submit_button.click(timeout=self.runtime.timeout_ms)
        except Exception as exc:
            exc.diagnostic_path = await capture_failure(
                self.page,
                self.diagnostics_dir,
                exc,
                stage="submit",
                extra={"click_attempted": True},
            )
            raise
        deadline = time.monotonic() + self.runtime.confirmation_timeout_ms / 1000
        while time.monotonic() < deadline:
            await ensure_access(self.page, self.runtime)
            if await any_visible(self.page, self.runtime.message.success):
                if self.events:
                    self.events.emit(
                        "Site confirmed delivery",
                        "UIPreparedMessage.submit",
                        "--confirmed",
                        "success marker visible",
                    )
                return
            await asyncio.sleep(0.1)
        error = SendUncertain("No fresh positive UI confirmation after submit")
        error.diagnostic_path = await capture_failure(
            self.page,
            self.diagnostics_dir,
            error,
            stage="send confirmation",
            extra={"click_attempted": True},
        )
        raise error

    async def close(self) -> None:
        await self.page.close()


class PlaywrightMessageSender:
    def __init__(
        self,
        context,
        runtime: Runtime,
        listing_filter=None,
        diagnostics_dir=None,
        pause_before_listing=None,
        allow_unpriced=False,
        events=None,
        details_observer=None,
        preferences=None,
    ):
        self.context, self.runtime = context, runtime
        self.listing_filter = listing_filter
        self.diagnostics_dir = diagnostics_dir
        self.pause_before_listing = pause_before_listing
        self.allow_unpriced, self.events = allow_unpriced, events
        self.details_observer = details_observer
        self.preferences = preferences

    async def prepare(self, listing, text: str) -> UIPreparedMessage:
        validate_site_url(listing.url)
        if getattr(listing, "is_request", False):
            raise ListingFiltered("excluded", "Housing request, not an apartment offer")
        if (
            self.listing_filter is not None
            and listing.price_eur is not None
            and self.listing_filter.max_warm_eur is not None
            and Decimal(listing.price_eur) > self.listing_filter.max_warm_eur
        ):
            raise ListingFiltered("excluded", "Listed rent exceeds the warm-rent limit")
        if self.listing_filter is not None:
            title_only = self.listing_filter.check(listing.title, "")
            if title_only.outcome == "excluded":
                raise ListingFiltered(title_only.outcome, title_only.reason)
        if self.pause_before_listing is not None:
            await self.pause_before_listing()
        page = await self.context.new_page()
        warnings = []
        try:
            if self.events:
                self.events.emit("Opening listing", "sender.prepare", "--live", listing.url)
            response = await page.goto(
                listing.url,
                wait_until="domcontentloaded",
                timeout=self.runtime.timeout_ms,
            )
            await ensure_access(page, self.runtime)
            if response is not None and response.status >= 400:
                raise SelectorNotFound(f"Listing HTTP status {response.status}")
            if self.listing_filter is not None:
                description = await select_one(
                    page, self.runtime.listing_description, self.runtime, "listing description"
                )
                costs = []
                async for item in visible_matches(page, self.runtime.listing_costs):
                    costs.append(await item.inner_text())
                description_text = await description.inner_text()
                cost_text = "\n".join(costs)
                if self.details_observer:
                    self.details_observer(
                        listing, {"description": description_text, "costs": cost_text}
                    )
                decision = self.listing_filter.check(listing.title, description_text, cost_text)
                if self.preferences is not None:
                    decision = match_listing(
                        replace(listing, description=description_text, cost_details=cost_text),
                        self.preferences,
                        self.listing_filter,
                    )
                if (
                    decision.outcome == "review"
                    and self.allow_unpriced
                    and decision.reason == "Warmmiete is not specified reliably"
                ):
                    warnings.append(decision.reason)
                elif decision.outcome != "allowed":
                    raise ListingFiltered(decision.outcome, decision.reason)
            opener = await select_one(page, self.runtime.message.open, self.runtime, "open message")
            await opener.click()
            field = await select_one(
                page, self.runtime.message.input, self.runtime, "message input"
            )
            await field.fill(text)
            if await field.input_value() != text:
                raise SelectorNotFound("Message field differs from requested text")
            button = await select_one(page, self.runtime.message.submit, self.runtime, "submit")
            if not await button.is_enabled():
                raise SelectorNotFound("Submit button is disabled")
            if await any_visible(page, self.runtime.message.success):
                raise SelectorNotFound("Stale success marker: refusing to prepare")
            prepared = UIPreparedMessage(
                page,
                button,
                self.runtime,
                field=field,
                text=text,
                listing_url=listing.url,
                events=self.events,
                diagnostics_dir=self.diagnostics_dir,
            )
            prepared.warnings = warnings
            return prepared
        except BaseException as exc:
            if isinstance(exc, SelectorNotFound):
                exc.diagnostic_path = await capture_failure(
                    page,
                    self.diagnostics_dir,
                    exc,
                    stage=exc.stage if exc.stage != "unknown" else "prepare",
                    extra={"listing_id": listing.id},
                )
            await page.close()
            raise
