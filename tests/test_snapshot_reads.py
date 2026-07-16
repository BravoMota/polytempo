"""Tests for storage.snapshot_reads."""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from polytempo.markets.polymarket import PolymarketBucket, PolymarketEvent
from polytempo.storage.postgres import (
    get_connection,
    insert_forecast_snapshot,
    insert_open_meteo_fetch_cycle,
    insert_open_meteo_forecast_snapshot,
    insert_station,
    upsert_wu_history_daily_observation,
)
from polytempo.storage.snapshot_reads import (
    fetch_nearest_clob_snapshot,
    fetch_nearest_open_meteo_forecast,
    fetch_nearest_wunderground_adjusted_tmax,
    hydrate_event_from_clob_snapshot,
    missing_open_meteo_forecast_reason,
)
from polytempo.weather.stations import get_station


def _event() -> PolymarketEvent:
    return PolymarketEvent(
        event_id="evt-1",
        slug="london-test",
        title="London test",
        settlement_date=date(2026, 6, 17),
        buckets=[
            PolymarketBucket(
                market_id="m1",
                label="26°C",
                yes_bid=0.1,
                yes_ask=0.12,
                liquidity_usd=None,
                spread=None,
                rules=None,
            ),
            PolymarketBucket(
                market_id="m2",
                label="27°C",
                yes_bid=0.2,
                yes_ask=0.22,
                liquidity_usd=None,
                spread=None,
                rules=None,
            ),
        ],
    )


def test_hydrate_event_from_clob_snapshot_overlays_prices() -> None:
    event = _event()
    rows = [
        {"bucket_label": "26°C", "yes_bid": 0.38, "yes_ask": 0.4, "spread": 0.02, "liquidity_usd": 50.0},
    ]
    hydrated = hydrate_event_from_clob_snapshot(event, rows)
    by_label = {b.label: b for b in hydrated.buckets}
    assert by_label["26°C"].yes_ask == 0.4
    assert by_label["27°C"].yes_ask == 0.22


def test_hydrate_event_from_clob_snapshot_skips_settled_prices() -> None:
    event = _event()
    rows = [
        {"bucket_label": "26°C", "yes_bid": 0.99, "yes_ask": 1.0, "spread": 0.01, "liquidity_usd": 50.0},
    ]
    hydrated = hydrate_event_from_clob_snapshot(event, rows)
    assert hydrated.buckets[0].yes_ask == 0.12


def test_fetch_nearest_open_meteo_forecast(weather_db_url: str) -> None:
    station = get_station("london")
    target = date(2026, 6, 17)
    at_utc = datetime(2026, 6, 16, 18, 5, tzinfo=timezone.utc)

    with get_connection(weather_db_url) as conn:
        insert_station(
            conn,
            station_id="EGLC",
            name="London City Airport",
            timezone="Europe/London",
            lat=station.latitude,
            lon=station.longitude,
        )
        early_id = insert_open_meteo_fetch_cycle(
            conn,
            station_id="EGLC",
            fetched_at_utc="2026-06-16T17:50:00Z",
            requested_lat=station.latitude,
            requested_lon=station.longitude,
            returned_lat=station.latitude,
            returned_lon=station.longitude,
            collector_name="open_meteo",
            meta_staleness_detected=False,
        )
        late_id = insert_open_meteo_fetch_cycle(
            conn,
            station_id="EGLC",
            fetched_at_utc="2026-06-16T18:10:00Z",
            requested_lat=station.latitude,
            requested_lon=station.longitude,
            returned_lat=station.latitude,
            returned_lon=station.longitude,
            collector_name="open_meteo",
            meta_staleness_detected=False,
        )
        insert_open_meteo_forecast_snapshot(
            conn,
            fetch_cycle_id=early_id,
            station_id="EGLC",
            model="ukmo_seamless",
            target_date_local=target.isoformat(),
            predicted_tmax_c=24.0,
            run_init_utc="2026-06-16T12:00:00Z",
            init_lead_hours=30.0,
            wall_clock_lead_hours=30.0,
            fetched_at_utc="2026-06-16T17:50:00Z",
        )
        insert_open_meteo_forecast_snapshot(
            conn,
            fetch_cycle_id=late_id,
            station_id="EGLC",
            model="ukmo_seamless",
            target_date_local=target.isoformat(),
            predicted_tmax_c=25.0,
            run_init_utc="2026-06-16T12:00:00Z",
            init_lead_hours=29.0,
            wall_clock_lead_hours=29.0,
            fetched_at_utc="2026-06-16T18:10:00Z",
        )
        conn.commit()

    bundle = fetch_nearest_open_meteo_forecast(
        station, target, at_utc, database_url=weather_db_url
    )
    assert bundle is not None
    assert bundle.fetch_cycle_id == early_id
    assert bundle.forecast.values_c == [24.0]
    assert bundle.forecast.model_run_init_utc == ["2026-06-16T12:00:00Z"]
    assert bundle.forecast.init_lead_hours == [30.0]


