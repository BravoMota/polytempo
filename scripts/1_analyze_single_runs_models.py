#!/usr/bin/env python3
"""Probe Open-Meteo Single Runs per model: run-init cadence, grid offset, daily Tmax.

Edit the configuration block below, then run (see COMMANDS.md). Writes one CSV row
per model. Every **successful** API response is saved under ``raw_capabilities/`` for
manual inspection (filenames encode model, probe, run, forecast_days).
"""

from __future__ import annotations

import csv
import json
import math
import sys
import time
from pathlib import Path
from typing import Any

import httpx

# --- configuration (edit here) ---

MODELS: list[str] = [
    "ecmwf_ifs",
    "ecmwf_ifs025",
    "gem_global",
    "gfs_seamless",
    "icon_d2",
    "icon_eu",
    "icon_global",
    "meteofrance_arpege_europe",
    "ukmo_global_deterministic_10km",
    "ukmo_uk_deterministic_2km",
]

STATION_LABEL = "EGLC"
LATITUDE = 51.5053
LONGITUDE = 0.0553
PROBE_DATE = "2026-03-29"  # UTC date for every run= probe
TIMEZONE = "Europe/London"

BASE_URL = "https://single-runs-api.open-meteo.com/v1/forecast"
REQUEST_TIMEOUT_S = 120.0
SLEEP_BETWEEN_REQUESTS_S = 0.5

FORECAST_DAYS_MAX = 16
# Daily Tmax non-null count at run= …T18:00 (UTC), forecast_days=FORECAST_DAYS_MAX.
DAILY_NON_NULL_RUN_HOUR_UTC = 18
DAILY_NON_NULL_ALT_RUN_HOUR_UTC = 12
RUN_INIT_LADDER_UTC = [0, 1, 3, 6, 12]

