from dataclasses import replace

from ..core.diagnostics import capture_failure
from ..core.exceptions import ListingUnavailable, SelectorNotFound
from ..core.ui import ensure_access, select_one, visible_matches
from ..settings import validate_site_url


class ListingInspector:
    """Read the public description and costs without opening a contact dialog."""

    def __init__(self, context, runtime, diagnostics_dir=None, pause_before_listing=None):
        self.context, self.runtime = context, runtime
        self.diagnostics_dir, self.pause_before_listing = diagnostics_dir, pause_before_listing

    async def inspect(self, listing):
        validate_site_url(listing.url)
        if self.pause_before_listing:
            await self.pause_before_listing()
        page = await self.context.new_page()
        try:
            response = await page.goto(
                listing.url, wait_until="domcontentloaded", timeout=self.runtime.timeout_ms
            )
            await ensure_access(page, self.runtime)
            validate_site_url(page.url)
            if response is not None and response.status in {404, 410}:
                raise ListingUnavailable(f"Listing HTTP status {response.status}")
            if response is not None and response.status >= 400:
                raise SelectorNotFound(f"Listing HTTP status {response.status}", stage="inspection")
            description = await select_one(
                page, self.runtime.listing_description, self.runtime, "listing description"
            )
            costs = []
            async for element in visible_matches(page, self.runtime.listing_costs):
                costs.append(await element.inner_text())
            return replace(
                listing, description=await description.inner_text(), cost_details="\n".join(costs)
            )
        except SelectorNotFound as exc:
            exc.diagnostic_path = await capture_failure(
                page,
                self.diagnostics_dir,
                exc,
                stage="inspection",
                extra={"listing_id": listing.id},
            )
            raise
        finally:
            await page.close()
