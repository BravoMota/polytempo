"""Open-Meteo forecast ingestion.

Fetches daily maximum temperature forecasts from Open-Meteo and normalizes them
into a list of Celsius values suitable for the distribution builder. Should not
make trading decisions.

When multiple ``models`` are requested, Open-Meteo returns a separate
``temperature_2m_max_<model>`` series per model; we collect every value for the
target date as the forecast sample set.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import httpx

from polytempo.weather.schema import ForecastValues
from polytempo.weather.stations import Station

DEFAULT_MODELS: tuple[str, ...] = (
    "ukmo_global_deterministic_10km",
    "icon_eu",
    "gfs_seamless",
    "ecmwf_ifs025",
    "ukmo_uk_deterministic_2km",  # UK Met Office UK 2 km (UKV)
    "ukmo_seamless",  # UKMO Seamless (Global 10 km + UK 2 km)
    "ecmwf_ifs",  # ECMWF IFS HRES 9 km
    "icon_seamless",  # DWD ICON EU / Global seamless
)

PLAUSIBLE_MIN_C = -40.0
PLAUSIBLE_MAX_C = 60.0


@dataclass(frozen=True)
class DailyMaxForecast:
    """Normalized daily-max-temperature forecast for one location/date."""

    target_date: date
    latitude: float
    longitude: float
    values_c: list[float]
    models: list[str]

    def to_forecast_values(self, source: str = "open_meteo") -> ForecastValues:
        """Map into :class:`~polytempo.weather.schema.ForecastValues` for calibration and analysis."""
        return ForecastValues(
            source=source,
            latitude=self.latitude,
            longitude=self.longitude,
            target_date=self.target_date,
            values_c=list(self.values_c),
            models=list(self.models) if self.models else None,
        )


def parse_forecast_payload(payload: dict, target_date: date) -> DailyMaxForecast:
    """Parse an Open-Meteo forecast payload into a DailyMaxForecast."""
    latitude = payload.get("latitude")
    longitude = payload.get("longitude")
    daily = payload.get("daily")

    if latitude is None:
        raise ValueError("latitude is required")
    if longitude is None:
        raise ValueError("longitude is required")
    if not isinstance(daily, dict):
        raise ValueError("daily block is required")

    times = daily.get("time")
    if not isinstance(times, list) or not times:
        raise ValueError("daily.time must be a non-empty list")

    target_iso = target_date.isoformat()
    try:
        index = times.index(target_iso)
    except ValueError as exc:
        raise ValueError(f"target_date {target_iso} not found in daily.time") from exc

    values_c: list[float] = []
    models: list[str] = []
    for key, series in daily.items():
        if not key.startswith("temperature_2m_max"):
            continue
        if not isinstance(series, list) or index >= len(series):
            continue
        raw_value = series[index]
        if raw_value is None:
            continue
        try:
            value = float(raw_value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{key}[{index}] must be numeric") from exc
        if not PLAUSIBLE_MIN_C <= value <= PLAUSIBLE_MAX_C:
            raise ValueError(
                f"{key}[{index}]={value} outside plausible range "
                f"[{PLAUSIBLE_MIN_C}, {PLAUSIBLE_MAX_C}] °C"
            )
        values_c.append(value)
        model_suffix = key[len("temperature_2m_max"):].lstrip("_")
        models.append(model_suffix or "default")

    if not values_c:
        raise ValueError("no temperature_2m_max values found for target_date")

    return DailyMaxForecast(
        target_date=target_date,
        latitude=float(latitude),
        longitude=float(longitude),
        values_c=values_c,
        models=models,
    )


def fetch_daily_max(
    latitude: float,
    longitude: float,
    target_date: date,
    timezone: str,
    models: tuple[str, ...] | list[str] = DEFAULT_MODELS,
    base_url: str = "https://api.open-meteo.com/v1/forecast",
) -> DailyMaxForecast:
    """Fetch a daily-max temperature ensemble across models for one date."""
    if not models:
        raise ValueError("models must not be empty")
    if not timezone.strip():
        raise ValueError("timezone must not be empty")

    target_iso = target_date.isoformat()
    response = httpx.get(
        base_url,
        params={
            "latitude": latitude,
            "longitude": longitude,
            "daily": "temperature_2m_max",
            "temperature_unit": "celsius",
            "models": ",".join(models),
            "timezone": timezone,
            "start_date": target_iso,
            "end_date": target_iso,
        },
    )
    response.raise_for_status()
    return parse_forecast_payload(response.json(), target_date)


def fetch_for_station(
    station: Station,
    target_date: date,
    models: tuple[str, ...] | list[str] = DEFAULT_MODELS,
    base_url: str = "https://api.open-meteo.com/v1/forecast",
) -> DailyMaxForecast:
    """Fetch a daily-max forecast for a registered contract station."""
    return fetch_daily_max(
        latitude=station.latitude,
        longitude=station.longitude,
        target_date=target_date,
        timezone=station.timezone,
        models=models,
        base_url=base_url,
    )
