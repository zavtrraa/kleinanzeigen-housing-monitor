import re
from copy import copy
from datetime import date
from decimal import Decimal, InvalidOperation

from ..core.eligibility import ListingFilter, warm_amounts
from .models import Candidate, Decision, HousingConfig


def evaluate(
    candidate: Candidate, config: HousingConfig, listing_filter: ListingFilter
) -> Decision:
    notes = []
    if candidate.kind != "apartment":
        return Decision("excluded", ("Not a self-contained apartment",))
    # Reuse text rules, but budget decisions are handled against structured costs below.
    text_filter = copy(listing_filter)
    text_filter.max_warm_eur = None
    filtered = text_filter.check(candidate.title, candidate.description)
    if filtered.outcome == "excluded":
        return Decision("excluded", (filtered.reason,))
    if candidate.city is None:
        notes.append("City is not confirmed")
    elif candidate.city.strip().casefold() not in {
        value.casefold() for value in (config.allowed_cities or [config.city])
    }:
        return Decision("excluded", ("Outside the selected region",))
    try:
        if candidate.size_sqm is None:
            notes.append("Area is not specified")
        elif Decimal(candidate.size_sqm) < config.min_size_sqm:
            return Decision("excluded", ("Area is below the configured minimum",))
        amounts = warm_amounts(candidate.description)
        if candidate.warm_eur is not None:
            amounts.add(Decimal(candidate.warm_eur))
        if not amounts:
            notes.append("Warm rent is not specified")
        elif len(amounts) != 1:
            notes.append("Conflicting warm-rent amounts")
        else:
            amount = next(iter(amounts))
            if not amount.is_finite() or amount <= 0:
                notes.append("Invalid warm-rent amount")
            elif amount > config.max_warm_eur:
                return Decision("excluded", (f"Warmmiete {amount} € > {config.max_warm_eur} €",))
    except InvalidOperation:
        notes.append("Unrecognized rent or area format")
    if candidate.source in {"hanova", "heimkehr"} and not candidate.description:
        notes.append("Review the full description and included costs")
    text = candidate.title + " " + candidate.description
    requires_wbs = candidate.wbs_required
    if requires_wbs is None:
        requires_wbs = bool(
            re.search(r"\bWBS\b|\bWohnberechtigungsschein\b", text, re.I)
        ) and not bool(
            re.search(r"\b(?:ohne|kein(?:en)?)\s+(?:WBS|Wohnberechtigungsschein)\b", text, re.I)
        )
    if requires_wbs and config.wbs_status != "yes":
        notes.append("WBS required; eligibility needs confirmation")
    if re.search(
        r"\b(?:zzgl\.?|zuzüglich|ohne|exklusive)\s+(?:der\s+)?(?:Heizkosten|Heizung)\b", text, re.I
    ):
        notes.append("Heating is paid separately")
    if candidate.available_from:
        try:
            available = date.fromisoformat(candidate.available_from)
            if available < config.move_in_from:
                notes.append("Available before your preferred date; agree on the start date")
        except ValueError:
            notes.append("Confirm move-in date")
    else:
        notes.append("Move-in date is not specified")
    return Decision("review" if notes else "eligible", tuple(dict.fromkeys(notes)))
