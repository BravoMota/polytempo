"""Load calibration automation configuration."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from datetime import timezone as dt_timezone
from pathlib import Path

import yaml

from polytempo.collectors.config import (
    DEFAULT_CONFIG_PATH,
    StationConfig,
    load_weather_collectors_config,
)
from polytempo.weather.data_dir import REPO_ROOT, WEATHER_DATA_DIR
from polytempo.weather.calibration_stats_csv import (
    DEFAULT_UPDATED_CALIBRATION_STATS_CSV_PATH,
)

DEFAULT_CALIBRATION_CONFIG_PATH = REPO_ROOT / "config" / "calibration.yaml"


@dataclass(frozen=True)
class CalibrationModelConfig:
    name: str
    run_init_interval_hours: float
    forecast_days: int


@dataclass(frozen=True)
class WundergroundForecastConfig:
    enabled: bool
    model: str
    max_lead_hours: int
    forecast_snapshot_min_utc: datetime | None


@dataclass(frozen=True)
class CalibrationConfig:
    start_date: date
    schedule_anchor_time_utc: str
    updated_stats_csv: Path
    station_ids: list[str]
    stations: list[StationConfig]
    models: list[CalibrationModelConfig]
    collector_config_path: Path
    wunderground_forecast: WundergroundForecastConfig | None = None
    station_models: dict[str, list[CalibrationModelConfig]] = field(default_factory=dict)

    def models_for(self, station_id: str) -> list[CalibrationModelConfig]:
        """Per-station model list, falling back to the global default."""
        return self.station_models.get(station_id) or self.models

    def subset_stations(self, station_ids: Iterable[str]) -> "CalibrationConfig":
        """Return a copy restricted to ``station_ids`` (config order preserved)."""
        wanted = {str(sid).strip().upper() for sid in station_ids if str(sid).strip()}
        if not wanted:
            return self
        unknown = wanted - {sid.upper() for sid in self.station_ids}
        if unknown:
            raise ValueError(
                f"station(s) not configured for calibration: {sorted(unknown)}; "
                f"configured: {self.station_ids}"
            )
        return replace(
            self,
            station_ids=[sid for sid in self.station_ids if sid.upper() in wanted],
            stations=[st for st in self.stations if st.station_id.upper() in wanted],
        )


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


def _parse_models(raw_entries: object, *, context: str) -> list[CalibrationModelConfig]:
    if not isinstance(raw_entries, list):
        raise ValueError(f"{context} must be a list of model entries")
    models: list[CalibrationModelConfig] = []
    for entry in raw_entries:
        if not isinstance(entry, dict):
            raise ValueError(f"each model entry in {context} must be a mapping")
        name = str(entry.get("name") or "").strip()
        if not name:
            raise ValueError(f"model name is required in {context}")
        models.append(
            CalibrationModelConfig(
                name=name,
                run_init_interval_hours=float(entry["run_init_interval_hours"]),
                forecast_days=int(entry["forecast_days"]),
            )
        )
    if not models:
        raise ValueError(f"{context} must define at least one model")
    return models


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

    models = _parse_models(raw.get("models") or [], context="models")

    station_models_raw = raw.get("station_models") or {}
    if not isinstance(station_models_raw, dict):
        raise ValueError("station_models must be a mapping of station_id -> model list")
    station_models: dict[str, list[CalibrationModelConfig]] = {}
    for raw_sid, raw_entries in station_models_raw.items():
        sid = str(raw_sid)
        if sid not in by_id:
            raise ValueError(f"unknown station_id in station_models: {sid!r}")
        station_models[sid] = _parse_models(raw_entries, context=f"station_models[{sid}]")

    updated_stats = _resolve_path(
        raw.get("updated_stats_csv", DEFAULT_UPDATED_CALIBRATION_STATS_CSV_PATH),
        base=REPO_ROOT,
    )

    wu_raw = raw.get("wunderground_forecast")
    wu_config: WundergroundForecastConfig | None = None
    if isinstance(wu_raw, dict) and wu_raw.get("enabled"):
        min_utc_raw = wu_raw.get("forecast_snapshot_min_utc")
        min_utc: datetime | None = None
        if isinstance(min_utc_raw, str) and min_utc_raw.strip():
            min_utc = datetime.fromisoformat(min_utc_raw.replace("Z", "+00:00")).astimezone(
                dt_timezone.utc
            )
        wu_config = WundergroundForecastConfig(
            enabled=True,
            model=str(wu_raw.get("model") or "wunderground"),
            max_lead_hours=int(wu_raw.get("max_lead_hours", 60)),
            forecast_snapshot_min_utc=min_utc,
        )

    return CalibrationConfig(
        start_date=date.fromisoformat(str(raw.get("start_date", "2026-02-01"))),
        schedule_anchor_time_utc=str(raw.get("schedule_anchor_time_utc", "02:00")),
        updated_stats_csv=updated_stats,
        station_ids=station_ids,
        stations=stations,
        models=models,
        collector_config_path=collector_path,
        wunderground_forecast=wu_config,
        station_models=station_models,
    )
