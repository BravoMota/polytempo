"""Offline historical forecast ingestion via Open-Meteo Single Runs.

Fetches past model-run daily Tmax predictions for calibration. Not used by live
analysis.
"""

from __future__ import annotations

import json
import math
import sys
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from datetime import timezone as dt_timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from polytempo.weather.data_dir import WEATHER_DATA_DIR

DEFAULT_SINGLE_RUNS_BASE_URL = "https://single-runs-api.open-meteo.com/v1/forecast"
DEFAULT_SINGLE_RUNS_TIMEOUT_S = 120.0
DEFAULT_FORECAST_DAYS = 7
DEFAULT_FORECAST_SOURCE = "open_meteo_single_runs"
DEFAULT_MODEL_RUN_HOURS_UTC: tuple[int, ...] = (0, 6, 12, 18)
DEFAULT_HISTORICAL_FORECASTS_PATH = WEATHER_DATA_DIR / "historical_forecasts.jsonl"
DEFAULT_RAW_FORECASTS_DIR = WEATHER_DATA_DIR / "raw" / "single-runs"


@dataclass(frozen=True)
class HistoricalForecastRecord:
    """One historical model-run Tmax prediction for a target calendar day."""

    station_id: str
    source: str
    model: str
    target_date: date
    forecast_run_time: datetime
    lead_hours: float
    predicted_tmax_c: float


@dataclass(frozen=True)
class HistoricalForecastJob:
    """Planned Single-Runs fetch for one station, model, target day, and lead."""

    station_id: str
    latitude: float
    longitude: float
    model: str
    target_date: date
    forecast_run_time: datetime
    lead_hours: float
    timezone: str


def floor_run_time_to_init_grid(run_time: datetime, interval_hours: float) -> datetime:
    """Floor ``run_time`` to the latest init on a UTC-midnight ``interval_hours`` grid.

    Bootstrap and daily calibration step Single Runs fetches on this grid (e.g. 6h →
    00/06/12/18Z; 3h → 00/03/06/…/21Z). Wall-clock job timestamps must be snapped
    before ``generate_run_times_utc`` or ``lead_hours`` becomes fractional.
    """
    if interval_hours <= 0:
        raise ValueError("interval_hours must be positive")

    run_utc = run_time.astimezone(dt_timezone.utc)
    anchor = datetime.combine(run_utc.date(), time.min, tzinfo=dt_timezone.utc)
    elapsed_h = (run_utc - anchor).total_seconds() / 3600.0
    slot_h = math.floor(elapsed_h / interval_hours) * interval_hours
    return anchor + timedelta(hours=slot_h)


def snap_run_time_to_model_init(
    run_time: datetime,
    model_run_hours_utc: tuple[int, ...] = DEFAULT_MODEL_RUN_HOURS_UTC,
) -> datetime:
    """Floor to the latest archived model init at or before ``run_time`` (UTC)."""
    if not model_run_hours_utc:
        raise ValueError("model_run_hours_utc must not be empty")

    run_utc = run_time.astimezone(dt_timezone.utc)
    day = run_utc.date()
    candidates: list[datetime] = []
    for day_offset in (0, -1):
        base = datetime.combine(
            day + timedelta(days=day_offset),
            time.min,
            tzinfo=dt_timezone.utc,
        )
        for hour in model_run_hours_utc:
            candidate = base.replace(hour=hour)
            if candidate <= run_utc:
                candidates.append(candidate)

    if not candidates:
        raise ValueError("no synoptic model init at or before run_time")

    return max(candidates)


def next_synoptic_run_time(
    run_time: datetime,
    model_run_hours_utc: tuple[int, ...] = DEFAULT_MODEL_RUN_HOURS_UTC,
) -> datetime:
    """Return the next synoptic init strictly after ``run_time`` (UTC)."""
    run_utc = run_time.astimezone(dt_timezone.utc)
    day = run_utc.date()
    candidates: list[datetime] = []
    for day_offset in (0, 1):
        base = datetime.combine(
            day + timedelta(days=day_offset),
            time.min,
            tzinfo=dt_timezone.utc,
        )
        for hour in model_run_hours_utc:
            candidate = base.replace(hour=hour)
            if candidate > run_utc:
                candidates.append(candidate)

    if not candidates:
        raise ValueError("no later synoptic model init")

    return min(candidates)


