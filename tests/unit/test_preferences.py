from decimal import Decimal

import pytest

from kleinanzeigen_bot.runtime import preferences
from kleinanzeigen_bot.runtime.queue import RunQueue


def test_filters_survive_restart_without_reenabling_replies(tmp_path):
    path = tmp_path / "queue.sqlite3"
    queue = RunQueue(path)
    store = preferences.PreferenceStore(queue)
    store.update(cities=["Hannover"], query="Balcony", max_rent="600", min_area="30")
    queue.close()
    queue = RunQueue(path)
    try:
        value = preferences.PreferenceStore(queue).load()
        assert value.cities == ("hannover",)
        assert value.query == "Balcony"
        assert value.max_rent == Decimal("600")
        assert value.min_area == Decimal("30")
        assert value.auto_reply is False
    finally:
        queue.close()


@pytest.mark.parametrize(
    "changes",
    [
        {"max_rent": "NaN"},
        {"max_rent": "Infinity"},
        {"max_rent": "-1"},
        {"max_rent": "0"},
        {"min_area": "-20"},
        {"min_area": "5000"},
        {"cities": []},
        {"cities": ["unknown"]},
        {"query": "a" * 121},
        {"query": "test\x00query"},
        {"unrecognized": 1},
    ],
)
def test_invalid_filter_does_not_overwrite_saved_preferences(tmp_path, changes):
    queue = RunQueue(tmp_path / "queue.sqlite3")
    try:
        store = preferences.PreferenceStore(queue)
        store.update(max_rent="650")
        with pytest.raises(ValueError):
            store.update(**changes)
        assert store.load().max_rent == Decimal("650")
    finally:
        queue.close()


def test_replayed_update_cannot_undo_newer_configuration(tmp_path):
    queue = RunQueue(tmp_path / "queue.sqlite3")
    try:
        store = preferences.PreferenceStore(queue)
        assert store.update(update_id=100, max_rent="600")
        assert store.update(update_id=102, max_rent="700")
        assert not store.update(update_id=100, max_rent="600")
        assert store.load().max_rent == Decimal("700")
    finally:
        queue.close()


def test_profiles_encode_query_and_filters_without_url_injection():
    value = preferences.SearchPreferences(
        cities=["Hannover", "Garbsen"], query="Balkon & Küche", max_rent="600", min_area="30.5"
    )
    profiles = preferences.build_profiles(value).profiles
    assert [p.name for p in profiles] == ["hannover", "garbsen"]
    assert profiles[0].url == (
        "https://www.kleinanzeigen.de/s-wohnung-mieten/hannover/"
        "sortierung:neuste/anzeige:angebote/preis::600/Balkon%20%26%20K%C3%BCche/"
        "k0c203l3155+wohnung_mieten.qm_d:30.5%2C"
    )
    assert "c203l3067" in profiles[1].url


def test_snapshot_is_immutable_and_cities_are_deduplicated():
    value = preferences.SearchPreferences(cities=["Hannover", "hannover"])
    assert value.cities == ("hannover",)
    with pytest.raises(ValueError):
        value.max_rent = Decimal("999")


def test_reading_saved_preferences_does_not_interrupt_active_worker(tmp_path):
    path = tmp_path / "queue.sqlite3"
    queue = RunQueue(path)
    try:
        store = preferences.PreferenceStore(queue)
        store.update(max_rent="600")
        queue.request("manual", "test")
        job = queue.claim()
        value = preferences.read_preferences(path)
        assert value.max_rent == 600
        assert queue.status()["active"]["id"] == job["id"]
    finally:
        queue.close()


def test_lower_update_id_after_a_week_of_inactivity_is_accepted(tmp_path, monkeypatch):
    queue = RunQueue(tmp_path / "queue.sqlite3")
    try:
        now = [1_000_000.0]
        monkeypatch.setattr("time.time", lambda: now[0])
        store = preferences.PreferenceStore(queue)
        assert store.update(update_id=1000, max_rent="400")
        now[0] += 8 * 86400
        assert store.update(update_id=10, max_rent="450")
        assert not store.update(update_id=10, max_rent="900")
        assert store.load().max_rent == 450
    finally:
        queue.close()
