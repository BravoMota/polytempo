"""Join historical forecasts with observations and compute calibration stats."""

from __future__ import annotations

import json
import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from polytempo.weather.data_dir import WEATHER_DATA_DIR
from polytempo.weather.historical_forecasts import HistoricalForecastRecord
from polytempo.weather.observations import ObservedTmax

DEFAULT_CALIBRATION_STATS_PATH = WEATHER_DATA_DIR / "calibration_stats.json"

LEAD_BUCKET_RANGES: tuple[tuple[float, float | None, str], ...] = (
    (0.0, 6.0, "0-6h"),
    (6.0, 12.0, "6-12h"),
    (12.0, 24.0, "12-24h"),
    (24.0, 36.0, "24-36h"),
    (36.0, 48.0, "36-48h"),
    (48.0, 72.0, "48-72h"),
    (72.0, None, "72h+"),
)


@dataclass(frozen=True)
class ForecastErrorRecord:
    """Joined forecast vs observation with signed error."""

    station_id: str
    source: str
    model: str
    target_date: date
    lead_hours: float
    predicted_tmax_c: float
    observed_tmax_c: float
    error_c: float


@dataclass(frozen=True)
class CalibrationStats:
    """Aggregated error statistics for one station/model/lead bucket."""

    station_id: str
    source: str
    model: str
    lead_bucket: str
    bias_c: float
    rmse_c: float
    mae_c: float
    n_samples: int


def lead_bucket_for_hours(lead_hours: float) -> str:
    """Map lead time in hours to a calibration bucket label."""
    if lead_hours < 0:
        raise ValueError("lead_hours must be non-negative")

    for lower, upper, label in LEAD_BUCKET_RANGES:
        if upper is None:
            if lead_hours >= lower:
                return label
        elif lower <= lead_hours < upper:
            return label

    raise ValueError(f"no lead bucket for lead_hours={lead_hours}")


def join_forecasts_with_observations(
    forecasts: list[HistoricalForecastRecord],
    observations: list[ObservedTmax],
) -> list[ForecastErrorRecord]:
    """Inner join on (station_id, target_date)."""
    obs_by_key = {(obs.station_id, obs.target_date): obs for obs in observations}

    joined: list[ForecastErrorRecord] = []
    for forecast in forecasts:
        obs = obs_by_key.get((forecast.station_id, forecast.target_date))
        if obs is None:
            continue
        error_c = forecast.predicted_tmax_c - obs.observed_tmax_c
        joined.append(
            ForecastErrorRecord(
                station_id=forecast.station_id,
                source=forecast.source,
                model=forecast.model,
                target_date=forecast.target_date,
                lead_hours=forecast.lead_hours,
                predicted_tmax_c=forecast.predicted_tmax_c,
                observed_tmax_c=obs.observed_tmax_c,
                error_c=error_c,
            )
        )

    joined.sort(
        key=lambda row: (
            row.station_id,
            row.model,
            row.target_date,
            row.lead_hours,
        )
    )
    return joined


def compute_calibration_stats(
    errors: list[ForecastErrorRecord],
) -> list[CalibrationStats]:
    """Group errors and compute bias, RMSE, MAE, and sample count."""
    groups: dict[tuple[str, str, str, str], list[float]] = defaultdict(list)

    for row in errors:
        bucket = lead_bucket_for_hours(row.lead_hours)
        key = (row.station_id, row.source, row.model, bucket)
        groups[key].append(row.error_c)

    stats: list[CalibrationStats] = []
    for (station_id, source, model, lead_bucket), values in sorted(groups.items()):
        n_samples = len(values)
        bias_c = sum(values) / n_samples
        mae_c = sum(abs(value) for value in values) / n_samples
        rmse_c = math.sqrt(sum(value * value for value in values) / n_samples)
        stats.append(
            CalibrationStats(
                station_id=station_id,
                source=source,
                model=model,
                lead_bucket=lead_bucket,
                bias_c=bias_c,
                rmse_c=rmse_c,
                mae_c=mae_c,
                n_samples=n_samples,
            )
        )
    return stats


def _stats_to_dict(stats: CalibrationStats) -> dict[str, Any]:
    return {
        "station_id": stats.station_id,
        "source": stats.source,
        "model": stats.model,
        "lead_bucket": stats.lead_bucket,
        "bias_c": stats.bias_c,
        "rmse_c": stats.rmse_c,
        "mae_c": stats.mae_c,
        "n_samples": stats.n_samples,
    }


def _stats_from_dict(data: dict[str, Any]) -> CalibrationStats:
    return CalibrationStats(
        station_id=str(data["station_id"]),
        source=str(data["source"]),
        model=str(data["model"]),
        lead_bucket=str(data["lead_bucket"]),
        bias_c=float(data["bias_c"]),
        rmse_c=float(data["rmse_c"]),
        mae_c=float(data["mae_c"]),
        n_samples=int(data["n_samples"]),
    )


def write_calibration_stats_json(stats: list[CalibrationStats], path: Path) -> None:
    """Write calibration stats as a JSON array."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = [_stats_to_dict(row) for row in stats]
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def read_calibration_stats_json(path: Path) -> list[CalibrationStats]:
    """Read calibration stats JSON array."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError("calibration stats JSON must be an array")
    return [_stats_from_dict(item) for item in raw]
