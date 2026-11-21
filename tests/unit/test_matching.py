from dataclasses import replace

import pytest

from kleinanzeigen_bot.core.eligibility import ListingFilter
from kleinanzeigen_bot.core.matching import match_listing
from kleinanzeigen_bot.core.models import Listing
from kleinanzeigen_bot.runtime.preferences import SearchPreferences
from kleinanzeigen_bot.settings import ExclusionRule, Exclusions


@pytest.fixture
def filters():
    return ListingFilter(
        Exclusions(
            max_warm_eur="600",
            rules=[
                ExclusionRule(name="shared", label="Shared housing", patterns=[r"\bWG-Zimmer\b"])
            ],
        )
    )


def apartment(**changes):
    return replace(
        Listing(
            "1",
            "https://www.kleinanzeigen.de/s-anzeige/example/1-203-1",
            "Apartment with balcony",
            "450",
            "2",
            "42",
            profile="hannover",
            description="Warmmiete 550 EUR. A quiet apartment.",
        ),
        **changes,
    )


@pytest.mark.parametrize(
    "changes,expected",
    [
        ({}, "allowed"),
        ({"size_sqm": "19"}, "excluded"),
        ({"price_eur": "650"}, "excluded"),
        ({"description": "Warmmiete 650 EUR"}, "excluded"),
        ({"size_sqm": None}, "review"),
        ({"size_sqm": "NaN"}, "review"),
        ({"price_eur": "NaN"}, "review"),
        ({"description": "Rent on request"}, "review"),
        ({"title": "WG-Zimmer with balcony"}, "excluded"),
        ({"is_request": True}, "excluded"),
        ({"profile": "seelze"}, "excluded"),
        ({"title": "Apartment", "description": "Warmmiete 550 EUR"}, "excluded"),
    ],
)
def test_matching_enforces_search_and_review_boundaries(filters, changes, expected):
    prefs = SearchPreferences(cities=["hannover"], query="balcony", max_rent="600", min_area="30")
    assert match_listing(apartment(**changes), prefs, filters).outcome == expected


def test_query_can_match_description_and_is_not_a_regular_expression(filters):
    prefs = SearchPreferences(cities=["hannover"], query="south balcony", max_rent="600")
    listing = apartment(title="Apartment", description="Warmmiete 550 EUR. South-facing BALCONY.")
    assert match_listing(listing, prefs, filters).outcome == "allowed"
    prefs = prefs.model_copy(update={"query": ".*"})
    assert match_listing(listing, prefs, filters).outcome == "excluded"
