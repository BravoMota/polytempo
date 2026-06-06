#!/usr/bin/env python3
"""Run continuous weather data collectors with optional config hot-reload."""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from polytempo.collectors import COLLECTORS  # noqa: E402
from polytempo.collectors.config import (  # noqa: E402
    DEFAULT_CONFIG_PATH,
    WeatherCollectorsConfig,
    load_weather_collectors_config,
    sync_stations_from_config,
)
from polytempo.collectors.schedule import (  # noqa: E402
    is_slot_due,
    next_scheduled_instant_utc,
)
from polytempo.storage.postgres import get_connection, resolve_database_url  # noqa: E402

logger = logging.getLogger(__name__)

_stop = False


@dataclass
class CollectorScheduleState:
    last_obs_slot: datetime | None = None
    last_fc_slot: datetime | None = None


@dataclass
class RunState:
    schedule_by_collector: dict[str, CollectorScheduleState] = field(default_factory=dict)


def _handle_signal(signum: int, _frame: object) -> None:
    global _stop
    logger.info("received signal %s, stopping...", signum)
    _stop = True


def _config_mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def _reload_config_if_changed(
    config_path: Path,
    last_mtime: float | None,
    database_url: str,
) -> tuple[WeatherCollectorsConfig, float | None]:
    mtime = _config_mtime(config_path)
    if last_mtime is not None and mtime == last_mtime:
        return load_weather_collectors_config(config_path), last_mtime

    logger.info("loading config from %s", config_path)
    config = load_weather_collectors_config(config_path)
    with get_connection(database_url) as conn:
        sync_stations_from_config(conn, config)
        conn.commit()
    return config, mtime


def _schedule_state(run_state: RunState, collector_name: str) -> CollectorScheduleState:
    if collector_name not in run_state.schedule_by_collector:
        run_state.schedule_by_collector[collector_name] = CollectorScheduleState()
    return run_state.schedule_by_collector[collector_name]


def _next_wake_utc(config: WeatherCollectorsConfig, now_utc: datetime) -> datetime | None:
    wake_times: list[datetime] = []
    for collector in config.enabled_collectors:
        wake_times.append(
            next_scheduled_instant_utc(
                now_utc,
                collector.observations_interval_seconds,
                collector.observations_anchor_time_utc,
            )
        )
        wake_times.append(
            next_scheduled_instant_utc(
                now_utc,
                collector.forecast_interval_seconds,
                collector.forecast_anchor_time_utc,
            )
        )
    return min(wake_times) if wake_times else None


def _run_due_tasks(
    config: WeatherCollectorsConfig,
    database_url: str,
    run_state: RunState,
    *,
    force_all: bool,
) -> None:
    now_utc = datetime.now(timezone.utc)
    for collector in config.enabled_collectors:
        runner = COLLECTORS.get(collector.name)
        if runner is None:
            logger.error(
                "unknown collector %r (known: %s)",
                collector.name,
                list(COLLECTORS),
            )
            continue

        schedule = _schedule_state(run_state, collector.name)
        obs_due, obs_slot = is_slot_due(
            now_utc,
            collector.observations_interval_seconds,
            collector.observations_anchor_time_utc,
            schedule.last_obs_slot,
        )
        fc_due, fc_slot = is_slot_due(
            now_utc,
            collector.forecast_interval_seconds,
            collector.forecast_anchor_time_utc,
            schedule.last_fc_slot,
        )

        if force_all:
            obs_due = True
            fc_due = True
        if not obs_due and not fc_due:
            continue

        logger.info(
            "running collector=%s stations=%d obs=%s forecast=%s",
            collector.name,
            len(collector.stations),
            obs_due,
            fc_due,
        )
        with get_connection(database_url) as conn:
            if obs_due:
                runner(
                    conn,
                    config,
                    collector,
                    fetch_observations=True,
                    fetch_forecasts=False,
                )
                schedule.last_obs_slot = obs_slot
            if fc_due:
                runner(
                    conn,
                    config,
                    collector,
                    fetch_observations=False,
                    fetch_forecasts=True,
                )
                schedule.last_fc_slot = fc_slot

        if obs_due:
            next_obs = next_scheduled_instant_utc(
                datetime.now(timezone.utc),
                collector.observations_interval_seconds,
                collector.observations_anchor_time_utc,
            )
            logger.info(
                "next observations slot collector=%s at %s",
                collector.name,
                next_obs.strftime("%Y-%m-%dT%H:%M:%SZ"),
            )
        if fc_due:
            next_fc = next_scheduled_instant_utc(
                datetime.now(timezone.utc),
                collector.forecast_interval_seconds,
                collector.forecast_anchor_time_utc,
            )
            logger.info(
                "next forecast slot collector=%s at %s",
                collector.name,
                next_fc.strftime("%Y-%m-%dT%H:%M:%SZ"),
            )


def _sleep_until(
    deadline_utc: datetime | None,
    config_path: Path,
    last_mtime: float | None,
) -> bool:
    """Sleep until deadline or config change. Returns True if config changed."""
    while not _stop:
        if deadline_utc is None:
            time.sleep(0.5)
            mtime = _config_mtime(config_path)
            return mtime != last_mtime

        remaining = (deadline_utc - datetime.now(timezone.utc)).total_seconds()
        if remaining <= 0:
            return False
        time.sleep(min(remaining, 0.5))
        mtime = _config_mtime(config_path)
        if mtime != last_mtime:
            logger.info("config changed, reloading before next sleep completes")
            return True
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description="Run weather data collectors")
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help="Path to weather_collectors.yaml",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run one cycle per enabled collector then exit",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    database_url = resolve_database_url()
    config_path = args.config.resolve()
    config = load_weather_collectors_config(config_path)
    last_mtime = _config_mtime(config_path)
    run_state = RunState()

    with get_connection(database_url) as conn:
        sync_stations_from_config(conn, config)
        conn.commit()

    while not _stop:
        config, last_mtime = _reload_config_if_changed(
            config_path,
            last_mtime,
            database_url,
        )

        if not config.enabled_collectors:
            logger.warning("no enabled collectors in config")

        _run_due_tasks(config, database_url, run_state, force_all=args.once)

        if args.once:
            break

        now_utc = datetime.now(timezone.utc)
        wake_at = _next_wake_utc(config, now_utc)
        if wake_at:
            logger.info("sleeping until %s", wake_at.strftime("%Y-%m-%dT%H:%M:%SZ"))
        if _sleep_until(wake_at, config_path, last_mtime):
            continue

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
