"""Join and aggregate calibration forecast errors (updated store)."""

from __future__ import annotations

import csv
import math
import statistics
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from datetime import timezone as dt_timezone
from pathlib import Path
from typing import Any

from polytempo.weather.calibration_stats_csv import CalibrationStatRow


@dataclass(frozen=True)
class ForecastRecord:
    """One predicted daily Tmax from a Single Runs payload or DB row."""

    station_id: str
    model: str
    run_time_utc: datetime
    target_date: date
    lead_hours: float
    predicted_tmax_c: float
    forecast_lat: float | None = None
    forecast_lon: float | None = None


@dataclass(frozen=True)
class ForecastError:
    """One forecast joined with its observed Tmax."""

    station_id: str
    model: str
    run_time_utc: datetime
    target_date: date
    lead_hours: float
    predicted_tmax_c: float
    observed_tmax_c: float
    error_c: float
    abs_error_c: float
    squared_error_c: float


CALIBRATION_STAT_COLUMNS = (
    "station_id",
    "model",
    "lead_hours",
    "n_samples",
    "bias_c",
    "mae_c",
    "rmse_c",
    "error_std_c",
)


def compute_lead_hours(run_time_utc: datetime, target_date: date) -> float:
    """Hours from run_time_utc to UTC midnight at end of target_date."""
    if run_time_utc.tzinfo is None:
        raise ValueError("run_time_utc must be timezone-aware")
    end_of_target_utc = datetime.combine(
        target_date + timedelta(days=1),
        time.min,
        tzinfo=dt_timezone.utc,
    )
    run_utc = run_time_utc.astimezone(dt_timezone.utc)
    return (end_of_target_utc - run_utc).total_seconds() / 3600.0


def _extract_forecast_coords(payload: dict[str, Any]) -> tuple[float, float]:
    latitude = payload.get("latitude")
    longitude = payload.get("longitude")
    if not isinstance(latitude, (int, float)) or not isinstance(longitude, (int, float)):
        raise ValueError("payload latitude and longitude must be numeric")
    return float(latitude), float(longitude)


def iter_forecast_records_from_payload(
    payload: dict[str, Any],
    *,
    station_id: str,
    model: str,
    run_time_utc: datetime,
) -> Iterator[ForecastRecord]:
    """Yield one row per non-null daily Tmax in a Single Runs payload."""
    daily = payload.get("daily")
    if not isinstance(daily, dict):
        raise ValueError("daily block is required")

    times = daily.get("time")
    temps = daily.get("temperature_2m_max")
    if not isinstance(times, list) or not isinstance(temps, list):
        raise ValueError("daily.time and daily.temperature_2m_max must be lists")
    if len(times) != len(temps):
        raise ValueError("daily.time and daily.temperature_2m_max length mismatch")

    forecast_lat, forecast_lon = _extract_forecast_coords(payload)
    run_utc = run_time_utc.astimezone(dt_timezone.utc)

    for time_raw, temp_raw in zip(times, temps, strict=True):
        if temp_raw is None:
            continue
        target_date = date.fromisoformat(str(time_raw))
        predicted = float(temp_raw)
        if not math.isfinite(predicted):
            continue
        yield ForecastRecord(
            station_id=station_id,
            model=model,
            run_time_utc=run_utc,
            target_date=target_date,
            lead_hours=compute_lead_hours(run_utc, target_date),
            predicted_tmax_c=predicted,
            forecast_lat=forecast_lat,
            forecast_lon=forecast_lon,
        )


def join_with_observations(
    forecast_records: list[ForecastRecord],
    observations: dict[tuple[str, date], float],
) -> list[ForecastError]:
    """Inner-join forecast rows on (station_id, target_date) with observations."""
    joined: list[ForecastError] = []
    for record in forecast_records:
        key = (record.station_id, record.target_date)
        observed = observations.get(key)
        if observed is None:
            continue

        predicted = record.predicted_tmax_c
        error_c = predicted - observed
        joined.append(
            ForecastError(
                station_id=record.station_id,
                model=record.model,
                run_time_utc=record.run_time_utc,
                target_date=record.target_date,
                lead_hours=record.lead_hours,
                predicted_tmax_c=predicted,
                observed_tmax_c=observed,
                error_c=error_c,
                abs_error_c=abs(error_c),
                squared_error_c=error_c * error_c,
            )
        )

    joined.sort(
        key=lambda row: (
            row.station_id,
            row.model,
            row.run_time_utc,
            row.lead_hours,
            row.target_date,
        )
    )
    return joined


def compute_calibration_stats(errors: list[ForecastError]) -> list[CalibrationStatRow]:
    """Group by (station_id, model, lead_hours) and compute bias, MAE, RMSE, std."""
    groups: dict[tuple[str, str, float], list[ForecastError]] = {}
    for row in errors:
        key = (row.station_id, row.model, float(row.lead_hours))
        groups.setdefault(key, []).append(row)

    stats: list[CalibrationStatRow] = []
    for (station_id, model, lead_hours), rows in sorted(groups.items()):
        errs = [row.error_c for row in rows]
        abs_errs = [row.abs_error_c for row in rows]
        sq_errs = [row.squared_error_c for row in rows]
        n_samples = len(rows)

        bias_c = statistics.fmean(errs)
        mae_c = statistics.fmean(abs_errs)
        rmse_c = math.sqrt(statistics.fmean(sq_errs))
        error_std_c = statistics.stdev(errs) if n_samples >= 2 else 0.0

        stats.append(
            CalibrationStatRow(
                station_id=station_id,
                model=model,
                lead_hours=lead_hours,
                n_samples=n_samples,
                bias_c=bias_c,
                mae_c=mae_c,
                rmse_c=rmse_c,
                error_std_c=error_std_c,
            )
        )
    return stats


def _format_lead_hours(value: float) -> str:
    return f"{value:g}"


def write_calibration_stats_csv(rows: list[CalibrationStatRow], path: Path) -> None:
    """Write per-(station, model, lead_hours) aggregated metrics."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CALIBRATION_STAT_COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    "station_id": row.station_id,
                    "model": row.model,
                    "lead_hours": _format_lead_hours(row.lead_hours),
                    "n_samples": row.n_samples,
                    "bias_c": row.bias_c,
                    "mae_c": row.mae_c,
                    "rmse_c": row.rmse_c,
                    "error_std_c": row.error_std_c,
                }
            )