def _actual_lead_hours(anchor_local: datetime, run_time_utc: datetime) -> float:
    run_local = run_time_utc.astimezone(anchor_local.tzinfo)
    return (anchor_local - run_local).total_seconds() / 3600.0


def generate_forecast_run_times(
    target_date: date,
    max_lead_hours: float,
    lead_step_hours: float,
    *,
    timezone: str,
    model_run_hours_utc: tuple[int, ...] = DEFAULT_MODEL_RUN_HOURS_UTC,
) -> list[tuple[datetime, float]]:
    """Return synoptic (run_time_utc, actual_lead_hours) from max lead down to step."""
    if max_lead_hours <= 0:
        raise ValueError("max_lead_hours must be positive")
    if lead_step_hours <= 0:
        raise ValueError("lead_step_hours must be positive")
    if max_lead_hours < lead_step_hours:
        raise ValueError("max_lead_hours must be >= lead_step_hours")

    tz = ZoneInfo(timezone)
    anchor_local = datetime.combine(target_date, time.min, tzinfo=tz)

    lead_hours_values: list[float] = []
    lead = max_lead_hours
    while lead >= lead_step_hours - 1e-9:
        lead_hours_values.append(lead)
        lead -= lead_step_hours

    seen_runs: set[str] = set()
    out: list[tuple[datetime, float]] = []
    for nominal_lead in lead_hours_values:
        ideal_local = anchor_local - timedelta(hours=nominal_lead)
        snapped_utc = snap_run_time_to_model_init(
            ideal_local.astimezone(dt_timezone.utc),
            model_run_hours_utc=model_run_hours_utc,
        )
        run_key = snapped_utc.isoformat()
        if run_key in seen_runs:
            continue
        seen_runs.add(run_key)

        actual_lead = _actual_lead_hours(anchor_local, snapped_utc)
        if actual_lead <= 0:
            continue
        out.append((snapped_utc, actual_lead))
    return out


def parse_forecast_run_time(value: str) -> datetime:
    """Parse a CLI ``--run-time`` value into a timezone-aware UTC datetime."""
    cleaned = value.strip()
    if not cleaned:
        raise ValueError("run time must not be empty")

    normalized = cleaned.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt_timezone.utc)
    return parsed.astimezone(dt_timezone.utc)


def generate_run_times_utc(
    start: datetime,
    end: datetime,
    interval_hours: float,
) -> list[datetime]:
    """Return UTC inits from ``start`` through ``end`` stepping by ``interval_hours``."""
    if interval_hours <= 0:
        raise ValueError("interval_hours must be positive")

    start_utc = start.astimezone(dt_timezone.utc)
    end_utc = end.astimezone(dt_timezone.utc)
    if end_utc < start_utc:
        raise ValueError("end must be on or after start")

    out: list[datetime] = []
    seen: set[str] = set()
    current = start_utc
    while current <= end_utc:
        key = current.isoformat()
        if key not in seen:
            seen.add(key)
            out.append(current)
        current += timedelta(hours=interval_hours)
    return out


def resolve_raw_fetch_run_times(
    explicit_run_times: list[str],
    *,
    run_time_start: str | None = None,
    run_time_end: str | None = None,
    run_interval_hours: float | None = None,
) -> list[datetime]:
    """Merge explicit ``--run-time`` values with an optional start/end interval range."""
    runs: list[datetime] = [parse_forecast_run_time(value) for value in explicit_run_times]

    if run_time_start:
        start = parse_forecast_run_time(run_time_start)
        end = (
            parse_forecast_run_time(run_time_end)
            if run_time_end
            else start
        )
        if run_interval_hours is None:
            raise ValueError("--run-interval-hours is required with --run-time-start")
        runs.extend(
            generate_run_times_utc(start, end, run_interval_hours),
        )

    unique: list[datetime] = []
    seen: set[str] = set()
    for run_time in runs:
        key = run_time.astimezone(dt_timezone.utc).isoformat()
        if key in seen:
            continue
        seen.add(key)
        unique.append(run_time.astimezone(dt_timezone.utc))
    return unique


