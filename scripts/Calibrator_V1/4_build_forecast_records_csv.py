#!/usr/bin/env python3
"""Build a processed CSV of daily Tmax predictions from raw Single Runs JSON.

Reads all cached raw Open-Meteo responses under ``data/weather/raw/single-runs/`` and writes
one row per non-null ``daily.temperature_2m_max`` prediction to
``data/weather/processed/forecast_records.csv``. Raw JSON files are
left untouched. Offline calibration tooling only.
"""

from __future__ import annotations

import csv
import math
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from datetime import timezone as dt_timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from polytempo.weather.data_dir import WEATHER_DATA_DIR
from polytempo.weather.historical_forecasts import (  # noqa: E402
    DEFAULT_RAW_FORECASTS_DIR,
    _station_model_from_raw_filename,
    parse_run_time_utc_from_raw_filename,
    read_raw_forecast_response,
)

# --- magic vars (edit here) ---

RAW_DIR = DEFAULT_RAW_FORECASTS_DIR
OUT_PATH = WEATHER_DATA_DIR / "processed" / "forecast_records.csv"

FORECAST_RECORD_COLUMNS = (
    "station_id",
    "model",
    "run_time_utc",
    "target_date",
    "lead_hours",
    "predicted_tmax_c",
    "forecast_lat",
    "forecast_lon",
)


@dataclass(frozen=True)
class ForecastRow:
    """One predicted daily Tmax from a single raw Single Runs response."""

    station_id: str
    model: str
    run_time_utc: datetime
    target_date: date
    lead_hours: float
    predicted_tmax_c: float
    forecast_lat: float
    forecast_lon: float


def compute_lead_hours(run_time_utc: datetime, target_date: date) -> float:
    """Return hours from ``run_time_utc`` to end-of-``target_date`` (UTC).

    End-of-target is midnight UTC on the day after ``target_date``.
    Example: run 2026-04-01T12:00Z, target 2026-04-02 -> 36h.
    """
    if run_time_utc.tzinfo is None:
        raise ValueError("run_time_utc must be timezone-aware")

    end_of_target_utc = datetime.combine(
        target_date + timedelta(days=1),
        time.min,
        tzinfo=dt_timezone.utc,
    )
    run_utc = run_time_utc.astimezone(dt_timezone.utc)
    return (end_of_target_utc - run_utc).total_seconds() / 3600.0


def _format_run_time_utc(value: datetime) -> str:
    return value.astimezone(dt_timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _format_lead_hours(value: float) -> str:
    return f"{value:g}"


def _extract_forecast_coords(payload: dict[str, Any]) -> tuple[float, float]:
    latitude = payload.get("latitude")
    longitude = payload.get("longitude")
    if not isinstance(latitude, (int, float)) or not isinstance(longitude, (int, float)):
        raise ValueError("payload latitude and longitude must be numeric")
    return float(latitude), float(longitude)


def iter_forecast_rows_from_payload(
    payload: dict[str, Any],
    station_id: str,
    model: str,
    run_time_utc: datetime,
) -> Iterator[ForecastRow]:
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

        try:
            predicted_tmax_c = float(temp_raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"temperature must be numeric, got {temp_raw!r}") from exc
        if not math.isfinite(predicted_tmax_c):
            continue

        target_date = date.fromisoformat(str(time_raw))
        lead_hours = compute_lead_hours(run_utc, target_date)
        if lead_hours <= 0:
            continue

        yield ForecastRow(
            station_id=station_id,
            model=model,
            run_time_utc=run_utc,
            target_date=target_date,
            lead_hours=lead_hours,
            predicted_tmax_c=predicted_tmax_c,
            forecast_lat=forecast_lat,
            forecast_lon=forecast_lon,
        )


def build_forecast_rows(raw_dir: Path) -> list[ForecastRow]:
    """Read all raw JSON files under ``raw_dir`` and return sorted forecast rows."""
    if not raw_dir.is_dir():
        return []

    rows: list[ForecastRow] = []
    for path in sorted(raw_dir.glob("*.json")):
        try:
            run_time_utc = parse_run_time_utc_from_raw_filename(path.name)
            station_id, model = _station_model_from_raw_filename(path.name)
            payload = read_raw_forecast_response(path)
        except (ValueError, OSError) as exc:
            print(f"skip {path.name}: {exc}", file=sys.stderr)
            continue

        try:
            rows.extend(
                iter_forecast_rows_from_payload(
                    payload,
                    station_id=station_id,
                    model=model,
                    run_time_utc=run_time_utc,
                )
            )
        except ValueError as exc:
            print(f"skip {path.name}: {exc}", file=sys.stderr)
            continue

    rows.sort(
        key=lambda row: (
            row.station_id,
            row.model,
            row.run_time_utc,
            row.target_date,
        )
    )
    return rows


def write_forecast_records_csv(rows: list[ForecastRow], path: Path) -> None:
    """Write forecast rows to CSV with a stable column order."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FORECAST_RECORD_COLUMNS)
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
                    "forecast_lat": row.forecast_lat,
                    "forecast_lon": row.forecast_lon,
                }
            )


def main() -> int:
    rows = build_forecast_rows(RAW_DIR)
    if not rows:
        print("no forecast rows found; not writing output", file=sys.stderr)
        return 2

    write_forecast_records_csv(rows, OUT_PATH)
    print(f"wrote {len(rows)} rows to {OUT_PATH}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
