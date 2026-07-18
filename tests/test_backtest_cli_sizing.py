"""Tests for backtest CLI profile selection / sizing-mode lock."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import backtest as backtest_cli  # noqa: E402
from polytempo.profiles.load import generate_all_twelve_profiles  # noqa: E402
from polytempo.profiles.models import (  # noqa: E402
    SIZING_MODE_BUDGET_NORMALIZE_WALLET_PERCENT,
    SIZING_MODE_LEGACY,
)


def test_normalize_sizing_mode_aliases() -> None:
    assert backtest_cli._normalize_sizing_mode(None) is None
    assert backtest_cli._normalize_sizing_mode("legacy") == SIZING_MODE_LEGACY
    assert (
        backtest_cli._normalize_sizing_mode("budget_normalize_wallet_percent")
        == SIZING_MODE_BUDGET_NORMALIZE_WALLET_PERCENT
    )
    assert (
        backtest_cli._normalize_sizing_mode("bnwp")
        == SIZING_MODE_BUDGET_NORMALIZE_WALLET_PERCENT
    )
    with pytest.raises(SystemExit):
        backtest_cli._normalize_sizing_mode("bl")


def test_select_profiles_filters_sizing_mode() -> None:
    legacy = generate_all_twelve_profiles(
        lead_gates={"lead30": {"target_lead_hours": 30}},
        model_strategies=["best_historical"],
        trade_strategies=["dist_arb"],
    )
    both = backtest_cli._apply_sizing_mode(
        legacy,
        sizing_mode=None,
        event_budget_fraction=0.10,
    )
    bnwp_only = backtest_cli._select_profiles(
        both,
        ids=None,
        trade_strategy=None,
        model_strategy=None,
        sizing_mode=SIZING_MODE_BUDGET_NORMALIZE_WALLET_PERCENT,
    )
    legacy_only = backtest_cli._select_profiles(
        both,
        ids=None,
        trade_strategy=None,
        model_strategy=None,
        sizing_mode=SIZING_MODE_LEGACY,
    )
    assert len(both) == 2
    assert {p.id for p in bnwp_only} == {"bh_dist_arb_lead30_bnwp"}
    assert {p.id for p in legacy_only} == {"bh_dist_arb_lead30"}


def test_apply_sizing_mode_legacy_only() -> None:
    legacy = generate_all_twelve_profiles(
        lead_gates={"lead24": {"target_lead_hours": 24}},
        model_strategies=["weighted_historical_updated"],
        trade_strategies=["max_edge"],
    )
    out = backtest_cli._apply_sizing_mode(
        legacy,
        sizing_mode=SIZING_MODE_LEGACY,
        event_budget_fraction=0.10,
    )
    assert [p.id for p in out] == ["whu_max_edge_lead24"]
