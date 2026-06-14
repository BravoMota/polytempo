#!/usr/bin/env python3
"""Backup PolyTempo PostgreSQL databases to compressed pg_dump files."""

from __future__ import annotations

import argparse
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from polytempo.collectors.schedule import (  # noqa: E402
    is_slot_due,
    next_scheduled_instant_utc,
)
from polytempo.storage.postgres import database_name_from_url  # noqa: E402

DATABASES: tuple[tuple[str, str], ...] = (
    ("weather", "POLYTEMPO_DATABASE_URL"),
    ("weather_test", "POLYTEMPO_TEST_DATABASE_URL"),
    ("paper", "POLYTEMPO_PAPER_DATABASE_URL"),
    ("paper_test", "POLYTEMPO_PAPER_TEST_DATABASE_URL"),
)

LOGICAL_NAMES = tuple(name for name, _ in DATABASES)
DATE_DIR_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
DEFAULT_RETENTION_DAYS = 14
DEFAULT_SCHEDULE_ANCHOR_UTC = "03:00"
SCHEDULE_INTERVAL_SECONDS = 24 * 3600

logger = logging.getLogger(__name__)
_stop = False


def resolve_output_dir(*, override: Path | None = None) -> Path:
    if override is not None:
        return override
    env_dir = os.environ.get("POLYTEMPO_BACKUP_DIR")
    if env_dir:
        return Path(env_dir)
    return REPO_ROOT / "backups"


def resolve_database_url(env_var: str) -> str:
    url = os.environ.get(env_var)
    if url:
        return url
    if env_var == "POLYTEMPO_DATABASE_URL":
        fallback = os.environ.get("DATABASE_URL")
        if fallback:
            return fallback
    raise RuntimeError(
        f"Set {env_var}"
        + (" or DATABASE_URL" if env_var == "POLYTEMPO_DATABASE_URL" else "")
    )


def selected_databases(only: list[str] | None) -> list[tuple[str, str]]:
    if only is None:
        return list(DATABASES)
    unknown = set(only) - set(LOGICAL_NAMES)
    if unknown:
        raise RuntimeError(f"unknown database name(s): {', '.join(sorted(unknown))}")
    by_name = dict(DATABASES)
    return [(name, by_name[name]) for name in only]


def run_date_dir(*, now: datetime | None = None) -> str:
    instant = now or datetime.now(timezone.utc)
    return instant.strftime("%Y-%m-%d")


def dump_filename(db_name: str, *, now: datetime | None = None) -> str:
    instant = now or datetime.now(timezone.utc)
    stamp = instant.strftime("%Y%m%dT%H%M%SZ")
    return f"{db_name}_{stamp}.dump"


def dump_path(output_dir: Path, db_name: str, *, now: datetime | None = None) -> Path:
    return output_dir / run_date_dir(now=now) / dump_filename(db_name, now=now)


def pg_dump(url: str, out_path: Path, *, dry_run: bool = False) -> None:
    if dry_run:
        print(f"would dump {database_name_from_url(url)} -> {out_path}")
        return
    if shutil.which("pg_dump") is None:
        raise RuntimeError("pg_dump not found on PATH")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "pg_dump",
            url,
            "--format=custom",
            "--file",
            str(out_path),
            "--no-owner",
            "--no-acl",
        ],
        check=True,
    )
    print(f"backup={out_path}")


def prune_old_backups(
    output_dir: Path,
    *,
    retention_days: int,
    today: date | None = None,
    dry_run: bool = False,
) -> list[Path]:
    if retention_days < 1:
        raise ValueError("retention_days must be >= 1")
    if not output_dir.is_dir():
        return []

    cutoff = (today or date.today()) - timedelta(days=retention_days)
    removed: list[Path] = []
    for entry in sorted(output_dir.iterdir()):
        if not entry.is_dir() or not DATE_DIR_PATTERN.match(entry.name):
            continue
        try:
            dir_date = date.fromisoformat(entry.name)
        except ValueError:
            continue
        if dir_date >= cutoff:
            continue
        removed.append(entry)
        if dry_run:
            print(f"would remove {entry}")
        else:
            shutil.rmtree(entry)
            print(f"removed={entry}")
    return removed