def iter_target_dates(start_date: date, end_date: date) -> list[date]:
    """Inclusive calendar-day range."""
    if end_date < start_date:
        raise ValueError("end_date must be on or after start_date")
    days: list[date] = []
    current = start_date
    while current <= end_date:
        days.append(current)
        current += timedelta(days=1)
    return days


def plan_historical_forecast_requests(
    station_id: str,
    latitude: float,
    longitude: float,
    model: str,
    start_date: date,
    end_date: date,
    max_lead_hours: float,
    lead_step_hours: float,
    timezone: str,
) -> list[HistoricalForecastJob]:
    """Build the full fetch plan for one station/model over a date range."""
    jobs: list[HistoricalForecastJob] = []
    seen_job_keys: set[tuple[str, str, str, str]] = set()
    for target_date in iter_target_dates(start_date, end_date):
        for forecast_run_time, lead_hours in generate_forecast_run_times(
            target_date,
            max_lead_hours,
            lead_step_hours,
            timezone=timezone,
        ):
            job = HistoricalForecastJob(
                station_id=station_id,
                latitude=latitude,
                longitude=longitude,
                model=model,
                target_date=target_date,
                forecast_run_time=forecast_run_time,
                lead_hours=lead_hours,
                timezone=timezone,
            )
            key = job_key(job)
            if key in seen_job_keys:
                continue
            seen_job_keys.add(key)
            jobs.append(job)
    return jobs


def sanitize_filename_component(value: str) -> str:
    """Return a filesystem-safe token (no colons, spaces, or path separators)."""
    cleaned = value.strip()
    if not cleaned:
        raise ValueError("filename component must not be empty")

    out: list[str] = []
    for char in cleaned:
        if char.isalnum() or char in "._-":
            out.append(char)
        else:
            out.append("_")
    return "".join(out)


def format_run_time_utc_for_filename(run_time_utc: datetime) -> str:
    """Format UTC init for raw filenames, e.g. ``2026-05-01T120000Z``."""
    if run_time_utc.tzinfo is None:
        raise ValueError("run_time_utc must be timezone-aware")

    run_utc = run_time_utc.astimezone(dt_timezone.utc)
    return run_utc.strftime("%Y-%m-%dT%H%M%S") + "Z"


def build_raw_forecast_filename(
    station_id: str,
    model: str,
    run_time_utc: datetime,
) -> str:
    """Build a raw Single-Runs JSON filename encoding station, model, and run init."""
    station = sanitize_filename_component(station_id)
    model_part = sanitize_filename_component(model)
    run_part = format_run_time_utc_for_filename(run_time_utc)
    return f"{station}_{model_part}_{run_part}.json"


def raw_forecast_path(
    raw_dir: Path,
    station_id: str,
    model: str,
    run_time_utc: datetime,
) -> Path:
    """Full path for one cached raw Single-Runs response."""
    return raw_dir / build_raw_forecast_filename(station_id, model, run_time_utc)


def write_raw_forecast_response(payload: dict[str, Any], path: Path) -> None:
    """Write a full API JSON payload to disk."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def read_raw_forecast_response(path: Path) -> dict[str, Any]:
    """Read a raw Single-Runs JSON file written by ``write_raw_forecast_response``."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"raw forecast file must contain a JSON object: {path}")
    return data


