import re
from decimal import Decimal
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def validate_site_url(url: str) -> str:
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in {"kleinanzeigen.de", "www.kleinanzeigen.de"}
        or parsed.username
        or parsed.password
        or parsed.port not in (None, 443)
    ):
        raise ValueError("Only HTTPS Kleinanzeigen URLs are allowed")
    return url


def safe_image_url(url: str | None) -> str | None:
    if not url or len(url) > 2048:
        return None
    try:
        parsed = urlsplit(url)
        if (
            parsed.scheme == "https"
            and parsed.hostname in {"img.kleinanzeigen.de", "img.classistatic.de", "i.ebayimg.com"}
            and not parsed.username
            and not parsed.password
            and parsed.port in (None, 443)
        ):
            return url
    except ValueError:
        pass
    return None


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="forbid",
        env_ignore_empty=True,
        hide_input_in_errors=True,
    )
    dry_run: bool = True
    auto_reply: bool = False
    custom_search_profiles: bool = False
    allow_unpriced_replies: bool = False
    fresh_first: bool = True
    excluded_recheck_seconds: int = Field(21600, ge=60)
    prepare_retry_seconds: int = Field(3600, ge=60)
    prepare_failure_limit: int = Field(2, ge=1, le=5)
    telegram_events_chat_id: str = ""
    watch_interval_seconds: int = Field(3600, ge=600)
    watch_jitter_seconds: int = Field(600, ge=0, le=1200)
    watch_queue_limit: int = Field(20, ge=1, le=100)
    max_messages_per_cycle: int = Field(1, ge=1, le=10)
    interval_seconds: int = Field(7200, ge=600)
    jitter_seconds: int = Field(300, ge=0, le=3600)
    min_message_pause_seconds: int = Field(180, ge=0)
    max_message_pause_seconds: int = Field(420, ge=0)
    min_listing_pause_seconds: int = Field(30, ge=0)
    max_listing_pause_seconds: int = Field(60, ge=0)
    headless: bool = False
    browser_window_width: int = Field(1000, ge=400, le=2400)
    browser_window_height: int = Field(720, ge=400, le=1800)
    browser_color_scheme: Literal["dark", "light", "no-preference"] = "dark"
    browser_engine: Literal["playwright", "patchright"] = "playwright"
    browser_channel: Literal["chrome", "msedge", "chromium"] | None = None
    browser_locale: str | None = None
    browser_timezone: str | None = "Europe/Berlin"
    data_dir: Path = Path("data")
    search_profiles_path: Path = Path("config/search_profiles.yaml")
    runtime_path: Path = Path("config/runtime.yaml")
    exclusions_path: Path = Path("config/exclusions.yaml")
    message_template_path: Path = Path("config/message_templates/base_de.txt")
    message_variants_path: Path = Path("config/message_templates/variants.yaml")
    signature: str = ""
    telegram_bot_token: SecretStr = SecretStr("")
    telegram_chat_id: str = ""
    telegram_allowed_user_ids: list[int] = Field(default_factory=list)
    gmail_address: str = ""
    gmail_app_password: SecretStr = SecretStr("")
    housing_config_path: Path = Path("config/housing.yaml")

    @model_validator(mode="after")
    def validate_relationships(self):
        if self.watch_jitter_seconds >= self.watch_interval_seconds:
            raise ValueError("Watch jitter must be smaller than interval")
        if self.telegram_events_chat_id and self.telegram_events_chat_id == self.telegram_chat_id:
            raise ValueError("Event chat must differ from control chat")
        if self.browser_engine == "patchright" and self.browser_locale is not None:
            raise ValueError("Patchright profile requires native locale; remove BROWSER_LOCALE")
        if self.min_listing_pause_seconds > self.max_listing_pause_seconds:
            raise ValueError("Minimum listing pause must not exceed maximum")
        if self.min_message_pause_seconds > self.max_message_pause_seconds:
            raise ValueError("Minimum pause must not exceed maximum pause")
        if bool(self.telegram_bot_token.get_secret_value()) != bool(self.telegram_chat_id):
            raise ValueError("Set both Telegram token and chat ID, or neither")
        return self


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class SearchProfile(StrictModel):
    name: str = Field(min_length=1)
    url: str
    city: str | None = None
    enabled: bool = True

    @model_validator(mode="after")
    def validate_url(self):
        validate_site_url(self.url)
        return self