def test_fetch_nearest_open_meteo_forecast_marks_no_meta_models_ineligible(
    weather_db_url: str,
) -> None:
    from polytempo.weather.calibration_stats_csv import verified_init_lead_hours_by_model

    station = get_station("london")
    target = date(2026, 6, 17)
    at_utc = datetime(2026, 6, 16, 18, 5, tzinfo=timezone.utc)

    with get_connection(weather_db_url) as conn:
        insert_station(
            conn,
            station_id="EGLC",
            name="London City Airport",
            timezone="Europe/London",
            lat=station.latitude,
            lon=station.longitude,
        )
        cycle_id = insert_open_meteo_fetch_cycle(
            conn,
            station_id="EGLC",
            fetched_at_utc="2026-06-16T18:00:00Z",
            requested_lat=station.latitude,
            requested_lon=station.longitude,
            returned_lat=station.latitude,
            returned_lon=station.longitude,
            collector_name="open_meteo",
            meta_staleness_detected=False,
        )
        insert_open_meteo_forecast_snapshot(
            conn,
            fetch_cycle_id=cycle_id,
            station_id="EGLC",
            model="gfs_seamless",
            target_date_local=target.isoformat(),
            predicted_tmax_c=36.0,
            run_init_utc=None,
            init_lead_hours=None,
            wall_clock_lead_hours=30.0,
            fetched_at_utc="2026-06-16T18:00:00Z",
        )
        insert_open_meteo_forecast_snapshot(
            conn,
            fetch_cycle_id=cycle_id,
            station_id="EGLC",
            model="ecmwf_ifs025",
            target_date_local=target.isoformat(),
            predicted_tmax_c=35.0,
            run_init_utc="2026-06-16T12:00:00Z",
            init_lead_hours=36.0,
            wall_clock_lead_hours=30.0,
            fetched_at_utc="2026-06-16T18:00:00Z",
        )
        conn.commit()

    bundle = fetch_nearest_open_meteo_forecast(
        station, target, at_utc, database_url=weather_db_url
    )
    assert bundle is not None
    assert bundle.forecast.model_run_init_utc == ["", "2026-06-16T12:00:00Z"]
    eligible = verified_init_lead_hours_by_model(bundle.forecast)
    assert eligible == {"ecmwf_ifs025": 36.0}


def test_fetch_nearest_open_meteo_uses_nearest_cycle_not_older_with_target(
    weather_db_url: str,
) -> None:
    """Nearest cycle by OPEN time wins even if an older cycle has the target date."""
    station = get_station("london")
    target = date(2026, 6, 22)
    at_utc = datetime(2026, 6, 20, 18, 5, tzinfo=timezone.utc)

    with get_connection(weather_db_url) as conn:
        insert_station(
            conn,
            station_id="EGLC",
            name="London City Airport",
            timezone="Europe/London",
            lat=station.latitude,
            lon=station.longitude,
        )
        older_with_target_id = insert_open_meteo_fetch_cycle(
            conn,
            station_id="EGLC",
            fetched_at_utc="2026-06-20T17:50:00Z",
            requested_lat=station.latitude,
            requested_lon=station.longitude,
            returned_lat=station.latitude,
            returned_lon=station.longitude,
            collector_name="open_meteo",
            meta_staleness_detected=False,
        )
        nearest_id = insert_open_meteo_fetch_cycle(
            conn,
            station_id="EGLC",
            fetched_at_utc="2026-06-20T18:00:06Z",
            requested_lat=station.latitude,
            requested_lon=station.longitude,
            returned_lat=station.latitude,
            returned_lon=station.longitude,
            collector_name="open_meteo",
            meta_staleness_detected=False,
        )
        insert_open_meteo_forecast_snapshot(
            conn,
            fetch_cycle_id=older_with_target_id,
            station_id="EGLC",
            model="ukmo_seamless",
            target_date_local=target.isoformat(),
            predicted_tmax_c=27.0,
            run_init_utc="2026-06-20T12:00:00Z",
            init_lead_hours=54.0,
            wall_clock_lead_hours=54.0,
            fetched_at_utc="2026-06-20T17:50:00Z",
        )
        insert_open_meteo_forecast_snapshot(
            conn,
            fetch_cycle_id=nearest_id,
            station_id="EGLC",
            model="ukmo_seamless",
            target_date_local="2026-06-21",
            predicted_tmax_c=26.0,
            run_init_utc="2026-06-20T12:00:00Z",
            init_lead_hours=30.0,
            wall_clock_lead_hours=30.0,
            fetched_at_utc="2026-06-20T18:00:06Z",
        )
        conn.commit()

    assert (
        fetch_nearest_open_meteo_forecast(
            station, target, at_utc, database_url=weather_db_url
        )
        is None
    )


