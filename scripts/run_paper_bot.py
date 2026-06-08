#!/usr/bin/env python3
"""Run the paper trading bot continuously (lead-time gate scheduled)."""

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

from polytempo.paper.bot import (  # noqa: E402
    SETTLE_SWEEP_INTERVAL,
    compute_next_wake,
    reload_profiles,
    run_preview,
    run_tick,
)
from polytempo.paper.ledger import PostgresLedgerStore  # noqa: E402
from polytempo.profiles.load import DEFAULT_PROFILES_PATH  # noqa: E402
from polytempo.storage.paper_postgres import resolve_paper_database_url  # noqa: E402

logger = logging.getLogger(__name__)
_stop = False


def _handle_signal(signum: int, _frame: object) -> None:
    global _stop
    logger.info("received signal %s, stopping...", signum)
    _stop = True


def _config_mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def _sleep_until(deadline: datetime) -> None:
    while not _stop:
        now = datetime.now(timezone.utc)
        if now >= deadline:
            return
        remaining = (deadline - now).total_seconds()
        time.sleep(min(remaining, 1.0))


def main() -> int:
    parser = argparse.ArgumentParser(description="Run paper trading bot")
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_PROFILES_PATH,
        help="Path to paper_profiles.yaml",
    )
    parser.add_argument(
        "--database-url",
        default=None,
        help="Override POLYTEMPO_PAPER_DATABASE_URL (not required with --once)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Dry-run preview for today + 2 days ahead; no DB writes, ignores gates",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    config_path = args.config
    profiles = reload_profiles(config_path)

    if args.once:
        try:
            print(run_preview(profiles))
        except Exception:
            logger.exception("preview failed")
            return 1
        return 0

    database_url = resolve_paper_database_url(override=args.database_url)
    store = PostgresLedgerStore(database_url=database_url)
    last_mtime = _config_mtime(config_path)

    from polytempo.paper.bot import BotState

    state = BotState(profiles=profiles)

    while not _stop:
        mtime = _config_mtime(config_path)
        if mtime != last_mtime:
            logger.info("reloading profiles from %s", config_path)
            state.profiles = reload_profiles(config_path)
            last_mtime = mtime

        try:
            tick = run_tick(store, state.profiles, enforce_gate=True)
            print(tick.box)
        except Exception:
            logger.exception("tick failed")
            tick = None

        now = datetime.now(timezone.utc)
        state.next_settle_wake = now + SETTLE_SWEEP_INTERVAL
        wake_at = compute_next_wake(state, now)
        if tick is not None and tick.gate_retry_at is not None and tick.gate_retry_at > now:
            wake_at = min(wake_at, tick.gate_retry_at)
            logger.info(
                "gate fetch failed at entry window; retrying at %s",
                tick.gate_retry_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            )
        logger.info(
            "sleeping until %s (%d profiles active)",
            wake_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            len(state.profiles),
        )
        _sleep_until(wake_at)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
