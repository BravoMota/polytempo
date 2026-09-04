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


def test_weighted_historical_updated_sharp_uses_updated_calibration_path() -> None:
    profiles = generate_all_twelve_profiles(
        lead_gates={"lead30": {"target_lead_hours": 30}},
        model_strategies=["weighted_historical_updated_sharp"],
        trade_strategies=["dist_arb"],
    )
    assert len(profiles) == 1
    assert profiles[0].id == "whus_dist_arb_lead30"
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
    # 9 lead gates × 14 trade strategies × 3 model strategies hold-to-settle
    # (legacy + budget_normalize_wallet_percent _bnwp twins), plus the
    # active-sell experiment wallets (exit_policy set) and the edge-following
    # active wallets (active_params set, model × strat, no lead).
    hold = [
        p for p in profiles if p.exit_policy is None and p.active_params is None
    ]
    legacy = [p for p in hold if p.sizing_mode == "legacy"]
    bnwp = [
        p
        for p in hold
        if p.sizing_mode == "budget_normalize_wallet_percent"
    ]
    xsell = [p for p in profiles if p.exit_policy is not None]
    active = [p for p in profiles if p.active_params is not None]
    assert len(legacy) == 378
    assert len(bnwp) == 378
    assert len(hold) == 756
    assert len(xsell) == 16
    assert len(active) == 42  # 3 model × 14 trade
    assert all(p.id.endswith("_active") for p in active)
    assert all(p.id.endswith("_bnwp") for p in bnwp)
    assert all(p.event_budget_fraction == 0.10 for p in bnwp)
    assert "bh_dist_arb_lead30_bnwp" in {p.id for p in bnwp}
    assert not any(p.id.startswith("es_") for p in hold)
    assert any(p.id.startswith("whu_") for p in hold)


def test_load_backtest_profiles_event_budgets() -> None:
    path = Path("config/backtest_profiles.yaml")
    if not path.is_file():
        pytest.skip("config/backtest_profiles.yaml missing")
    profiles = load_paper_profiles(path)
    hold = [
        p for p in profiles if p.exit_policy is None and p.active_params is None
    ]
    modes = sorted({p.sizing_mode for p in hold})
    assert modes == ["budget_normalize_wallet_percent", "legacy"]
    bnwp = [
        p for p in hold if p.sizing_mode == "budget_normalize_wallet_percent"
    ]
    assert all(p.event_budget_fraction == 0.10 for p in bnwp)
    assert all(p.id.endswith("_bnwp") for p in bnwp)
    assert not any(p.id.endswith(("_bnwp05", "_bnwp20")) for p in bnwp)


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


# --------------------------------------------------------------------------- #
# Madrid grids: separate config files, city is the only difference
# --------------------------------------------------------------------------- #
MADRID_CONFIGS = (
    (Path("config/paper_profiles.yaml"), Path("config/paper_profiles_madrid.yaml")),
    (
        Path("config/backtest_profiles.yaml"),
        Path("config/backtest_profiles_madrid.yaml"),
    ),
)


@pytest.mark.parametrize("madrid_path", [m for _, m in MADRID_CONFIGS])
def test_madrid_configs_load_with_city_madrid(madrid_path: Path) -> None:
    if not madrid_path.is_file():
        pytest.skip(f"{madrid_path} missing")
    profiles = load_paper_profiles(madrid_path)
    assert profiles
    assert {p.city for p in profiles} == {"madrid"}
    assert all(p.calibration_stats_path.is_absolute() for p in profiles)


@pytest.mark.parametrize(("london_path", "madrid_path"), MADRID_CONFIGS)
def test_madrid_configs_differ_from_london_only_in_city(
    london_path: Path, madrid_path: Path
) -> None:
    """Like-for-like: same strategy grid, city is the only variable."""
    if not (london_path.is_file() and madrid_path.is_file()):
        pytest.skip("profile configs missing")
    london = yaml.safe_load(london_path.read_text(encoding="utf-8"))
    madrid = yaml.safe_load(madrid_path.read_text(encoding="utf-8"))
    assert london.pop("city") == "london"
    assert madrid.pop("city") == "madrid"
    assert madrid == london


def test_madrid_paper_grid_matches_london_wallet_counts() -> None:
    london_path = Path("config/paper_profiles.yaml")
    madrid_path = Path("config/paper_profiles_madrid.yaml")
    if not (london_path.is_file() and madrid_path.is_file()):
        pytest.skip("profile configs missing")
    london = load_paper_profiles(london_path)
    madrid = load_paper_profiles(madrid_path)
    assert {p.id for p in madrid} == {p.id for p in london}
