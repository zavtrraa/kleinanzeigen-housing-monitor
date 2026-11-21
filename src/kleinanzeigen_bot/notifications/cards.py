from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from ..settings import validate_site_url


def listing_card(listing, decision):
    validate_site_url(listing.url)
    label = "Matches your search" if decision.outcome == "allowed" else "Review details"
    text = "\n".join(
        filter(
            None,
            [
                label,
                listing.title[:240],
                listing.profile.title(),
                f"Listed rent: EUR {listing.price_eur or '?'} / month",
                f"{listing.size_sqm or '?'} m² | {listing.rooms or '?'} rooms",
                "Listed rent may exclude service and heating costs.",
                decision.reason[:240],
                f"Published: {listing.published_at}" if listing.published_at else "",
            ],
        )
    )
    text = text.encode("utf-16-le")[:1900].decode("utf-16-le", errors="ignore")
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("View listing", url=listing.url)]])
    return text, keyboard
