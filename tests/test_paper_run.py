"""Tests for the profile-based paper pipeline orchestrator."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from polytempo.markets.polymarket import PolymarketBucket, PolymarketEvent
from polytempo.paper.ledger import PostgresLedgerStore, STARTING_BALANCE_USD
from polytempo.paper.run import open_event_ids, run_profile, run_profiles
from polytempo.profiles.models import EntryGate, TradingProfile
from polytempo.storage.paper_postgres import get_paper_connection
from polytempo.weather.schema import ForecastValues


def _forecast(values_c: list[float]) -> ForecastValues:
    return ForecastValues(
        source="open_meteo",
        latitude=51.5,
        longitude=-0.1,
        target_date=date(2026, 5, 22),
        values_c=values_c,
    )


def _bucket(
    label: str,
    *,
    yes_ask: float | None = 0.30,
    yes_bid: float = 0.25,
    liquidity: float | None = 250.0,
    resolved: bool = False,
    outcome: str | None = None,
) -> PolymarketBucket:
    return PolymarketBucket(
        market_id=f"m-{label}",
        label=label,
        yes_bid=yes_bid,
        yes_ask=yes_ask,
        liquidity_usd=liquidity,
        spread=None,
        rules=None,
        resolved=resolved,
        outcome=outcome,
    )


def _event(buckets: list[PolymarketBucket], *, event_id: str = "evt-1") -> PolymarketEvent:
    return PolymarketEvent(
        event_id=event_id,
        slug="london",
        title="London max temp",
        settlement_date=None,
        buckets=buckets,
    )


def _profile(
    trade: str,
    *,
    profile_id: str | None = None,
    target_lead_hours: float = 0.0,
    tolerance_seconds: float = 999999.0,
) -> TradingProfile:
    pid = profile_id or f"test_{trade}"
    return TradingProfile(
        id=pid,
        model_strategy="ensemble_spread",
        trade_strategy=trade,
        entry_gate=EntryGate(
            target_lead_hours=target_lead_hours,
            tolerance_seconds=tolerance_seconds,
        ),
    )


def _profiles() -> list[TradingProfile]:
    return [_profile("argmax_yes"), _profile("dist_arb"), _profile("mid_band")]


def test_open_run_writes_trades_per_profile(paper_db_url: str) -> None:
    store = PostgresLedgerStore(database_url=paper_db_url)
    forecast = _forecast([24.0])
    event = _event(
        [
            _bucket("23°C", yes_ask=0.30, yes_bid=0.25),
            _bucket("24°C", yes_ask=0.40, yes_bid=0.35),
            _bucket("25°C", yes_ask=0.25, yes_bid=0.20),
        ]
    )

    summary = run_profiles(
        store,
        _profiles(),
        forecast,
        event,
        enforce_gate=False,
    )

    assert summary.resolved is False
    ids = [p.profile_id for p in summary.profiles]
    assert ids == ["test_argmax_yes", "test_dist_arb", "test_mid_band"]


def test_resolved_event_settles(paper_db_url: str) -> None:
    store = PostgresLedgerStore(database_url=paper_db_url)
    forecast = _forecast([24.0])
    open_event = _event([_bucket("24°C", yes_ask=0.30, yes_bid=0.25)])
    run_profiles(store, [_profile("argmax_yes")], forecast, open_event, enforce_gate=False)

    resolved = _event(
        [_bucket("24°C", yes_ask=0.30, yes_bid=0.25, resolved=True, outcome="YES")]
    )
    summary = run_profiles(store, [_profile("argmax_yes")], forecast, resolved, enforce_gate=False)

    assert summary.resolved is True
    assert summary.winning_label == "24°C"
    assert summary.profiles[0].action in ("SETTLED", "NOTHING_TO_SETTLE")
    state = store.read_state("test_argmax_yes")
    assert state.open_trades == []


def test_gate_skip_outside_target(paper_db_url: str) -> None:
    store = PostgresLedgerStore(database_url=paper_db_url)
    forecast = _forecast([24.0])
    event = _event([_bucket("24°C", yes_ask=0.30, yes_bid=0.25)])
    profile = _profile("argmax_yes", target_lead_hours=30.0, tolerance_seconds=90.0)

    result = run_profile(
        store,
        profile,
        forecast,
        event,
        lead_hours=20.0,
        enforce_gate=True,
    )
    assert result.action == "GATE_SKIP"
    assert result.opened == []


def test_dedupe_blocks_second_open_same_profile(paper_db_url: str) -> None:
    store = PostgresLedgerStore(database_url=paper_db_url)
    forecast = _forecast([24.0])
    event = _event([_bucket("24°C", yes_ask=0.30, yes_bid=0.25)])
    profile = _profile("argmax_yes")

    first = run_profile(store, profile, forecast, event, enforce_gate=False)
    assert first.action == "OPENED"

    second = run_profile(store, profile, forecast, event, dedupe=True, enforce_gate=False)
    assert second.action == "DEDUPED_OPEN_TRADES_EXIST"


def test_dedupe_is_per_profile_not_global(paper_db_url: str) -> None:
    store = PostgresLedgerStore(database_url=paper_db_url)
    forecast = _forecast([24.0])
    event = _event([_bucket("24°C", yes_ask=0.30, yes_bid=0.25)])
    p1 = _profile("argmax_yes", profile_id="p1")
    p2 = _profile("dist_arb", profile_id="p2")

    run_profile(store, p1, forecast, event, enforce_gate=False)
    second = run_profile(store, p2, forecast, event, dedupe=True, enforce_gate=False)
    assert second.action in ("OPENED", "SKIP")


def test_open_event_ids(paper_db_url: str) -> None:
    store = PostgresLedgerStore(database_url=paper_db_url)
    assert open_event_ids(store) == []

    forecast = _forecast([24.0])
    event = _event([_bucket("24°C", yes_ask=0.30, yes_bid=0.25)], event_id="evt-a")
    run_profile(store, _profile("argmax_yes"), forecast, event, enforce_gate=False)
    assert open_event_ids(store) == ["evt-a"]


def test_preview_mode_no_opens(paper_db_url: str) -> None:
    store = PostgresLedgerStore(database_url=paper_db_url)
    forecast = _forecast([24.0])
    event = _event([_bucket("24°C", yes_ask=0.30, yes_bid=0.25)])

    summary = run_profiles(
        store,
        [_profile("argmax_yes")],
        forecast,
        event,
        mode="preview",
        enforce_gate=False,
    )
    assert summary.profiles[0].action == "PREVIEW"
    state = store.read_state("test_argmax_yes")
    assert state.balance_usd == STARTING_BALANCE_USD


def test_model_strategy_skip_on_calibration_fallback(
    paper_db_url: str,
    tmp_path: Path,
) -> None:
    store = PostgresLedgerStore(database_url=paper_db_url)
    profile = TradingProfile(
        id="bhu_test",
        model_strategy="best_historical_updated",
        trade_strategy="argmax_yes",
        entry_gate=EntryGate(target_lead_hours=12.0, tolerance_seconds=90.0),
        calibration_stats_path=tmp_path / "missing.csv",
        city="london",
    )
    forecast = ForecastValues(
        source="open_meteo",
        latitude=51.5,
        longitude=-0.1,
        target_date=date(2026, 5, 22),
        values_c=[24.0],
        models=["ukmo_uk_deterministic_2km"],
        init_lead_hours=[12.0],
        model_run_init_utc=["2026-06-01T00:00:00+00:00"],
    )
    event = _event(
        [
            _bucket("23°C", yes_ask=0.30, yes_bid=0.25),
            _bucket("24°C", yes_ask=0.40, yes_bid=0.35),
            _bucket("25°C", yes_ask=0.25, yes_bid=0.20),
        ]
    )

    result = run_profile(
        store,
        profile,
        forecast,
        event,
        lead_hours=12.0,
        enforce_gate=False,
    )

    assert result.action == "MODEL_STRATEGY_SKIP"
    assert result.analysis is not None
    assert result.analysis.fallback_reason == "no_calibration_csv"
    assert result.opened == []
    state = store.read_state("bhu_test")
    assert state.balance_usd == STARTING_BALANCE_USD

    with get_paper_connection(paper_db_url) as conn:
        row = conn.execute(
            """
            SELECT event_type, metadata
            FROM paper_events
            WHERE profile_id = %(pid)s
            ORDER BY id DESC
            LIMIT 1
            """,
            {"pid": "bhu_test"},
        ).fetchone()
    assert row is not None
    assert row["event_type"] == "GATE_SKIP"
    assert row["metadata"]["reason"] == "model_strategy_fallback"
    assert row["metadata"]["requested_model_strategy"] == "best_historical_updated"
    assert row["metadata"]["resolved_model_strategy"] == "ensemble_spread"
