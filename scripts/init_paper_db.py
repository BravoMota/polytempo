#!/usr/bin/env python3
"""Initialize the PolyTempo paper trading PostgreSQL schema."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from polytempo.storage.paper_postgres import (  # noqa: E402
    initialize_paper_database,
    resolve_paper_database_url,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Create paper trading PostgreSQL schema")
    parser.add_argument(
        "--database-url",
        default=None,
        help="Override POLYTEMPO_PAPER_DATABASE_URL",
    )
    args = parser.parse_args()

    database_url = resolve_paper_database_url(override=args.database_url)
    initialize_paper_database(database_url)
    print(f"initialized paper database={database_url.split('@')[-1]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
