"""Tests for the active-sell exit layer (TP/SL bracket + close primitive)."""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from polytempo.analysis import AnalysisResult, AnalysisRow
from polytempo.markets.polymarket import PolymarketBucket, PolymarketEvent
from polytempo.model.distribution import DistributionBuildInfo
from polytempo.paper.bot import (
    ACTIVE_EXIT_POLL_INTERVAL,
    _in_active_exit_window,
    _mark_to_market,
    sweep_active_exits,
)
from polytempo.paper.ledger import LedgerState, OpenTrade, PostgresLedgerStore
from polytempo.profiles.load import load_paper_profiles
from polytempo.profiles.models import EntryGate, ExitPolicy, TradingProfile


# --------------------------------------------------------------------------- #
# Fabrication helpers
# --------------------------------------------------------------------------- #
def _bucket(label: str, *, yes_bid=None, yes_ask=None, resolved=False) -> PolymarketBucket:
    return PolymarketBucket(
        market_id="m",
        label=label,
        yes_bid=yes_bid,
        yes_ask=yes_ask,
        liquidity_usd=None,
        spread=None,
        rules=None,
        resolved=resolved,
    )


def _event(buckets, *, eid="evt-1", settlement_date=None) -> PolymarketEvent:
    return PolymarketEvent(
        event_id=eid,
        slug="s",
        title="t",
        settlement_date=settlement_date,
        buckets=buckets,
    )


def _trade(*, bucket="26°C", entry_price=0.24, side="YES", tid="t1") -> OpenTrade:
    return OpenTrade(
        trade_id=tid,
        event_id="evt-1",
        bucket_label=bucket,
        yes_ask=entry_price,
        edge_pp=10.0,
        stake_usd=10.0,
        shares=round(10.0 / entry_price, 4),
        side=side,
        entry_price=entry_price,
    )


def _xsell_profile(pid="bh_max_edge_lead42_xsell", *, tp=1.5, sl=0.5) -> TradingProfile:
    return TradingProfile(
        id=pid,
        model_strategy="best_historical",
        trade_strategy="max_edge",
        entry_gate=EntryGate(target_lead_hours=42),
        exit_policy=ExitPolicy(take_profit_ratio=tp, stop_loss_ratio=sl),
    )


def _hold_profile(pid="bh_max_edge_lead42") -> TradingProfile:
    return TradingProfile(
        id=pid,
        model_strategy="best_historical",
        trade_strategy="max_edge",
        entry_gate=EntryGate(target_lead_hours=42),
    )


class _FakeStore:
    """In-memory LedgerStore stand-in for sweep tests (no Postgres)."""

    def __init__(self, open_by_profile: dict[str, list[OpenTrade]]):
        self._open = {k: list(v) for k, v in open_by_profile.items()}
        self.closed: list[tuple[str, str, float, str]] = []

    def read_state(self, profile_id: str) -> LedgerState:
        trades = self._open.get(profile_id, [])
        return LedgerState(
            balance_usd=0.0,
            open_trades=list(trades),
            settled_count=0,
            realized_pnl_usd=0.0,
        )

    def close_position(self, profile_id, trade_id, sell_price, *, reason):
        trades = self._open.get(profile_id, [])
        match = next((t for t in trades if t.trade_id == trade_id), None)
        if match is None:
            return None
        trades.remove(match)
        self.closed.append((profile_id, trade_id, round(sell_price, 4), reason))
        return match


def _run_sweep(monkeypatch, store, profiles, event, *, now=None):
    monkeypatch.setattr("polytempo.paper.bot._fetch_live_event", lambda eid: event)
    now = now or datetime(2026, 6, 18, 2, 0, tzinfo=timezone.utc)  # outside window
    return sweep_active_exits(store, profiles, now=now)


# --------------------------------------------------------------------------- #
# ExitPolicy validation
# --------------------------------------------------------------------------- #
def test_exit_policy_defaults() -> None:
    p = ExitPolicy()
    assert p.take_profit_ratio == 1.5
    assert p.stop_loss_ratio == 0.5


@pytest.mark.parametrize("tp,sl", [(1.0, 0.5), (0.9, 0.5), (1.5, 1.0), (1.5, 0.0), (1.5, -0.1)])
def test_exit_policy_rejects_bad_ratios(tp, sl) -> None:
    with pytest.raises(ValueError):
        ExitPolicy(take_profit_ratio=tp, stop_loss_ratio=sl)


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #
def test_mark_to_market_yes_uses_bid() -> None:
    assert _mark_to_market(_trade(side="YES"), _bucket("26°C", yes_bid=0.37)) == 0.37


