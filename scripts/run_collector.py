#!/usr/bin/env python3
"""Run continuous weather data collectors with optional config hot-reload."""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import time
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
from polytempo.storage.sqlite import get_connection  # noqa: E402

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


def _reload_config_if_changed(
    config_path: Path,
    last_mtime: float | None,
    db_path: Path,
) -> tuple[WeatherCollectorsConfig, float | None]:
    mtime = _config_mtime(config_path)
    if last_mtime is not None and mtime == last_mtime:
        return load_weather_collectors_config(config_path), last_mtime

    logger.info("loading config from %s", config_path)
    config = load_weather_collectors_config(config_path)
    with get_connection(db_path) as conn:
        sync_stations_from_config(conn, config)
        conn.commit()
    return config, mtime


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

    config_path = args.config.resolve()
    config = load_weather_collectors_config(config_path)
    last_mtime = _config_mtime(config_path)

    with get_connection(config.weather_db_path) as conn:
        sync_stations_from_config(conn, config)
        conn.commit()

    while not _stop:
        config, last_mtime = _reload_config_if_changed(
            config_path,
            last_mtime,
            config.weather_db_path,
        )

        enabled = config.enabled_collectors
        if not enabled:
            logger.warning("no enabled collectors in config")

        for collector in enabled:
            if _stop:
                break
            runner = COLLECTORS.get(collector.name)
            if runner is None:
                logger.error("unknown collector %r (known: %s)", collector.name, list(COLLECTORS))
                continue

            logger.info(
                "running collector=%s stations=%d interval=%ds",
                collector.name,
                len(collector.stations),
                collector.interval_seconds,
            )
            with get_connection(config.weather_db_path) as conn:
                runner(conn, config, collector)

        if args.once:
            break

        sleep_s = min(c.interval_seconds for c in enabled) if enabled else 60
        logger.info("sleeping %ds until next cycle", sleep_s)
        deadline = time.monotonic() + sleep_s
        while time.monotonic() < deadline and not _stop:
            mtime = _config_mtime(config_path)
            if mtime != last_mtime:
                logger.info("config changed, reloading before next sleep completes")
                break
            time.sleep(min(1.0, deadline - time.monotonic()))

    logger.info("collector stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
