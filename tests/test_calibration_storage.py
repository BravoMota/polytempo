"""Tests for calibration PostgreSQL storage."""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from polytempo.storage.postgres import get_connection, insert_station
from polytempo.weather.calibration_compute import ForecastRecord
from polytempo.weather.calibration_storage import (
    load_forecast_records,
    load_observations_map,
    upsert_forecast_record,
    upsert_observation,
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
