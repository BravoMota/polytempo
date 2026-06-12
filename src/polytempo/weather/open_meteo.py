"""Open-Meteo forecast ingestion.

Fetches daily maximum temperature forecasts from Open-Meteo and normalizes them
into a list of Celsius values suitable for the distribution builder. Should not
make trading decisions.

When multiple ``models`` are requested, Open-Meteo returns a separate
``temperature_2m_max_<model>`` series per model; we collect every value for the
target date as the forecast sample set.

Live collector path pairs rolling S3 metadata (run init) with Forecast API values
in :func:`fetch_open_meteo_live_bundle` and persists parsed rows via
:func:`persist_open_meteo_fetch`.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any

import httpx
from psycopg import Connection

from polytempo.model.lead_time import lead_hours_to_end_of_target_day
from polytempo.storage.postgres import (
    insert_open_meteo_fetch_cycle,
    insert_open_meteo_forecast_snapshot,
    insert_open_meteo_model_meta_snapshot,
    utc_now_iso,
)
from polytempo.weather.calibration_compute import compute_lead_hours
from polytempo.weather.schema import ForecastValues
from polytempo.weather.stations import Station

logger = logging.getLogger(__name__)

DEFAULT_MODELS: tuple[str, ...] = (
    "ukmo_global_deterministic_10km",
    "icon_eu",
    "gfs_seamless",
    "ecmwf_ifs025",
    "ukmo_uk_deterministic_2km",
    "ukmo_seamless",
    "ecmwf_ifs",
    "icon_seamless",
)

PLAUSIBLE_MIN_C = -40.0
PLAUSIBLE_MAX_C = 60.0

DEFAULT_FORECAST_TIMEOUT_S = 60.0
DEFAULT_FORECAST_MAX_RETRIES = 3
DEFAULT_FORECAST_RETRY_BACKOFF_S = (2.0, 4.0)
DEFAULT_META_TIMEOUT_S = 30.0

ROLLING_META_URL = "https://openmeteo.s3.amazonaws.com/data/{model}/static/meta.json"

_RETRYABLE_STATUS_CODES = frozenset({502, 503, 504})


@dataclass(frozen=True)
class ModelRunMeta:
    """Parsed rolling-store metadata for one Open-Meteo model."""

    model: str
    run_init_utc: datetime
    run_available_utc: datetime
    run_modified_utc: datetime
    update_interval_seconds: int
    temporal_resolution_seconds: int
    data_end_utc: datetime | None


@dataclass(frozen=True)
class OpenMeteoLiveBundle:
    """One paired meta + forecast fetch cycle."""

    fetched_at_utc: datetime
    requested_lat: float
    requested_lon: float
    returned_lat: float
    returned_lon: float
    daily_by_date: dict[date, DailyMaxForecast]
    meta_by_model: dict[str, ModelRunMeta]
    init_lead_hours: dict[tuple[str, date], float]
    wall_clock_lead_hours: dict[tuple[str, date], float]
    meta_staleness_detected: bool


@dataclass(frozen=True)
class DailyMaxForecast:
    """Normalized daily-max-temperature forecast for one location/date."""

    target_date: date
    latitude: float
    longitude: float
    values_c: list[float]
    models: list[str]

    def to_forecast_values(self, source: str = "open_meteo") -> ForecastValues:
        """Map into :class:`~polytempo.weather.schema.ForecastValues` for calibration and analysis."""
        return ForecastValues(
            source=source,
            latitude=self.latitude,
            longitude=self.longitude,
            target_date=self.target_date,
            values_c=list(self.values_c),
            models=list(self.models) if self.models else None,
        )


def daily_to_forecast_values(
    bundle: OpenMeteoLiveBundle,
    target_date: date,
    *,
    source: str = "open_meteo",
) -> ForecastValues:
    """Build :class:`ForecastValues` with per-model init lead from a live bundle."""
    daily = bundle.daily_by_date[target_date]
    init_leads: list[float] = []
    run_inits: list[str] = []
    for model in daily.models:
        key = (model, target_date)
        if model in bundle.meta_by_model and key in bundle.init_lead_hours:
            init_leads.append(bundle.init_lead_hours[key])
            run_inits.append(format_run_time_utc(bundle.meta_by_model[model].run_init_utc))
            continue
        wall_lead = bundle.wall_clock_lead_hours.get(
            key,
            lead_hours_to_end_of_target_day(target_date, now=bundle.fetched_at_utc),
        )
        init_leads.append(wall_lead)
        run_inits.append("")
    return ForecastValues(
        source=source,
        latitude=daily.latitude,
        longitude=daily.longitude,
        target_date=target_date,
        values_c=list(daily.values_c),
        models=list(daily.models),
        init_lead_hours=init_leads,
        model_run_init_utc=run_inits,
    )


def format_run_time_utc(value: datetime) -> str:
    """UTC ISO instant matching calibration_forecast_records.run_time_utc."""
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _timestamp_to_utc(seconds: int) -> datetime:
    return datetime.fromtimestamp(seconds, tz=timezone.utc)


def parse_rolling_meta_payload(model: str, payload: dict[str, Any]) -> ModelRunMeta:
    """Parse Open-Meteo rolling ``meta.json`` into :class:`ModelRunMeta`."""
    try:
        init_s = int(payload["last_run_initialisation_time"])
        avail_s = int(payload["last_run_availability_time"])
        mod_s = int(payload["last_run_modification_time"])
        update_interval = int(payload["update_interval_seconds"])
        temporal_resolution = int(payload["temporal_resolution_seconds"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"invalid meta.json for model {model!r}") from exc

    data_end_raw = payload.get("data_end_time")
    data_end_utc: datetime | None = None
    if data_end_raw is not None:
        data_end_utc = _timestamp_to_utc(int(data_end_raw))

    return ModelRunMeta(
        model=model,
        run_init_utc=_timestamp_to_utc(init_s),
        run_available_utc=_timestamp_to_utc(avail_s),
        run_modified_utc=_timestamp_to_utc(mod_s),
        update_interval_seconds=update_interval,
        temporal_resolution_seconds=temporal_resolution,
        data_end_utc=data_end_utc,
    )


def availability_lag_hours(meta: ModelRunMeta) -> float:
    """Hours from model init to rolling-store availability."""
    delta = meta.run_available_utc - meta.run_init_utc
    return max(0.0, delta.total_seconds() / 3600.0)


def fetch_rolling_meta(
    model: str,
    *,
    client: httpx.Client | None = None,
    timeout_s: float = DEFAULT_META_TIMEOUT_S,
) -> ModelRunMeta:
    """Fetch and parse rolling metadata for one model."""
    url = ROLLING_META_URL.format(model=model)
    own_client = client is None
    http = client or httpx.Client()
    try:
        response = http.get(url, timeout=timeout_s)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError(f"meta.json for {model!r} must be a JSON object")
        return parse_rolling_meta_payload(model, payload)
    finally:
        if own_client:
            http.close()


def fetch_rolling_meta_batch(
    models: tuple[str, ...] | list[str],
    *,
    client: httpx.Client | None = None,
    skip_missing: bool = False,
) -> dict[str, ModelRunMeta]:
    """Fetch rolling metadata for each model.

    When ``skip_missing`` is True, models whose ``meta.json`` returns 404 are
    omitted (logged at WARNING) instead of failing the batch.
    """
    own_client = client is None
    http = client or httpx.Client()
    try:
        result: dict[str, ModelRunMeta] = {}
        for model in models:
            try:
                result[model] = fetch_rolling_meta(model, client=http)
            except httpx.HTTPStatusError as exc:
                if skip_missing and exc.response.status_code == 404:
                    logger.warning(
                        "rolling meta.json missing for model %s (404); "
                        "init lead will fall back to wall-clock",
                        model,
                    )
                    continue
                raise
        return result
    finally:
        if own_client:
            http.close()


def parse_forecast_payload(payload: dict, target_date: date) -> DailyMaxForecast:
    """Parse an Open-Meteo forecast payload into a DailyMaxForecast."""
    latitude = payload.get("latitude")
    longitude = payload.get("longitude")
    daily = payload.get("daily")

    if latitude is None:
        raise ValueError("latitude is required")
    if longitude is None:
        raise ValueError("longitude is required")
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

    values_c: list[float] = []
    models: list[str] = []
    for key, series in daily.items():
        if not key.startswith("temperature_2m_max"):
            continue
        if not isinstance(series, list) or index >= len(series):
            continue
        raw_value = series[index]
        if raw_value is None:
            continue
        try:
            value = float(raw_value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{key}[{index}] must be numeric") from exc
        if not PLAUSIBLE_MIN_C <= value <= PLAUSIBLE_MAX_C:
            raise ValueError(
                f"{key}[{index}]={value} outside plausible range "
                f"[{PLAUSIBLE_MIN_C}, {PLAUSIBLE_MAX_C}] °C"
            )
        values_c.append(value)
        model_suffix = key[len("temperature_2m_max") :].lstrip("_")
        models.append(model_suffix or "default")

    if not values_c:
        raise ValueError("no temperature_2m_max values found for target_date")

    return DailyMaxForecast(
        target_date=target_date,
        latitude=float(latitude),
        longitude=float(longitude),
        values_c=values_c,
        models=models,
    )


def _parse_forecast_value_for_model(
    payload: dict[str, Any],
    *,
    model: str,
    target_date: date,
) -> float | None:
    daily = payload.get("daily")
    if not isinstance(daily, dict):
        return None
    times = daily.get("time")
    if not isinstance(times, list):
        return None
    target_iso = target_date.isoformat()
    try:
        index = times.index(target_iso)
    except ValueError:
        return None

    keys = (f"temperature_2m_max_{model}", "temperature_2m_max")
    for key in keys:
        series = daily.get(key)
        if not isinstance(series, list) or index >= len(series):
            continue
        raw_value = series[index]
        if raw_value is None:
            continue
        value = float(raw_value)
        if PLAUSIBLE_MIN_C <= value <= PLAUSIBLE_MAX_C:
            return value
    return None


def _is_retryable_forecast_error(exc: BaseException) -> bool:
    if isinstance(exc, (httpx.ReadTimeout, httpx.ConnectTimeout, httpx.ConnectError)):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in _RETRYABLE_STATUS_CODES
    return False


def fetch_forecast_payload(
    latitude: float,
    longitude: float,
    target_dates: list[date],
    timezone: str,
    models: tuple[str, ...] | list[str],
    base_url: str = "https://api.open-meteo.com/v1/forecast",
    *,
    client: httpx.Client | None = None,
    timeout_s: float = DEFAULT_FORECAST_TIMEOUT_S,
    max_retries: int = DEFAULT_FORECAST_MAX_RETRIES,
    retry_backoff_s: tuple[float, ...] = DEFAULT_FORECAST_RETRY_BACKOFF_S,
) -> dict[str, Any]:
    """Fetch raw Forecast API JSON for a date range and model set."""
    if not models:
        raise ValueError("models must not be empty")
    if not target_dates:
        raise ValueError("target_dates must not be empty")
    if not timezone.strip():
        raise ValueError("timezone must not be empty")
    if max_retries < 1:
        raise ValueError("max_retries must be >= 1")

    sorted_dates = sorted(target_dates)
    params = {
        "latitude": latitude,
        "longitude": longitude,
        "daily": "temperature_2m_max",
        "temperature_unit": "celsius",
        "models": ",".join(models),
        "timezone": timezone,
        "start_date": sorted_dates[0].isoformat(),
        "end_date": sorted_dates[-1].isoformat(),
    }

    own_client = client is None
    http = client or httpx.Client()
    last_exc: BaseException | None = None
    try:
        for attempt in range(max_retries):
            try:
                response = http.get(base_url, params=params, timeout=timeout_s)
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict):
                    raise ValueError("forecast response must be a JSON object")
                return payload
            except Exception as exc:
                last_exc = exc
                if attempt >= max_retries - 1 or not _is_retryable_forecast_error(exc):
                    raise
                if attempt < len(retry_backoff_s):
                    time.sleep(retry_backoff_s[attempt])
        assert last_exc is not None
        raise last_exc
    finally:
        if own_client:
            http.close()


def _build_lead_maps(
    *,
    meta_by_model: dict[str, ModelRunMeta],
    target_dates: list[date],
    fetched_at: datetime,
    forecast_payload: dict[str, Any],
) -> tuple[
    dict[date, DailyMaxForecast],
    dict[tuple[str, date], float],
    dict[tuple[str, date], float],
]:
    latitude = float(forecast_payload["latitude"])
    longitude = float(forecast_payload["longitude"])
    daily_by_date: dict[date, DailyMaxForecast] = {}
    init_lead_hours: dict[tuple[str, date], float] = {}
    wall_clock_lead_hours: dict[tuple[str, date], float] = {}

    for target_date in target_dates:
        daily = parse_forecast_payload(forecast_payload, target_date)
        daily_by_date[target_date] = daily
        for model in daily.models:
            wall_clock_lead_hours[(model, target_date)] = lead_hours_to_end_of_target_day(
                target_date,
                now=fetched_at,
            )
        for model in meta_by_model:
            value = _parse_forecast_value_for_model(
                forecast_payload,
                model=model,
                target_date=target_date,
            )
            if value is None:
                continue
            init_lead_hours[(model, target_date)] = compute_lead_hours(
                meta_by_model[model].run_init_utc,
                target_date,
            )

    return daily_by_date, init_lead_hours, wall_clock_lead_hours


def fetch_open_meteo_live_bundle(
    *,
    latitude: float,
    longitude: float,
    timezone: str,
    models: tuple[str, ...] | list[str],
    target_dates: list[date],
    fetched_at_utc: datetime | None = None,
    client: httpx.Client | None = None,
) -> OpenMeteoLiveBundle:
    """Fetch rolling meta + forecast, with post-forecast meta staleness guard."""
    if not models:
        raise ValueError("models must not be empty")
    if not target_dates:
        raise ValueError("target_dates must not be empty")

    fetched_at = fetched_at_utc or datetime.now(timezone.utc)
    if fetched_at.tzinfo is None:
        raise ValueError("fetched_at_utc must be timezone-aware")

    own_client = client is None
    http = client or httpx.Client()
    try:
        meta_before = fetch_rolling_meta_batch(models, client=http, skip_missing=True)
        forecast_payload = fetch_forecast_payload(
            latitude,
            longitude,
            target_dates,
            timezone,
            models,
            client=http,
        )
        meta_after = fetch_rolling_meta_batch(models, client=http, skip_missing=True)
    finally:
        if own_client:
            http.close()

    shared_models = meta_before.keys() & meta_after.keys()
    meta_staleness_detected = any(
        meta_before[m].run_init_utc != meta_after[m].run_init_utc for m in shared_models
    )
    meta_by_model = meta_after

    daily_by_date, init_lead_hours, wall_clock_lead_hours = _build_lead_maps(
        meta_by_model=meta_by_model,
        target_dates=target_dates,
        fetched_at=fetched_at,
        forecast_payload=forecast_payload,
    )

    return OpenMeteoLiveBundle(
        fetched_at_utc=fetched_at,
        requested_lat=latitude,
        requested_lon=longitude,
        returned_lat=float(forecast_payload["latitude"]),
        returned_lon=float(forecast_payload["longitude"]),
        daily_by_date=daily_by_date,
        meta_by_model=meta_by_model,
        init_lead_hours=init_lead_hours,
        wall_clock_lead_hours=wall_clock_lead_hours,
        meta_staleness_detected=meta_staleness_detected,
    )


def persist_open_meteo_fetch(
    conn: Connection,
    bundle: OpenMeteoLiveBundle,
    *,
    station_id: str,
    collector_name: str = "open_meteo",
) -> int:
    """Write one fetch cycle and child meta/forecast rows. Returns cycle id."""
    fetched_iso = format_run_time_utc(bundle.fetched_at_utc)
    cycle_id = insert_open_meteo_fetch_cycle(
        conn,
        station_id=station_id,
        fetched_at_utc=fetched_iso,
        requested_lat=bundle.requested_lat,
        requested_lon=bundle.requested_lon,
        returned_lat=bundle.returned_lat,
        returned_lon=bundle.returned_lon,
        collector_name=collector_name,
        meta_staleness_detected=bundle.meta_staleness_detected,
        created_at_utc=utc_now_iso(),
    )

    for model, meta in bundle.meta_by_model.items():
        data_end_iso = (
            format_run_time_utc(meta.data_end_utc) if meta.data_end_utc is not None else None
        )
        insert_open_meteo_model_meta_snapshot(
            conn,
            fetch_cycle_id=cycle_id,
            station_id=station_id,
            model=model,
            run_init_utc=format_run_time_utc(meta.run_init_utc),
            run_available_utc=format_run_time_utc(meta.run_available_utc),
            run_modified_utc=format_run_time_utc(meta.run_modified_utc),
            availability_lag_hours=availability_lag_hours(meta),
            update_interval_seconds=meta.update_interval_seconds,
            temporal_resolution_seconds=meta.temporal_resolution_seconds,
            data_end_utc=data_end_iso,
            fetched_at_utc=fetched_iso,
        )

    for target_date, daily in bundle.daily_by_date.items():
        target_iso = target_date.isoformat()
        for model in bundle.meta_by_model:
            key = (model, target_date)
            if key not in bundle.init_lead_hours:
                continue
            try:
                index = daily.models.index(model)
            except ValueError:
                continue
            insert_open_meteo_forecast_snapshot(
                conn,
                fetch_cycle_id=cycle_id,
                station_id=station_id,
                model=model,
                target_date_local=target_iso,
                predicted_tmax_c=daily.values_c[index],
                run_init_utc=format_run_time_utc(bundle.meta_by_model[model].run_init_utc),
                init_lead_hours=bundle.init_lead_hours[key],
                wall_clock_lead_hours=bundle.wall_clock_lead_hours[key],
                fetched_at_utc=fetched_iso,
            )

    return cycle_id


def fetch_daily_max(
    latitude: float,
    longitude: float,
    target_date: date,
    timezone: str,
    models: tuple[str, ...] | list[str] = DEFAULT_MODELS,
    base_url: str = "https://api.open-meteo.com/v1/forecast",
    *,
    timeout_s: float = DEFAULT_FORECAST_TIMEOUT_S,
    max_retries: int = DEFAULT_FORECAST_MAX_RETRIES,
    retry_backoff_s: tuple[float, ...] = DEFAULT_FORECAST_RETRY_BACKOFF_S,
) -> DailyMaxForecast:
    """Fetch a daily-max temperature ensemble across models for one date."""
    payload = fetch_forecast_payload(
        latitude,
        longitude,
        [target_date],
        timezone,
        models,
        base_url=base_url,
        timeout_s=timeout_s,
        max_retries=max_retries,
        retry_backoff_s=retry_backoff_s,
    )
    return parse_forecast_payload(payload, target_date)


def fetch_for_station(
    station: Station,
    target_date: date,
    models: tuple[str, ...] | list[str] = DEFAULT_MODELS,
    base_url: str = "https://api.open-meteo.com/v1/forecast",
) -> DailyMaxForecast:
    """Fetch a daily-max forecast for a registered contract station."""
    return fetch_daily_max(
        latitude=station.latitude,
        longitude=station.longitude,
        target_date=target_date,
        timezone=station.timezone,
        models=models,
        base_url=base_url,
    )
