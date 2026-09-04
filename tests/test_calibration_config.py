"""Tests for per-station calibration model sets in config/calibration.yaml."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
import yaml

from polytempo.collectors.config import load_weather_collectors_config
from polytempo.weather.calibration_config import (
    DEFAULT_CALIBRATION_CONFIG_PATH,
    load_calibration_config,
)
from polytempo.weather.wunderground import DEFAULT_COUNTRY_CODE

# Frozen snapshot of the London (EGLC) model set as it stood before per-station
# model sets were introduced. EGLC must keep resolving to exactly this, in this
# order, with these run intervals and horizons.
EGLC_FROZEN_MODELS = (
    ("ukmo_uk_deterministic_2km", 3.0, 3),
    ("ukmo_global_deterministic_10km", 6.0, 7),
    ("ecmwf_ifs025", 6.0, 15),
    ("icon_eu", 3.0, 5),
    ("gfs_seamless", 6.0, 16),
)

LEMD_EXPECTED_MODELS = (
    ("meteofrance_arpege_europe", 6.0, 4),
    ("icon_eu", 3.0, 5),
    ("ecmwf_ifs025", 6.0, 15),
    ("gfs_seamless", 6.0, 16),
    ("ukmo_global_deterministic_10km", 6.0, 7),
)


def _as_tuples(models) -> tuple[tuple[str, float, int], ...]:
    return tuple(
        (m.name, float(m.run_init_interval_hours), int(m.forecast_days)) for m in models
    )


@pytest.fixture(scope="module")
def config():
    return load_calibration_config(DEFAULT_CALIBRATION_CONFIG_PATH)


# --- London must not change ---------------------------------------------------


def test_eglc_resolves_to_the_frozen_default_model_set(config) -> None:
    """EGLC's effective list is byte-identical to the pre-change global list."""
    assert _as_tuples(config.models_for("EGLC")) == EGLC_FROZEN_MODELS


def test_top_level_default_models_are_unchanged(config) -> None:
    """The top-level `models:` block itself is untouched."""
    assert _as_tuples(config.models) == EGLC_FROZEN_MODELS


def test_eglc_has_no_station_override(config) -> None:
    assert "EGLC" not in config.station_models
    assert config.models_for("EGLC") is config.models


def test_eglc_never_sees_a_madrid_only_model(config) -> None:
    names = {m.name for m in config.models_for("EGLC")}
    assert "meteofrance_arpege_europe" not in names


def test_unknown_station_falls_back_to_the_default_models(config) -> None:
    assert _as_tuples(config.models_for("LIMC")) == EGLC_FROZEN_MODELS


# --- Madrid -------------------------------------------------------------------


def test_lemd_is_enabled(config) -> None:
    assert config.station_ids == ["EGLC", "LEMD"]
    assert [s.station_id for s in config.stations] == ["EGLC", "LEMD"]


def test_lemd_resolves_to_its_override(config) -> None:
    assert _as_tuples(config.models_for("LEMD")) == LEMD_EXPECTED_MODELS


def test_lemd_never_sees_the_uk_domain_2km_model(config) -> None:
    names = {m.name for m in config.models_for("LEMD")}
    assert "ukmo_uk_deterministic_2km" not in names


def test_lemd_calibration_models_match_the_collector_models(config) -> None:
    """Calibration must be fitted over the same model set the trading path requests."""
    collectors = load_weather_collectors_config(config.collector_config_path)
    open_meteo = next(c for c in collectors.collectors if c.name == "open_meteo")
    lemd = next(s for s in open_meteo.stations if s.station_id == "LEMD")
    assert tuple(m.name for m in config.models_for("LEMD")) == tuple(lemd.models)


# --- WU history country code (Spain, not the GB fallback) ---------------------


