"""Collector configuration loading."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from polytempo.collectors.schedule import DEFAULT_ANCHOR_TIME_UTC, parse_anchor_time_utc
from polytempo.weather.data_dir import REPO_ROOT, WEATHER_DATA_DIR

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = REPO_ROOT / "config" / "weather_collectors.yaml"
DEFAULT_RAW_BASE_DIR = WEATHER_DATA_DIR / "raw"
OPEN_METEO_COLLECTOR_NAME = "open_meteo"


@dataclass(frozen=True)
class StationConfig:
    """One station entry from collector YAML."""

    station_id: str
    station_type: str
    name: str
    timezone: str
    lat: float | None
    lon: float | None
    country: str
    city_slug: str
    pws_id: str | None = None
    models: tuple[str, ...] = ()


@dataclass(frozen=True)
class CollectorConfig:
    """One collector block from YAML."""

    name: str
    enabled: bool
    source: str
    observations_interval_seconds: int
    observations_anchor_time_utc: str
    forecast_interval_seconds: int
    forecast_anchor_time_utc: str
    stations: list[StationConfig]
    models: tuple[str, ...] = ()
    target_horizon_days: int = 2


@dataclass(frozen=True)
class WeatherCollectorsConfig:
    """Top-level weather collector runtime configuration."""

    raw_base_dir: Path
    collectors: list[CollectorConfig]

    @property
    def enabled_collectors(self) -> list[CollectorConfig]:
        return [c for c in self.collectors if c.enabled]


def _resolve_path(value: str | Path, *, base: Path = REPO_ROOT) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return (base / path).resolve()


def _normalize_anchor(value: str | None) -> str:
    if value in (None, ""):
        return DEFAULT_ANCHOR_TIME_UTC
    parse_anchor_time_utc(str(value))
    return str(value).strip()


def _parse_interval(raw: dict[str, Any], key: str, *, legacy: int | None) -> int:
    if key in raw:
        interval = int(raw[key])
    elif legacy is not None:
        interval = legacy
    else:
        interval = 300
    if interval <= 0:
        raise ValueError(f"{key} must be positive")
    return interval


def _parse_station(raw: dict[str, Any]) -> StationConfig:
    station_id = str(raw["station_id"]).strip()
    station_type = str(raw["station_type"]).strip().lower()
    if station_type not in {"icao", "pws"}:
        raise ValueError(f"station_type must be icao or pws, got {station_type!r}")

    pws_raw = raw.get("pws_id")
    pws_id = str(pws_raw).strip() if pws_raw not in (None, "") else None
    if station_type == "pws" and not pws_id:
        raise ValueError(f"station {station_id!r} with type pws requires pws_id")

    lat = raw.get("lat")
    lon = raw.get("lon")

    models_raw = raw.get("models") or []
    if not isinstance(models_raw, list):
        raise ValueError(f"station {station_id!r} models must be a list")
    models = tuple(str(m).strip() for m in models_raw if str(m).strip())

    return StationConfig(
        station_id=station_id,
        station_type=station_type,
        name=str(raw["name"]).strip(),
        timezone=str(raw["timezone"]).strip(),
        lat=float(lat) if lat is not None else None,
        lon=float(lon) if lon is not None else None,
        country=str(raw["country"]).strip().lower(),
        city_slug=str(raw["city_slug"]).strip().lower(),
        pws_id=pws_id,
        models=models,
    )


def _parse_collector(raw: dict[str, Any]) -> CollectorConfig:
    legacy_interval: int | None = None
    if "interval_seconds" in raw:
        legacy_interval = int(raw["interval_seconds"])
        if legacy_interval <= 0:
            raise ValueError("interval_seconds must be positive")
        logger.warning(
            "interval_seconds is deprecated; use observations_interval_seconds "
            "and forecast_interval_seconds"
        )

    legacy_anchor = raw.get("anchor_time_local")
    if legacy_anchor not in (None, ""):
        logger.warning(
            "anchor_time_local is deprecated; use observations_anchor_time_utc "
            "and forecast_anchor_time_utc"
        )

    obs_interval = _parse_interval(
        raw,
        "observations_interval_seconds",
        legacy=legacy_interval,
    )
    fc_interval = _parse_interval(
        raw,
        "forecast_interval_seconds",
        legacy=legacy_interval,
    )

    obs_anchor_raw = raw.get("observations_anchor_time_utc", legacy_anchor)
    fc_anchor_raw = raw.get("forecast_anchor_time_utc", legacy_anchor)

    stations_raw = raw.get("stations") or []
    if not isinstance(stations_raw, list):
        raise ValueError("collectors[].stations must be a list")

    name = str(raw["name"]).strip()
    models_raw = raw.get("models") or []
    if not isinstance(models_raw, list):
        raise ValueError("collectors[].models must be a list")
    models = tuple(str(m).strip() for m in models_raw if str(m).strip())

    horizon_raw = raw.get("target_horizon_days", 2)
    target_horizon_days = int(horizon_raw)
    if target_horizon_days < 1:
        raise ValueError("target_horizon_days must be >= 1")

    if name == "open_meteo" and not models:
        raise ValueError("open_meteo collector requires a non-empty models list")

    return CollectorConfig(
        name=name,
        enabled=bool(raw.get("enabled", True)),
        source=str(raw.get("source", raw["name"])).strip(),
        observations_interval_seconds=obs_interval,
        observations_anchor_time_utc=_normalize_anchor(
            str(obs_anchor_raw) if obs_anchor_raw not in (None, "") else None
        ),
        forecast_interval_seconds=fc_interval,
        forecast_anchor_time_utc=_normalize_anchor(
            str(fc_anchor_raw) if fc_anchor_raw not in (None, "") else None
        ),
        stations=[_parse_station(s) for s in stations_raw],
        models=models,
        target_horizon_days=target_horizon_days,
    )


def load_weather_collectors_config(
    path: Path = DEFAULT_CONFIG_PATH,
) -> WeatherCollectorsConfig:
    """Load and validate weather collector YAML."""
    if not path.is_file():
        raise FileNotFoundError(f"config not found: {path}")

    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("config root must be a mapping")

    raw_base = _resolve_path(
        payload.get("raw_base_dir", DEFAULT_RAW_BASE_DIR),
    )

    collectors_raw = payload.get("collectors") or []
    if not isinstance(collectors_raw, list):
        raise ValueError("collectors must be a list")

    collectors = [_parse_collector(c) for c in collectors_raw]

    return WeatherCollectorsConfig(
        raw_base_dir=raw_base,
        collectors=collectors,
    )


@lru_cache(maxsize=1)
def _open_meteo_station_model_overrides() -> dict[str, tuple[str, ...]]:
    """Per-station ``models:`` overrides in the open_meteo collector, cached."""
    config = load_weather_collectors_config()
    overrides: dict[str, tuple[str, ...]] = {}
    for collector in config.collectors:
        if collector.name != OPEN_METEO_COLLECTOR_NAME:
            continue
        for station in collector.stations:
            if station.models:
                overrides[station.station_id] = station.models
    return overrides


def models_for_station(station_id: str) -> tuple[str, ...] | None:
    """Open-Meteo model override for ``station_id``, or None when it has none.

    Sits on the per-tick trading path, so the parsed YAML is cached. Callers
    fall back to their own default when this returns None; a config that cannot
    be read is treated as "no override" so the fallback still applies.
    """
    try:
        overrides = _open_meteo_station_model_overrides()
    except Exception:
        logger.warning(
            "could not read per-station Open-Meteo models from %s; using defaults",
            DEFAULT_CONFIG_PATH,
            exc_info=True,
        )
        return None
    return overrides.get(station_id.strip())


def sync_stations_from_config(
    conn: Any,
    config: WeatherCollectorsConfig,
) -> None:
    """Upsert all stations referenced by enabled collectors."""
    from polytempo.storage.postgres import insert_station

    seen: set[str] = set()
    for collector in config.enabled_collectors:
        for station in collector.stations:
            if station.station_id in seen:
                continue
            seen.add(station.station_id)
            insert_station(
                conn,
                station_id=station.station_id,
                name=station.name,
                timezone=station.timezone,
                lat=station.lat,
                lon=station.lon,
                country=station.country,
                active=1,
            )
