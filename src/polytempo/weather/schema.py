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
    """Forecast temperature samples for one location and target day.

    ``models`` is optional and, when set, must be the same length as ``values_c``
    so each sample is paired with the model id that produced it. Older callers
    that flatten the ensemble may leave it ``None``.
    """

    source: str
    latitude: float
    longitude: float
    target_date: date
    values_c: list[float]
    models: list[str] | None = None

    def __post_init__(self) -> None:
        if self.models is not None and len(self.models) != len(self.values_c):
            raise ValueError(
                "ForecastValues.models length must match values_c "
                f"({len(self.models)} != {len(self.values_c)})"
            )
