"""PostgreSQL helpers for weather observation and forecast collection."""

from __future__ import annotations

import os
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Generator

import psycopg
from psycopg import Connection
from psycopg.rows import dict_row

DEFAULT_SCHEMA_PATH = Path(__file__).resolve().parent / "schema_postgres.sql"


def utc_now_iso() -> str:
    """Return current UTC time as ISO-8601 with Z suffix."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def resolve_database_url(*, override: str | None = None) -> str:
    """Resolve Postgres URL from override, POLYTEMPO_DATABASE_URL, or DATABASE_URL."""
    if override:
        return override
    url = os.environ.get("POLYTEMPO_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("Set POLYTEMPO_DATABASE_URL or DATABASE_URL")
    return url


@contextmanager
def get_connection(database_url: str) -> Generator[Connection, None, None]:
    """Open a PostgreSQL connection with dict rows."""
    conn = psycopg.connect(database_url, row_factory=dict_row, autocommit=False)
    try:
        yield conn
    finally:
        conn.close()


def _execute_sql_script(conn: Connection, sql: str) -> None:
    """Execute a SQL script containing multiple statements."""
    statement = ""
    for line in sql.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("--"):
            continue
        statement += line + "\n"
        if stripped.endswith(";"):
            conn.execute(statement.strip())
            statement = ""
    if statement.strip():
        conn.execute(statement.strip())


def initialize_database(
    database_url: str,
    schema_path: Path = DEFAULT_SCHEMA_PATH,
) -> None:
    """Create or upgrade the database from schema_postgres.sql. Safe to run multiple times."""
    if not schema_path.is_file():
        raise FileNotFoundError(f"schema not found: {schema_path}")
    sql = schema_path.read_text(encoding="utf-8")
    with get_connection(database_url) as conn:
        _execute_sql_script(conn, sql)
        conn.commit()


def insert_station(
    conn: Connection,
    *,
    station_id: str,
    name: str,
    timezone: str,
    lat: float | None = None,
    lon: float | None = None,
    country: str | None = None,
    active: bool | int = True,
) -> None:
    """Insert or update a station row."""
    active_bool = bool(active)
    conn.execute(
        """
        INSERT INTO stations (
            station_id, name, timezone, lat, lon, country, active
        ) VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (station_id) DO UPDATE SET
            name = EXCLUDED.name,
            timezone = EXCLUDED.timezone,
            lat = EXCLUDED.lat,
            lon = EXCLUDED.lon,
            country = EXCLUDED.country,
            active = EXCLUDED.active
        """,
        (station_id, name, timezone, lat, lon, country, active_bool),
    )


def insert_observation_snapshot(
    conn: Connection,
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
    row = conn.execute(
        """
        INSERT INTO observation_snapshots (
            station_id, source, scraped_at_utc, observed_at_utc, observed_at_local,
            target_date_local, station_timezone, temp_c, raw_temp_text,
            raw_file_path, content_hash, created_at_utc
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
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
    ).fetchone()
    assert row is not None
    return int(row["id"])


def insert_forecast_snapshot(
    conn: Connection,
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
    row = conn.execute(
        """
        INSERT INTO forecast_snapshots (
            station_id, source, model, scraped_at_utc, forecast_generated_at_utc,
            target_time_utc, target_time_local, target_date_local, station_timezone,
            lead_hours_to_day_end, temp_c, requested_lat, requested_lon,
            returned_lat, returned_lon, raw_file_path, content_hash, created_at_utc
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
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
    ).fetchone()
    assert row is not None
    return int(row["id"])


def _fetch_collector_state_row(
    conn: Connection,
    collector_name: str,
    station_id: str,
    source: str,
) -> dict[str, object] | None:
    return conn.execute(
        """
        SELECT success_count, error_count FROM collector_state
        WHERE collector_name = %s AND station_id = %s AND source = %s
        """,
        (collector_name, station_id, source),
    ).fetchone()


def upsert_collector_state_success(
    conn: Connection,
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
            ) VALUES (%s, %s, %s, %s, 1, 0, %s)
            """,
            (collector_name, station_id, source, now, now),
        )
        return

    conn.execute(
        """
        UPDATE collector_state SET
            last_success_at_utc = %s,
            success_count = success_count + 1,
            updated_at_utc = %s
        WHERE collector_name = %s AND station_id = %s AND source = %s
        """,
        (now, now, collector_name, station_id, source),
    )


def upsert_collector_state_error(
    conn: Connection,
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
            ) VALUES (%s, %s, %s, %s, %s, 0, 1, %s)
            """,
            (collector_name, station_id, source, now, error_message, now),
        )
        return

    conn.execute(
        """
        UPDATE collector_state SET
            last_error_at_utc = %s,
            last_error_message = %s,
            error_count = error_count + 1,
            updated_at_utc = %s
        WHERE collector_name = %s AND station_id = %s AND source = %s
        """,
        (now, error_message, now, collector_name, station_id, source),
    )


def mark_collector_started(
    conn: Connection,
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
        ) VALUES (%s, %s, %s, %s, 0, 0, %s)
        ON CONFLICT (collector_name, station_id, source) DO UPDATE SET
            last_started_at_utc = EXCLUDED.last_started_at_utc,
            updated_at_utc = EXCLUDED.updated_at_utc
        """,
        (collector_name, station_id, source, now, now),
    )
