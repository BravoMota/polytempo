"""Collector configuration loading."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from polytempo.weather.data_dir import REPO_ROOT, WEATHER_DATA_DIR

DEFAULT_CONFIG_PATH = REPO_ROOT / "config" / "weather_collectors.yaml"
DEFAULT_WEATHER_DB_PATH = WEATHER_DATA_DIR / "polytempo_weather.db"
DEFAULT_RAW_BASE_DIR = WEATHER_DATA_DIR / "raw"


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


@dataclass(frozen=True)
class CollectorConfig:
    """One collector block from YAML."""

    name: str
    enabled: bool
    source: str
    interval_seconds: int
    anchor_time_local: str | None
    stations: list[StationConfig]


@dataclass(frozen=True)
class WeatherCollectorsConfig:
    """Top-level weather collector runtime configuration."""

    weather_db_path: Path
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
    )


def _parse_collector(raw: dict[str, Any]) -> CollectorConfig:
    interval = int(raw.get("interval_seconds", 300))
    if interval <= 0:
        raise ValueError("interval_seconds must be positive")

    anchor = raw.get("anchor_time_local")
    anchor_s = str(anchor).strip() if anchor not in (None, "") else None

    stations_raw = raw.get("stations") or []
    if not isinstance(stations_raw, list):
        raise ValueError("collectors[].stations must be a list")

    return CollectorConfig(
        name=str(raw["name"]).strip(),
        enabled=bool(raw.get("enabled", True)),
        source=str(raw.get("source", raw["name"])).strip(),
        interval_seconds=interval,
        anchor_time_local=anchor_s,
        stations=[_parse_station(s) for s in stations_raw],
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

    db_path = _resolve_path(
        payload.get("weather_db_path", DEFAULT_WEATHER_DB_PATH),
    )
    raw_base = _resolve_path(
        payload.get("raw_base_dir", DEFAULT_RAW_BASE_DIR),
    )

    collectors_raw = payload.get("collectors") or []
    if not isinstance(collectors_raw, list):
        raise ValueError("collectors must be a list")

    return WeatherCollectorsConfig(
        weather_db_path=db_path,
        raw_base_dir=raw_base,
        collectors=[_parse_collector(c) for c in collectors_raw],
    )


def sync_stations_from_config(
    conn: Any,
    config: WeatherCollectorsConfig,
) -> None:
    """Upsert all stations referenced by enabled collectors."""
    from polytempo.storage.sqlite import insert_station

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
