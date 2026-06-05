#!/usr/bin/env python3
"""Initialize the PolyTempo weather PostgreSQL schema from schema_postgres.sql."""

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
from polytempo.storage.postgres import (  # noqa: E402
    get_connection,
    initialize_database,
    resolve_database_url,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Create weather PostgreSQL schema")
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help="Path to weather_collectors.yaml",
    )
    parser.add_argument(
        "--database-url",
        default=None,
        help="Override POLYTEMPO_DATABASE_URL / DATABASE_URL",
    )
    args = parser.parse_args()

    database_url = resolve_database_url(override=args.database_url)
    config = load_weather_collectors_config(args.config)

    initialize_database(database_url)
    (config.raw_base_dir / "wunderground").mkdir(parents=True, exist_ok=True)

    with get_connection(database_url) as conn:
        sync_stations_from_config(conn, config)
        conn.commit()

    print(f"initialized database={database_url.split('@')[-1]}")
    print(f"raw_dir={config.raw_base_dir / 'wunderground'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
