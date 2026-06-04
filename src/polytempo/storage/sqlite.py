"""SQLite helpers for weather observation and forecast collection."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"


def utc_now_iso() -> str:
    """Return current UTC time as ISO-8601 with Z suffix."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def get_connection(db_path: Path) -> sqlite3.Connection:
    """Open a SQLite connection with foreign keys enabled."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def initialize_database(
    db_path: Path,
    schema_path: Path = DEFAULT_SCHEMA_PATH,
) -> None:
    """Create or upgrade the database from schema.sql. Safe to run multiple times."""
    if not schema_path.is_file():
        raise FileNotFoundError(f"schema not found: {schema_path}")
    sql = schema_path.read_text(encoding="utf-8")
    with get_connection(db_path) as conn:
        conn.executescript(sql)
        conn.commit()


def insert_station(
    conn: sqlite3.Connection,
    *,
    station_id: str,
    name: str,
    timezone: str,
    lat: float | None = None,
    lon: float | None = None,
    country: str | None = None,
    active: int = 1,
) -> None:
    """Insert or replace a station row."""
    conn.execute(
        """
        INSERT OR REPLACE INTO stations (
            station_id, name, timezone, lat, lon, country, active
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (station_id, name, timezone, lat, lon, country, active),
    )


def insert_observation_snapshot(
    conn: sqlite3.Connection,
    *,
    station_id: str,
    source: str,
    scraped_at_utc: str,
    target_date_local: str,
    station_timezone: str,
    created_at_utc: str | None = None,
    observed_at_utc: str | None = None,
    observed_at_local: str | None = None,
    temp_c: float | None = None,
    raw_temp_text: str | None = None,
    raw_file_path: str | None = None,
    content_hash: str | None = None,
) -> int:
    """Insert one observation snapshot row and return its id."""
    created = created_at_utc or utc_now_iso()
    cursor = conn.execute(
        """
        INSERT INTO observation_snapshots (
            station_id, source, scraped_at_utc, observed_at_utc, observed_at_local,
            target_date_local, station_timezone, temp_c, raw_temp_text,
            raw_file_path, content_hash, created_at_utc
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            station_id,
            source,
            scraped_at_utc,
            observed_at_utc,
            observed_at_local,
            target_date_local,
            station_timezone,
            temp_c,
            raw_temp_text,
            raw_file_path,
            content_hash,
            created,
        ),
    )
    return int(cursor.lastrowid)


def insert_forecast_snapshot(
    conn: sqlite3.Connection,
    *,
    station_id: str,
    source: str,
    scraped_at_utc: str,
    target_date_local: str,
    station_timezone: str,
    created_at_utc: str | None = None,
    model: str | None = None,
    forecast_generated_at_utc: str | None = None,
    target_time_utc: str | None = None,
    target_time_local: str | None = None,
    lead_hours_to_day_end: float | None = None,
    temp_c: float | None = None,
    requested_lat: float | None = None,
    requested_lon: float | None = None,
    returned_lat: float | None = None,
    returned_lon: float | None = None,
    raw_file_path: str | None = None,
    content_hash: str | None = None,
) -> int:
    """Insert one forecast snapshot row and return its id."""
    created = created_at_utc or utc_now_iso()
    cursor = conn.execute(
        """
        INSERT INTO forecast_snapshots (
            station_id, source, model, scraped_at_utc, forecast_generated_at_utc,
            target_time_utc, target_time_local, target_date_local, station_timezone,
            lead_hours_to_day_end, temp_c, requested_lat, requested_lon,
            returned_lat, returned_lon, raw_file_path, content_hash, created_at_utc
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            station_id,
            source,
            model,
            scraped_at_utc,
            forecast_generated_at_utc,
            target_time_utc,
            target_time_local,
            target_date_local,
            station_timezone,
            lead_hours_to_day_end,
            temp_c,
            requested_lat,
            requested_lon,
            returned_lat,
            returned_lon,
            raw_file_path,
            content_hash,
            created,
        ),
    )
    return int(cursor.lastrowid)


def _fetch_collector_state_row(
    conn: sqlite3.Connection,
    collector_name: str,
    station_id: str,
    source: str,
) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT success_count, error_count FROM collector_state
        WHERE collector_name = ? AND station_id = ? AND source = ?
        """,
        (collector_name, station_id, source),
    ).fetchone()


def upsert_collector_state_success(
    conn: sqlite3.Connection,
    collector_name: str,
    station_id: str,
    source: str,
    *,
    now_utc: str | None = None,
) -> None:
    """Record a successful collector cycle for one station."""
    now = now_utc or utc_now_iso()
    row = _fetch_collector_state_row(conn, collector_name, station_id, source)
    if row is None:
        conn.execute(
            """
            INSERT INTO collector_state (
                collector_name, station_id, source,
                last_success_at_utc, success_count, error_count, updated_at_utc
            ) VALUES (?, ?, ?, ?, 1, 0, ?)
            """,
            (collector_name, station_id, source, now, now),
        )
        return

    conn.execute(
        """
        UPDATE collector_state SET
            last_success_at_utc = ?,
            success_count = success_count + 1,
            updated_at_utc = ?
        WHERE collector_name = ? AND station_id = ? AND source = ?
        """,
        (now, now, collector_name, station_id, source),
    )


def upsert_collector_state_error(
    conn: sqlite3.Connection,
    collector_name: str,
    station_id: str,
    source: str,
    error_message: str,
    *,
    now_utc: str | None = None,
) -> None:
    """Record a failed collector cycle for one station."""
    now = now_utc or utc_now_iso()
    row = _fetch_collector_state_row(conn, collector_name, station_id, source)
    if row is None:
        conn.execute(
            """
            INSERT INTO collector_state (
                collector_name, station_id, source,
                last_error_at_utc, last_error_message,
                success_count, error_count, updated_at_utc
            ) VALUES (?, ?, ?, ?, ?, 0, 1, ?)
            """,
            (collector_name, station_id, source, now, error_message, now),
        )
        return

    conn.execute(
        """
        UPDATE collector_state SET
            last_error_at_utc = ?,
            last_error_message = ?,
            error_count = error_count + 1,
            updated_at_utc = ?
        WHERE collector_name = ? AND station_id = ? AND source = ?
        """,
        (now, error_message, now, collector_name, station_id, source),
    )


def mark_collector_started(
    conn: sqlite3.Connection,
    collector_name: str,
    station_id: str,
    source: str,
    *,
    now_utc: str | None = None,
) -> None:
    """Record that a collector cycle started for one station."""
    now = now_utc or utc_now_iso()
    conn.execute(
        """
        INSERT INTO collector_state (
            collector_name, station_id, source,
            last_started_at_utc, success_count, error_count, updated_at_utc
        ) VALUES (?, ?, ?, ?, 0, 0, ?)
        ON CONFLICT(collector_name, station_id, source) DO UPDATE SET
            last_started_at_utc = excluded.last_started_at_utc,
            updated_at_utc = excluded.updated_at_utc
        """,
        (collector_name, station_id, source, now, now),
    )
