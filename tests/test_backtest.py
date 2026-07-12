"""Integration tests for the hold-to-settlement backtest harness.

All snapshots are mocked in-memory: no PostgreSQL and no network. These prove
the harness reuses ``run_profile`` and produces matching OPEN / SKIP / SETTLE
outcomes for known fixtures.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

from polytempo.markets.polymarket import PolymarketBucket, PolymarketEvent
from polytempo.model.lead_time import gate_target_utc
from polytempo.paper.backtest import (
    GateInputs,
    InMemoryLedgerStore,
    calibration_path_as_of,
    run_backtest,
)
from polytempo.paper.ledger import STARTING_BALANCE_USD
from polytempo.paper.run import run_profile
from polytempo.profiles.models import EntryGate, TradingProfile
from polytempo.weather.schema import ForecastValues

_TARGET = date(2026, 5, 22)


def _forecast(values_c: list[float]) -> ForecastValues:
    return ForecastValues(
        source="open_meteo_snapshot",
        latitude=51.5,
        longitude=-0.1,
        target_date=_TARGET,
        values_c=values_c,
    )


def _bucket(
    label: str,
    *,
    yes_ask: float | None,
    yes_bid: float,
    resolved: bool = False,
    outcome: str | None = None,
) -> PolymarketBucket:
    return PolymarketBucket(
        market_id=f"m-{label}",
        label=label,
        yes_bid=yes_bid,
        yes_ask=yes_ask,
        liquidity_usd=250.0,
        spread=None,
        rules=None,
        resolved=resolved,
        outcome=outcome,
    )


def _open_event() -> PolymarketEvent:
    return PolymarketEvent(
        event_id="evt-1",
        slug="london",
        title="Highest temperature in London on May 22?",
        settlement_date=_TARGET,
        buckets=[
            _bucket("23°C", yes_ask=0.30, yes_bid=0.25),
            _bucket("24°C", yes_ask=0.40, yes_bid=0.35),
            _bucket("25°C", yes_ask=0.25, yes_bid=0.20),
        ],
    )


def _resolved_event(winner: str) -> PolymarketEvent:
    buckets = [
        _bucket(
            label,
            yes_ask=None,
            yes_bid=0.0,
            resolved=True,
            outcome="YES" if label == winner else "NO",
        )
        for label in ("23°C", "24°C", "25°C")
    ]
    return PolymarketEvent(
        event_id="evt-1",
        slug="london",
        title="Highest temperature in London on May 22?",
        settlement_date=_TARGET,
        buckets=buckets,
    )


def _profile(trade: str = "argmax_yes", *, lead: float = 24.0) -> TradingProfile:
    return TradingProfile(
        id=f"bt_{trade}_lead{int(lead)}",
        model_strategy="ensemble_spread",
        trade_strategy=trade,
        entry_gate=EntryGate(target_lead_hours=lead, tolerance_seconds=90.0),
        city="london",
    )


def _inputs(profile: TradingProfile, winner: str) -> GateInputs:
    at_utc = gate_target_utc(_TARGET, profile.entry_gate.target_lead_hours)
    return GateInputs(
        at_utc=at_utc,
        lead_hours=profile.entry_gate.target_lead_hours,
        forecast=_forecast([24.0]),
        open_event=_open_event(),
        resolved_event=_resolved_event(winner),
        winning_label=winner,
        calibration_path=profile.calibration_stats_path,
    )


def _builder_for(winner: str):
    def _build(profile, event, settlement_date, at_utc):
        return _inputs(profile, winner), None

    return _build


def test_open_and_settle_win() -> None:
    profile = _profile()
    result = run_backtest(
        [profile],
        _TARGET,
        _TARGET,
        event_for_date=lambda c, d: _resolved_event("24°C"),
        input_builder=_builder_for("24°C"),
    )

    assert result.events_processed == 1
    pr = result.profiles[profile.id]
    assert pr.trade_count == 1
    trade = pr.trades[0]
    # argmax_yes buys the highest-probability bucket (24°C, the forecast mode).
    assert trade.bucket_label == "24°C"
    assert trade.side == "YES"
    assert trade.won is True
    assert trade.payout_usd == round(trade.shares, 4)
    assert trade.pnl_usd > 0
    assert pr.win_rate == 1.0
    assert pr.final_balance > STARTING_BALANCE_USD
    assert pr.max_drawdown_usd == 0.0


def test_settle_loss_records_drawdown() -> None:
    profile = _profile()
    result = run_backtest(
        [profile],
        _TARGET,
        _TARGET,
        event_for_date=lambda c, d: _resolved_event("25°C"),
        input_builder=_builder_for("25°C"),
    )

    pr = result.profiles[profile.id]
    assert pr.trade_count == 1
    trade = pr.trades[0]
    assert trade.bucket_label == "24°C"
    assert trade.won is False
    assert trade.payout_usd == 0.0
    assert trade.pnl_usd < 0
    assert pr.final_balance < STARTING_BALANCE_USD
    assert pr.max_drawdown_usd > 0.0


def test_skip_when_no_snapshot() -> None:
    profile = _profile()

    def _no_inputs(prof, event, settlement_date, at_utc):
        return None, "no_clob_snapshot"

    result = run_backtest(
        [profile],
        _TARGET,
        _TARGET,
        event_for_date=lambda c, d: _resolved_event("24°C"),
        input_builder=_no_inputs,
    )

    pr = result.profiles[profile.id]
    assert pr.trade_count == 0
    assert pr.skips == {"no_clob_snapshot": 1}
    assert pr.final_balance == STARTING_BALANCE_USD


def test_no_event_discovered_skips_date() -> None:
    profile = _profile()
    result = run_backtest(
        [profile],
        _TARGET,
        _TARGET,
        event_for_date=lambda c, d: None,
        input_builder=_builder_for("24°C"),
    )
    assert result.events_processed == 0
    assert result.profiles[profile.id].trade_count == 0


def test_open_matches_standalone_run_profile() -> None:
    """The trade the harness opens must equal a direct ``run_profile`` OPEN."""
    profile = _profile()
    inputs = _inputs(profile, "24°C")

    store = InMemoryLedgerStore()
    open_result = run_profile(
        store,
        profile,
        inputs.forecast,
        inputs.open_event,
        lead_hours=inputs.lead_hours,
        dedupe=True,
        enforce_gate=True,
    )
    assert open_result.action == "OPENED"
    assert len(open_result.opened) == 1
    reference = open_result.opened[0]

    harness = run_backtest(
        [profile],
        _TARGET,
        _TARGET,
        event_for_date=lambda c, d: inputs.resolved_event,
        input_builder=_builder_for("24°C"),
    )
    trade = harness.profiles[profile.id].trades[0]
    assert trade.bucket_label == reference.bucket_label
    assert trade.side == reference.side
    assert trade.entry_price == reference.entry_price
    assert trade.stake_usd == reference.stake_usd
    assert trade.shares == reference.shares


def test_model_strategy_skip_matches_run_profile(tmp_path: Path) -> None:
    """A best_historical_updated profile with no calibration CSV skips the open."""
    profile = TradingProfile(
        id="bhu_argmax_yes_lead24",
        model_strategy="best_historical_updated",
        trade_strategy="argmax_yes",
        entry_gate=EntryGate(target_lead_hours=24.0, tolerance_seconds=90.0),
        calibration_stats_path=tmp_path / "missing.csv",
        city="london",
    )
    forecast = ForecastValues(
        source="open_meteo_snapshot",
        latitude=51.5,
        longitude=-0.1,
        target_date=_TARGET,
        values_c=[24.0],
        models=["ukmo_uk_deterministic_2km"],
        init_lead_hours=[24.0],
        model_run_init_utc=["2026-05-21T00:00:00+00:00"],
    )
    at_utc = gate_target_utc(_TARGET, 24.0)
    inputs = GateInputs(
        at_utc=at_utc,
        lead_hours=24.0,
        forecast=forecast,
        open_event=_open_event(),
        resolved_event=_resolved_event("24°C"),
        winning_label="24°C",
        calibration_path=tmp_path / "missing.csv",
    )

    def _build(prof, event, settlement_date, at):
        return inputs, None

    # Reference: standalone run_profile takes the MODEL_STRATEGY_SKIP path.
    ref_result = run_profile(
        InMemoryLedgerStore(),
        profile,
        forecast,
        _open_event(),
        lead_hours=24.0,
        enforce_gate=False,
    )
    assert ref_result.action == "MODEL_STRATEGY_SKIP"

    result = run_backtest(
        [profile],
        _TARGET,
        _TARGET,
        event_for_date=lambda c, d: inputs.resolved_event,
        input_builder=_build,
    )
    pr = result.profiles[profile.id]
    assert pr.trade_count == 0
    assert pr.skips == {"MODEL_STRATEGY_SKIP": 1}
    assert pr.final_balance == STARTING_BALANCE_USD


def test_compounding_across_two_events() -> None:
    """Bankroll compounds across events in settlement order."""
    profile = _profile()
    d1, d2 = date(2026, 5, 22), date(2026, 5, 23)

    def _event(city, settlement_date):
        return _resolved_event("24°C")

    def _build(prof, event, settlement_date, at_utc):
        return _inputs(prof, "24°C"), None

    result = run_backtest(
        [profile], d1, d2, event_for_date=_event, input_builder=_build
    )
    pr = result.profiles[profile.id]
    # One winning trade per day; the second stake rides the compounded balance.
    assert pr.trade_count == 2
    assert len(pr.equity_curve) == 2
    assert pr.trades[1].stake_usd > pr.trades[0].stake_usd
    assert pr.final_balance > STARTING_BALANCE_USD


class _FakeCursor:
    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows

    def fetchall(self) -> list[dict]:
        return self._rows


class _FakeConn:
    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows
        self.params: dict | None = None

    def execute(self, _sql: str, params: dict) -> _FakeCursor:
        self.params = params
        return _FakeCursor(self._rows)


def test_fetch_backtest_event_ids_picks_most_tracked() -> None:
    from polytempo.storage.snapshot_reads import fetch_backtest_event_ids

    # Two events share 2026-06-02; the one with more snapshot rows must win.
    rows = [
        {"settlement_date": "2026-06-02", "polymarket_event_id": "evt-big", "n": 40},
        {"settlement_date": "2026-06-02", "polymarket_event_id": "evt-small", "n": 3},
        {"settlement_date": "2026-06-03", "polymarket_event_id": "evt-c", "n": 12},
    ]
    conn = _FakeConn(rows)
    mapping = fetch_backtest_event_ids(
        "london", date(2026, 6, 1), date(2026, 6, 20), conn=conn
    )
    assert mapping == {
        date(2026, 6, 2): "evt-big",
        date(2026, 6, 3): "evt-c",
    }
    assert conn.params == {
        "city": "london",
        "start": "2026-06-01",
        "end": "2026-06-20",
    }


def test_calibration_path_as_of(tmp_path: Path) -> None:
    base = tmp_path / "calibration_stats_updated.csv"
    base.write_text("current\n", encoding="utf-8")
    historic = tmp_path / "historic"
    historic.mkdir()
    early = historic / "calibration_stats_updated_20260601T000000Z.csv"
    late = historic / "calibration_stats_updated_20260610T000000Z.csv"
    early.write_text("early\n", encoding="utf-8")
    late.write_text("late\n", encoding="utf-8")

    def _at(day: str) -> datetime:
        return datetime.fromisoformat(day).replace(tzinfo=timezone.utc)

    # Before the first archive -> earliest archive (state live until 06-01).
    assert calibration_path_as_of(base, _at("2026-05-30")) == early
    # Between archives -> next archive after the instant (06-10 snapshot).
    assert calibration_path_as_of(base, _at("2026-06-05")) == late
    # After the last archive -> the current live file.
    assert calibration_path_as_of(base, _at("2026-06-20")) == base
