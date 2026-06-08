#!/usr/bin/env python3
"""Probe Open-Meteo forecast API at UTC :00, :05, :10 each hour (demand-spike study)."""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from polytempo.weather.open_meteo_probe import (  # noqa: E402
    append_jsonl,
    next_probe_instant,
    probe_slot_key,
    run_probe,
    slot_for_instant,
)

logger = logging.getLogger(__name__)

DEFAULT_OUTPUT = REPO_ROOT / "data" / "open_meteo_probe.jsonl"

_stop = False


def _handle_signal(signum: int, _frame: object) -> None:
    global _stop
    logger.info("received signal %s, stopping...", signum)
    _stop = True


def _sleep_until(deadline: datetime) -> None:
    while not _stop:
        now = datetime.now(timezone.utc)
        if now >= deadline:
            return
        remaining = (deadline - now).total_seconds()
        time.sleep(min(remaining, 1.0))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Probe Open-Meteo at UTC :00/:05/:10 each hour"
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="JSONL output path",
    )
    parser.add_argument(
        "--city",
        default="london",
        help="Contract station city slug (default: london)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    last_slot_key: str | None = None
    output_path = args.output

    logger.info("probing Open-Meteo; output=%s city=%s", output_path, args.city)

    while not _stop:
        now = datetime.now(timezone.utc)
        wake_at = next_probe_instant(now)
        logger.info("sleeping until %s", wake_at.strftime("%Y-%m-%dT%H:%M:%SZ"))
        _sleep_until(wake_at)
        if _stop:
            break

        now = datetime.now(timezone.utc)
        slot_meta = slot_for_instant(now)
        if slot_meta is None:
            aligned = now.replace(second=0, microsecond=0)
            slot_meta = slot_for_instant(aligned)
        if slot_meta is None:
            logger.warning("woke at non-probe instant %s; skipping", now.isoformat())
            continue

        key = probe_slot_key(slot_meta)
        if key == last_slot_key:
            continue
        last_slot_key = key

        target_date = (now + timedelta(days=1)).date()
        record = run_probe(city=args.city, target_date=target_date, slot=slot_meta)
        append_jsonl(output_path, record)
        logger.info(
            "probe %s hour=%d success=%s latency_ms=%s",
            slot_meta.slot,
            slot_meta.hour_utc,
            record["success"],
            record["latency_ms"],
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
