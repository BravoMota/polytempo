"""Shared weather forecast types.

Normalized forecast values used by calibration and analysis. Open-Meteo ingestion
returns :class:`~polytempo.weather.open_meteo.DailyMaxForecast`; call
:meth:`~polytempo.weather.open_meteo.DailyMaxForecast.to_forecast_values` before
the calibration / analysis pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class ForecastValues:
    """Forecast temperature samples for one location and target day."""

    source: str
    latitude: float
    longitude: float
    target_date: date
    values_c: list[float]
