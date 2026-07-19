"""Tests for the live pre-trade risk gate (pure, no I/O beyond the kill file)."""

from __future__ import annotations

from pathlib import Path

import pytest

from polytempo.live.config import RiskConfig
from polytempo.live.risk import OpenCheckInputs, RiskEngine


def _config(kill_switch_file: Path) -> RiskConfig:
    return RiskConfig(
        kill_switch_file=kill_switch_file,
        max_daily_loss_usd=50.0,
        max_open_exposure_usd=120.0,
        max_event_exposure_usd=40.0,
        min_price=0.02,
        max_price=0.90,
        max_spread=0.10,
        max_forecast_age_hours=6.0,
    )


def _inputs(**overrides: object) -> OpenCheckInputs:
    base = dict(
        stake_usd=10.0,
        limit_price=0.30,
        spread=0.02,
        depth_usd_available=100.0,
        open_exposure_usd=0.0,
        event_exposure_usd=0.0,
        realized_pnl_today_usd=0.0,
        forecast_age_hours=1.0,
        ledger_halted=False,
    )
    base.update(overrides)
    return OpenCheckInputs(**base)  # type: ignore[arg-type]


@pytest.fixture
def kill_file(tmp_path: Path) -> Path:
    return tmp_path / "KILL_LIVE"


def test_allow_all_clear(kill_file: Path) -> None:
    decision = RiskEngine(_config(kill_file)).check_open(_inputs())
    assert decision.allowed is True
    assert decision.reason is None


def test_deny_kill_switch(kill_file: Path) -> None:
    kill_file.write_text("stop", encoding="utf-8")
    decision = RiskEngine(_config(kill_file)).check_open(_inputs())
    assert not decision.allowed
    assert decision.reason == "kill switch"


def test_deny_ledger_halted(kill_file: Path) -> None:
    decision = RiskEngine(_config(kill_file)).check_open(_inputs(ledger_halted=True))
    assert not decision.allowed
    assert decision.reason == "ledger halted"


def test_deny_daily_loss_limit(kill_file: Path) -> None:
    decision = RiskEngine(_config(kill_file)).check_open(
        _inputs(realized_pnl_today_usd=-50.0)
    )
    assert not decision.allowed
    assert decision.reason == "daily loss limit"


def test_deny_price_below_band(kill_file: Path) -> None:
    decision = RiskEngine(_config(kill_file)).check_open(_inputs(limit_price=0.01))
    assert not decision.allowed
    assert decision.reason == "price out of band"


def test_deny_price_above_band(kill_file: Path) -> None:
    decision = RiskEngine(_config(kill_file)).check_open(_inputs(limit_price=0.95))
    assert not decision.allowed
    assert decision.reason == "price out of band"


def test_deny_spread_none(kill_file: Path) -> None:
    decision = RiskEngine(_config(kill_file)).check_open(_inputs(spread=None))
    assert not decision.allowed
    assert decision.reason == "spread too wide"


def test_deny_spread_too_wide(kill_file: Path) -> None:
    decision = RiskEngine(_config(kill_file)).check_open(_inputs(spread=0.20))
    assert not decision.allowed
    assert decision.reason == "spread too wide"


def test_deny_insufficient_depth(kill_file: Path) -> None:
    decision = RiskEngine(_config(kill_file)).check_open(
        _inputs(depth_usd_available=5.0)
    )
    assert not decision.allowed
    assert decision.reason == "insufficient depth"


def test_deny_open_exposure_limit(kill_file: Path) -> None:
    decision = RiskEngine(_config(kill_file)).check_open(
        _inputs(open_exposure_usd=115.0)
    )
    assert not decision.allowed
    assert decision.reason == "open exposure limit"


def test_deny_event_exposure_limit(kill_file: Path) -> None:
    decision = RiskEngine(_config(kill_file)).check_open(
        _inputs(event_exposure_usd=35.0)
    )
    assert not decision.allowed
    assert decision.reason == "event exposure limit"


def test_deny_stale_forecast_none(kill_file: Path) -> None:
    decision = RiskEngine(_config(kill_file)).check_open(
        _inputs(forecast_age_hours=None)
    )
    assert not decision.allowed
    assert decision.reason == "stale forecast"


def test_deny_stale_forecast_too_old(kill_file: Path) -> None:
    decision = RiskEngine(_config(kill_file)).check_open(
        _inputs(forecast_age_hours=10.0)
    )
    assert not decision.allowed
    assert decision.reason == "stale forecast"


def test_kill_switch_takes_priority_over_halt(kill_file: Path) -> None:
    kill_file.write_text("stop", encoding="utf-8")
    decision = RiskEngine(_config(kill_file)).check_open(_inputs(ledger_halted=True))
    assert decision.reason == "kill switch"
