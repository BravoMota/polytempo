"""Tests for trading profile loading."""

from pathlib import Path

import pytest

from polytempo.profiles.load import generate_all_twelve_profiles, load_paper_profiles
from polytempo.weather.calibration_stats_csv import (
    DEFAULT_CALIBRATION_STATS_CSV_PATH,
    DEFAULT_UPDATED_CALIBRATION_STATS_CSV_PATH,
)


def test_generate_all_twelve_profiles_count() -> None:
    profiles = generate_all_twelve_profiles(
        lead_gates={
            "lead30": {"target_lead_hours": 30},
            "lead24": {"target_lead_hours": 24},
        },
        model_strategies=["best_historical", "ensemble_spread"],
    )
    assert len(profiles) == 12
    ids = {p.id for p in profiles}
    assert "bh_dist_arb_lead30" in ids
    assert "es_mid_band_lead24" in ids


def test_best_historical_updated_uses_updated_calibration_path() -> None:
    profiles = generate_all_twelve_profiles(
        lead_gates={"lead30": {"target_lead_hours": 30}},
        model_strategies=["best_historical_updated"],
        trade_strategies=["dist_arb"],
    )
    assert len(profiles) == 1
    assert profiles[0].calibration_stats_path == DEFAULT_UPDATED_CALIBRATION_STATS_CSV_PATH


def test_best_historical_uses_static_calibration_path() -> None:
    profiles = generate_all_twelve_profiles(
        lead_gates={"lead30": {"target_lead_hours": 30}},
        model_strategies=["best_historical"],
        trade_strategies=["dist_arb"],
    )
    assert profiles[0].calibration_stats_path == DEFAULT_CALIBRATION_STATS_CSV_PATH


def test_load_paper_profiles_from_repo_config() -> None:
    path = Path("config/paper_profiles.yaml")
    if not path.is_file():
        pytest.skip("config/paper_profiles.yaml missing")
    profiles = load_paper_profiles(path)
    # 9 lead gates × 3 trade strategies × 2 model strategies (bh + es)
    assert len(profiles) == 54