def test_missing_open_meteo_forecast_reason_horizon_gap(weather_db_url: str) -> None:
    station = get_station("london")
    target = date(2026, 6, 22)
    at_utc = datetime(2026, 6, 20, 18, 5, tzinfo=timezone.utc)

    with get_connection(weather_db_url) as conn:
        insert_station(
            conn,
            station_id="EGLC",
            name="London City Airport",
            timezone="Europe/London",
            lat=station.latitude,
            lon=station.longitude,
        )
        cycle_id = insert_open_meteo_fetch_cycle(
            conn,
            station_id="EGLC",
            fetched_at_utc="2026-06-20T18:00:06Z",
            requested_lat=station.latitude,
            requested_lon=station.longitude,
            returned_lat=station.latitude,
            returned_lon=station.longitude,
            collector_name="open_meteo",
            meta_staleness_detected=False,
        )
        insert_open_meteo_forecast_snapshot(
            conn,
            fetch_cycle_id=cycle_id,
            station_id="EGLC",
            model="ukmo_seamless",
            target_date_local="2026-06-21",
            predicted_tmax_c=26.0,
            run_init_utc="2026-06-20T12:00:00Z",
            init_lead_hours=30.0,
            wall_clock_lead_hours=30.0,
            fetched_at_utc="2026-06-20T18:00:06Z",
        )
        conn.commit()

    assert (
        fetch_nearest_open_meteo_forecast(
            station, target, at_utc, database_url=weather_db_url
        )
        is None
    )
    reason = missing_open_meteo_forecast_reason(
        station, target, at_utc, database_url=weather_db_url
    )
    assert "2026-06-20T18:00:06Z" in reason
    assert "2026-06-22" in reason
    assert "target_horizon_days" in reason


def test_fetch_nearest_clob_snapshot(weather_db_url: str) -> None:
    at_utc = datetime(2026, 6, 16, 16, 7, tzinfo=timezone.utc)
    with get_connection(weather_db_url) as conn:
        conn.execute(
            """
            INSERT INTO clob_bucket_snapshots (
                city_slug, station_id, polymarket_event_id, settlement_date,
                market_id, bucket_label, yes_bid, yes_ask, spread, liquidity_usd,
                poll_slot_utc, fetched_at_utc, lead_hours_to_day_end,
                wall_clock_lead_hours, created_at_utc
            ) VALUES
            ('london', 'EGLC', 'evt-1', '2026-06-17', 'm1', '26°C',
             0.3, 0.32, 0.02, 10.0,
             '2026-06-16T16:00:00Z', '2026-06-16T16:00:05Z', 30.0, 30.0,
             '2026-06-16T16:00:05Z'),
            ('london', 'EGLC', 'evt-1', '2026-06-17', 'm2', '27°C',
             0.4, 0.42, 0.02, 12.0,
             '2026-06-16T16:00:00Z', '2026-06-16T16:00:05Z', 30.0, 30.0,
             '2026-06-16T16:00:05Z'),
            ('london', 'EGLC', 'evt-1', '2026-06-17', 'm1', '26°C',
             0.5, 0.52, 0.02, 10.0,
             '2026-06-16T16:10:00Z', '2026-06-16T16:10:05Z', 29.0, 29.0,
             '2026-06-16T16:10:05Z')
            """
        )
        conn.commit()

    bundle = fetch_nearest_clob_snapshot("evt-1", at_utc, database_url=weather_db_url)
    assert bundle is not None
    assert bundle.poll_slot_utc == "2026-06-16T16:00:00Z"
    assert len(bundle.rows) == 2


