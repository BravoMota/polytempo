"""Open-Meteo forecast collector — rolling meta + Forecast API audit trail."""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any
import httpx

from polytempo.collectors.config import CollectorConfig, StationConfig, WeatherCollectorsConfig
from polytempo.collectors.util import local_today
from polytempo.storage.postgres import (
    mark_collector_started,
    upsert_collector_state_error,
    upsert_collector_state_success,
    utc_now_iso,
)
from polytempo.weather.open_meteo import fetch_open_meteo_live_bundle, persist_open_meteo_fetch

logger = logging.getLogger(__name__)

COLLECTOR_NAME = "open_meteo"


def _target_dates_for_station(
    station: StationConfig,
    *,
    horizon_days: int,
    now_utc: datetime,
) -> list[date]:
    today_local = local_today(station.timezone, now_utc)
    return [today_local + timedelta(days=offset) for offset in range(horizon_days)]


def run_station_forecasts(
    conn: Any,
    collector: CollectorConfig,
    station: StationConfig,
    *,
    client: httpx.Client,
    now_utc: datetime | None = None,
) -> None:
    """Fetch Open-Meteo meta + forecast and persist parsed rows for one station."""
    now = now_utc or datetime.now(timezone.utc)
    source = collector.source

    if station.lat is None or station.lon is None:
        raise ValueError(f"station {station.station_id!r} requires lat and lon")

    mark_collector_started(conn, COLLECTOR_NAME, station.station_id, source, now_utc=utc_now_iso())

    try:
        target_dates = _target_dates_for_station(
            station,
            horizon_days=collector.target_horizon_days,
            now_utc=now,
        )
        bundle = fetch_open_meteo_live_bundle(
            latitude=station.lat,
            longitude=station.lon,
            timezone=station.timezone,
            models=collector.models,
            target_dates=target_dates,
            fetched_at_utc=now.astimezone(timezone.utc),
            client=client,
        )
        persist_open_meteo_fetch(
            conn,
            bundle,
            station_id=station.station_id,
            collector_name=COLLECTOR_NAME,
        )
    except Exception as exc:
        conn.rollback()
        upsert_collector_state_error(
            conn,
            COLLECTOR_NAME,
            station.station_id,
            source,
            f"forecast: {exc}",
        )
        conn.commit()
        logger.error(
            "open_meteo forecast failed station=%s: %s",
            station.station_id,
            exc,
        )
        return

    upsert_collector_state_success(
        conn,
        COLLECTOR_NAME,
        station.station_id,
        source,
    )
    conn.commit()


def run_station_cycle(
    conn: Any,
    collector: CollectorConfig,
    station: StationConfig,
    *,
    client: httpx.Client | None = None,
    now_utc: datetime | None = None,
    fetch_observations: bool = True,
    fetch_forecasts: bool = True,
) -> None:
    """Fetch forecasts for one station (observations are not used)."""
    _ = fetch_observations
    if not fetch_forecasts:
        return

    own_client = client is None
    http = client or httpx.Client()
    try:
        run_station_forecasts(
            conn,
            collector,
            station,
            client=http,
            now_utc=now_utc,
        )
    finally:
        if own_client:
            http.close()


def run_cycle(
    conn: Any,
    config: WeatherCollectorsConfig,
    collector: CollectorConfig,
    *,
    fetch_observations: bool = True,
    fetch_forecasts: bool = True,
) -> None:
    """Run one open_meteo collector cycle for all configured stations."""
    if not collector.enabled:
        return
    if not fetch_forecasts:
        return

    with httpx.Client() as client:
        for station in collector.stations:
            try:
                run_station_cycle(
                    conn,
                    collector,
                    station,
                    client=client,
                    fetch_observations=False,
                    fetch_forecasts=True,
                )
            except Exception as exc:
                logger.exception(
                    "unexpected error station=%s collector=%s: %s",
                    station.station_id,
                    collector.name,
                    exc,
                )
                upsert_collector_state_error(
                    conn,
                    COLLECTOR_NAME,
                    station.station_id,
                    collector.source,
                    str(exc),
                )
                conn.commit()
