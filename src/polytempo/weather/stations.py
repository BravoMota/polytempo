"""Contract-station registry.

Maps city slugs to the named observation station used by Polymarket weather
contracts. Built from the research note's priority list:
London > Madrid > Amsterdam > Warsaw > Paris > Milan.

Forecasting must target the contract's named station, not a city centroid. Keep
this table explicit and versioned so a source switch (e.g. Paris CDG → Le Bourget)
is a single, auditable change.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Station:
    """One contract observation station."""

    city: str
    icao: str
    latitude: float
    longitude: float
    timezone: str


STATIONS: dict[str, Station] = {
    "london": Station("London", "EGLC", 51.5053, 0.0553, "Europe/London"),
    "madrid": Station("Madrid", "LEMD", 40.4936, -3.5668, "Europe/Madrid"),
    "amsterdam": Station("Amsterdam", "EHAM", 52.3086, 4.7639, "Europe/Amsterdam"),
    "warsaw": Station("Warsaw", "EPWA", 52.1657, 20.9671, "Europe/Warsaw"),
    "paris": Station("Paris", "LFPB", 48.9694, 2.4414, "Europe/Paris"),
    "milan": Station("Milan", "LIMC", 45.6306, 8.7281, "Europe/Rome"),
}


def get_station(city: str) -> Station:
    """Look up a station by city name (case-insensitive)."""
    key = city.strip().lower()
    if key not in STATIONS:
        raise KeyError(f"unknown city: {city!r}")
    return STATIONS[key]
