"""Calibration CSV readiness checks for paper-trading profiles."""

from __future__ import annotations

import time
from pathlib import Path

from polytempo.analysis import (
    MODEL_STRATEGY_BEST_HISTORICAL,
    MODEL_STRATEGY_BEST_HISTORICAL_UPDATED,
    MODEL_STRATEGY_WEIGHTED_HISTORICAL_MARKET_SIGMA,
    MODEL_STRATEGY_WEIGHTED_HISTORICAL_UPDATED,
    MODEL_STRATEGY_WEIGHTED_HISTORICAL_UPDATED_SHARP,
)
from polytempo.profiles.models import TradingProfile
from polytempo.weather.calibration_stats_csv import read_calibration_stats_csv
from polytempo.weather.stations import get_station

DEFAULT_BHU_MAX_AGE_HOURS = 48.0


def calibration_csv_ready(
    path: Path,
    *,
    max_age_hours: float | None = None,
    station_id: str | None = None,
) -> tuple[bool, str]:
    """Return ``(ready, reason)`` for a calibration stats CSV path.

    ``station_id`` (the profile's contract station) additionally requires the CSV
    to hold rows for that station. Without it a Madrid profile reads ready on a
    London-only CSV and then fails silently downstream: calibrated strategies
    skip on ``no_ceiling_row_for_any_live_model`` while ``ensemble_spread``
    trades uncalibrated.
    """
    if not path.is_file():
        return False, "missing_calibration_csv"
    if max_age_hours is not None:
        age_hours = (time.time() - path.stat().st_mtime) / 3600.0
        if age_hours > max_age_hours:
            return False, "stale_calibration_csv"
    rows = read_calibration_stats_csv(path)
    if not rows:
        return False, "empty_calibration_csv"
    if station_id is not None:
        wanted = station_id.strip().upper()
        if not any(row.station_id.strip().upper() == wanted for row in rows):
            return False, "no_calibration_rows_for_station"
    return True, ""


def filter_profiles_by_calibration(
    profiles: list[TradingProfile],
    *,
    bhu_max_age_hours: float = DEFAULT_BHU_MAX_AGE_HOURS,
) -> tuple[list[TradingProfile], list[str]]:
    """Drop profiles whose calibration CSV is not ready; return warnings."""
    enabled: list[TradingProfile] = []
    warnings: list[str] = []
    seen_warning_keys: set[str] = set()

    for profile in profiles:
        station_id = get_station(profile.city).icao
        if profile.model_strategy in (
            MODEL_STRATEGY_BEST_HISTORICAL_UPDATED,
            MODEL_STRATEGY_WEIGHTED_HISTORICAL_UPDATED,
            MODEL_STRATEGY_WEIGHTED_HISTORICAL_MARKET_SIGMA,
            MODEL_STRATEGY_WEIGHTED_HISTORICAL_UPDATED_SHARP,
        ):
            ready, reason = calibration_csv_ready(
                profile.calibration_stats_path,
                max_age_hours=bhu_max_age_hours,
                station_id=station_id,
            )
            if not ready:
                key = f"{profile.model_strategy}:{profile.calibration_stats_path}:{reason}"
                if key not in seen_warning_keys:
                    seen_warning_keys.add(key)
                    warnings.append(
                        f"disabled {profile.model_strategy} profiles: "
                        f"{profile.calibration_stats_path} ({reason})"
                    )
                continue
        elif profile.model_strategy == MODEL_STRATEGY_BEST_HISTORICAL:
            ready, reason = calibration_csv_ready(
                profile.calibration_stats_path,
                station_id=station_id,
            )
            if not ready:
                key = f"{profile.model_strategy}:{profile.calibration_stats_path}:{reason}"
                if key not in seen_warning_keys:
                    seen_warning_keys.add(key)
                    warnings.append(
                        f"disabled {profile.model_strategy} profiles: "
                        f"{profile.calibration_stats_path} ({reason})"
                    )
                continue
        enabled.append(profile)

    return enabled, warnings