class Profiles(StrictModel):
    profiles: list[SearchProfile]

    @model_validator(mode="after")
    def unique_names(self):
        names = [p.name for p in self.profiles]
        if len(names) != len(set(names)):
            raise ValueError("Search profile names must be unique")
        return self


class Selector(StrictModel):
    kind: Literal["role", "text", "css"]
    value: str = Field(min_length=1)
    role: str | None = None
    regex: bool = False
    exact: bool = True

    @model_validator(mode="after")
    def valid_selector(self):
        if self.kind == "role" and not self.role:
            raise ValueError("Role selector needs a role")
        if self.kind == "css" and self.regex:
            raise ValueError("CSS selectors cannot use regex")
        if self.regex:
            re.compile(self.value)
        return self


class SearchSelectors(StrictModel):
    cards: str
    link: str
    title: str
    price: str
    details: str
    date: str
    empty: list[Selector] = Field(min_length=1)
    next_page: list[Selector] = Field(
        default_factory=lambda: [
            Selector(kind="css", value='a[aria-label="Nächste"][href]'),
            Selector(kind="css", value="a.pagination-next[href]"),
        ]
    )


class MessageSelectors(StrictModel):
    open: list[Selector] = Field(min_length=1)
    input: list[Selector] = Field(min_length=1)
    submit: list[Selector] = Field(min_length=1)
    success: list[Selector] = Field(min_length=1)
    challenge: list[Selector] = Field(min_length=1)
    auth: list[Selector] = Field(min_length=1)


class ExclusionRule(StrictModel):
    name: str = Field(min_length=1)
    label: str = Field(min_length=1)
    patterns: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def valid_patterns(self):
        try:
            for pattern in self.patterns:
                if not pattern:
                    raise ValueError("Empty exclusion pattern")
                re.compile(pattern)
        except re.error as exc:
            raise ValueError("Invalid exclusion regex") from exc
        return self


class Exclusions(StrictModel):
    household_size: int = Field(1, ge=1)
    max_warm_eur: Decimal | None = Field(None, gt=0)
    rules: list[ExclusionRule] = Field(default_factory=list)


class Runtime(StrictModel):
    selectors_verified: bool = False
    live_validation: Literal["verified", "per_listing"] = "verified"
    timeout_ms: int = Field(15000, ge=100, le=120000)
    confirmation_timeout_ms: int = Field(20000, ge=100, le=120000)
    max_listings_per_profile: int = Field(125, ge=1, le=250)
    max_pages_per_profile: int = Field(5, ge=1, le=10)
    search: SearchSelectors
    message: MessageSelectors
    listing_description: list[Selector] = Field(
        default_factory=lambda: [
            Selector(kind="css", value="#viewad-description-text"),
            Selector(kind="css", value='[itemprop="description"]'),
        ]
    )
    listing_costs: list[Selector] = Field(
        default_factory=lambda: [
            Selector(kind="css", value="#viewad-details"),
        ]
    )


class Variants(StrictModel):
    greetings: list[str] = Field(min_length=1)
    closings: list[str] = Field(min_length=1)


def load_yaml(path: Path, model):
    return model.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


def validate_ready(settings: Settings, profiles: Profiles, runtime: Runtime) -> None:
    if not any(profile.enabled for profile in profiles.profiles):
        raise ValueError("Configure and enable at least one search profile")
    if not settings.dry_run:
        if not runtime.selectors_verified:
            if runtime.live_validation != "per_listing":
                raise ValueError("Validate selectors in dry run before enabling live mode")
            if settings.headless or settings.max_messages_per_cycle != 1:
                raise ValueError(
                    "Per-listing LIVE requires visible browser and one message per cycle"
                )
        if not settings.telegram_bot_token.get_secret_value():
            raise ValueError("Telegram notifications are required in live mode")
