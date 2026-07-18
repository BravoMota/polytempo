"""Tests for paper stake allocation (legacy vs bnwp)."""

from __future__ import annotations

import pytest

from polytempo.analysis import AnalysisRow
from polytempo.paper.sizing import (
    BUDGET_MIN_TICKET_USD,
    allocate_stakes,
    implied_stake_usd,
    stake_fraction,
)
from polytempo.profiles.models import (
    SIZING_MODE_BUDGET_NORMALIZE_WALLET_PERCENT,
    SIZING_MODE_LEGACY,
)


def _buy(label: str, *, edge_pp: float, stake_fraction: float | None = None) -> AnalysisRow:
    return AnalysisRow(
        label=label,
        probability=0.5,
        yes_ask=0.40,
        edge_yes_pp=edge_pp,
        action="BUY_YES",
        reason="test",
        confidence="medium",
        warnings=[],
        stake_fraction=stake_fraction,
    )


def test_allocate_stakes_empty_when_no_buys() -> None:
    rows = [
        AnalysisRow(
            label="23°C",
            probability=0.5,
            yes_ask=0.40,
            edge_yes_pp=10.0,
            action="SKIP",
            reason="test",
            confidence="none",
            warnings=[],
        )
    ]
    assert allocate_stakes(rows, 1000.0) == []


def test_bnwp_preserves_relative_weights() -> None:
    rows = [_buy("23°C", edge_pp=10.0), _buy("24°C", edge_pp=10.0)]
    allocated = allocate_stakes(
        rows,
        1000.0,
        sizing_mode=SIZING_MODE_BUDGET_NORMALIZE_WALLET_PERCENT,
        event_budget_fraction=0.10,
    )
    assert len(allocated) == 2
    stakes = [s for _, s in allocated]
    assert sum(stakes) == pytest.approx(100.0)
    assert stakes[0] == pytest.approx(stakes[1])


def test_bnwp_pool_is_fraction_of_balance() -> None:
    rows = [_buy("23°C", edge_pp=15.0)]
    allocated = allocate_stakes(
        rows,
        balance=300.0,
        sizing_mode=SIZING_MODE_BUDGET_NORMALIZE_WALLET_PERCENT,
        event_budget_fraction=0.10,
    )
    assert len(allocated) == 1
    assert allocated[0][1] == pytest.approx(30.0)


def test_bnwp_weights_scale_with_balance() -> None:
    rows = [
        _buy("a", edge_pp=7.0),
        _buy("b", edge_pp=15.0),
    ]
    low = allocate_stakes(
        rows,
        500.0,
        sizing_mode=SIZING_MODE_BUDGET_NORMALIZE_WALLET_PERCENT,
        event_budget_fraction=0.10,
    )
    high = allocate_stakes(
        rows,
        2000.0,
        sizing_mode=SIZING_MODE_BUDGET_NORMALIZE_WALLET_PERCENT,
        event_budget_fraction=0.10,
    )
    assert len(low) == len(high) == 2
    ratio_low = low[0][1] / low[1][1]
    ratio_high = high[0][1] / high[1][1]
    assert ratio_low == pytest.approx(ratio_high, rel=1e-3)
    assert sum(s for _, s in high) == pytest.approx(
        4.0 * sum(s for _, s in low), rel=1e-3
    )


def test_bnwp_skips_dust_under_fifty_cents() -> None:
    labels = [f"b{i}" for i in range(30)]
    rows = [_buy(label, edge_pp=10.0) for label in labels]
    allocated = allocate_stakes(
        rows,
        balance=100.0,
        sizing_mode=SIZING_MODE_BUDGET_NORMALIZE_WALLET_PERCENT,
        event_budget_fraction=0.10,
    )
    assert allocated == []


def test_bnwp_floors_sub_dollar_to_min_ticket() -> None:
    rows = [_buy("23°C", edge_pp=10.0), _buy("24°C", edge_pp=10.0)]
    allocated = allocate_stakes(
        rows,
        balance=15.0,
        sizing_mode=SIZING_MODE_BUDGET_NORMALIZE_WALLET_PERCENT,
        event_budget_fraction=0.10,
    )
    assert len(allocated) == 2
    assert all(s == BUDGET_MIN_TICKET_USD for _, s in allocated)


def test_implied_stake_uses_edge_ramp() -> None:
    row = _buy("23°C", edge_pp=7.0)
    assert implied_stake_usd(row, 1000.0) == pytest.approx(1000.0 * stake_fraction(7.0))


def test_legacy_allocate_sequential() -> None:
    rows = [_buy("23°C", edge_pp=7.0), _buy("24°C", edge_pp=7.0)]
    allocated = allocate_stakes(rows, 1000.0, sizing_mode=SIZING_MODE_LEGACY)
    assert len(allocated) == 2
    first = round(1000.0 * stake_fraction(7.0), 2)
    second = round((1000.0 - first) * stake_fraction(7.0), 2)
    assert allocated[0][1] == pytest.approx(first)
    assert allocated[1][1] == pytest.approx(second)


def test_inmemory_ledger_bnwp_uses_fraction() -> None:
    from polytempo.analysis import AnalysisResult
    from polytempo.model.distribution import DistributionBuildInfo
    from polytempo.paper.backtest import InMemoryLedgerStore

    build = DistributionBuildInfo(
        values_used_c=[24.0],
        default_sigma_c=1.0,
        lead_hours=None,
        lead_hours_sigma_floor_c=None,
        ensemble_stdev_c=None,
        mean_c=24.0,
        sigma_c=1.0,
        method="test",
    )
    result = AnalysisResult(
        distribution_mean_c=24.0,
        distribution_sigma_c=1.0,
        distribution_build=build,
        rows=[_buy("23°C", edge_pp=10.0), _buy("24°C", edge_pp=10.0)],
    )
    opened = InMemoryLedgerStore().open_trades_from_analysis(
        "p_bnwp",
        result,
        "evt-1",
        sizing_mode=SIZING_MODE_BUDGET_NORMALIZE_WALLET_PERCENT,
        event_budget_fraction=0.10,
    )
    assert len(opened) == 2
    assert sum(t.stake_usd for t in opened) == pytest.approx(100.0)
