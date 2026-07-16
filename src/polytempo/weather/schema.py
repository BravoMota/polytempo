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
    init_lead_hours: list[float] | None = None
    model_run_init_utc: list[str] | None = None
    # Same-station WU running observed max (°C) at decision time; optional.
    observed_running_max_c: float | None = None

    def __post_init__(self) -> None:
        if self.models is not None and len(self.models) != len(self.values_c):
            raise ValueError(
                "ForecastValues.models length must match values_c "
                f"({len(self.models)} != {len(self.values_c)})"
            )
        if self.init_lead_hours is not None:
            if self.models is None:
                raise ValueError("init_lead_hours requires models")
            if len(self.init_lead_hours) != len(self.models):
                raise ValueError(
                    "ForecastValues.init_lead_hours length must match models "
                    f"({len(self.init_lead_hours)} != {len(self.models)})"
                )
        if self.model_run_init_utc is not None:
            if self.models is None:
                raise ValueError("model_run_init_utc requires models")
            if len(self.model_run_init_utc) != len(self.models):
                raise ValueError(
                    "ForecastValues.model_run_init_utc length must match models "
                    f"({len(self.model_run_init_utc)} != {len(self.models)})"
                )