def test_mark_to_market_no_uses_complement_of_ask() -> None:
    mark = _mark_to_market(_trade(side="NO"), _bucket("26°C", yes_ask=0.70))
    assert mark == pytest.approx(0.30)


def test_mark_to_market_none_when_unpriced() -> None:
    assert _mark_to_market(_trade(side="YES"), _bucket("26°C")) is None
    assert _mark_to_market(_trade(side="NO"), _bucket("26°C")) is None


def test_active_exit_window_true_on_resolution_day_afternoon() -> None:
    # 12:00 UTC = 13:00 London (BST) on the settlement day -> in window.
    now = datetime(2026, 6, 18, 12, 0, tzinfo=timezone.utc)
    assert _in_active_exit_window(now, date(2026, 6, 18)) is True


def test_active_exit_window_false_before_window_and_other_days() -> None:
    early = datetime(2026, 6, 18, 2, 0, tzinfo=timezone.utc)  # 03:00 London
    assert _in_active_exit_window(early, date(2026, 6, 18)) is False
    noon = datetime(2026, 6, 18, 12, 0, tzinfo=timezone.utc)
    assert _in_active_exit_window(noon, date(2026, 6, 19)) is False
    assert _in_active_exit_window(noon, None) is False


# --------------------------------------------------------------------------- #
# Bracket logic via sweep_active_exits
# --------------------------------------------------------------------------- #
def test_take_profit_fires_on_jun18_path(monkeypatch) -> None:
    # Entry 24c, live bid 37c -> 1.54x >= 1.5 -> TP, before resolution.
    store = _FakeStore({"bh_max_edge_lead42_xsell": [_trade(entry_price=0.24)]})
    event = _event([_bucket("26°C", yes_bid=0.37)])
    result = _run_sweep(monkeypatch, store, [_xsell_profile()], event)
    assert result.closed == 1
    assert store.closed == [("bh_max_edge_lead42_xsell", "t1", 0.37, "TP")]


def test_stop_loss_fires(monkeypatch) -> None:
    store = _FakeStore({"bh_max_edge_lead42_xsell": [_trade(entry_price=0.30)]})
    event = _event([_bucket("26°C", yes_bid=0.14)])  # 0.467x <= 0.5
    result = _run_sweep(monkeypatch, store, [_xsell_profile()], event)
    assert result.closed == 1
    assert store.closed[0][3] == "SL"


def test_holds_between_thresholds(monkeypatch) -> None:
    store = _FakeStore({"bh_max_edge_lead42_xsell": [_trade(entry_price=0.30)]})
    event = _event([_bucket("26°C", yes_bid=0.40)])  # 1.33x -> hold
    result = _run_sweep(monkeypatch, store, [_xsell_profile()], event)
    assert result.closed == 0
    assert store.closed == []


def test_no_side_take_profit(monkeypatch) -> None:
    # NO bought at 0.20, mark = 1 - yes_ask = 0.30 -> 1.5x -> TP.
    store = _FakeStore({"bh_max_edge_lead42_xsell": [_trade(entry_price=0.20, side="NO")]})
    event = _event([_bucket("26°C", yes_ask=0.70)])
    result = _run_sweep(monkeypatch, store, [_xsell_profile()], event)
    assert result.closed == 1
    assert store.closed[0][3] == "TP"


def test_hold_to_settle_profiles_untouched(monkeypatch) -> None:
    store = _FakeStore({"bh_max_edge_lead42": [_trade(entry_price=0.24)]})
    event = _event([_bucket("26°C", yes_bid=0.99)])  # would TP if it were xsell
    result = _run_sweep(monkeypatch, store, [_hold_profile()], event)
    assert result.closed == 0
    assert store.closed == []


def test_resolved_event_is_skipped(monkeypatch) -> None:
    store = _FakeStore({"bh_max_edge_lead42_xsell": [_trade(entry_price=0.24)]})
    event = _event([_bucket("26°C", yes_bid=0.99, resolved=True)])
    result = _run_sweep(monkeypatch, store, [_xsell_profile()], event)
    assert result.closed == 0


