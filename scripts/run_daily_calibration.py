#!/usr/bin/env python3
"""Incremental daily calibration job (cron target at 02:00 UTC)."""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from polytempo.collectors.schedule import (  # noqa: E402
    is_slot_due,
    next_scheduled_instant_utc,
)
from polytempo.storage.postgres import resolve_database_url  # noqa: E402
from polytempo.weather.calibration_config import (  # noqa: E402
    DEFAULT_CALIBRATION_CONFIG_PATH,
    load_calibration_config,
)
from polytempo.weather.calibration_runner import (
    run_daily,
    run_wu_daily,
)  # noqa: E402

logger = logging.getLogger(__name__)
_stop = False


def _handle_signal(signum: int, _frame: object) -> None:
    global _stop
    logger.info("received signal %s, stopping...", signum)
    _stop = True


def _run_once(config_path: Path, database_url: str) -> int:
    config = load_calibration_config(config_path)
    om_code = run_daily(config, database_url)
    wu_code = run_wu_daily(config, database_url)
    if om_code != 0:
        return om_code
    if wu_code != 0:
        return wu_code
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Run daily calibration update")
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CALIBRATION_CONFIG_PATH,
    )
    parser.add_argument(
        "--database-url",
        default=None,
        help="Override POLYTEMPO_DATABASE_URL / DATABASE_URL",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run one cycle and exit (for cron)",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    database_url = resolve_database_url(override=args.database_url)

    if args.once:
        return _run_once(args.config, database_url)

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    config = load_calibration_config(args.config)
    interval_seconds = 24 * 3600
    last_slot: datetime | None = None

    while not _stop:
        now = datetime.now(timezone.utc)
        if is_slot_due(
            now,
            last_slot,
            interval_seconds=interval_seconds,
            anchor_time_utc=config.schedule_anchor_time_utc,
        ):
            logger.info("running daily calibration slot")
            code = _run_once(args.config, database_url)
            if code != 0:
                logger.error("daily calibration failed with exit code %s", code)
            last_slot = now

        if _stop:
            break
        wake = next_scheduled_instant_utc(
            now,
            interval_seconds=interval_seconds,
            anchor_time_utc=config.schedule_anchor_time_utc,
        )
        sleep_s = max(1.0, (wake - now).total_seconds())
        logger.info("sleeping %.0fs until next slot %s", sleep_s, wake.isoformat())
        time.sleep(min(sleep_s, 60.0))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
