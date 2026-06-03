#!/usr/bin/env python3
"""Offline calibration: join forecast records with observations and aggregate.

Reads ``data/weather/processed/forecast_records.csv`` and
``data/weather/observed_tmax.csv`` and writes:

- ``data/weather/statistical/forecast_errors.csv`` (one row per joined prediction)
- ``data/weather/statistical/calibration_stats.csv`` (grouped by station/model/lead)

Grouping uses **exact** ``lead_hours`` (not lead buckets) to preserve native
model cadence resolution (e.g. 3h for ukmo_uk_deterministic_2km).

One-sample groups report ``error_std_c = 0.0`` (numeric, not blank) so the
column always parses as float. ``error_std_c`` is the sample standard deviation
of ``error_c``; it is **not** named ``std_error_c`` because "standard error"
conventionally means ``sd / sqrt(n)``, which is not the quantity reported here.

Offline tooling only. Must not be used by live analysis.
"""

from __future__ import annotations

import csv
import math
import statistics
import sys
from dataclasses import dataclass
from datetime import date, datetime
from datetime import timezone as dt_timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from polytempo.weather.data_dir import WEATHER_DATA_DIR  # noqa: E402

# --- magic vars (edit here) ---

FORECASTS_CSV = WEATHER_DATA_DIR / "processed" / "forecast_records.csv"
OBSERVATIONS_CSV = WEATHER_DATA_DIR / "observed_tmax.csv"
FORECAST_ERRORS_CSV = WEATHER_DATA_DIR / "statistical" / "forecast_errors.csv"
CALIBRATION_STATS_CSV = WEATHER_DATA_DIR / "statistical" / "calibration_stats.csv"

FORECAST_ERROR_COLUMNS = (
    "station_id",
    "model",
    "run_time_utc",
    "target_date",
    "lead_hours",
    "predicted_tmax_c",
    "observed_tmax_c",
    "error_c",
    "abs_error_c",
    "squared_error_c",
)

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


@dataclass(frozen=True)
class CalibrationStatRow:
    """Aggregated error metrics for one (station, model, lead_hours) group."""

    station_id: str
    model: str
    lead_hours: float
    n_samples: int
    bias_c: float
    mae_c: float
    rmse_c: float
    error_std_c: float


def _parse_float(value: Any) -> float | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = float(text)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed):
        return None
    return parsed


def _parse_run_time_utc(value: str) -> datetime | None:
    text = str(value).strip()
    if not text:
        return None
    normalized = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt_timezone.utc)
    return parsed.astimezone(dt_timezone.utc)


def _parse_date(value: str) -> date | None:
    text = str(value).strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def load_forecast_records(path: Path) -> list[dict[str, Any]]:
    """Read forecast_records.csv and parse typed fields. Skips invalid rows."""
    if not path.exists():
        return []

    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        for raw in reader:
            station_id = (raw.get("station_id") or "").strip()
            model = (raw.get("model") or "").strip()
            run_time_utc = _parse_run_time_utc(raw.get("run_time_utc") or "")
            target_date = _parse_date(raw.get("target_date") or "")
            lead_hours = _parse_float(raw.get("lead_hours"))
            predicted_tmax_c = _parse_float(raw.get("predicted_tmax_c"))

            if (
                not station_id
                or not model
                or run_time_utc is None
                or target_date is None
                or lead_hours is None
                or predicted_tmax_c is None
            ):
                continue

            rows.append(
                {
                    "station_id": station_id,
                    "model": model,
                    "run_time_utc": run_time_utc,
                    "target_date": target_date,
                    "lead_hours": lead_hours,
                    "predicted_tmax_c": predicted_tmax_c,
                }
            )
    return rows


def load_observations_map(path: Path) -> dict[tuple[str, date], float]:
    """Read observed_tmax.csv into a (station_id, target_date) -> tmax map."""
    if not path.exists():
        return {}

    out: dict[tuple[str, date], float] = {}
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        for raw in reader:
            station_id = (raw.get("station_id") or "").strip()
            target_date = _parse_date(raw.get("target_date") or "")
            observed_tmax_c = _parse_float(raw.get("observed_tmax_c"))

            if not station_id or target_date is None or observed_tmax_c is None:
                continue
            out[(station_id, target_date)] = observed_tmax_c
    return out


def join_with_observations(
    forecast_records: list[dict[str, Any]],
    observations: dict[tuple[str, date], float],
) -> list[ForecastError]:
    """Inner-join forecast rows on (station_id, target_date) with observations."""
    joined: list[ForecastError] = []
    for record in forecast_records:
        key = (record["station_id"], record["target_date"])
        observed = observations.get(key)
        if observed is None:
            continue

        predicted = record["predicted_tmax_c"]
        error_c = predicted - observed
        joined.append(
            ForecastError(
                station_id=record["station_id"],
                model=record["model"],
                run_time_utc=record["run_time_utc"],
                target_date=record["target_date"],
                lead_hours=record["lead_hours"],
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


def compute_calibration_stats(
    errors: list[ForecastError],
) -> list[CalibrationStatRow]:
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


def _format_run_time_utc(value: datetime) -> str:
    return value.astimezone(dt_timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _format_lead_hours(value: float) -> str:
    return f"{value:g}"


def write_forecast_errors_csv(rows: list[ForecastError], path: Path) -> None:
    """Write joined forecast/observation/error rows."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FORECAST_ERROR_COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    "station_id": row.station_id,
                    "model": row.model,
                    "run_time_utc": _format_run_time_utc(row.run_time_utc),
                    "target_date": row.target_date.isoformat(),
                    "lead_hours": _format_lead_hours(row.lead_hours),
                    "predicted_tmax_c": row.predicted_tmax_c,
                    "observed_tmax_c": row.observed_tmax_c,
                    "error_c": row.error_c,
                    "abs_error_c": row.abs_error_c,
                    "squared_error_c": row.squared_error_c,
                }
            )


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


def main() -> int:
    forecast_records = load_forecast_records(FORECASTS_CSV)
    observations = load_observations_map(OBSERVATIONS_CSV)

    errors = join_with_observations(forecast_records, observations)
    if not errors:
        print(
            "no forecast rows joined with observations; not writing output",
            file=sys.stderr,
        )
        return 2

    write_forecast_errors_csv(errors, FORECAST_ERRORS_CSV)
    stats = compute_calibration_stats(errors)
    write_calibration_stats_csv(stats, CALIBRATION_STATS_CSV)

    print(
        f"wrote {len(errors)} error rows to {FORECAST_ERRORS_CSV}",
        file=sys.stderr,
    )
    print(
        f"wrote {len(stats)} stat groups to {CALIBRATION_STATS_CSV}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
