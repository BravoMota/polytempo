#!/usr/bin/env python3
"""Initialize the PolyTempo weather SQLite database from schema.sql."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from polytempo.collectors.config import (  # noqa: E402
    DEFAULT_CONFIG_PATH,
    load_weather_collectors_config,
    sync_stations_from_config,
)
from polytempo.storage.sqlite import get_connection, initialize_database  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Create weather SQLite DB from schema.sql")
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help="Path to weather_collectors.yaml",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=None,
        help="Override database path (default from config)",
    )
    args = parser.parse_args()

    config = load_weather_collectors_config(args.config)
    db_path = args.db or config.weather_db_path

    initialize_database(db_path)
    (config.raw_base_dir / "wunderground").mkdir(parents=True, exist_ok=True)

    with get_connection(db_path) as conn:
        sync_stations_from_config(conn, config)
        conn.commit()

    print(f"initialized db={db_path}")
    print(f"raw_dir={config.raw_base_dir / 'wunderground'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
