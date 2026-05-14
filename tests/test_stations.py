"""Tests for the contract-station registry."""

import pytest

from polytempo.weather.stations import STATIONS, get_station


def test_registry_contains_priority_cities() -> None:
    expected = {"london", "madrid", "amsterdam", "warsaw", "paris", "milan"}
    assert expected <= STATIONS.keys()


def test_get_station_is_case_insensitive() -> None:
    london = get_station("London")
    assert get_station("london") is london
    assert get_station("  LONDON ") is london


def test_get_station_london_resolves_to_eglc() -> None:
    london = get_station("London")
    assert london.icao == "EGLC"
    assert london.timezone == "Europe/London"


def test_get_station_paris_resolves_to_le_bourget() -> None:
    paris = get_station("Paris")
    assert paris.icao == "LFPB"


def test_get_station_unknown_city_raises() -> None:
    with pytest.raises(KeyError):
        get_station("Atlantis")
