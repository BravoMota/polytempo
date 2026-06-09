"""Load calibration automation configuration."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

import yaml

from polytempo.collectors.config import (
    DEFAULT_CONFIG_PATH,
    StationConfig,
    load_weather_collectors_config,
)
from polytempo.weather.data_dir import REPO_ROOT, WEATHER_DATA_DIR
from polytempo.weather.calibration_stats_csv import DEFAULT_UPDATED_CALIBRATION_STATS_CSV_PATH

DEFAULT_CALIBRATION_CONFIG_PATH = REPO_ROOT / "config" / "calibration.yaml"


@dataclass(frozen=True)
class CalibrationModelConfig:
    name: str
    run_init_interval_hours: float
    forecast_days: int


@dataclass(frozen=True)
class CalibrationConfig:
    start_date: date
    schedule_anchor_time_utc: str
    updated_stats_csv: Path
    station_ids: list[str]
    stations: list[StationConfig]
    models: list[CalibrationModelConfig]
    collector_config_path: Path


def _resolve_path(value: str | Path, *, base: Path = REPO_ROOT) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return (base / path).resolve()


def _stations_by_id(collector_config_path: Path) -> dict[str, StationConfig]:
    config = load_weather_collectors_config(collector_config_path)
    out: dict[str, StationConfig] = {}
    for collector in config.collectors:
        for station in collector.stations:
            out[station.station_id] = station
    return out


def load_calibration_config(
    path: Path = DEFAULT_CALIBRATION_CONFIG_PATH,
) -> CalibrationConfig:
    if not path.is_file():
        raise FileNotFoundError(f"calibration config not found: {path}")

    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"expected mapping in {path}")

    collector_path = _resolve_path(raw.get("collector_config", DEFAULT_CONFIG_PATH))
    station_ids = [str(sid) for sid in (raw.get("station_ids") or [])]
    if not station_ids:
        raise ValueError("station_ids must be defined in calibration.yaml")

    by_id = _stations_by_id(collector_path)
    stations: list[StationConfig] = []
    for station_id in station_ids:
        if station_id not in by_id:
            raise ValueError(f"unknown station_id in calibration.yaml: {station_id!r}")
        station = by_id[station_id]
        if station.station_type != "icao":
            raise ValueError(
                f"calibration requires ICAO stations for WU history scrape; got {station_id!r}"
            )
        if station.lat is None or station.lon is None:
            raise ValueError(f"station {station_id!r} missing lat/lon in collector config")
        stations.append(station)

    models_raw = raw.get("models") or []
    models: list[CalibrationModelConfig] = []
    for entry in models_raw:
        if not isinstance(entry, dict):
            raise ValueError("each model entry must be a mapping")
        name = str(entry.get("name") or "").strip()
        interval = float(entry["run_init_interval_hours"])
        forecast_days = int(entry["forecast_days"])
        if not name:
            raise ValueError("model name is required")
        models.append(
            CalibrationModelConfig(
                name=name,
                run_init_interval_hours=interval,
                forecast_days=forecast_days,
            )
        )
    if not models:
        raise ValueError("models must be defined in calibration.yaml")

    updated_stats = _resolve_path(
        raw.get("updated_stats_csv", DEFAULT_UPDATED_CALIBRATION_STATS_CSV_PATH),
        base=REPO_ROOT,
    )

    return CalibrationConfig(
        start_date=date.fromisoformat(str(raw.get("start_date", "2026-02-01"))),
        schedule_anchor_time_utc=str(raw.get("schedule_anchor_time_utc", "02:00")),
        updated_stats_csv=updated_stats,
        station_ids=station_ids,
        stations=stations,
        models=models,
        collector_config_path=collector_path,
    )
