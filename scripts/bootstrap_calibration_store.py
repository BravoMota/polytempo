#!/usr/bin/env python3
"""One-time bootstrap of the updated calibration store from 2026-02-01."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from polytempo.storage.postgres import initialize_database, resolve_database_url  # noqa: E402
from polytempo.weather.calibration_config import (  # noqa: E402
    DEFAULT_CALIBRATION_CONFIG_PATH,
    load_calibration_config,
)
from polytempo.weather.calibration_runner import run_bootstrap  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Bootstrap updated calibration store")
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CALIBRATION_CONFIG_PATH,
        help="Path to calibration.yaml",
    )
    parser.add_argument(
        "--database-url",
        default=None,
        help="Override POLYTEMPO_DATABASE_URL / DATABASE_URL",
    )
    parser.add_argument(
        "--no-obs",
        action="store_true",
        help="Skip WU observation fetch/upsert; use observations already in the DB",
    )
    parser.add_argument(
        "--station",
        "--station-id",
        dest="stations",
        action="append",
        default=None,
        metavar="STATION_ID",
        help=(
            "Limit INGEST to this station (repeatable; default: all configured stations). "
            "The recomputed stats CSV always covers every station in the store."
        ),
    )
    args = parser.parse_args()

    database_url = resolve_database_url(override=args.database_url)
    initialize_database(database_url)
    config = load_calibration_config(args.config)
    if args.stations:
        config = config.subset_stations(args.stations)
    return run_bootstrap(config, database_url, skip_observations=args.no_obs)


if __name__ == "__main__":
    raise SystemExit(main())