def parse_run_time_utc_from_raw_filename(filename: str) -> datetime:
    """Parse the UTC run init suffix from a raw forecast filename."""
    name = Path(filename).name
    if not name.endswith(".json"):
        raise ValueError(f"not a raw forecast filename: {filename}")

    stem = name[: -len(".json")]
    underscore = stem.rfind("_")
    if underscore < 0:
        raise ValueError(f"run time suffix not found in filename: {filename}")

    run_part = stem[underscore + 1 :]
    if len(run_part) != 18 or run_part[10] != "T" or not run_part.endswith("Z"):
        raise ValueError(f"run time suffix not found in filename: {filename}")

    year = int(run_part[0:4])
    month = int(run_part[5:7])
    day = int(run_part[8:10])
    hour = int(run_part[11:13])
    minute = int(run_part[13:15])
    second = int(run_part[15:17])
    return datetime(year, month, day, hour, minute, second, tzinfo=dt_timezone.utc)


def _station_model_from_raw_filename(filename: str) -> tuple[str, str]:
    """Return ``(station_id, model)`` from a raw forecast filename."""
    name = Path(filename).name
    stem = name[: -len(".json")]
    underscore = stem.rfind("_")
    if underscore < 0:
        raise ValueError(f"invalid raw forecast filename: {filename}")

    prefix = stem[:underscore]
    station_id, model = prefix.split("_", 1)
    return station_id, model


def existing_raw_forecast_keys(raw_dir: Path) -> set[tuple[str, str, str]]:
    """Load ``(station_id, model, run_time_utc_iso)`` keys from ``raw_dir``."""
    if not raw_dir.is_dir():
        return set()

    keys: set[tuple[str, str, str]] = set()
    for path in raw_dir.glob("*.json"):
        try:
            run_time = parse_run_time_utc_from_raw_filename(path.name)
            station_id, model = _station_model_from_raw_filename(path.name)
        except ValueError:
            continue
        keys.add((station_id, model, run_time.isoformat()))
    return keys


def build_single_run_request_params(
    latitude: float,
    longitude: float,
    model: str,
    target_date: date,
    forecast_run_time: datetime,
    *,
    timezone: str,
    forecast_days: int = DEFAULT_FORECAST_DAYS,
) -> dict[str, str | float]:
    """Build query params for one Open-Meteo Single Runs request.

    The Single Runs host rejects ``start_date`` / ``end_date`` when ``run`` is set.
    The full forecast horizon is returned; pick ``target_date`` from ``daily.time``
    during parsing.
    """
    if forecast_run_time.tzinfo is None:
        raise ValueError("forecast_run_time must be timezone-aware")

    if forecast_days < 1:
        raise ValueError("forecast_days must be >= 1")

    run_utc = forecast_run_time.astimezone(dt_timezone.utc)
    run_str = run_utc.strftime("%Y-%m-%dT%H:%M")
    return {
        "latitude": latitude,
        "longitude": longitude,
        "daily": "temperature_2m_max",
        "temperature_unit": "celsius",
        "models": model,
        "timezone": timezone,
        "run": run_str,
        "forecast_days": forecast_days,
    }


def fetch_single_run_payload(
    latitude: float,
    longitude: float,
    model: str,
    target_date: date,
    forecast_run_time: datetime,
    *,
    timezone: str,
    forecast_days: int = DEFAULT_FORECAST_DAYS,
    base_url: str = DEFAULT_SINGLE_RUNS_BASE_URL,
    timeout_s: float = DEFAULT_SINGLE_RUNS_TIMEOUT_S,
    client: Any = httpx,
) -> dict[str, Any]:
    """GET one Single Runs forecast payload."""
    params = build_single_run_request_params(
        latitude,
        longitude,
        model,
        target_date,
        forecast_run_time,
        timezone=timezone,
        forecast_days=forecast_days,
    )
    response = client.get(base_url, params=params, timeout=timeout_s)
    response.raise_for_status()
    return response.json()


