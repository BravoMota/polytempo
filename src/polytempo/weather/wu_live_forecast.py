"""Live Wunderground adjusted Tmax for model selection (``best_historical_updated``)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime
from datetime import timezone as dt_timezone

import httpx

from polytempo.model.lead_time import lead_hours_to_end_of_target_day
from polytempo.weather.calibration_storage import WU_FORECAST_MODEL, WuHistoryObservationRow
from polytempo.weather.calibration_wu_forecasts import (
    ForecastHourRow,
    adjusted_predicted_tmax_c,
    observed_running_max_c,
)
from polytempo.weather.schema import ForecastValues
from polytempo.weather.stations import Station
from polytempo.weather.wunderground import (
    fetch_hourly_forecast_payload,
    fetch_station_country_code,
    fetch_v1_historical_observations_payload,
    parse_v1_historical_observations_payload,
    resolve_station_geocode,
)

logger = logging.getLogger(__name__)


def parse_hourly_forecast_metric_payload(
    payload: dict[str, object],
    target_date: date,
) -> list[ForecastHourRow]:
    """Parse metric °C hourly forecast rows for one local calendar day."""
    temps = payload.get("temperature")
    times_local = payload.get("validTimeLocal")
    times_utc = payload.get("validTimeUtc")
    if not isinstance(temps, list) or not isinstance(times_local, list) or not isinstance(
        times_utc, list
    ):
        raise ValueError("hourly forecast metric arrays missing")

    prefix = target_date.isoformat()
    rows: list[ForecastHourRow] = []
    for temp_raw, local_iso, utc_epoch in zip(temps, times_local, times_utc, strict=False):
        if temp_raw is None or utc_epoch is None:
            continue
        if not str(local_iso).startswith(prefix):
            continue
        rows.append(
            ForecastHourRow(
                target_time_utc=datetime.fromtimestamp(int(utc_epoch), tz=dt_timezone.utc),
                temp_c=float(temp_raw),
            )
        )
    if not rows:
        raise ValueError(f"no hourly forecast rows for {target_date.isoformat()}")
    return rows


def _history_observations_for_target_day(
    *,
    station_id: str,
    target_date: date,
    country_code: str,
    client: httpx.Client,
) -> list[WuHistoryObservationRow]:
    try:
        payload = fetch_v1_historical_observations_payload(
            station_id,
            target_date,
            country_code=country_code,
            client=client,
        )
        parsed = parse_v1_historical_observations_payload(payload)
    except Exception as exc:
        logger.debug(
            "WU v1 history unavailable station=%s date=%s: %s",
            station_id,
            target_date.isoformat(),
            exc,
        )
        return []

    return [
        WuHistoryObservationRow(
            station_id=station_id,
            target_date=target_date,
            observed_at_utc=row.observed_at_utc,
            observed_at_local=row.observed_at_local,
            temp_c=row.temp_c,
        )
        for row in parsed
    ]


@dataclass(frozen=True)
class WuLiveAdjustedTmax:
    """Live WU adjusted daily Tmax plus same-day running observed max."""

    predicted_tmax_c: float
    observed_running_max_c: float | None


def fetch_wu_adjusted_tmax_c(
    station: Station,
    target_date: date,
    *,
    as_of_utc: datetime,
    client: httpx.Client | None = None,
) -> WuLiveAdjustedTmax | None:
    """Fetch WU hourly forecast + same-day obs; return adjusted Tmax and running max."""
    as_of = as_of_utc.astimezone(dt_timezone.utc)
    own_client = client is None
    http = client or httpx.Client()
    try:
        geocode = resolve_station_geocode(
            station.icao,
            client=http,
            lat=station.latitude,
            lon=station.longitude,
        )
        country_code = fetch_station_country_code(station.icao, client=http)
        metric_fc = fetch_hourly_forecast_payload(geocode, units="m", client=http)
        hourly_rows = parse_hourly_forecast_metric_payload(metric_fc, target_date)
        history_rows = _history_observations_for_target_day(
            station_id=station.icao,
            target_date=target_date,
            country_code=country_code,
            client=http,
        )
        predicted = adjusted_predicted_tmax_c(
            target_date=target_date,
            as_of_utc=as_of,
            hourly_forecast_rows=hourly_rows,
            history_observations=history_rows,
        )
        if predicted is None:
            return None
        return WuLiveAdjustedTmax(
            predicted_tmax_c=predicted,
            observed_running_max_c=observed_running_max_c(
                history_rows,
                target_date=target_date,
                as_of_utc=as_of,
            ),
        )
    finally:
        if own_client:
            http.close()


def _pad_to_model_count(items: list, n_models: int, fill) -> list:
    if len(items) >= n_models:
        return items[:n_models]
    return [*items, *[fill] * (n_models - len(items))]


def append_wunderground_snapshot_forecast(
    forecast: ForecastValues,
    *,
    predicted_tmax_c: float,
    as_of_utc: datetime,
    observed_running_max_c: float | None = None,
) -> ForecastValues:
    """Append WU adjusted Tmax from a historical snapshot (no live HTTP)."""
    return _append_wunderground_predicted(
        forecast,
        predicted_tmax_c=predicted_tmax_c,
        as_of_utc=as_of_utc,
        observed_running_max_c=observed_running_max_c,
    )


def _append_wunderground_predicted(
    forecast: ForecastValues,
    *,
    predicted_tmax_c: float,
    as_of_utc: datetime,
    observed_running_max_c: float | None = None,
) -> ForecastValues:
    if forecast.models is None:
        raise ValueError("append_wunderground_forecast requires per-model ForecastValues")

    if WU_FORECAST_MODEL in forecast.models:
        return forecast

    wall_lead = lead_hours_to_end_of_target_day(forecast.target_date, now=as_of_utc)
    n_models = len(forecast.models)
    init_leads = _pad_to_model_count(
        list(forecast.init_lead_hours or []), n_models, 0.0
    )
    init_leads.append(wall_lead)

    run_inits: list[str] | None
    if forecast.model_run_init_utc is None:
        run_inits = None
    else:
        run_inits = _pad_to_model_count(list(forecast.model_run_init_utc), n_models, "")
        run_inits.append("")

    floor = (
        observed_running_max_c
        if observed_running_max_c is not None
        else forecast.observed_running_max_c
    )
    return ForecastValues(
        source=forecast.source,
        latitude=forecast.latitude,
        longitude=forecast.longitude,
        target_date=forecast.target_date,
        values_c=[*forecast.values_c, predicted_tmax_c],
        models=[*forecast.models, WU_FORECAST_MODEL],
        init_lead_hours=init_leads,
        model_run_init_utc=run_inits,
        observed_running_max_c=floor,
    )


def append_wunderground_forecast(
    forecast: ForecastValues,
    station: Station,
    *,
    as_of_utc: datetime,
    client: httpx.Client | None = None,
) -> ForecastValues:
    """Append WU adjusted Tmax when available; otherwise return ``forecast`` unchanged."""
    if forecast.models is None:
        raise ValueError("append_wunderground_forecast requires per-model ForecastValues")

    if WU_FORECAST_MODEL in forecast.models:
        return forecast

    try:
        adjusted = fetch_wu_adjusted_tmax_c(
            station,
            forecast.target_date,
            as_of_utc=as_of_utc,
            client=client,
        )
    except Exception as exc:
        logger.warning(
            "WU live forecast failed station=%s target=%s: %s",
            station.icao,
            forecast.target_date.isoformat(),
            exc,
        )
        return forecast

    if adjusted is None:
        logger.warning(
            "WU live forecast returned no adjusted Tmax station=%s target=%s",
            station.icao,
            forecast.target_date.isoformat(),
        )
        return forecast

    return _append_wunderground_predicted(
        forecast,
        predicted_tmax_c=adjusted.predicted_tmax_c,
        as_of_utc=as_of_utc,
        observed_running_max_c=adjusted.observed_running_max_c,
    )
