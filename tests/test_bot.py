"""Tests for paper bot scheduling helpers."""

from datetime import date, datetime, timezone

import httpx
import pytest

from polytempo.model.lead_time import gate_target_utc, lead_hours_at_target
from polytempo.paper.bot import (
    GATE_RETRY_INTERVAL,
    next_gate_wake_utc,
    run_tick,
    work_units_for_profiles,
)
from polytempo.paper.ledger import PostgresLedgerStore
from polytempo.profiles.models import EntryGate, TradingProfile


def _profile(*, lead: float, target_day: str = "tomorrow") -> TradingProfile:
    return TradingProfile(
        id=f"bh_dist_arb_lead{int(lead)}",
        model_strategy="best_historical",
        trade_strategy="dist_arb",
        entry_gate=EntryGate(target_lead_hours=lead),
        target_day=target_day,
    )


def test_next_gate_wake_when_before_target() -> None:
    profile = _profile(lead=30)
    now = datetime(2026, 6, 8, 12, 0, tzinfo=timezone.utc)
    wake, pid = next_gate_wake_utc([profile], now)
    assert pid == "bh_dist_arb_lead30"
    assert wake is not None
    assert wake == gate_target_utc(date(2026, 6, 9), 30.0)
    assert wake > now


def test_next_gate_wake_schedules_next_settlement_when_at_target() -> None:
    profile = _profile(lead=30)
    now = datetime(2026, 6, 8, 18, 0, tzinfo=timezone.utc)
    assert lead_hours_at_target(30.0, 30.0) is True
    wake, pid = next_gate_wake_utc([profile], now)
    assert pid == "bh_dist_arb_lead30"
    assert wake == gate_target_utc(date(2026, 6, 10), 30.0)


def test_work_units_include_settlement_day_for_20h_gate() -> None:
    profile = _profile(lead=20)
    now = datetime(2026, 6, 9, 4, 0, tzinfo=timezone.utc)
    units = work_units_for_profiles([profile], now=now)
    assert any(u.target_date == date(2026, 6, 9) for u in units)


def test_work_units_include_today_plus_two_for_54h_gate() -> None:
    profile = _profile(lead=54)
    now = datetime(2026, 6, 7, 12, 0, tzinfo=timezone.utc)
    units = work_units_for_profiles([profile], now=now)
    assert any(u.target_date == date(2026, 6, 9) for u in units)


def _patch_bot_now(monkeypatch: pytest.MonkeyPatch, fixed_now: datetime) -> None:
    import polytempo.paper.bot as bot_module

    original_datetime = bot_module.datetime

    class MockDatetimeModule:
        def __getattr__(self, name: str):
            if name == "now":
                return lambda tz=None: fixed_now
            return getattr(original_datetime, name)

    monkeypatch.setattr(bot_module, "datetime", MockDatetimeModule())


def test_run_tick_gate_retry_on_fetch_failure_at_gate(
    monkeypatch: pytest.MonkeyPatch,
    paper_db_url: str,
) -> None:
    fixed_now = datetime(2026, 6, 8, 9, 0, tzinfo=timezone.utc)
    _patch_bot_now(monkeypatch, fixed_now)
    monkeypatch.setattr(
        "polytempo.paper.bot.settle_resolved_open_events",
        lambda *args, **kwargs: 0,
    )

    def _raise_timeout(*args, **kwargs):
        raise httpx.ReadTimeout("timed out")

    monkeypatch.setattr("polytempo.paper.bot.fetch_market_context", _raise_timeout)

    store = PostgresLedgerStore(database_url=paper_db_url)
    profile = _profile(lead=15, target_day="tomorrow")
    result = run_tick(store, [profile])

    assert result.gate_retry_at == fixed_now + GATE_RETRY_INTERVAL


def test_run_tick_no_gate_retry_when_fetch_fails_outside_gate(
    monkeypatch: pytest.MonkeyPatch,
    paper_db_url: str,
) -> None:
    fixed_now = datetime(2026, 6, 8, 12, 0, tzinfo=timezone.utc)
    _patch_bot_now(monkeypatch, fixed_now)
    monkeypatch.setattr(
        "polytempo.paper.bot.settle_resolved_open_events",
        lambda *args, **kwargs: 0,
    )

    def _raise_timeout(*args, **kwargs):
        raise httpx.ReadTimeout("timed out")

    monkeypatch.setattr("polytempo.paper.bot.fetch_market_context", _raise_timeout)

    store = PostgresLedgerStore(database_url=paper_db_url)
    profile = _profile(lead=15, target_day="tomorrow")
    result = run_tick(store, [profile])

    assert result.gate_retry_at is None