def load_or_fetch_single_run_payload(
    latitude: float,
    longitude: float,
    model: str,
    forecast_run_time: datetime,
    *,
    station_id: str,
    timezone: str,
    raw_dir: Path | None = DEFAULT_RAW_FORECASTS_DIR,
    target_date: date | None = None,
    forecast_days: int = DEFAULT_FORECAST_DAYS,
    base_url: str = DEFAULT_SINGLE_RUNS_BASE_URL,
    timeout_s: float = DEFAULT_SINGLE_RUNS_TIMEOUT_S,
    client: Any = httpx,
) -> dict[str, Any]:
    """Load cached raw JSON or fetch and persist under ``raw_dir``."""
    if forecast_run_time.tzinfo is None:
        raise ValueError("forecast_run_time must be timezone-aware")

    run_utc = forecast_run_time.astimezone(dt_timezone.utc)
    if raw_dir is not None:
        path = raw_forecast_path(raw_dir, station_id, model, run_utc)
        if path.exists():
            return read_raw_forecast_response(path)

    placeholder_date = target_date or run_utc.date()
    payload = fetch_single_run_payload(
        latitude,
        longitude,
        model,
        placeholder_date,
        run_utc,
        timezone=timezone,
        forecast_days=forecast_days,
        base_url=base_url,
        timeout_s=timeout_s,
        client=client,
    )
    if raw_dir is not None:
        write_raw_forecast_response(
            payload,
            raw_forecast_path(raw_dir, station_id, model, run_utc),
        )
    return payload


def _find_tmax_series(daily: dict[str, Any], model: str) -> tuple[str, list[Any]]:
    """Locate daily Tmax series; prefer model-specific key."""
    preferred = f"temperature_2m_max_{model}"
    if preferred in daily and isinstance(daily[preferred], list):
        return preferred, daily[preferred]

    if "temperature_2m_max" in daily and isinstance(daily["temperature_2m_max"], list):
        return "temperature_2m_max", daily["temperature_2m_max"]

    for key, series in daily.items():
        if key.startswith("temperature_2m_max") and isinstance(series, list):
            return key, series

    raise ValueError("no temperature_2m_max series found in daily payload")


def extract_predicted_tmax_c(
    payload: dict[str, Any],
    model: str,
    target_date: date,
) -> float:
    """Read predicted daily Tmax for ``target_date`` from a Single Runs payload."""
    daily = payload.get("daily")
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

    _key, series = _find_tmax_series(daily, model)
    if index >= len(series):
        raise ValueError(f"temperature series too short for index {index}")

    raw_value = series[index]
    if raw_value is None:
        raise ValueError(f"null temperature for {target_iso}")

    try:
        predicted_tmax_c = float(raw_value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"temperature at index {index} must be numeric") from exc

    if not math.isfinite(predicted_tmax_c):
        raise ValueError(f"temperature at index {index} must be finite")

    return predicted_tmax_c


def parse_single_run_payload(
    payload: dict[str, Any],
    station_id: str,
    source: str,
    model: str,
    target_date: date,
    forecast_run_time: datetime,
    lead_hours: float,
) -> HistoricalForecastRecord:
    """Parse Single Runs JSON into a HistoricalForecastRecord.

    TODO: Open-Meteo Single-Runs daily variable keys may differ by model
    (plain ``temperature_2m_max`` vs ``temperature_2m_max_<model>``). Verify
    against ``http/open_meteo_single_runs.http`` when adding new models.
    """
    predicted_tmax_c = extract_predicted_tmax_c(payload, model, target_date)

    if forecast_run_time.tzinfo is None:
        raise ValueError("forecast_run_time must be timezone-aware")

    return HistoricalForecastRecord(
        station_id=station_id,
        source=source,
        model=model,
        target_date=target_date,
        forecast_run_time=forecast_run_time.astimezone(dt_timezone.utc),
        lead_hours=lead_hours,
        predicted_tmax_c=predicted_tmax_c,
    )


def _run_times_to_try_for_job(
    job: HistoricalForecastJob,
    model_run_hours_utc: tuple[int, ...] = DEFAULT_MODEL_RUN_HOURS_UTC,
) -> list[tuple[datetime, float]]:
    """Primary synoptic run plus the next init before target when daily Tmax is null."""
    tz = ZoneInfo(job.timezone)
    anchor_local = datetime.combine(job.target_date, time.min, tzinfo=tz)
    candidates: list[datetime] = [job.forecast_run_time.astimezone(dt_timezone.utc)]

    try:
        later = next_synoptic_run_time(candidates[0], model_run_hours_utc)
    except ValueError:
        later = None

    if later is not None and later.astimezone(tz) < anchor_local:
        candidates.append(later)

    out: list[tuple[datetime, float]] = []
    for run_time in candidates:
        lead_hours = _actual_lead_hours(anchor_local, run_time)
        if lead_hours > 0:
            out.append((run_time, lead_hours))
    return out