OUTPUT_CSV = Path(__file__).resolve().parent.parent / "data" / "weather" / "single_runs_model_capabilities.csv"
RAW_CAPABILITIES_DIR = Path(__file__).resolve().parent.parent / "data" / "weather" / "raw_capabilities"


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius_km = 6371.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    a = (
        math.sin(d_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    )
    return 2 * radius_km * math.asin(math.sqrt(min(1.0, a)))


def _run_time(hour_utc: int) -> str:
    return f"{PROBE_DATE}T{hour_utc:02d}:00"


def _safe_token(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in value)


def _write_raw(
    *,
    model: str,
    probe: str,
    run_time_utc: str,
    forecast_days: int,
    payload: dict[str, Any],
) -> None:
    RAW_CAPABILITIES_DIR.mkdir(parents=True, exist_ok=True)
    run_token = _safe_token(run_time_utc.replace(":", ""))
    filename = (
        f"{STATION_LABEL}_{_safe_token(model)}_{_safe_token(probe)}_"
        f"{run_token}_fd{forecast_days}.json"
    )
    (RAW_CAPABILITIES_DIR / filename).write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _daily_tmax_series(daily: dict[str, Any], model: str) -> list[Any] | None:
    for key in (f"temperature_2m_max_{model}", "temperature_2m_max"):
        value = daily.get(key)
        if isinstance(value, list):
            return value
    for key, value in daily.items():
        if key.startswith("temperature_2m_max") and isinstance(value, list):
            return value
    return None


def _count_non_null(series: list[Any] | None) -> int:
    if not series:
        return 0
    return sum(1 for value in series if value is not None)


def _fetch(
    client: httpx.Client,
    *,
    model: str,
    probe: str,
    run_time_utc: str,
    forecast_days: int,
    daily: bool = True,
    hourly: bool = False,
) -> tuple[dict[str, Any] | None, str | None]:
    params: dict[str, str | float | int] = {
        "latitude": LATITUDE,
        "longitude": LONGITUDE,
        "models": model,
        "timezone": TIMEZONE,
        "run": run_time_utc,
        "forecast_days": forecast_days,
        "temperature_unit": "celsius",
    }
    if daily:
        params["daily"] = "temperature_2m_max"
    if hourly:
        params["hourly"] = "temperature_2m"

    try:
        response = client.get(BASE_URL, params=params, timeout=REQUEST_TIMEOUT_S)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            return None, "response is not a JSON object"
    except httpx.HTTPStatusError as exc:
        body = exc.response.text[:160].replace("\n", " ")
        return None, f"HTTP {exc.response.status_code}: {body}"
    except Exception as exc:
        return None, str(exc)

    _write_raw(
        model=model,
        probe=probe,
        run_time_utc=run_time_utc,
        forecast_days=forecast_days,
        payload=payload,
    )
    return payload, None


def _probe_baseline_at_00z(
    client: httpx.Client,
    model: str,
) -> tuple[int, dict[str, Any] | None, str | None]:
    """00Z baseline: grid cell + non-null daily Tmax count at 00Z (internal cadence input)."""
    run_time_utc = _run_time(0)
    payload, error = _fetch(
        client,
        model=model,
        probe="baseline_00z",
        run_time_utc=run_time_utc,
        forecast_days=FORECAST_DAYS_MAX,
    )
    time.sleep(SLEEP_BETWEEN_REQUESTS_S)

    if payload is None:
        return 0, None, error

    daily = payload.get("daily")
    series = _daily_tmax_series(daily, model) if isinstance(daily, dict) else None
    return _count_non_null(series), payload, None


def _probe_daily_non_null_at_18z(
    client: httpx.Client,
    model: str,
) -> tuple[int | None, str | None]:
    """Count non-null daily Tmax at run= PROBE_DATE T18:00 UTC."""
    run_time_utc = _run_time(DAILY_NON_NULL_RUN_HOUR_UTC)
    payload, error = _fetch(
        client,
        model=model,
        probe="daily_non_null_18z",
        run_time_utc=run_time_utc,
        forecast_days=FORECAST_DAYS_MAX,
    )
    time.sleep(SLEEP_BETWEEN_REQUESTS_S)
    if payload is None:
        return None, error

    daily = payload.get("daily") if isinstance(payload, dict) else None
    series = _daily_tmax_series(daily, model) if isinstance(daily, dict) else None
    return _count_non_null(series), None


def _probe_daily_non_null_at_12z(
    client: httpx.Client,
    model: str,
) -> tuple[int | None, str | None]:
    """Count non-null daily Tmax at run= PROBE_DATE T12:00 UTC."""
    run_time_utc = _run_time(DAILY_NON_NULL_ALT_RUN_HOUR_UTC)
    payload, error = _fetch(
        client,
        model=model,
        probe="daily_non_null_12z",
        run_time_utc=run_time_utc,
        forecast_days=FORECAST_DAYS_MAX,
    )
    time.sleep(SLEEP_BETWEEN_REQUESTS_S)
    if payload is None:
        return None, error

    daily = payload.get("daily") if isinstance(payload, dict) else None
    series = _daily_tmax_series(daily, model) if isinstance(daily, dict) else None
    return _count_non_null(series), None


def _probe_run_init_cadence(
    client: httpx.Client,
    model: str,
    baseline_non_null_at_00z: int,
) -> dict[str, Any]:
    """First ladder hour > 0 with non-null daily Tmax → run_init_interval_hours."""
    cadence_hours: float | None = None
    cadence_run_time = ""
    probe_error = ""

    if baseline_non_null_at_00z == 0:
        return {
            "run_init_interval_hours": "",
            "run_init_interval_source_run_utc": "",
            "run_init_probe_error": "baseline 00Z had no non-null Tmax",
        }

    for hour in RUN_INIT_LADDER_UTC:
        if hour == 0:
            continue
        run_time_utc = _run_time(hour)
        payload, error = _fetch(
            client,
            model=model,
            probe=f"run_init_{hour:02d}z",
            run_time_utc=run_time_utc,
            forecast_days=2,
        )
        time.sleep(SLEEP_BETWEEN_REQUESTS_S)

        if payload is None:
            continue

        daily = payload.get("daily") if isinstance(payload, dict) else None
        series = _daily_tmax_series(daily, model) if isinstance(daily, dict) else None
        if _count_non_null(series) > 0:
            cadence_hours = float(hour)
            cadence_run_time = run_time_utc
            break

    if cadence_hours is None:
        probe_error = "no ladder hour > 0 returned non-null Tmax"

    return {
        "run_init_interval_hours": cadence_hours if cadence_hours is not None else "",
        "run_init_interval_source_run_utc": cadence_run_time,
        "run_init_probe_error": probe_error,
    }


def analyze_model(client: httpx.Client, model: str) -> dict[str, str]:
    row: dict[str, str] = {
        "station": STATION_LABEL,
        "model": model,
        "status": "ok",
        "error": "",
        "response_latitude": "",
        "response_longitude": "",
        "grid_distance_km": "",
        "daily_non_null_at_00z": "",
        "daily_non_null_at_12z": "",
        "daily_non_null_at_18z": "",
        "run_init_interval_hours": "",
        "run_init_interval_source_run_utc": "",
        "run_init_probe_error": "",
        "generationtime_ms": "",
    }

    baseline_non_null, snapshot, baseline_error = _probe_baseline_at_00z(client, model)
    row["daily_non_null_at_00z"] = str(baseline_non_null)

    if snapshot is None:
        row["status"] = "error"
        row["error"] = baseline_error or "baseline 00Z fetch failed"
        return row

    resp_lat = snapshot.get("latitude")
    resp_lon = snapshot.get("longitude")
    if isinstance(resp_lat, (int, float)) and isinstance(resp_lon, (int, float)):
        row["response_latitude"] = str(resp_lat)
        row["response_longitude"] = str(resp_lon)
        row["grid_distance_km"] = (
            f"{haversine_km(LATITUDE, LONGITUDE, float(resp_lat), float(resp_lon)):.3f}"
        )

    gen_ms = snapshot.get("generationtime_ms")
    row["generationtime_ms"] = str(gen_ms) if gen_ms is not None else ""

    count_18, err_18 = _probe_daily_non_null_at_18z(client, model)
    count_12, err_12 = _probe_daily_non_null_at_12z(client, model)
    if count_12 is None:
        row["daily_non_null_at_12z"] = ""
    else:
        row["daily_non_null_at_12z"] = str(count_12)

    if count_18 is None:
        row["daily_non_null_at_18z"] = ""
        if count_12 is None:
            row["status"] = "error"
            row["error"] = (err_18 or "18Z daily fetch failed") + "; " + (
                err_12 or "12Z daily fetch failed"
            )
        elif count_12 == 0:
            row["status"] = "no_daily_tmax_12z_18z"
            row["error"] = "no non-null daily Tmax at 12:00 or 18:00 UTC run init"
        else:
            row["status"] = "ok"
            row["error"] = ""
    else:
        row["daily_non_null_at_18z"] = str(count_18)
        if count_18 == 0 and (count_12 or 0) == 0:
            row["status"] = "no_daily_tmax_12z_18z"
            row["error"] = "no non-null daily Tmax at 12:00 or 18:00 UTC run init"
        elif count_18 == 0 and (count_12 or 0) > 0:
            row["status"] = "ok"
            row["error"] = ""

    cadence = _probe_run_init_cadence(client, model, baseline_non_null)
    row.update({key: str(value) for key, value in cadence.items()})

    if row["status"] == "ok" and cadence.get("run_init_probe_error"):
        row["status"] = "partial"
        row["error"] = str(cadence["run_init_probe_error"])

    return row


def main() -> int:
    OUTPUT_CSV.parent.mkdir(parents=True, exist_ok=True)

    fieldnames = [
        "station",
        "model",
        "status",
        "error",
        "response_latitude",
        "response_longitude",
        "grid_distance_km",
        "daily_non_null_at_00z",
        "daily_non_null_at_12z",
        "daily_non_null_at_18z",
        "run_init_interval_hours",
        "run_init_interval_source_run_utc",
        "run_init_probe_error",
        "generationtime_ms",
    ]

    rows: list[dict[str, str]] = []
    with httpx.Client() as client:
        for index, model in enumerate(MODELS):
            print(f"[{index + 1}/{len(MODELS)}] {model}", file=sys.stderr)
            rows.append(analyze_model(client, model))

    with OUTPUT_CSV.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

    print(f"wrote {OUTPUT_CSV} ({len(rows)} models)", file=sys.stderr)
    print(f"raw responses under {RAW_CAPABILITIES_DIR}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
