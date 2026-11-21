from decimal import Decimal, InvalidOperation

from .eligibility import EligibilityDecision, normalize


def _number(value):
    try:
        number = Decimal(value)
        return number if number.is_finite() and number >= 0 else None
    except (InvalidOperation, TypeError, ValueError):
        return None


def match_listing(listing, preferences, listing_filter):
    """Check actual listing data as well as the search URL's requested filters."""
    if listing.is_request:
        return EligibilityDecision("excluded", "Housing request, not an apartment offer")
    listing_city = (listing.city or listing.profile).casefold()
    if listing_city in preferences.cities:
        pass
    elif listing.city or listing_city in {
        "hannover", "laatzen", "langenhagen", "garbsen", "seelze"
    }:
        return EligibilityDecision("excluded", "Outside the selected search cities")
    text = normalize(listing.title + " " + listing.description).casefold()
    if any(word not in text for word in preferences.query.casefold().split()):
        return EligibilityDecision("excluded", "Search terms not found in the listing")
    size, price = _number(listing.size_sqm), _number(listing.price_eur)
    if size is not None and size < preferences.min_area:
        return EligibilityDecision("excluded", "Area is below the configured minimum")
    if price is not None and price > preferences.max_rent:
        return EligibilityDecision("excluded", "Listed rent exceeds the configured maximum")
    decision = listing_filter.check(listing.title, listing.description, listing.cost_details)
    if decision.outcome == "excluded":
        return decision
    if size is None:
        return EligibilityDecision("review", "Area is not specified reliably")
    if price is None:
        return EligibilityDecision("review", "Listed rent is not specified reliably")
    return decision