def fetch_historical_forecast_record(
    job: HistoricalForecastJob,
    *,
    base_url: str = DEFAULT_SINGLE_RUNS_BASE_URL,
    source: str = DEFAULT_FORECAST_SOURCE,
    raw_dir: Path | None = DEFAULT_RAW_FORECASTS_DIR,
    forecast_days: int = DEFAULT_FORECAST_DAYS,
    client: Any = httpx,
) -> HistoricalForecastRecord:
    """Fetch one job, trying a later synoptic init when the primary daily Tmax is null."""
    last_error: Exception | None = None
    for run_time, lead_hours in _run_times_to_try_for_job(job):
        try:
            payload = load_or_fetch_single_run_payload(
                job.latitude,
                job.longitude,
                job.model,
                run_time,
                station_id=job.station_id,
                timezone=job.timezone,
                raw_dir=raw_dir,
                target_date=job.target_date,
                forecast_days=forecast_days,
                base_url=base_url,
                client=client,
            )
            return parse_single_run_payload(
                payload,
                station_id=job.station_id,
                source=source,
                model=job.model,
                target_date=job.target_date,
                forecast_run_time=run_time,
                lead_hours=lead_hours,
            )
        except Exception as exc:
            last_error = exc
            continue

    assert last_error is not None
    raise last_error


def record_key(record: HistoricalForecastRecord) -> tuple[str, str, str, str]:
    """Stable dedupe key for resumable fetches."""
    return (
        record.station_id,
        record.model,
        record.target_date.isoformat(),
        record.forecast_run_time.astimezone(dt_timezone.utc).isoformat(),
    )


def job_key(job: HistoricalForecastJob) -> tuple[str, str, str, str]:
    """Dedupe key for a planned job (matches record_key after fetch)."""
    return (
        job.station_id,
        job.model,
        job.target_date.isoformat(),
        job.forecast_run_time.astimezone(dt_timezone.utc).isoformat(),
    )


def _record_to_dict(record: HistoricalForecastRecord) -> dict[str, Any]:
    return {
        "station_id": record.station_id,
        "source": record.source,
        "model": record.model,
        "target_date": record.target_date.isoformat(),
        "forecast_run_time": record.forecast_run_time.astimezone(dt_timezone.utc).isoformat(),
        "lead_hours": record.lead_hours,
        "predicted_tmax_c": record.predicted_tmax_c,
    }


def _record_from_dict(data: dict[str, Any]) -> HistoricalForecastRecord:
    run_raw = data["forecast_run_time"]
    if isinstance(run_raw, str):
        forecast_run_time = datetime.fromisoformat(run_raw)
        if forecast_run_time.tzinfo is None:
            forecast_run_time = forecast_run_time.replace(tzinfo=dt_timezone.utc)
    else:
        raise ValueError("forecast_run_time must be an ISO8601 string")

    return HistoricalForecastRecord(
        station_id=str(data["station_id"]),
        source=str(data["source"]),
        model=str(data["model"]),
        target_date=date.fromisoformat(str(data["target_date"])),
        forecast_run_time=forecast_run_time.astimezone(dt_timezone.utc),
        lead_hours=float(data["lead_hours"]),
        predicted_tmax_c=float(data["predicted_tmax_c"]),
    )


def write_historical_forecasts_jsonl(
    records: list[HistoricalForecastRecord],
    path: Path,
) -> None:
    """Append forecast records as JSONL."""
    if not records:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(_record_to_dict(record), sort_keys=True))
            handle.write("\n")


