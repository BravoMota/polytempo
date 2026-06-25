"""Tests for calibration PostgreSQL storage."""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from polytempo.storage.postgres import get_connection, insert_station
from polytempo.weather.calibration_compute import ForecastRecord
from polytempo.weather.calibration_storage import (
    load_forecast_records,
    load_observations_map,
    load_wu_history_observations_for_day,
    parse_run_time_utc_value,
    upsert_forecast_record,
    upsert_observation,
    upsert_wu_history_observation,
    WuHistoryObservationRow,
)
from polytempo.weather.observations import CalibrationObservedTmax


def test_calibration_observation_and_forecast_round_trip(weather_db_url: str) -> None:
    with get_connection(weather_db_url) as conn:
        insert_station(
            conn,
            station_id="EGLC",
            name="London City Airport",
            timezone="Europe/London",
            lat=51.5053,
            lon=0.0553,
            country="gb",
        )
        upsert_observation(
            conn,
            CalibrationObservedTmax(
                station_id="EGLC",
                target_date=date(2026, 4, 2),
                observed_tmax_f=61.0,
                observed_tmax_c=16.11,
                source="wunderground",
            ),
        )
        upsert_forecast_record(
            conn,
            ForecastRecord(
                station_id="EGLC",
                model="ecmwf_ifs025",
                run_time_utc=datetime(2026, 4, 1, 12, 0, tzinfo=timezone.utc),
                target_date=date(2026, 4, 2),
                lead_hours=36.0,
                predicted_tmax_c=17.0,
            ),
        )
        conn.commit()

        obs = load_observations_map(conn)
        forecasts = load_forecast_records(conn)

    assert obs[("EGLC", date(2026, 4, 2))] == pytest.approx(16.11)
    assert len(forecasts) == 1
    assert forecasts[0].predicted_tmax_c == pytest.approx(17.0)


def test_calibration_observation_null_fahrenheit(weather_db_url: str) -> None:
    with get_connection(weather_db_url) as conn:
        insert_station(
            conn,
            station_id="LEMD",
            name="Madrid",
            timezone="Europe/Madrid",
            lat=40.4722,
            lon=-3.5608,
            country="es",
        )
        upsert_observation(
            conn,
            CalibrationObservedTmax(
                station_id="LEMD",
                target_date=date(2026, 4, 3),
                observed_tmax_f=None,
                observed_tmax_c=23.0,
                source="wunderground",
            ),
        )
        conn.commit()

        row = conn.execute(
            """
            SELECT observed_tmax_f, observed_tmax_c
            FROM calibration_observed_tmax
            WHERE station_id = 'LEMD' AND target_date = '2026-04-03'
            """
        ).fetchone()

    assert row is not None
    assert row["observed_tmax_f"] is None
    assert row["observed_tmax_c"] == pytest.approx(23.0)


def test_wu_history_observation_round_trip(weather_db_url: str) -> None:
    target = date(2026, 6, 24)
    with get_connection(weather_db_url) as conn:
        insert_station(
            conn,
            station_id="EGLC",
            name="London City Airport",
            timezone="Europe/London",
            lat=51.5053,
            lon=0.0553,
            country="gb",
        )
        upsert_wu_history_observation(
            conn,
            WuHistoryObservationRow(
                station_id="EGLC",
                target_date=target,
                observed_at_utc=datetime(2026, 6, 24, 11, 0, tzinfo=timezone.utc),
                observed_at_local="2026-06-24T12:00:00+0100",
                temp_c=21.5,
            ),
        )
        conn.commit()
        loaded = load_wu_history_observations_for_day(
            conn,
            station_id="EGLC",
            target_date=target,
        )

    assert len(loaded) == 1
    assert loaded[0].temp_c == pytest.approx(21.5)


def test_parse_run_time_utc_value_accepts_canonical_formats() -> None:
    assert parse_run_time_utc_value("2026-04-01T12:00:00Z") == datetime(
        2026, 4, 1, 12, 0, tzinfo=timezone.utc
    )
    assert parse_run_time_utc_value("2026-04-01T12:00:00+00:00") == datetime(
        2026, 4, 1, 12, 0, tzinfo=timezone.utc
    )


def test_parse_run_time_utc_value_repairs_duplicate_seconds() -> None:
    parsed = parse_run_time_utc_value("2026-06-20T00:00:00:00+00:00")
    assert parsed == datetime(2026, 6, 20, 0, 0, tzinfo=timezone.utc)


def test_load_forecast_records_repairs_malformed_run_time_text(weather_db_url: str) -> None:
    with get_connection(weather_db_url) as conn:
        insert_station(
            conn,
            station_id="EGLC",
            name="London City Airport",
            timezone="Europe/London",
            lat=51.5053,
            lon=0.0553,
            country="gb",
        )
        conn.execute(
            """
            INSERT INTO calibration_forecast_records (
                station_id, model, run_time_utc, target_date, lead_hours,
                predicted_tmax_c, ingested_at_utc
            ) VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (
                "EGLC",
                "ecmwf_ifs025",
                "2026-06-20T00:00:00:00+00:00",
                "2026-06-21",
                24.0,
                18.0,
                "2026-06-25T00:00:00Z",
            ),
        )
        conn.commit()
        forecasts = load_forecast_records(conn)

    assert len(forecasts) == 1
    assert forecasts[0].run_time_utc == datetime(2026, 6, 20, 0, 0, tzinfo=timezone.utc)
