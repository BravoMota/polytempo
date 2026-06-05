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
    assert wu.interval_seconds == 300


def test_load_config_resolves_relative_paths(tmp_path: Path) -> None:
    cfg = tmp_path / "weather_collectors.yaml"
    cfg.write_text(
        """
raw_base_dir: data/weather/raw
collectors:
  - name: wunderground
    enabled: true
    source: wunderground
    interval_seconds: 120
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
    assert config.enabled_collectors[0].interval_seconds == 120


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


def test_reload_picks_up_changed_interval(tmp_path: Path) -> None:
    cfg = tmp_path / "weather_collectors.yaml"
    cfg.write_text(
        """
raw_base_dir: raw
collectors:
  - name: wunderground
    enabled: true
    source: wunderground
    interval_seconds: 100
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
    assert first.collectors[0].interval_seconds == 100

    cfg.write_text(
        cfg.read_text(encoding="utf-8").replace("interval_seconds: 100", "interval_seconds: 200"),
        encoding="utf-8",
    )
    second = load_weather_collectors_config(cfg)
    assert second.collectors[0].interval_seconds == 200
