"""PostgreSQL helpers for weather observation and forecast collection."""

from __future__ import annotations

import os
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Generator
from urllib.parse import urlparse

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


def database_name_from_url(url: str) -> str:
    """Return the database name component from a Postgres URL."""
    parsed = urlparse(url)
    path = parsed.path.lstrip("/")
    if not path:
        raise ValueError(f"database name missing from URL: {url!r}")
    return path.split("/", 1)[0]


def assert_test_database_url(url: str) -> None:
    """Refuse URLs that do not clearly target a test database."""
    db_name = database_name_from_url(url)
    if "test" not in db_name.lower():
        raise RuntimeError(f"refusing non-test database: {db_name!r}")


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
        conn.execute(
            "ALTER TABLE forecast_snapshots ADD COLUMN IF NOT EXISTS raw_temp_text TEXT"
        )
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
    raw_temp_text: str | None = None,
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
            lead_hours_to_day_end, temp_c, raw_temp_text, requested_lat, requested_lon,
            returned_lat, returned_lon, raw_file_path, content_hash, created_at_utc
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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
            raw_temp_text,
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


def upsert_calibration_observed_tmax(
    conn: Connection,
    *,
    station_id: str,
    target_date: str,
    observed_tmax_f: float,
    observed_tmax_c: float,
    source: str,
    fetched_at_utc: str,
) -> None:
    conn.execute(
        """
        INSERT INTO calibration_observed_tmax (
            station_id, target_date, observed_tmax_f, observed_tmax_c,
            source, fetched_at_utc
        ) VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (station_id, target_date) DO UPDATE SET
            observed_tmax_f = EXCLUDED.observed_tmax_f,
            observed_tmax_c = EXCLUDED.observed_tmax_c,
            source = EXCLUDED.source,
            fetched_at_utc = EXCLUDED.fetched_at_utc
        """,
        (
            station_id,
            target_date,
            observed_tmax_f,
            observed_tmax_c,
            source,
            fetched_at_utc,
        ),
    )


def upsert_calibration_forecast_record(
    conn: Connection,
    *,
    station_id: str,
    model: str,
    run_time_utc: str,
    target_date: str,
    lead_hours: float,
    predicted_tmax_c: float,
    ingested_at_utc: str,
    forecast_lat: float | None = None,
    forecast_lon: float | None = None,
    raw_file_path: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO calibration_forecast_records (
            station_id, model, run_time_utc, target_date, lead_hours,
            predicted_tmax_c, forecast_lat, forecast_lon, raw_file_path,
            ingested_at_utc
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (station_id, model, run_time_utc, target_date) DO UPDATE SET
            lead_hours = EXCLUDED.lead_hours,
            predicted_tmax_c = EXCLUDED.predicted_tmax_c,
            forecast_lat = EXCLUDED.forecast_lat,
            forecast_lon = EXCLUDED.forecast_lon,
            raw_file_path = EXCLUDED.raw_file_path,
            ingested_at_utc = EXCLUDED.ingested_at_utc
        """,
        (
            station_id,
            model,
            run_time_utc,
            target_date,
            lead_hours,
            predicted_tmax_c,
            forecast_lat,
            forecast_lon,
            raw_file_path,
            ingested_at_utc,
        ),
    )


def get_calibration_job_state(
    conn: Connection,
    job_name: str,
) -> dict[str, object] | None:
    return conn.execute(
        "SELECT * FROM calibration_job_state WHERE job_name = %s",
        (job_name,),
    ).fetchone()


def insert_open_meteo_fetch_cycle(
    conn: Connection,
    *,
    station_id: str,
    fetched_at_utc: str,
    requested_lat: float | None,
    requested_lon: float | None,
    returned_lat: float | None,
    returned_lon: float | None,
    collector_name: str,
    meta_staleness_detected: bool,
    created_at_utc: str | None = None,
) -> int:
    """Insert one Open-Meteo fetch cycle row and return its id."""
    created = created_at_utc or utc_now_iso()
    row = conn.execute(
        """
        INSERT INTO open_meteo_fetch_cycles (
            station_id, fetched_at_utc, requested_lat, requested_lon,
            returned_lat, returned_lon, collector_name, meta_staleness_detected,
            created_at_utc
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (
            station_id,
            fetched_at_utc,
            requested_lat,
            requested_lon,
            returned_lat,
            returned_lon,
            collector_name,
            meta_staleness_detected,
            created,
        ),
    ).fetchone()
    assert row is not None
    return int(row["id"])


def insert_open_meteo_model_meta_snapshot(
    conn: Connection,
    *,
    fetch_cycle_id: int,
    station_id: str,
    model: str,
    run_init_utc: str,
    run_available_utc: str,
    run_modified_utc: str,
    availability_lag_hours: float,
    update_interval_seconds: int,
    temporal_resolution_seconds: int,
    data_end_utc: str | None,
    fetched_at_utc: str,
) -> None:
    conn.execute(
        """
        INSERT INTO open_meteo_model_meta_snapshots (
            fetch_cycle_id, station_id, model, run_init_utc, run_available_utc,
            run_modified_utc, availability_lag_hours, update_interval_seconds,
            temporal_resolution_seconds, data_end_utc, fetched_at_utc
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            fetch_cycle_id,
            station_id,
            model,
            run_init_utc,
            run_available_utc,
            run_modified_utc,
            availability_lag_hours,
            update_interval_seconds,
            temporal_resolution_seconds,
            data_end_utc,
            fetched_at_utc,
        ),
    )


def insert_open_meteo_forecast_snapshot(
    conn: Connection,
    *,
    fetch_cycle_id: int,
    station_id: str,
    model: str,
    target_date_local: str,
    predicted_tmax_c: float,
    run_init_utc: str,
    init_lead_hours: float,
    wall_clock_lead_hours: float,
    fetched_at_utc: str,
) -> None:
    conn.execute(
        """
        INSERT INTO open_meteo_forecast_snapshots (
            fetch_cycle_id, station_id, model, target_date_local,
            predicted_tmax_c, run_init_utc, init_lead_hours,
            wall_clock_lead_hours, fetched_at_utc
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            fetch_cycle_id,
            station_id,
            model,
            target_date_local,
            predicted_tmax_c,
            run_init_utc,
            init_lead_hours,
            wall_clock_lead_hours,
            fetched_at_utc,
        ),
    )


def upsert_calibration_job_state(
    conn: Connection,
    *,
    job_name: str,
    updated_at_utc: str,
    last_success_at_utc: str | None = None,
    last_error_at_utc: str | None = None,
    last_error_message: str | None = None,
    last_target_date: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO calibration_job_state (
            job_name, last_success_at_utc, last_error_at_utc,
            last_error_message, last_target_date, updated_at_utc
        ) VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (job_name) DO UPDATE SET
            last_success_at_utc = COALESCE(EXCLUDED.last_success_at_utc, calibration_job_state.last_success_at_utc),
            last_error_at_utc = COALESCE(EXCLUDED.last_error_at_utc, calibration_job_state.last_error_at_utc),
            last_error_message = COALESCE(EXCLUDED.last_error_message, calibration_job_state.last_error_message),
            last_target_date = COALESCE(EXCLUDED.last_target_date, calibration_job_state.last_target_date),
            updated_at_utc = EXCLUDED.updated_at_utc
        """,
        (
            job_name,
            last_success_at_utc,
            last_error_at_utc,
            last_error_message,
            last_target_date,
            updated_at_utc,
        ),
    )
