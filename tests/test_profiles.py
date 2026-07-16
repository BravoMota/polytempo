"""Tests for trading profile loading."""

from pathlib import Path

import pytest
import yaml

from polytempo.profiles.load import generate_all_twelve_profiles, load_paper_profiles
from polytempo.profiles.registry import trade_strategy_for_name
from polytempo.strategy import TopKStrategy
from polytempo.weather.calibration_stats_csv import (
    DEFAULT_CALIBRATION_STATS_CSV_PATH,
    DEFAULT_UPDATED_CALIBRATION_STATS_CSV_PATH,
)
from polytempo.weather.data_dir import REPO_ROOT


def test_generate_all_twelve_profiles_count() -> None:
    profiles = generate_all_twelve_profiles(
        lead_gates={
            "lead30": {"target_lead_hours": 30},
            "lead24": {"target_lead_hours": 24},
        },
        model_strategies=["best_historical", "ensemble_spread"],
        trade_strategies=["argmax_yes", "dist_arb", "mid_band"],
    )
    assert len(profiles) == 12
    ids = {p.id for p in profiles}
    assert "bh_dist_arb_lead30" in ids
    assert "es_mid_band_lead24" in ids


def test_weighted_historical_market_sigma_uses_updated_calibration_path() -> None:
    profiles = generate_all_twelve_profiles(
        lead_gates={"lead30": {"target_lead_hours": 30}},
        model_strategies=["weighted_historical_market_sigma"],
        trade_strategies=["dist_arb"],
    )
    assert len(profiles) == 1
    assert profiles[0].id == "whums_dist_arb_lead30"
    assert profiles[0].calibration_stats_path == (
        REPO_ROOT / DEFAULT_UPDATED_CALIBRATION_STATS_CSV_PATH
    ).resolve()


def test_weighted_historical_updated_uses_updated_calibration_path() -> None:
    profiles = generate_all_twelve_profiles(
        lead_gates={"lead30": {"target_lead_hours": 30}},
        model_strategies=["weighted_historical_updated"],
        trade_strategies=["dist_arb"],
    )
    assert len(profiles) == 1
    assert profiles[0].id == "whu_dist_arb_lead30"
    assert profiles[0].calibration_stats_path == (
        REPO_ROOT / DEFAULT_UPDATED_CALIBRATION_STATS_CSV_PATH
    ).resolve()


def test_best_historical_updated_uses_updated_calibration_path() -> None:
    profiles = generate_all_twelve_profiles(
        lead_gates={"lead30": {"target_lead_hours": 30}},
        model_strategies=["best_historical_updated"],
        trade_strategies=["dist_arb"],
    )
    assert len(profiles) == 1
    assert profiles[0].calibration_stats_path == (
        REPO_ROOT / DEFAULT_UPDATED_CALIBRATION_STATS_CSV_PATH
    ).resolve()


def test_best_historical_uses_static_calibration_path() -> None:
    profiles = generate_all_twelve_profiles(
        lead_gates={"lead30": {"target_lead_hours": 30}},
        model_strategies=["best_historical"],
        trade_strategies=["dist_arb"],
    )
    assert profiles[0].calibration_stats_path == (
        REPO_ROOT / DEFAULT_CALIBRATION_STATS_CSV_PATH
    ).resolve()


def test_load_paper_profiles_resolves_absolute_calibration_paths() -> None:
    path = Path("config/paper_profiles.yaml")
    if not path.is_file():
        pytest.skip("config/paper_profiles.yaml missing")
    profiles = load_paper_profiles(path)
    bhu = next(p for p in profiles if p.id == "bhu_dist_arb_lead30")
    bh = next(p for p in profiles if p.id == "bh_dist_arb_lead30")
    assert bhu.calibration_stats_path.is_absolute()
    assert bh.calibration_stats_path.is_absolute()
    assert bhu.calibration_stats_path == (
        REPO_ROOT / DEFAULT_UPDATED_CALIBRATION_STATS_CSV_PATH
    ).resolve()


def test_load_paper_profiles_from_repo_config() -> None:
    path = Path("config/paper_profiles.yaml")
    if not path.is_file():
        pytest.skip("config/paper_profiles.yaml missing")
    profiles = load_paper_profiles(path)
    # 9 lead gates × 14 trade strategies × 3 model strategies hold-to-settle,
    # plus the active-sell experiment wallets (exit_policy set) and the
    # edge-following active wallets (active_params set, model × strat, no lead).
    hold = [
        p for p in profiles if p.exit_policy is None and p.active_params is None
    ]
    xsell = [p for p in profiles if p.exit_policy is not None]
    active = [p for p in profiles if p.active_params is not None]
    assert len(hold) == 378
    assert len(xsell) == 16
    assert len(active) == 42  # 3 model × 14 trade
    assert all(p.id.endswith("_active") for p in active)
    assert not any(p.id.startswith("es_") for p in hold)
    assert any(p.id.startswith("whu_") for p in hold)


def test_config_trade_strategies_are_registered() -> None:
    path = Path("config/paper_profiles.yaml")
    if not path.is_file():
        pytest.skip("config/paper_profiles.yaml missing")
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    for name in raw["trade_strategies"]:
        trade_strategy_for_name(name)


def test_topk_no_resolves_to_no_side_variant() -> None:
    strat = trade_strategy_for_name("topk_no")
    assert isinstance(strat, TopKStrategy)
    assert strat.name == "topk_no"
    assert strat.side == "NO"
