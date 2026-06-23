"""Tests for weather collector YAML configuration."""

from __future__ import annotations

from pathlib import Path

import pytest

from polytempo.collectors.config import load_weather_collectors_config


def test_load_default_config_shape() -> None:
    from polytempo.collectors.config import DEFAULT_CONFIG_PATH

    if not DEFAULT_CONFIG_PATH.is_file():
        pytest.skip("default config not present")

    config = load_weather_collectors_config(DEFAULT_CONFIG_PATH)
    assert config.raw_base_dir.name == "raw"
    assert len(config.collectors) >= 1
    wu = config.collectors[0]
    assert wu.name == "wunderground"
    assert wu.observations_interval_seconds == 300
    assert wu.forecast_interval_seconds == 600
    om = next(c for c in config.collectors if c.name == "open_meteo")
    assert om.models
    assert "ukmo_uk_deterministic_2km" in om.models
    assert om.target_horizon_days == 4
    assert wu.observations_anchor_time_utc == "00:00"
    assert wu.forecast_anchor_time_utc == "00:00"


def test_load_config_resolves_relative_paths(tmp_path: Path) -> None:
    cfg = tmp_path / "weather_collectors.yaml"
    cfg.write_text(
        """
raw_base_dir: data/weather/raw
collectors:
  - name: wunderground
    enabled: true
    source: wunderground
    observations_interval_seconds: 120
    forecast_interval_seconds: 900
    stations:
      - station_id: EGLC
        station_type: icao
        name: London City Airport
        timezone: Europe/London
        country: gb
        city_slug: london
""",
        encoding="utf-8",
    )

    config = load_weather_collectors_config(cfg)
    assert config.raw_base_dir.is_absolute()
    assert config.raw_base_dir.name == "raw"
    collector = config.enabled_collectors[0]
    assert collector.observations_interval_seconds == 120
    assert collector.forecast_interval_seconds == 900


def test_load_config_legacy_interval_fallback(tmp_path: Path) -> None:
    cfg = tmp_path / "legacy.yaml"
    cfg.write_text(
        """
raw_base_dir: raw
collectors:
  - name: wunderground
    enabled: true
    source: wunderground
    interval_seconds: 240
    anchor_time_local: "06:30"
    stations:
      - station_id: EGLC
        station_type: icao
        name: EGLC
        timezone: Europe/London
        country: gb
        city_slug: london
""",
        encoding="utf-8",
    )

    collector = load_weather_collectors_config(cfg).collectors[0]
    assert collector.observations_interval_seconds == 240
    assert collector.forecast_interval_seconds == 240
    assert collector.observations_anchor_time_utc == "06:30"
    assert collector.forecast_anchor_time_utc == "06:30"


def test_load_config_rejects_invalid_station_type(tmp_path: Path) -> None:
    cfg = tmp_path / "bad.yaml"
    cfg.write_text(
        """
raw_base_dir: raw
collectors:
  - name: wunderground
    enabled: true
    source: wunderground
    stations:
      - station_id: X
        station_type: unknown
        name: X
        timezone: UTC
        country: gb
        city_slug: london
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="icao or pws"):
        load_weather_collectors_config(cfg)


def test_load_config_rejects_invalid_anchor(tmp_path: Path) -> None:
    cfg = tmp_path / "bad_anchor.yaml"
    cfg.write_text(
        """
raw_base_dir: raw
collectors:
  - name: wunderground
    enabled: true
    source: wunderground
    observations_anchor_time_utc: "25:99"
    stations:
      - station_id: EGLC
        station_type: icao
        name: EGLC
        timezone: Europe/London
        country: gb
        city_slug: london
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="anchor time"):
        load_weather_collectors_config(cfg)


def test_load_config_open_meteo_requires_models(tmp_path: Path) -> None:
    cfg = tmp_path / "open_meteo.yaml"
    cfg.write_text(
        """
raw_base_dir: raw
collectors:
  - name: open_meteo
    enabled: true
    source: open_meteo
    stations:
      - station_id: EGLC
        station_type: icao
        name: EGLC
        timezone: Europe/London
        lat: 51.5
        lon: 0.05
        country: gb
        city_slug: london
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="models"):
        load_weather_collectors_config(cfg)


def test_reload_picks_up_changed_interval(tmp_path: Path) -> None:
    cfg = tmp_path / "weather_collectors.yaml"
    cfg.write_text(
        """
raw_base_dir: raw
collectors:
  - name: wunderground
    enabled: true
    source: wunderground
    observations_interval_seconds: 100
    forecast_interval_seconds: 500
    stations:
      - station_id: EGLC
        station_type: icao
        name: EGLC
        timezone: Europe/London
        country: gb
        city_slug: london
""",
        encoding="utf-8",
    )

    first = load_weather_collectors_config(cfg)
    assert first.collectors[0].observations_interval_seconds == 100
    assert first.collectors[0].forecast_interval_seconds == 500

    cfg.write_text(
        cfg.read_text(encoding="utf-8").replace(
            "observations_interval_seconds: 100",
            "observations_interval_seconds: 200",
        ),
        encoding="utf-8",
    )
    second = load_weather_collectors_config(cfg)
    assert second.collectors[0].observations_interval_seconds == 200
