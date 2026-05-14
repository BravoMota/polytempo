"""Forecast calibration.

Applies known/manual forecast bias corrections before distribution building.
This should happen before distribution building.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from polytempo.weather.schema import ForecastValues


@dataclass(frozen=True)
class CalibrationRule:
    """Manual bias correction rule for a forecast source or station."""

    source: str
    station_id: str | None
    bias_c: float = 0.0


def apply_bias_to_values(values_c: list[float], bias_c: float = 0.0) -> list[float]:
    """Apply manual bias correction to Celsius forecast values."""
    if not math.isfinite(bias_c):
        raise ValueError("bias_c must be finite")

    return [value - bias_c for value in values_c]


def calibrate_forecast(
    forecast: ForecastValues,
    rule: CalibrationRule | None = None,
) -> ForecastValues:
    """Return a new forecast with values corrected by the configured bias."""
    bias_c = rule.bias_c if rule is not None else 0.0
    return ForecastValues(
        source=forecast.source,
        latitude=forecast.latitude,
        longitude=forecast.longitude,
        target_date=forecast.target_date,
        values_c=apply_bias_to_values(forecast.values_c, bias_c),
    )