def read_historical_forecasts_jsonl(path: Path) -> list[HistoricalForecastRecord]:
    """Read all forecast records; missing file returns empty list."""
    if not path.exists():
        return []

    records: list[HistoricalForecastRecord] = []
    with path.open(encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                data = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSON on line {line_no} of {path}") from exc
            if not isinstance(data, dict):
                raise ValueError(f"expected object on line {line_no} of {path}")
            records.append(_record_from_dict(data))
    return records


def existing_record_keys(path: Path) -> set[tuple[str, str, str, str]]:
    """Load dedupe keys from an existing JSONL file."""
    return {record_key(record) for record in read_historical_forecasts_jsonl(path)}


def fetch_raw_forecast_runs(
    station_id: str,
    latitude: float,
    longitude: float,
    model: str,
    run_times_utc: list[datetime],
    *,
    timezone: str,
    raw_dir: Path = DEFAULT_RAW_FORECASTS_DIR,
    forecast_days: int = DEFAULT_FORECAST_DAYS,
    base_url: str = DEFAULT_SINGLE_RUNS_BASE_URL,
    client: Any = httpx,
) -> tuple[int, int, int]:
    """Fetch one raw JSON file per unique run init. Returns fetched/skipped/failed."""
    seen = existing_raw_forecast_keys(raw_dir)
    fetched = 0
    skipped = 0
    failed = 0
    station = station_id.strip()
    model_id = model.strip()

    unique_runs: list[datetime] = []
    seen_run_iso: set[str] = set()
    for run_time in run_times_utc:
        if run_time.tzinfo is None:
            raise ValueError("run_times_utc entries must be timezone-aware")
        run_utc = run_time.astimezone(dt_timezone.utc)
        run_iso = run_utc.isoformat()
        if run_iso in seen_run_iso:
            continue
        seen_run_iso.add(run_iso)
        unique_runs.append(run_utc)

    for run_utc in unique_runs:
        key = (
            sanitize_filename_component(station),
            sanitize_filename_component(model_id),
            run_utc.isoformat(),
        )
        if key in seen:
            skipped += 1
            continue

        try:
            load_or_fetch_single_run_payload(
                latitude,
                longitude,
                model_id,
                run_utc,
                station_id=station,
                timezone=timezone,
                raw_dir=raw_dir,
                forecast_days=forecast_days,
                base_url=base_url,
                client=client,
            )
        except Exception as exc:
            print(
                f"skip station={station} model={model_id} "
                f"run={run_utc.isoformat()}: {exc}",
                file=sys.stderr,
            )
            failed += 1
            continue

        seen.add(key)
        fetched += 1

    return fetched, skipped, failed


def fetch_historical_forecast_batch(
    jobs: list[HistoricalForecastJob],
    *,
    out_path: Path,
    base_url: str = DEFAULT_SINGLE_RUNS_BASE_URL,
    source: str = DEFAULT_FORECAST_SOURCE,
    raw_dir: Path | None = DEFAULT_RAW_FORECASTS_DIR,
    forecast_days: int = DEFAULT_FORECAST_DAYS,
    client: Any = httpx,
) -> tuple[int, int, int]:
    """Fetch planned jobs, skip duplicates, append successes. Returns fetched/skipped/failed."""
    seen = existing_record_keys(out_path)
    fetched = 0
    skipped = 0
    failed = 0
    batch: list[HistoricalForecastRecord] = []

    for job in jobs:
        key = job_key(job)
        if key in seen:
            skipped += 1
            continue

        try:
            record = fetch_historical_forecast_record(
                job,
                base_url=base_url,
                source=source,
                raw_dir=raw_dir,
                forecast_days=forecast_days,
                client=client,
            )
        except Exception as exc:
            print(
                f"skip station={job.station_id} model={job.model} "
                f"target={job.target_date.isoformat()} "
                f"run={job.forecast_run_time.isoformat()}: {exc}",
                file=sys.stderr,
            )
            failed += 1
            continue

        record_key_value = record_key(record)
        if record_key_value in seen:
            skipped += 1
            continue

        batch.append(record)
        seen.add(job_key(job))
        seen.add(record_key_value)
        fetched += 1

    write_historical_forecasts_jsonl(batch, out_path)
    return fetched, skipped, failed
