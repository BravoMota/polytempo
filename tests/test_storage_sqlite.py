"""Tests for weather SQLite storage helpers."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from polytempo.storage.sqlite import (
    get_connection,
    initialize_database,
    insert_forecast_snapshot,
    insert_observation_snapshot,
    insert_station,
    upsert_collector_state_error,
    upsert_collector_state_success,
)


def test_initialize_database_is_idempotent(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    initialize_database(db_path)
    initialize_database(db_path)

    with get_connection(db_path) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    assert "stations" in tables
    assert "observation_snapshots" in tables
    assert "forecast_snapshots" in tables
    assert "collector_state" in tables


def test_insert_station_and_observation_snapshot(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    initialize_database(db_path)

    with get_connection(db_path) as conn:
        insert_station(
            conn,
            station_id="EGLC",
            name="London City Airport",
            timezone="Europe/London",
            lat=51.5053,
            lon=0.0553,
            country="gb",
        )
        row_id = insert_observation_snapshot(
            conn,
            station_id="EGLC",
            source="wunderground",
            scraped_at_utc="2026-06-03T12:00:00Z",
            target_date_local="2026-06-03",
            station_timezone="Europe/London",
            temp_c=18.5,
            content_hash="abc123",
            created_at_utc="2026-06-03T12:00:01Z",
        )
        conn.commit()

    assert row_id == 1

    with get_connection(db_path) as conn:
        row = conn.execute(
            "SELECT temp_c, content_hash FROM observation_snapshots WHERE id = 1"
        ).fetchone()
    assert row["temp_c"] == pytest.approx(18.5)
    assert row["content_hash"] == "abc123"


def test_insert_forecast_snapshot(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    initialize_database(db_path)

    with get_connection(db_path) as conn:
        insert_station(
            conn,
            station_id="EGLC",
            name="London City Airport",
            timezone="Europe/London",
        )
        row_id = insert_forecast_snapshot(
            conn,
            station_id="EGLC",
            source="wunderground",
            scraped_at_utc="2026-06-03T12:00:00Z",
            target_date_local="2026-06-04",
            station_timezone="Europe/London",
            lead_hours_to_day_end=36.0,
            temp_c=19.0,
            created_at_utc="2026-06-03T12:00:01Z",
        )
        conn.commit()

    assert row_id == 1


def test_collector_state_success_and_error(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    initialize_database(db_path)

    with get_connection(db_path) as conn:
        insert_station(conn, station_id="EGLC", name="EGLC", timezone="Europe/London")
        upsert_collector_state_success(
            conn, "wunderground", "EGLC", "wunderground", now_utc="2026-06-03T12:00:00Z"
        )
        upsert_collector_state_success(
            conn, "wunderground", "EGLC", "wunderground", now_utc="2026-06-03T12:05:00Z"
        )
        upsert_collector_state_error(
            conn,
            "wunderground",
            "EGLC",
            "wunderground",
            "timeout",
            now_utc="2026-06-03T12:10:00Z",
        )
        conn.commit()

        row = conn.execute(
            """
            SELECT success_count, error_count, last_error_message
            FROM collector_state
            WHERE collector_name = 'wunderground' AND station_id = 'EGLC'
            """
        ).fetchone()

    assert row["success_count"] == 2
    assert row["error_count"] == 1
    assert row["last_error_message"] == "timeout"


def test_observation_snapshot_requires_station_fk(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    initialize_database(db_path)

    with get_connection(db_path) as conn:
        with pytest.raises(Exception):
            insert_observation_snapshot(
                conn,
                station_id="MISSING",
                source="wunderground",
                scraped_at_utc="2026-06-03T12:00:00Z",
                target_date_local="2026-06-03",
                station_timezone="Europe/London",
            )
            conn.commit()
