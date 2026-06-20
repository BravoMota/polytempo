"""Polymarket CLOB snapshot collector — executable bucket prices for backtests."""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any

from polytempo.collectors.config import CollectorConfig, StationConfig, WeatherCollectorsConfig
from polytempo.collectors.schedule import slot_for_instant
from polytempo.collectors.util import local_today
from polytempo.markets.polymarket import (
    PolymarketEvent,
    fetch_weather_events,
    first_parseable_weather_event,
    hydrate_prices,
    is_event_resolved,
)
from polytempo.model.lead_time import lead_hours_to_end_of_target_day
from polytempo.storage.postgres import (
    insert_clob_bucket_snapshots,
    mark_collector_started,
    upsert_collector_state_error,
    upsert_collector_state_success,
    utc_now_iso,
)

logger = logging.getLogger(__name__)

COLLECTOR_NAME = "polymarket_clob"


def _format_utc_instant(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _target_dates_for_station(
    station: StationConfig,
    *,
    horizon_days: int,
    now_utc: datetime,
) -> list[date]:
    today_local = local_today(station.timezone, now_utc)
    return [today_local + timedelta(days=offset) for offset in range(horizon_days)]


def _snapshot_rows_for_event(
    event: PolymarketEvent,
    *,
    city_slug: str,
    station_id: str,
    settlement_date: date,
    poll_slot: datetime,
    fetched_at: datetime,
    created_at_utc: str,
) -> list[dict[str, object]]:
    poll_slot_iso = _format_utc_instant(poll_slot)
    fetched_iso = _format_utc_instant(fetched_at)
    lead_slot = lead_hours_to_end_of_target_day(settlement_date, now=poll_slot)
    lead_wall = lead_hours_to_end_of_target_day(settlement_date, now=fetched_at)
    settlement_iso = settlement_date.isoformat()

    rows: list[dict[str, object]] = []
    for bucket in event.buckets:
        rows.append(
            {
                "city_slug": city_slug,
                "station_id": station_id,
                "polymarket_event_id": event.event_id,
                "settlement_date": settlement_iso,
                "market_id": bucket.market_id,
                "bucket_label": bucket.label,
                "yes_token_id": bucket.yes_token_id,
                "yes_bid": bucket.yes_bid,
                "yes_ask": bucket.yes_ask,
                "spread": bucket.spread,
                "liquidity_usd": bucket.liquidity_usd,
                "poll_slot_utc": poll_slot_iso,
                "fetched_at_utc": fetched_iso,
                "lead_hours_to_day_end": lead_slot,
                "wall_clock_lead_hours": lead_wall,
                "created_at_utc": created_at_utc,
            }
        )
    return rows


def run_station_snapshots(
    conn: Any,
    collector: CollectorConfig,
    station: StationConfig,
    *,
    now_utc: datetime | None = None,
) -> None:
    """Discover active weather events and persist CLOB book snapshots for one station."""
    now = now_utc or datetime.now(timezone.utc)
    source = collector.source

    mark_collector_started(conn, COLLECTOR_NAME, station.station_id, source, now_utc=utc_now_iso())

    poll_slot = slot_for_instant(
        now,
        collector.forecast_interval_seconds,
        collector.forecast_anchor_time_utc,
    )
    created_at = utc_now_iso()

    try:
        target_dates = _target_dates_for_station(
            station,
            horizon_days=collector.target_horizon_days,
            now_utc=now,
        )
        all_rows: list[dict[str, object]] = []

        for target_date in target_dates:
            events = fetch_weather_events(end_on_date=target_date)
            found = first_parseable_weather_event(
                events,
                city=station.city_slug,
                settlement_date=target_date,
            )
            if found is None:
                logger.info(
                    "polymarket_clob no event city=%s settlement_date=%s",
                    station.city_slug,
                    target_date.isoformat(),
                )
                continue

            if is_event_resolved(found):
                logger.info(
                    "polymarket_clob skip resolved event=%s settlement_date=%s",
                    found.event_id,
                    target_date.isoformat(),
                )
                continue

            fetched_at = datetime.now(timezone.utc)
            event = hydrate_prices(found)
            settlement_date = event.settlement_date or target_date
            all_rows.extend(
                _snapshot_rows_for_event(
                    event,
                    city_slug=station.city_slug,
                    station_id=station.station_id,
                    settlement_date=settlement_date,
                    poll_slot=poll_slot,
                    fetched_at=fetched_at,
                    created_at_utc=created_at,
                )
            )

        if all_rows:
            insert_clob_bucket_snapshots(conn, all_rows)
    except Exception as exc:
        conn.rollback()
        upsert_collector_state_error(
            conn,
            COLLECTOR_NAME,
            station.station_id,
            source,
            f"snapshot: {exc}",
        )
        conn.commit()
        logger.error(
            "polymarket_clob snapshot failed station=%s: %s",
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
    now_utc: datetime | None = None,
    fetch_observations: bool = True,
    fetch_forecasts: bool = True,
) -> None:
    """Fetch CLOB snapshots for one station (observations are not used)."""
    _ = fetch_observations
    if not fetch_forecasts:
        return
    run_station_snapshots(conn, collector, station, now_utc=now_utc)


def run_cycle(
    conn: Any,
    config: WeatherCollectorsConfig,
    collector: CollectorConfig,
    *,
    fetch_observations: bool = True,
    fetch_forecasts: bool = True,
) -> None:
    """Run one polymarket_clob collector cycle for all configured stations."""
    _ = config
    if not collector.enabled:
        return
    if not fetch_forecasts:
        return

    now_utc = datetime.now(timezone.utc)
    for station in collector.stations:
        try:
            run_station_cycle(
                conn,
                collector,
                station,
                now_utc=now_utc,
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
