#!/usr/bin/env python3
"""One-time bootstrap of WU forecast calibration (history obs + forecast snapshots)."""

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
from polytempo.weather.calibration_runner import run_wu_bootstrap  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Bootstrap WU forecast calibration store")
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
        help="Skip v1 API daily Tmax fetch; use observations already in the DB",
    )
    args = parser.parse_args()

    database_url = resolve_database_url(override=args.database_url)
    initialize_database(database_url)
    config = load_calibration_config(args.config)
    return run_wu_bootstrap(config, database_url, skip_observations=args.no_obs)


if __name__ == "__main__":
    raise SystemExit(main())
