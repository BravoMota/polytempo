#!/usr/bin/env python3
"""One-time migration from SQLite weather DB to PostgreSQL."""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from polytempo.storage.postgres import (  # noqa: E402
    get_connection,
    initialize_database,
    resolve_database_url,
)
from polytempo.weather.data_dir import WEATHER_DATA_DIR  # noqa: E402

DEFAULT_SQLITE_PATH = WEATHER_DATA_DIR / "polytempo_weather.db"
BATCH_SIZE = 500

TABLES = (
    "stations",
    "observation_snapshots",
    "forecast_snapshots",
    "collector_state",
)

STATION_COLUMNS = (
    "station_id",
    "name",
    "timezone",
    "lat",
    "lon",
    "country",
    "active",
)

OBSERVATION_COLUMNS = (
    "id",
    "station_id",
    "source",
    "scraped_at_utc",
    "observed_at_utc",
    "observed_at_local",
    "target_date_local",
    "station_timezone",
    "temp_c",
    "raw_temp_text",
    "raw_file_path",
    "content_hash",
    "created_at_utc",
)

FORECAST_COLUMNS = (
    "id",
    "station_id",
    "source",
    "model",
    "scraped_at_utc",
    "forecast_generated_at_utc",
    "target_time_utc",
    "target_time_local",
    "target_date_local",
    "station_timezone",
    "lead_hours_to_day_end",
    "temp_c",
    "requested_lat",
    "requested_lon",
    "returned_lat",
    "returned_lon",
    "raw_file_path",
    "content_hash",
    "created_at_utc",
)

COLLECTOR_STATE_COLUMNS = (
    "id",
    "collector_name",
    "station_id",
    "source",
    "last_started_at_utc",
    "last_success_at_utc",
    "last_error_at_utc",
    "last_error_message",
    "success_count",
    "error_count",
    "updated_at_utc",
)


def _sqlite_count(conn: sqlite3.Connection, table: str) -> int:
    row = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()
    assert row is not None
    return int(row[0])


def _postgres_count(conn: object, table: str) -> int:
    row = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()  # type: ignore[attr-defined]
    assert row is not None
    return int(row["n"])


def _normalize_station_row(row: sqlite3.Row) -> tuple[object, ...]:
    values = list(row)
    active_idx = STATION_COLUMNS.index("active")
    values[active_idx] = bool(values[active_idx])
    return tuple(values)


def _copy_table(
    sqlite_conn: sqlite3.Connection,
    pg_conn: object,
    *,
    table: str,
    columns: tuple[str, ...],
    normalize_row: object | None = None,
    dry_run: bool,
) -> int:
    select_sql = f"SELECT {', '.join(columns)} FROM {table} ORDER BY {columns[0]}"
    rows = sqlite_conn.execute(select_sql).fetchall()
    if dry_run or not rows:
        return len(rows)

    placeholders = ", ".join(["%s"] * len(columns))
    insert_sql = f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})"

    for start in range(0, len(rows), BATCH_SIZE):
        batch = rows[start : start + BATCH_SIZE]
        with pg_conn.cursor() as cur:  # type: ignore[attr-defined]
            for row in batch:
                values = normalize_row(row) if normalize_row else tuple(row)
                cur.execute(insert_sql, values)
        pg_conn.commit()  # type: ignore[attr-defined]

    if "id" in columns:
        pg_conn.execute(  # type: ignore[attr-defined]
            f"""
            SELECT setval(
                pg_get_serial_sequence('{table}', 'id'),
                COALESCE((SELECT MAX(id) FROM {table}), 1)
            )
            """
        )
        pg_conn.commit()  # type: ignore[attr-defined]

    return len(rows)


def migrate(
    sqlite_path: Path,
    database_url: str,
    *,
    init_target: bool,
    dry_run: bool,
) -> int:
    if not sqlite_path.is_file():
        raise FileNotFoundError(f"sqlite database not found: {sqlite_path}")

    sqlite_conn = sqlite3.connect(str(sqlite_path))
    sqlite_conn.row_factory = sqlite3.Row

    try:
        source_counts = {table: _sqlite_count(sqlite_conn, table) for table in TABLES}
        print("source counts:")
        for table in TABLES:
            print(f"  {table}: {source_counts[table]}")

        if dry_run:
            print("dry-run: no writes performed")
            return 0

        if init_target:
            initialize_database(database_url)

        with get_connection(database_url) as pg_conn:
            pg_conn.execute(
                "TRUNCATE TABLE forecast_snapshots, observation_snapshots, "
                "collector_state, stations RESTART IDENTITY CASCADE"
            )
            pg_conn.commit()

            copied = {
                "stations": _copy_table(
                    sqlite_conn,
                    pg_conn,
                    table="stations",
                    columns=STATION_COLUMNS,
                    normalize_row=_normalize_station_row,
                    dry_run=False,
                ),
                "observation_snapshots": _copy_table(
                    sqlite_conn,
                    pg_conn,
                    table="observation_snapshots",
                    columns=OBSERVATION_COLUMNS,
                    dry_run=False,
                ),
                "forecast_snapshots": _copy_table(
                    sqlite_conn,
                    pg_conn,
                    table="forecast_snapshots",
                    columns=FORECAST_COLUMNS,
                    dry_run=False,
                ),
                "collector_state": _copy_table(
                    sqlite_conn,
                    pg_conn,
                    table="collector_state",
                    columns=COLLECTOR_STATE_COLUMNS,
                    dry_run=False,
                ),
            }

            target_counts = {table: _postgres_count(pg_conn, table) for table in TABLES}
    finally:
        sqlite_conn.close()

    print("copied rows:")
    for table in TABLES:
        print(f"  {table}: {copied[table]}")

    print("target counts:")
    mismatches = []
    for table in TABLES:
        print(f"  {table}: {target_counts[table]}")
        if target_counts[table] != source_counts[table]:
            mismatches.append(table)

    if mismatches:
        print(f"ERROR: count mismatch for: {', '.join(mismatches)}", file=sys.stderr)
        return 1

    print("migration complete")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Migrate weather SQLite DB to PostgreSQL")
    parser.add_argument(
        "--sqlite",
        type=Path,
        default=DEFAULT_SQLITE_PATH,
        help="Path to source SQLite database",
    )
    parser.add_argument(
        "--database-url",
        default=None,
        help="Override POLYTEMPO_DATABASE_URL / DATABASE_URL",
    )
    parser.add_argument(
        "--init-target",
        action="store_true",
        help="Apply schema_postgres.sql before copying data",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print source row counts only",
    )
    args = parser.parse_args()

    database_url = resolve_database_url(override=args.database_url)
    return migrate(
        args.sqlite.resolve(),
        database_url,
        init_target=args.init_target,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    raise SystemExit(main())