def run_backup(
    *,
    output_dir: Path,
    only: list[str] | None = None,
    retention_days: int = DEFAULT_RETENTION_DAYS,
    skip_missing: bool = False,
    dry_run: bool = False,
    now: datetime | None = None,
) -> int:
    targets = selected_databases(only)
    for logical_name, env_var in targets:
        try:
            url = resolve_database_url(env_var)
        except RuntimeError as exc:
            if skip_missing:
                print(f"skip {logical_name}: {exc}")
                continue
            raise
        db_name = database_name_from_url(url)
        out_path = dump_path(output_dir, db_name, now=now)
        pg_dump(url, out_path, dry_run=dry_run)

    prune_old_backups(
        output_dir,
        retention_days=retention_days,
        today=(now or datetime.now(timezone.utc)).date(),
        dry_run=dry_run,
    )
    return 0


def _handle_signal(signum: int, _frame: object) -> None:
    global _stop
    logger.info("received signal %s, stopping...", signum)
    _stop = True


def _run_once(
    *,
    output_dir: Path,
    only: list[str] | None,
    retention_days: int,
    skip_missing: bool,
    dry_run: bool,
) -> int:
    try:
        return run_backup(
            output_dir=output_dir,
            only=only,
            retention_days=retention_days,
            skip_missing=skip_missing,
            dry_run=dry_run,
        )
    except (RuntimeError, subprocess.CalledProcessError) as exc:
        logger.error("backup failed: %s", exc)
        return 1


def _run_daemon(
    *,
    output_dir: Path,
    only: list[str] | None,
    retention_days: int,
    skip_missing: bool,
    anchor_time_utc: str,
) -> int:
    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    last_run_slot: datetime | None = None

    while not _stop:
        now = datetime.now(timezone.utc)
        due, slot = is_slot_due(
            now,
            SCHEDULE_INTERVAL_SECONDS,
            anchor_time_utc,
            last_run_slot,
        )
        if due:
            logger.info("running backup slot %s", slot.isoformat())
            code = _run_once(
                output_dir=output_dir,
                only=only,
                retention_days=retention_days,
                skip_missing=skip_missing,
                dry_run=False,
            )
            if code == 0:
                last_run_slot = slot
            else:
                logger.error("backup failed with exit code %s", code)

        if _stop:
            break
        wake = next_scheduled_instant_utc(
            now,
            SCHEDULE_INTERVAL_SECONDS,
            anchor_time_utc,
        )
        sleep_s = max(1.0, (wake - now).total_seconds())
        logger.info("sleeping %.0fs until next slot %s", sleep_s, wake.isoformat())
        time.sleep(min(sleep_s, 60.0))

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Backup PolyTempo PostgreSQL databases (pg_dump custom format)"
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Backup all four databases (default when --only is omitted)",
    )
    parser.add_argument(
        "--only",
        nargs="+",
        choices=LOGICAL_NAMES,
        metavar="NAME",
        help="Backup subset: " + ", ".join(LOGICAL_NAMES),
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Backup root (default: backups/ or POLYTEMPO_BACKUP_DIR)",
    )
    parser.add_argument(
        "--retention-days",
        type=int,
        default=DEFAULT_RETENTION_DAYS,
        help=f"Remove date dirs older than N days (default: {DEFAULT_RETENTION_DAYS})",
    )
    parser.add_argument(
        "--skip-missing",
        action="store_true",
        help="Skip databases whose env URL is unset instead of failing",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print planned dumps and pruning without writing or deleting (--once only)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run one backup cycle and exit (default: daemon at 03:00 UTC)",
    )
    args = parser.parse_args()

    if args.only and args.all:
        parser.error("use either --all or --only, not both")
    if args.dry_run and not args.once:
        parser.error("--dry-run requires --once")

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    output_dir = resolve_output_dir(override=args.output_dir)
    if args.once:
        return _run_once(
            output_dir=output_dir,
            only=args.only,
            retention_days=args.retention_days,
            skip_missing=args.skip_missing,
            dry_run=args.dry_run,
        )

    return _run_daemon(
        output_dir=output_dir,
        only=args.only,
        retention_days=args.retention_days,
        skip_missing=args.skip_missing,
        anchor_time_utc=DEFAULT_SCHEDULE_ANCHOR_UTC,
    )


if __name__ == "__main__":
    raise SystemExit(main())
