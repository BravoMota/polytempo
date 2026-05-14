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

DEFAULT_MODELS: tuple[str, ...] = (
    "ecmwf_ifs025",
    "gfs_seamless",
    "icon_seamless",
)


@dataclass(frozen=True)
class DailyMaxForecast:
    """Normalized daily-max-temperature forecast for one location/date."""

    target_date: date
    latitude: float
    longitude: float
    values_c: list[float]
    models: list[str]


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
            values_c.append(float(raw_value))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{key}[{index}] must be numeric") from exc
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
    models: tuple[str, ...] | list[str] = DEFAULT_MODELS,
    base_url: str = "https://api.open-meteo.com/v1/forecast",
) -> DailyMaxForecast:
    """Fetch a daily-max temperature ensemble across models for one date."""
    if not models:
        raise ValueError("models must not be empty")

    target_iso = target_date.isoformat()
    response = httpx.get(
        base_url,
        params={
            "latitude": latitude,
            "longitude": longitude,
            "daily": "temperature_2m_max",
            "models": ",".join(models),
            "timezone": "auto",
            "start_date": target_iso,
            "end_date": target_iso,
        },
    )
    response.raise_for_status()
    return parse_forecast_payload(response.json(), target_date)
