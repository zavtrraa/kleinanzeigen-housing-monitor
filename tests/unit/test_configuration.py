import pytest

from kleinanzeigen_bot.core.matching import match_listing
from kleinanzeigen_bot.core.models import Listing
from kleinanzeigen_bot.runtime.configuration import resolve_configuration
from kleinanzeigen_bot.runtime.preferences import SearchPreferences
from kleinanzeigen_bot.settings import Settings


def test_initial_preferences_preserve_configured_budget(tmp_path):
    exclusions = tmp_path / "exclusions.yaml"
    exclusions.write_text("max_warm_eur: '400'\nrules: []\n", encoding="utf-8")
    settings = Settings(_env_file=None, data_dir=tmp_path, exclusions_path=exclusions)
    config = resolve_configuration(settings)
    listing = Listing("1", "url", "Apartment", "450", "2", "40", profile="hannover",
                      description="Warmmiete 470 EUR")
    assert config.preferences.max_rent == 400
    assert match_listing(listing, config.preferences, config.filters).outcome == "excluded"
    changed = resolve_configuration(settings, SearchPreferences(max_rent="600"))
    assert match_listing(listing, changed.preferences, changed.filters).outcome == "allowed"


def test_monitoring_validation_does_not_require_seller_delivery_credentials(tmp_path):
    settings = Settings(_env_file=None, data_dir=tmp_path, dry_run=False, auto_reply=False)
    config = resolve_configuration(settings)
    assert config.mode == "MONITORING"


@pytest.mark.parametrize("name", ["Hannover", "central"])
def test_custom_profile_names_are_preserved_and_associated_with_the_city(tmp_path, name):
    profiles = tmp_path / "profiles.yaml"
    profiles.write_text(
        f"profiles:\n  - name: {name}\n    url: https://www.kleinanzeigen.de/s-wohnung-mieten/hannover/c203l3155\n",
        encoding="utf-8",
    )
    settings = Settings(_env_file=None, data_dir=tmp_path, custom_search_profiles=True,
                        search_profiles_path=profiles)
    config = resolve_configuration(settings)
    assert config.profiles.profiles[0].name == name
    assert config.profiles.profiles[0].city == "hannover"


def test_empty_custom_selection_is_reported_during_configuration_check(tmp_path):
    profiles = tmp_path / "profiles.yaml"
    profiles.write_text("profiles: []\n", encoding="utf-8")
    settings = Settings(_env_file=None, data_dir=tmp_path, custom_search_profiles=True,
                        search_profiles_path=profiles)
    with pytest.raises(ValueError, match="at least one"):
        resolve_configuration(settings, SearchPreferences(cities=["hannover"]))