def test_fast_poll_flag_in_resolution_window(monkeypatch) -> None:
    store = _FakeStore({"bh_max_edge_lead42_xsell": [_trade(entry_price=0.30)]})
    event = _event([_bucket("26°C", yes_bid=0.40)], settlement_date=date(2026, 6, 18))
    in_window = datetime(2026, 6, 18, 12, 0, tzinfo=timezone.utc)
    result = _run_sweep(monkeypatch, store, [_xsell_profile()], event, now=in_window)
    assert result.fast_poll is True
    # Outside the window the flag stays off.
    out = _run_sweep(
        monkeypatch, store, [_xsell_profile()], event,
        now=datetime(2026, 6, 18, 2, 0, tzinfo=timezone.utc),
    )
    assert out.fast_poll is False


def test_no_active_sell_profiles_is_noop(monkeypatch) -> None:
    store = _FakeStore({"bh_max_edge_lead42": [_trade()]})
    result = sweep_active_exits(store, [_hold_profile()])
    assert result.closed == 0 and result.fast_poll is False


def test_poll_interval_is_short() -> None:
    assert ACTIVE_EXIT_POLL_INTERVAL.total_seconds() <= 300


# --------------------------------------------------------------------------- #
# Profile loading of xsell wallets
# --------------------------------------------------------------------------- #
_XSELL_YAML = """
active_profiles: all_twelve
city: london
target_day: tomorrow
lead_gates:
  lead42:
    target_lead_hours: 42
  lead48:
    target_lead_hours: 48
model_strategies:
  - best_historical
trade_strategies:
  - max_edge
  - dist_arb
xsell_wallets:
  exit_policy:
    take_profit_ratio: 1.5
    stop_loss_ratio: 0.5
  models:
    - best_historical
  trade_strategies:
    - max_edge
    - dist_arb
  lead_gates:
    - lead42
    - lead48
"""


def test_load_generates_xsell_wallets(tmp_path: Path) -> None:
    cfg = tmp_path / "profiles.yaml"
    cfg.write_text(_XSELL_YAML, encoding="utf-8")
    profiles = load_paper_profiles(cfg)
    xs = [p for p in profiles if p.exit_policy is not None]
    assert len(xs) == 4  # 1 model x 2 trades x 2 gates
    ids = {p.id for p in profiles}
    for p in xs:
        assert p.id.endswith("_xsell")
        assert p.exit_policy.take_profit_ratio == 1.5
        assert p.id[: -len("_xsell")] in ids  # hold twin exists


def test_load_rejects_xsell_unknown_gate(tmp_path: Path) -> None:
    cfg = tmp_path / "bad.yaml"
    cfg.write_text(
        _XSELL_YAML.replace("    - lead48", "    - lead99"), encoding="utf-8"
    )
    with pytest.raises(ValueError):
        load_paper_profiles(cfg)


# --------------------------------------------------------------------------- #
# Close primitive + no double-settle (Postgres)
# --------------------------------------------------------------------------- #
def _row(label, *, yes_ask, edge_pp, action, side="YES", yes_bid=None) -> AnalysisRow:
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


def _result(rows) -> AnalysisResult:
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


def test_close_position_roundtrip(paper_db_url: str) -> None:
    store = PostgresLedgerStore(database_url=paper_db_url)
    pid = "xsell_close_test"
    opened = store.open_trades_from_analysis(
        pid, _result([_row("26°C", yes_ask=0.20, edge_pp=10.0, action="BUY_YES")]), "evt-9"
    )
    trade = opened[0]
    before = store.read_state(pid)

    closed = store.close_position(pid, trade.trade_id, 0.30, reason="TP")
    assert closed is not None

    after = store.read_state(pid)
    assert after.open_trades == []
    expected = before.balance_usd + round(trade.shares * 0.30, 4)
    assert after.balance_usd == pytest.approx(expected, abs=1e-2)


def test_closed_trade_not_double_settled(paper_db_url: str) -> None:
    store = PostgresLedgerStore(database_url=paper_db_url)
    pid = "xsell_nodouble_test"
    opened = store.open_trades_from_analysis(
        pid, _result([_row("26°C", yes_ask=0.20, edge_pp=10.0, action="BUY_YES")]), "evt-9"
    )
    store.close_position(pid, opened[0].trade_id, 0.30, reason="TP")
    balance_after_close = store.read_state(pid).balance_usd

    # Settling the now-closed event must not write a second payout.
    settled = store.settle_event(pid, "evt-9", "26°C")
    assert settled == []
    assert store.read_state(pid).balance_usd == pytest.approx(balance_after_close)