def test_lemd_country_resolves_to_es_not_the_gb_fallback(config) -> None:
    lemd = next(s for s in config.stations if s.station_id == "LEMD")
    # Mirrors calibration_runner.ingest_wu_history_observations.
    country_code = (lemd.country or DEFAULT_COUNTRY_CODE).upper()
    assert country_code == "ES"
    assert country_code != DEFAULT_COUNTRY_CODE


def test_every_calibration_station_has_an_explicit_country(config) -> None:
    for station in config.stations:
        assert station.country, f"{station.station_id} has no country in collector config"


# --- subset_stations (the --station flag) ------------------------------------


def test_subset_stations_keeps_only_the_requested_station(config) -> None:
    only_lemd = config.subset_stations(["LEMD"])
    assert only_lemd.station_ids == ["LEMD"]
    assert [s.station_id for s in only_lemd.stations] == ["LEMD"]


def test_subset_stations_is_case_insensitive_and_keeps_config_order(config) -> None:
    both = config.subset_stations(["lemd", "eglc"])
    assert both.station_ids == ["EGLC", "LEMD"]


def test_subset_stations_preserves_model_resolution(config) -> None:
    only_lemd = config.subset_stations(["LEMD"])
    assert _as_tuples(only_lemd.models_for("LEMD")) == LEMD_EXPECTED_MODELS
    assert _as_tuples(only_lemd.models) == EGLC_FROZEN_MODELS


def test_subset_stations_does_not_change_the_stats_csv_target(config) -> None:
    """The recompute/CSV-write step is not station scoped."""
    only_lemd = config.subset_stations(["LEMD"])
    assert only_lemd.updated_stats_csv == config.updated_stats_csv


def test_subset_stations_empty_is_a_noop(config) -> None:
    assert config.subset_stations([]).station_ids == config.station_ids


def test_subset_stations_rejects_unconfigured_station(config) -> None:
    with pytest.raises(ValueError, match="not configured for calibration"):
        config.subset_stations(["LIMC"])


# --- schema parsing -----------------------------------------------------------


def _write_config(tmp_path: Path, station_models_block: str) -> Path:
    path = tmp_path / "calibration.yaml"
    base = textwrap.dedent(
        f"""\
        start_date: "2026-02-01"
        schedule_anchor_time_utc: "02:00"
        updated_stats_csv: {tmp_path.as_posix()}/stats.csv
        station_ids:
          - EGLC
          - LEMD
        models:
          - name: icon_eu
            run_init_interval_hours: 3
            forecast_days: 5
        """
    )
    path.write_text(base + station_models_block, encoding="utf-8")
    return path


def test_station_models_is_optional(tmp_path: Path) -> None:
    path = _write_config(tmp_path, "")
    cfg = load_calibration_config(path)
    assert cfg.station_models == {}
    assert _as_tuples(cfg.models_for("LEMD")) == (("icon_eu", 3.0, 5),)


def test_station_models_rejects_an_unknown_station_id(tmp_path: Path) -> None:
    block = (
        "station_models:\n"
        "  LEMDD:\n"
        "    - name: icon_eu\n"
        "      run_init_interval_hours: 3\n"
        "      forecast_days: 5\n"
    )
    path = _write_config(tmp_path, block)
    with pytest.raises(ValueError, match="unknown station_id in station_models"):
        load_calibration_config(path)


def test_station_models_rejects_an_empty_override(tmp_path: Path) -> None:
    path = _write_config(tmp_path, "station_models:\n  LEMD: []\n")
    with pytest.raises(ValueError, match=r"station_models\[LEMD\]"):
        load_calibration_config(path)


def test_station_models_rejects_a_non_mapping(tmp_path: Path) -> None:
    path = _write_config(tmp_path, "station_models:\n  - LEMD\n")
    with pytest.raises(ValueError, match="station_models must be a mapping"):
        load_calibration_config(path)


def test_calibration_yaml_station_models_covers_only_lemd() -> None:
    raw = yaml.safe_load(DEFAULT_CALIBRATION_CONFIG_PATH.read_text(encoding="utf-8"))
    assert list(raw["station_models"]) == ["LEMD"]
