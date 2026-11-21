from types import SimpleNamespace

import pytest
from telegram.error import BadRequest

from kleinanzeigen_bot.core.eligibility import EligibilityDecision
from kleinanzeigen_bot.core.models import Listing
from kleinanzeigen_bot.ingestion.extractors import parse_card
from kleinanzeigen_bot.notifications.telegram_bot import TelegramNotifier


class Transport:
    def __init__(self, reject_photo=False):
        self.photos, self.messages = [], []
        self.reject_photo = reject_photo

    async def send_photo(self, **payload):
        if self.reject_photo:
            raise BadRequest("Wrong file identifier/HTTP URL specified")
        self.photos.append(payload)
        return SimpleNamespace(message_id=1)

    async def send_message(self, **payload):
        self.messages.append(payload)
        return SimpleNamespace(message_id=2)


@pytest.mark.asyncio
async def test_listing_card_includes_image_price_area_rooms_and_link():
    bot = Transport()
    listing = Listing(
        "1",
        "https://www.kleinanzeigen.de/s-anzeige/demo/1-203-1",
        "Balcony apartment",
        "450",
        "2",
        "42",
        profile="hannover",
        image_url="https://img.kleinanzeigen.de/apartment.jpg",
    )
    await TelegramNotifier(bot, "-1").listing(listing, EligibilityDecision("allowed", ""))
    assert len(bot.photos) == 1
    payload = bot.photos[0]
    assert payload["photo"] == listing.image_url
    assert "450" in payload["caption"] and "42" in payload["caption"]
    assert "2 rooms" in payload["caption"]
    assert payload["reply_markup"].inline_keyboard[0][0].url == listing.url


@pytest.mark.asyncio
async def test_rejected_image_falls_back_to_text_without_losing_the_listing():
    bot = Transport(reject_photo=True)
    listing = Listing(
        "1",
        "https://www.kleinanzeigen.de/s-anzeige/demo/1-203-1",
        "Apartment",
        image_url="https://img.kleinanzeigen.de/missing.jpg",
    )
    await TelegramNotifier(bot, "-1").listing(listing, EligibilityDecision("review", "Check rent"))
    assert len(bot.messages) == 1
    assert "Check rent" in bot.messages[0]["text"]


@pytest.mark.asyncio
async def test_untrusted_image_is_not_fetched_and_caption_is_bounded():
    bot = Transport()
    listing = Listing(
        "1",
        "https://www.kleinanzeigen.de/s-anzeige/demo/1-203-1",
        "🏡" * 2000,
        image_url="http://127.0.0.1/private",
    )
    await TelegramNotifier(bot, "-1").listing(
        listing, EligibilityDecision("review", "Unknown" * 500)
    )
    assert not bot.photos
    assert len(bot.messages[0]["text"].encode("utf-16-le")) // 2 <= 1024


def test_extractor_preserves_only_a_safe_source_image():
    raw = {
        "href": "/s-anzeige/demo/1-203-1",
        "title": "Apartment",
        "image": "https://img.kleinanzeigen.de/apartment.jpg",
    }
    assert parse_card(raw, "hannover").image_url == raw["image"]
    raw["image"] = "https://img.kleinanzeigen.de.attacker.example/private"
    assert parse_card(raw, "hannover").image_url is None