def test_fetch_nearest_clob_snapshot_ignores_future_only_slots(weather_db_url: str) -> None:
    at_utc = datetime(2026, 6, 16, 16, 7, tzinfo=timezone.utc)
    with get_connection(weather_db_url) as conn:
        conn.execute(
            """
            INSERT INTO clob_bucket_snapshots (
                city_slug, station_id, polymarket_event_id, settlement_date,
                market_id, bucket_label, yes_bid, yes_ask, spread, liquidity_usd,
                poll_slot_utc, fetched_at_utc, lead_hours_to_day_end,
                wall_clock_lead_hours, created_at_utc
            ) VALUES
            ('london', 'EGLC', 'evt-future', '2026-06-17', 'm1', '26°C',
             0.5, 0.52, 0.02, 10.0,
             '2026-06-16T16:10:00Z', '2026-06-16T16:10:05Z', 29.0, 29.0,
             '2026-06-16T16:10:05Z')
            """
        )
        conn.commit()

    bundle = fetch_nearest_clob_snapshot(
        "evt-future", at_utc, database_url=weather_db_url
    )
    assert bundle is None


def test_fetch_nearest_wunderground_adjusted_tmax(weather_db_url: str) -> None:
    station = get_station("london")
    target = date(2026, 6, 17)
    at_utc = datetime(2026, 6, 16, 18, 30, tzinfo=timezone.utc)
    scrape = "2026-06-16T18:00:00Z"

    with get_connection(weather_db_url) as conn:
        insert_station(
            conn,
            station_id="EGLC",
            name="London City Airport",
            timezone="Europe/London",
            lat=station.latitude,
            lon=station.longitude,
        )
        upsert_wu_history_daily_observation(
            conn,
            station_id="EGLC",
            target_date=target.isoformat(),
            observed_at_utc="2026-06-16T14:00:00Z",
            observed_at_local="2026-06-16T15:00:00+0100",
            temp_c=22.0,
            fetched_at_utc="2026-06-16T14:05:00Z",
        )
        insert_forecast_snapshot(
            conn,
            station_id="EGLC",
            source="wunderground",
            scraped_at_utc=scrape,
            target_date_local=target.isoformat(),
            station_timezone="Europe/London",
            target_time_utc="2026-06-16T19:00:00Z",
            temp_c=24.0,
        )
        insert_forecast_snapshot(
            conn,
            station_id="EGLC",
            source="wunderground",
            scraped_at_utc=scrape,
            target_date_local=target.isoformat(),
            station_timezone="Europe/London",
            target_time_utc="2026-06-16T20:00:00Z",
            temp_c=25.5,
        )
        insert_forecast_snapshot(
            conn,
            station_id="EGLC",
            source="wunderground",
            scraped_at_utc="2026-06-16T20:00:00Z",
            target_date_local=target.isoformat(),
            station_timezone="Europe/London",
            target_time_utc="2026-06-16T21:00:00Z",
            temp_c=99.0,
        )
        conn.commit()

    snapshot = fetch_nearest_wunderground_adjusted_tmax(
        station, target, at_utc, database_url=weather_db_url
    )
    assert snapshot is not None
    assert snapshot.scraped_at_utc == scrape
    assert snapshot.predicted_tmax_c == pytest.approx(25.5)
    assert snapshot.observed_running_max_c == pytest.approx(22.0)


def test_fetch_nearest_clob_snapshot_rejects_stale_slot(weather_db_url: str) -> None:
    at_utc = datetime(2026, 6, 16, 18, 0, tzinfo=timezone.utc)
    with get_connection(weather_db_url) as conn:
        conn.execute(
            """
            INSERT INTO clob_bucket_snapshots (
                city_slug, station_id, polymarket_event_id, settlement_date,
                market_id, bucket_label, yes_bid, yes_ask, spread, liquidity_usd,
                poll_slot_utc, fetched_at_utc, lead_hours_to_day_end,
                wall_clock_lead_hours, created_at_utc
            ) VALUES
            ('london', 'EGLC', 'evt-stale', '2026-06-17', 'm1', '26°C',
             0.3, 0.32, 0.02, 10.0,
             '2026-06-16T16:00:00Z', '2026-06-16T16:00:05Z', 30.0, 30.0,
             '2026-06-16T16:00:05Z')
            """
        )
        conn.commit()

    bundle = fetch_nearest_clob_snapshot(
        "evt-stale", at_utc, database_url=weather_db_url
    )
    assert bundle is None
