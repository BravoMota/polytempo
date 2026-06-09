#!/usr/bin/env python3
"""Backup PolyTempo PostgreSQL databases to timestamped pg_dump files."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

REPO_ROOT = Path(__file__).resolve().parent.parent


def _resolve_weather_url(*, override: str | None = None) -> str:
    if override:
        return override
    url = os.environ.get("POLYTEMPO_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("Set POLYTEMPO_DATABASE_URL or DATABASE_URL")
    return url


def _resolve_paper_url(*, override: str | None = None) -> str:
    if override:
        return override
    url = os.environ.get("POLYTEMPO_PAPER_DATABASE_URL")
    if not url:
        raise RuntimeError("Set POLYTEMPO_PAPER_DATABASE_URL")
    return url


def _database_name(url: str) -> str:
    name = urlparse(url).path.lstrip("/")
    return name.split("?")[0] or "postgres"


def _pg_dump(url: str, out_path: Path) -> None:
    if shutil.which("pg_dump") is None:
        raise RuntimeError("pg_dump not found on PATH")

    subprocess.run(
        [
            "pg_dump",
            url,
            "--file",
            str(out_path),
            "--no-owner",
            "--no-acl",
        ],
        check=True,
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Backup weather and/or paper PostgreSQL databases"
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=REPO_ROOT / "backups",
        help="Directory for dump files (default: backups/)",
    )
    parser.add_argument(
        "--weather",
        action="store_true",
        help="Backup weather DB (POLYTEMPO_DATABASE_URL / DATABASE_URL)",
    )
    parser.add_argument(
        "--paper",
        action="store_true",
        help="Backup paper DB (POLYTEMPO_PAPER_DATABASE_URL)",
    )
    parser.add_argument(
        "--weather-url",
        default=None,
        help="Override weather database URL",
    )
    parser.add_argument(
        "--paper-url",
        default=None,
        help="Override paper database URL",
    )
    args = parser.parse_args()

    backup_weather = args.weather or not args.paper
    backup_paper = args.paper or not args.weather

    args.output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    if backup_weather:
        url = _resolve_weather_url(override=args.weather_url)
        out_path = args.output_dir / f"{_database_name(url)}_{stamp}.sql"
        _pg_dump(url, out_path)
        print(f"weather backup={out_path}")

    if backup_paper:
        url = _resolve_paper_url(override=args.paper_url)
        out_path = args.output_dir / f"{_database_name(url)}_{stamp}.sql"
        _pg_dump(url, out_path)
        print(f"paper backup={out_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
