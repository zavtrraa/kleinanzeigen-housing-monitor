"""Resolve one validated configuration snapshot for every run mode."""

from dataclasses import dataclass

from ..core.eligibility import ListingFilter
from ..settings import Exclusions, Profiles, Runtime, Settings, load_yaml, validate_ready
from .preferences import CITIES, SearchPreferences, build_profiles


@dataclass(frozen=True)
class ResolvedConfiguration:
    preferences: SearchPreferences
    profiles: Profiles
    runtime: Runtime
    exclusions: Exclusions
    filters: ListingFilter
    effective_settings: Settings
    mode: str


def preference_defaults(settings: Settings) -> SearchPreferences:
    exclusions = load_yaml(settings.exclusions_path, Exclusions)
    return SearchPreferences(
        max_rent=exclusions.max_warm_eur or SearchPreferences.model_fields["max_rent"].default,
        auto_reply=settings.auto_reply,
    )


def _profile_city(profile) -> str:
    if profile.city:
        city = profile.city.casefold()
        if city not in CITIES:
            raise ValueError(f"Custom profile {profile.name!r} has an unsupported city")
        return city
    url = profile.url.casefold()
    for city, (_, location_id) in CITIES.items():
        if f"/{city}/" in url or f"l{location_id}" in url:
            return city
    raise ValueError(
        f"Custom profile {profile.name!r} must set city or use a supported city location URL"
    )


def _custom_profiles(settings: Settings, preferences: SearchPreferences) -> Profiles:
    configured = load_yaml(settings.search_profiles_path, Profiles)
    profiles = []
    for profile in configured.profiles:
        city = _profile_city(profile)
        if profile.enabled and city in preferences.cities:
            profiles.append(profile.model_copy(update={"city": city}))
    return Profiles(profiles=profiles)


def resolve_configuration(
    settings: Settings, preferences: SearchPreferences | None = None
) -> ResolvedConfiguration:
    preferences = preferences or preference_defaults(settings)
    profiles = (
        _custom_profiles(settings, preferences)
        if settings.custom_search_profiles
        else build_profiles(preferences)
    )
    runtime = load_yaml(settings.runtime_path, Runtime)
    configured_exclusions = load_yaml(settings.exclusions_path, Exclusions)
    exclusions = configured_exclusions.model_copy(update={"max_warm_eur": preferences.max_rent})
    effective_settings = settings.model_copy(
        update={"dry_run": settings.dry_run or not preferences.auto_reply}
    )
    validate_ready(effective_settings, profiles, runtime)
    return ResolvedConfiguration(
        preferences=preferences,
        profiles=profiles,
        runtime=runtime,
        exclusions=exclusions,
        filters=ListingFilter(exclusions),
        effective_settings=effective_settings,
        mode=(
            "LIVE"
            if not effective_settings.dry_run
            else "MONITORING"
            if not preferences.auto_reply
            else "DRY RUN"
        ),
    )
