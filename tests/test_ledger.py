"""Tests for the paper trading ledger (PostgreSQL)."""

from __future__ import annotations

import pytest

from polytempo.analysis import AnalysisResult, AnalysisRow
from polytempo.model.distribution import DistributionBuildInfo
from polytempo.paper.ledger import (
    EDGE_CEILING_PP,
    EDGE_FLOOR_PP,
    MAX_STAKE_FRAC,
    MIN_STAKE_FRAC,
    STARTING_BALANCE_USD,
    PostgresLedgerStore,
    stake_fraction,
)

PROFILE = "test_ledger"


@pytest.fixture
def store(paper_db_url: str) -> PostgresLedgerStore:
    return PostgresLedgerStore(database_url=paper_db_url)


def test_stake_fraction_clamps_at_floor() -> None:
    assert stake_fraction(0.0) == MIN_STAKE_FRAC
    assert stake_fraction(EDGE_FLOOR_PP) == MIN_STAKE_FRAC


def test_stake_fraction_clamps_at_ceiling() -> None:
    assert stake_fraction(EDGE_CEILING_PP) == MAX_STAKE_FRAC
    assert stake_fraction(EDGE_CEILING_PP + 5.0) == MAX_STAKE_FRAC


def test_stake_fraction_scales_linearly_between_bounds() -> None:
    midpoint = (EDGE_FLOOR_PP + EDGE_CEILING_PP) / 2
    expected = (MIN_STAKE_FRAC + MAX_STAKE_FRAC) / 2
    assert stake_fraction(midpoint) == pytest.approx(expected)


def test_empty_ledger_returns_starting_balance(store: PostgresLedgerStore) -> None:
    state = store.read_state(PROFILE)
    assert state.balance_usd == STARTING_BALANCE_USD
    assert state.open_trades == []


def test_open_trades_only_for_buy_rows(store: PostgresLedgerStore) -> None:
    result = _result(
        [
            _row("23°C", yes_ask=0.40, edge_pp=10.0, action="BUY_YES"),
            _row("24°C", yes_ask=0.50, edge_pp=2.0, action="SKIP"),
        ]
    )
    opened = store.open_trades_from_analysis(PROFILE, result, "evt-1")
    assert [t.bucket_label for t in opened] == ["23°C"]


def test_open_trade_stake_uses_2_percent_at_floor_edge(store: PostgresLedgerStore) -> None:
    result = _result([_row("23°C", yes_ask=0.40, edge_pp=EDGE_FLOOR_PP, action="BUY_YES")])
    opened = store.open_trades_from_analysis(PROFILE, result, "evt-1")
    assert opened[0].stake_usd == pytest.approx(STARTING_BALANCE_USD * MIN_STAKE_FRAC)


def test_settle_winning_bucket_pays_one_dollar_per_share(store: PostgresLedgerStore) -> None:
    result = _result([_row("23°C", yes_ask=0.40, edge_pp=10.0, action="BUY_YES")])
    opened = store.open_trades_from_analysis(PROFILE, result, "evt-1")
    store.settle_event(PROFILE, "evt-1", "23°C")
    state = store.read_state(PROFILE)
    expected = STARTING_BALANCE_USD - opened[0].stake_usd + opened[0].shares
    assert state.balance_usd == pytest.approx(expected)
    assert state.open_trades == []


def test_rerun_does_not_duplicate_open_for_same_bucket(store: PostgresLedgerStore) -> None:
    result = _result([_row("23°C", yes_ask=0.40, edge_pp=10.0, action="BUY_YES")])
    first = store.open_trades_from_analysis(PROFILE, result, "evt-1")
    second = store.open_trades_from_analysis(PROFILE, result, "evt-1")
    assert len(first) == 1
    assert second == []


def test_buy_no_entry_price(store: PostgresLedgerStore) -> None:
    result = _result(
        [
            _row(
                "23°C",
                yes_ask=0.40,
                edge_pp=10.0,
                action="BUY_NO",
                side="NO",
                yes_bid=0.30,
            )
        ]
    )
    opened = store.open_trades_from_analysis(PROFILE, result, "evt-1")
    assert opened[0].entry_price == pytest.approx(0.70)


def test_append_close_not_implemented(store: PostgresLedgerStore) -> None:
    with pytest.raises(NotImplementedError):
        store.append_close(PROFILE, "t1")


def _row(
    label: str,
    *,
    yes_ask: float,
    edge_pp: float,
    action: str,
    side: str = "YES",
    yes_bid: float | None = None,
) -> AnalysisRow:
    return AnalysisRow(
        label=label,
        probability=0.5,
        yes_ask=yes_ask,
        edge_yes_pp=edge_pp,
        action=action,
        reason="test",
        confidence="medium",
        warnings=[],
        side=side,
        yes_bid=yes_bid,
    )


def _result(rows: list[AnalysisRow]) -> AnalysisResult:
    build = DistributionBuildInfo(
        values_used_c=[24.0],
        default_sigma_c=1.0,
        lead_hours=None,
        lead_hours_sigma_floor_c=None,
        ensemble_stdev_c=None,
        mean_c=24.0,
        sigma_c=1.0,
        method="legacy_single_default_sigma",
    )
    return AnalysisResult(
        distribution_mean_c=24.0,
        distribution_sigma_c=1.0,
        distribution_build=build,
        rows=rows,
    )
