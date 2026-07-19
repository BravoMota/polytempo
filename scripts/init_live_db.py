#!/usr/bin/env python3
"""Initialize the PolyTempo live trading PostgreSQL schema."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from polytempo.storage.live_postgres import (  # noqa: E402
    initialize_live_database,
    resolve_live_database_url,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Create live trading PostgreSQL schema")
    parser.add_argument(
        "--database-url",
        default=None,
        help="Override POLYTEMPO_LIVE_DATABASE_URL",
    )
    args = parser.parse_args()

    database_url = resolve_live_database_url(override=args.database_url)
    initialize_live_database(database_url)
    print(f"initialized live database={database_url.split('@')[-1]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
