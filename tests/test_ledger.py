"""Tests for the paper trading ledger."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from polytempo.analysis import AnalysisResult, AnalysisRow
from polytempo.paper.ledger import (
    EDGE_CEILING_PP,
    EDGE_FLOOR_PP,
    MAX_STAKE_FRAC,
    MIN_STAKE_FRAC,
    STARTING_BALANCE_USD,
    open_trades_from_analysis,
    read_state,
    settle_event,
    stake_fraction,
)


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


def test_empty_ledger_returns_starting_balance(tmp_path: Path) -> None:
    state = read_state(tmp_path / "ledger.jsonl")
    assert state.balance_usd == STARTING_BALANCE_USD
    assert state.open_trades == []
    assert state.settled_count == 0
    assert state.realized_pnl_usd == 0.0


def test_open_trades_only_for_buy_yes_rows(tmp_path: Path) -> None:
    path = tmp_path / "ledger.jsonl"
    result = _result(
        [
            _row("23°C", yes_ask=0.40, edge_pp=10.0, action="BUY_YES"),
            _row("24°C", yes_ask=0.50, edge_pp=2.0, action="SKIP"),
        ]
    )

    opened = open_trades_from_analysis(result, event_id="evt-1", path=path)

    assert [t.bucket_label for t in opened] == ["23°C"]


def test_open_trade_stake_uses_2_percent_at_floor_edge(tmp_path: Path) -> None:
    path = tmp_path / "ledger.jsonl"
    result = _result([_row("23°C", yes_ask=0.40, edge_pp=EDGE_FLOOR_PP, action="BUY_YES")])

    opened = open_trades_from_analysis(result, event_id="evt-1", path=path)

    assert opened[0].stake_usd == pytest.approx(STARTING_BALANCE_USD * MIN_STAKE_FRAC)


def test_open_trade_stake_uses_5_percent_at_or_above_ceiling(tmp_path: Path) -> None:
    path = tmp_path / "ledger.jsonl"
    result = _result(
        [_row("23°C", yes_ask=0.40, edge_pp=EDGE_CEILING_PP + 3, action="BUY_YES")]
    )

    opened = open_trades_from_analysis(result, event_id="evt-1", path=path)

    assert opened[0].stake_usd == pytest.approx(STARTING_BALANCE_USD * MAX_STAKE_FRAC)


def test_shares_equal_stake_over_ask(tmp_path: Path) -> None:
    path = tmp_path / "ledger.jsonl"
    result = _result([_row("23°C", yes_ask=0.50, edge_pp=10.0, action="BUY_YES")])

    opened = open_trades_from_analysis(result, event_id="evt-1", path=path)

    assert opened[0].shares == pytest.approx(opened[0].stake_usd / 0.50, rel=1e-3)


def test_open_decrements_balance_in_state(tmp_path: Path) -> None:
    path = tmp_path / "ledger.jsonl"
    result = _result([_row("23°C", yes_ask=0.40, edge_pp=10.0, action="BUY_YES")])

    opened = open_trades_from_analysis(result, event_id="evt-1", path=path)
    state = read_state(path)

    assert state.balance_usd == pytest.approx(STARTING_BALANCE_USD - opened[0].stake_usd)
    assert len(state.open_trades) == 1


def test_settle_winning_bucket_pays_one_dollar_per_share(tmp_path: Path) -> None:
    path = tmp_path / "ledger.jsonl"
    result = _result([_row("23°C", yes_ask=0.40, edge_pp=10.0, action="BUY_YES")])
    opened = open_trades_from_analysis(result, event_id="evt-1", path=path)

    settle_event("evt-1", winning_label="23°C", path=path)
    state = read_state(path)

    expected_balance = (
        STARTING_BALANCE_USD - opened[0].stake_usd + opened[0].shares
    )
    assert state.balance_usd == pytest.approx(expected_balance)
    assert state.open_trades == []
    assert state.settled_count == 1
    assert state.realized_pnl_usd == pytest.approx(opened[0].shares - opened[0].stake_usd)


def test_settle_losing_bucket_pays_zero(tmp_path: Path) -> None:
    path = tmp_path / "ledger.jsonl"
    result = _result([_row("23°C", yes_ask=0.40, edge_pp=10.0, action="BUY_YES")])
    opened = open_trades_from_analysis(result, event_id="evt-1", path=path)

    settle_event("evt-1", winning_label="24°C", path=path)
    state = read_state(path)

    assert state.balance_usd == pytest.approx(STARTING_BALANCE_USD - opened[0].stake_usd)
    assert state.realized_pnl_usd == pytest.approx(-opened[0].stake_usd)


def test_settle_only_touches_matching_event(tmp_path: Path) -> None:
    path = tmp_path / "ledger.jsonl"
    open_trades_from_analysis(
        _result([_row("23°C", yes_ask=0.40, edge_pp=10.0, action="BUY_YES")]),
        event_id="evt-1",
        path=path,
    )
    open_trades_from_analysis(
        _result([_row("24°C", yes_ask=0.40, edge_pp=10.0, action="BUY_YES")]),
        event_id="evt-2",
        path=path,
    )

    settle_event("evt-1", winning_label="23°C", path=path)
    state = read_state(path)

    assert state.settled_count == 1
    assert {t.event_id for t in state.open_trades} == {"evt-2"}


def test_rerun_does_not_duplicate_open_for_same_event_bucket_side(tmp_path: Path) -> None:
    path = tmp_path / "ledger.jsonl"
    result = _result([_row("23°C", yes_ask=0.40, edge_pp=10.0, action="BUY_YES")])

    first = open_trades_from_analysis(result, event_id="evt-1", path=path)
    second = open_trades_from_analysis(result, event_id="evt-1", path=path)

    assert len(first) == 1
    assert second == []
    state = read_state(path)
    assert len(state.open_trades) == 1


def test_rerun_after_settlement_can_reopen_same_bucket(tmp_path: Path) -> None:
    path = tmp_path / "ledger.jsonl"
    result = _result([_row("23°C", yes_ask=0.40, edge_pp=10.0, action="BUY_YES")])

    open_trades_from_analysis(result, event_id="evt-1", path=path)
    settle_event("evt-1", winning_label="23°C", path=path)
    reopened = open_trades_from_analysis(result, event_id="evt-1", path=path)

    assert len(reopened) == 1


def test_rerun_distinct_sides_on_same_bucket_both_open(tmp_path: Path) -> None:
    path = tmp_path / "ledger.jsonl"
    open_trades_from_analysis(
        _result([_row("23°C", yes_ask=0.40, edge_pp=10.0, action="BUY_YES")]),
        event_id="evt-1",
        path=path,
    )
    no_row = _row(
        "23°C",
        yes_ask=0.40,
        edge_pp=10.0,
        action="BUY_NO",
        side="NO",
        yes_bid=0.30,
    )
    second = open_trades_from_analysis(_result([no_row]), event_id="evt-1", path=path)

    assert len(second) == 1
    assert second[0].side == "NO"


def test_ledger_file_is_append_only_jsonl(tmp_path: Path) -> None:
    path = tmp_path / "ledger.jsonl"
    open_trades_from_analysis(
        _result([_row("23°C", yes_ask=0.40, edge_pp=10.0, action="BUY_YES")]),
        event_id="evt-1",
        path=path,
    )
    settle_event("evt-1", winning_label="23°C", path=path)

    lines = [json.loads(line) for line in path.read_text().splitlines() if line]
    assert [r["type"] for r in lines] == ["OPEN", "SETTLE"]


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


def test_buy_no_opens_with_entry_price_one_minus_bid(tmp_path: Path) -> None:
    path = tmp_path / "ledger.jsonl"
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

    opened = open_trades_from_analysis(result, event_id="evt-1", path=path)

    assert len(opened) == 1
    assert opened[0].side == "NO"
    assert opened[0].entry_price == pytest.approx(0.70)
    assert opened[0].shares == pytest.approx(opened[0].stake_usd / 0.70, rel=1e-3)


def test_settle_no_side_pays_when_bucket_loses(tmp_path: Path) -> None:
    path = tmp_path / "ledger.jsonl"
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
    opened = open_trades_from_analysis(result, event_id="evt-1", path=path)

    settle_event("evt-1", winning_label="24°C", path=path)
    state = read_state(path)

    expected = STARTING_BALANCE_USD - opened[0].stake_usd + opened[0].shares
    assert state.balance_usd == pytest.approx(expected)
    assert state.realized_pnl_usd == pytest.approx(opened[0].shares - opened[0].stake_usd)


def test_stake_override_bypasses_edge_ramp(tmp_path: Path) -> None:
    path = tmp_path / "ledger.jsonl"
    row = AnalysisRow(
        label="23°C",
        probability=0.5,
        yes_ask=0.40,
        edge_yes_pp=2.0,
        action="BUY_YES",
        reason="test",
        confidence="medium",
        warnings=[],
        stake_usd=5.0,
    )
    result = _result([row])

    opened = open_trades_from_analysis(result, event_id="evt-1", path=path)

    assert opened[0].stake_usd == pytest.approx(5.0)
    assert opened[0].shares == pytest.approx(5.0 / 0.40, rel=1e-3)


def test_stake_override_skipped_when_exceeds_balance(tmp_path: Path) -> None:
    path = tmp_path / "ledger.jsonl"
    row = AnalysisRow(
        label="23°C",
        probability=0.5,
        yes_ask=0.40,
        edge_yes_pp=2.0,
        action="BUY_YES",
        reason="test",
        confidence="medium",
        warnings=[],
        stake_usd=999_999.0,
    )
    result = _result([row])

    opened = open_trades_from_analysis(result, event_id="evt-1", path=path)

    assert opened == []


def test_settle_no_side_pays_zero_when_bucket_wins(tmp_path: Path) -> None:
    path = tmp_path / "ledger.jsonl"
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
    opened = open_trades_from_analysis(result, event_id="evt-1", path=path)

    settle_event("evt-1", winning_label="23°C", path=path)
    state = read_state(path)

    assert state.balance_usd == pytest.approx(STARTING_BALANCE_USD - opened[0].stake_usd)
    assert state.realized_pnl_usd == pytest.approx(-opened[0].stake_usd)


def _result(rows: list[AnalysisRow]) -> AnalysisResult:
    return AnalysisResult(
        distribution_mean_c=24.0,
        distribution_sigma_c=1.0,
        rows=rows,
    )
