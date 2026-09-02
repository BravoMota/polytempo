"""Tests for the live node tick (fake ledger + real risk/exec, no DB, no network).

Fetchers are injected on ``run_node_tick`` / ``settle_open_events``; the one
non-injectable call, ``analyze_event``, is monkeypatched to a stub. Execution
goes through the real ``DryRunExecutionClient`` and ``RiskEngine`` so sizing,
risk, and fill simulation are exercised for real.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

import pytest

from polytempo.live.config import (
    ExecutionConfig,
    KnobConfig,
    LiveNodeConfig,
    RiskConfig,
    StakeConfig,
)
from polytempo.live.execution import DryRunExecutionClient
from polytempo.live.models import (
    MODE_DRY_RUN,
    SIDE_NO,
    SIDE_YES,
    BookDepth,
    BookLevel,
)
from polytempo.live.node import run_node_tick, settle_open_events
from polytempo.live.risk import RiskEngine
from polytempo.markets.polymarket import PolymarketBucket, PolymarketEvent

# now == exactly 24h before end of target day 2026-07-20 → the lead-24 gate is due.
GATE_NOW = datetime(2026, 7, 20, 0, 0, tzinfo=timezone.utc)
# Midday: no lead gate is at target, but a future gate wake still exists.
NO_GATE_NOW = datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc)
BOOK_TS = "2026-07-20T00:00:00+00:00"


# ── fakes / fixtures ────────────────────────────────────────────────────────────
@dataclass
class FakeLedger:
    """Only the methods node.py calls; every money-shaped call is recorded."""

    open_events: list[str] = field(default_factory=list)
    has_open: bool = False
    halted: bool = False
    open_exposure: float = 0.0
    event_exposure: float = 0.0
    realized_pnl: float = 0.0
    retry_pending: bool = False
    settlement_result: list[str] = field(default_factory=lambda: ["i1"])

    settlements: list[tuple[str, str]] = field(default_factory=list)
    retry_checks: list[tuple[str, str]] = field(default_factory=list)
    intents: list = field(default_factory=list)
    order_states: list[tuple[str, float]] = field(default_factory=list)
    results: list = field(default_factory=list)

    def open_event_ids(self) -> list[str]:
        return list(self.open_events)

    def record_settlement(self, event_id, winning_label, *, ts_utc=None):
        self.settlements.append((event_id, winning_label))
        return self.settlement_result

    def has_open_on_event(self, event_id, profile_id=None) -> bool:
        return self.has_open

    def unfilled_retry_pending(self, profile_id, since_iso) -> bool:
        self.retry_checks.append((profile_id, since_iso))
        return self.retry_pending

    def open_exposure_usd(self) -> float:
        return self.open_exposure

    def event_exposure_usd(self, event_id) -> float:
        return self.event_exposure

    def realized_pnl_on_day(self, day_iso) -> float:
        return self.realized_pnl

    def is_halted(self) -> bool:
        return self.halted

    def record_intent(self, intent) -> None:
        self.intents.append(intent)

    def record_order_state(
        self, intent, order_id, state, filled_shares=0.0, avg_fill_price=None
    ) -> None:
        self.order_states.append((state, filled_shares))

    def record_result(self, result) -> None:
        self.results.append(result)


@dataclass
class _Row:
    action: str
    label: str
    side: str = SIDE_YES
    edge_yes_pp: float | None = 5.0
    edge_no_pp: float | None = 4.0


@dataclass
class _Analysis:
    rows: list
    fallback_reason: str | None = None
    model_strategy: str = "ensemble_spread"


@dataclass
class _Ctx:
    event: PolymarketEvent
    forecast: object = None
    lead_hours: float = 24.0
    date_mismatch: bool = False


def _bucket(label, *, yes_token=None, no_token=None, resolved=False, outcome=None):
    return PolymarketBucket(
        market_id="m",
        label=label,
        yes_bid=None,
        yes_ask=None,
        liquidity_usd=None,
        spread=None,
        rules=None,
        resolved=resolved,
        outcome=outcome,
        yes_token_id=yes_token,
        no_token_id=no_token,
    )


def _event(event_id="evt1", *, buckets):
    return PolymarketEvent(
        event_id=event_id,
        slug="s",
        title="t",
        settlement_date=None,
        buckets=list(buckets),
    )


def _book(token, *, asks, bids=()):
    to_levels = lambda pairs: tuple(BookLevel(p, s) for p, s in pairs)
    return BookDepth(
        token_id=token, bids=to_levels(bids), asks=to_levels(asks), ts_utc=BOOK_TS
    )


def _config(
    tmp_path,
    *,
    model_strategy="ensemble_spread",
    fixed_usd=10.0,
    fraction=None,
    min_depth_usd=50.0,
    min_price=0.02,
    max_price=0.90,
    max_slippage=0.02,
    slippage_edge_fraction=None,
    kill_switch_file=None,
    bankroll_ref_usd=None,
    max_event_exposure_usd=40.0,
    retry_window_hours=0.0,
):
    stake = (
        StakeConfig(fraction=fraction)
        if fraction is not None
        else StakeConfig(fixed_usd=fixed_usd)
    )
    return LiveNodeConfig(
        mode=MODE_DRY_RUN,
        city="london",
        target_day="tomorrow",
        knob=KnobConfig(
            id="knob_test",
            model_strategy=model_strategy,
            trade_strategy="max_edge",
            lead_gates=(24.0,),
        ),
        stake=stake,
        execution=ExecutionConfig(
            max_slippage=max_slippage,
            fill_timeout_seconds=1.0,
            min_depth_usd=min_depth_usd,
            slippage_edge_fraction=slippage_edge_fraction,
            retry_window_hours=retry_window_hours,
        ),
        risk=RiskConfig(
            kill_switch_file=kill_switch_file or (tmp_path / "NO_KILL"),
            max_daily_loss_usd=50.0,
            max_open_exposure_usd=120.0,
            max_event_exposure_usd=max_event_exposure_usd,
            min_price=min_price,
            max_price=max_price,
            max_spread=0.10,
            max_forecast_age_hours=6.0,
            bankroll_ref_usd=bankroll_ref_usd,
        ),
    )


def _ctx_fn(ctx):
    def fetch(city, target_date, *, now):
        return ctx

    return fetch


def _run_tick(
    config,
    ledger,
    *,
    fetch_context_fn,
    now=GATE_NOW,
    books=None,
    starting_balance_usd=0.0,
    client=None,
):
    books = books or {}
    client = client or DryRunExecutionClient(
        lambda tid: books.get(tid), starting_balance_usd=starting_balance_usd
    )
    risk = RiskEngine(config.risk)
    return run_node_tick(
        config,
        ledger,
        client,
        risk,
        now=now,
        fetch_context_fn=fetch_context_fn,
        fetch_event_fn=lambda eid: None,
        fetch_book_fn=lambda tokens: {t: books[t] for t in tokens if t in books},
    )


# ── settle_open_events ──────────────────────────────────────────────────────────
def test_settle_records_resolved_event() -> None:
    ledger = FakeLedger(open_events=["evt1"])
    event = _event(
        "evt1",
        buckets=[
            _bucket("20-21C", resolved=True, outcome="YES"),
            _bucket("22-23C", resolved=True, outcome="NO"),
        ],
    )

    settled = settle_open_events(ledger, fetch_event_fn=lambda eid: event)

    assert settled == ["evt1"]
    assert ledger.settlements == [("evt1", "20-21C")]


def test_settle_skips_unresolved_event() -> None:
    ledger = FakeLedger(open_events=["evt1"])
    event = _event("evt1", buckets=[_bucket("20-21C", resolved=False)])

    settled = settle_open_events(ledger, fetch_event_fn=lambda eid: event)

    assert settled == []
    assert ledger.settlements == []


def test_settle_fetch_error_continues_other_events() -> None:
    ledger = FakeLedger(open_events=["bad", "good"])
    good = _event("good", buckets=[_bucket("20-21C", resolved=True, outcome="YES")])

    def fetch(eid):
        if eid == "bad":
            raise RuntimeError("boom")
        return good

    settled = settle_open_events(ledger, fetch_event_fn=fetch)

    assert settled == ["good"]
    assert ledger.settlements == [("good", "20-21C")]


# ── run_node_tick: gating / open path ─────────────────────────────────────────────
def test_no_gate_due_sets_wake_and_no_lines(tmp_path) -> None:
    ledger = FakeLedger()

    def boom(city, target_date, *, now):  # must not be called when no gate is due
        raise AssertionError("fetch_context_fn called with no gate due")

    result = _run_tick(
        _config(tmp_path), ledger, fetch_context_fn=boom, now=NO_GATE_NOW
    )

    assert result.lines == []
    assert result.settled_events == []
    assert result.next_gate_wake is not None
    assert result.halted is False


def test_gate_dedupe_skips_open_event(tmp_path, monkeypatch) -> None:
    ledger = FakeLedger(has_open=True)
    ctx = _Ctx(_event(buckets=[_bucket("20-21C", yes_token="tokYES")]))
    monkeypatch.setattr(
        "polytempo.live.node.analyze_event",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("analyze on dedupe")),
    )

    result = _run_tick(_config(tmp_path), ledger, fetch_context_fn=_ctx_fn(ctx))

    assert any("DEDUPED" in line for line in result.lines)
    assert ledger.intents == []


def test_calibrated_model_fallback_skips(tmp_path, monkeypatch) -> None:
    ledger = FakeLedger()
    ctx = _Ctx(_event(buckets=[_bucket("20-21C", yes_token="tokYES")]))
    analysis = _Analysis(
        rows=[_Row("BUY_YES", "20-21C")],
        fallback_reason="no calibration row",
        model_strategy="best_historical",
    )
    monkeypatch.setattr("polytempo.live.node.analyze_event", lambda *a, **k: analysis)

    result = _run_tick(
        _config(tmp_path, model_strategy="best_historical"),
        ledger,
        fetch_context_fn=_ctx_fn(ctx),
    )

    assert any(
        "MODEL_FALLBACK_SKIP" in line and "no calibration row" in line
        for line in result.lines
    )
    assert ledger.intents == []


def test_no_buy_rows(tmp_path, monkeypatch) -> None:
    ledger = FakeLedger()
    ctx = _Ctx(_event(buckets=[_bucket("20-21C", yes_token="tokYES")]))
    analysis = _Analysis(rows=[_Row("HOLD", "20-21C")])
    monkeypatch.setattr("polytempo.live.node.analyze_event", lambda *a, **k: analysis)

    result = _run_tick(_config(tmp_path), ledger, fetch_context_fn=_ctx_fn(ctx))

    assert any("NO_BUY_ROWS" in line for line in result.lines)
    assert ledger.intents == []


def test_missing_token_id_skips(tmp_path, monkeypatch) -> None:
    ledger = FakeLedger()
    # Bucket exists but carries no YES token id for the BUY_YES row.
    ctx = _Ctx(_event(buckets=[_bucket("20-21C", yes_token=None)]))
    analysis = _Analysis(rows=[_Row("BUY_YES", "20-21C")])
    monkeypatch.setattr("polytempo.live.node.analyze_event", lambda *a, **k: analysis)

    result = _run_tick(_config(tmp_path), ledger, fetch_context_fn=_ctx_fn(ctx))

    assert any("no YES token id" in line for line in result.lines)
    assert ledger.intents == []


def test_no_order_book_skips(tmp_path, monkeypatch) -> None:
    ledger = FakeLedger()
    ctx = _Ctx(_event(buckets=[_bucket("20-21C", yes_token="tokYES")]))
    analysis = _Analysis(rows=[_Row("BUY_YES", "20-21C")])
    monkeypatch.setattr("polytempo.live.node.analyze_event", lambda *a, **k: analysis)

    # No book supplied for tokYES → fetch_book_fn returns nothing for it.
    result = _run_tick(_config(tmp_path), ledger, fetch_context_fn=_ctx_fn(ctx), books={})

    assert any("no order book" in line for line in result.lines)
    assert ledger.intents == []


def test_unsizeable_book_skips(tmp_path, monkeypatch) -> None:
    ledger = FakeLedger()
    ctx = _Ctx(_event(buckets=[_bucket("20-21C", yes_token="tokYES")]))
    analysis = _Analysis(rows=[_Row("BUY_YES", "20-21C")])
    monkeypatch.setattr("polytempo.live.node.analyze_event", lambda *a, **k: analysis)

    # Bids only, no asks → best_ask is None → size_buy returns None.
    books = {"tokYES": _book("tokYES", asks=[], bids=[(0.48, 200)])}
    result = _run_tick(_config(tmp_path), ledger, fetch_context_fn=_ctx_fn(ctx), books=books)

    assert any("unsizeable" in line for line in result.lines)
    assert ledger.intents == []


# ── post-gate retry window ──────────────────────────────────────────────────────
# 30 min past the lead-24 gate: lead is 23.5h, inside a 2h retry window.
RETRY_NOW = datetime(2026, 7, 20, 0, 30, tzinfo=timezone.utc)


def _retry_setup(monkeypatch):
    ctx = _Ctx(_event(buckets=[_bucket("20-21C", yes_token="tokYES")]), lead_hours=23.5)
    analysis = _Analysis(rows=[_Row("BUY_YES", "20-21C")])
    monkeypatch.setattr("polytempo.live.node.analyze_event", lambda *a, **k: analysis)
    books = {"tokYES": _book("tokYES", asks=[(0.50, 200)], bids=[(0.48, 200)])}
    return ctx, books


def test_retry_window_reopens_gate_when_nothing_filled(tmp_path, monkeypatch) -> None:
    """A gate attempt that filled nothing is re-tried on a later tick."""
    ledger = FakeLedger(retry_pending=True)
    ctx, books = _retry_setup(monkeypatch)

    result = _run_tick(
        _config(tmp_path, min_depth_usd=0.0, retry_window_hours=2.0),
        ledger,
        fetch_context_fn=_ctx_fn(ctx),
        books=books,
        now=RETRY_NOW,
    )

    assert len(ledger.intents) == 1
    assert any("FILLED" in line for line in result.lines)
    # Retry eligibility is asked per profile, scoped to the window.
    assert ledger.retry_checks and ledger.retry_checks[0][0].endswith("_lead24")


def test_retry_window_does_not_refire_after_a_fill(tmp_path, monkeypatch) -> None:
    """Nothing to retry once the gate attempt filled: no second position."""
    ledger = FakeLedger(retry_pending=False)
    ctx, books = _retry_setup(monkeypatch)

    result = _run_tick(
        _config(tmp_path, min_depth_usd=0.0, retry_window_hours=2.0),
        ledger,
        fetch_context_fn=_ctx_fn(ctx),
        books=books,
        now=RETRY_NOW,
    )

    assert ledger.intents == []
    assert result.lines == []


def test_retry_window_off_by_default(tmp_path, monkeypatch) -> None:
    """retry_window_hours=0 keeps the old one-shot-at-the-gate behaviour."""
    ledger = FakeLedger(retry_pending=True)
    ctx, books = _retry_setup(monkeypatch)

    result = _run_tick(
        _config(tmp_path, min_depth_usd=0.0),
        ledger,
        fetch_context_fn=_ctx_fn(ctx),
        books=books,
        now=RETRY_NOW,
    )

    assert ledger.intents == []
    assert result.lines == []
    assert ledger.retry_checks == []


def test_retry_window_expires(tmp_path, monkeypatch) -> None:
    """Past the window the gate stays shut even with a retryable attempt."""
    ledger = FakeLedger(retry_pending=True)
    ctx, books = _retry_setup(monkeypatch)

    result = _run_tick(
        _config(tmp_path, min_depth_usd=0.0, retry_window_hours=2.0),
        ledger,
        fetch_context_fn=_ctx_fn(ctx),
        books=books,
        now=datetime(2026, 7, 20, 2, 30, tzinfo=timezone.utc),  # lead 21.5h
    )

    assert ledger.intents == []
    assert result.lines == []


def test_edge_fraction_walk_reaches_next_ask(tmp_path, monkeypatch) -> None:
    ledger = FakeLedger()
    ctx = _Ctx(_event(buckets=[_bucket("20-21C", yes_token="tokYES")]))
    analysis = _Analysis(rows=[_Row("BUY_YES", "20-21C", edge_yes_pp=20.0)])
    monkeypatch.setattr("polytempo.live.node.analyze_event", lambda *a, **k: analysis)

    # $5 at the touch, rest 5¢ up. Flat 2¢ cannot reach 0.55; 25% of 20¢ edge can.
    books = {
        "tokYES": _book("tokYES", asks=[(0.50, 10), (0.55, 200)], bids=[(0.48, 200)])
    }
    result = _run_tick(
        _config(
            tmp_path,
            min_depth_usd=0.0,
            max_slippage=0.08,
            slippage_edge_fraction=0.25,
        ),
        ledger,
        fetch_context_fn=_ctx_fn(ctx),
        books=books,
    )

    assert len(ledger.intents) == 1
    assert ledger.intents[0].limit_price == pytest.approx(0.55)
    assert ledger.results[0].state == "FILLED"
    assert any("FILLED" in line for line in result.lines)


def test_risk_deny_per_order_continues(tmp_path, monkeypatch) -> None:
    ledger = FakeLedger()
    ctx = _Ctx(_event(buckets=[_bucket("20-21C", yes_token="tokYES")]))
    analysis = _Analysis(rows=[_Row("BUY_YES", "20-21C")])
    monkeypatch.setattr("polytempo.live.node.analyze_event", lambda *a, **k: analysis)

    # Sizes fine but limit price (0.05) is below min_price (0.10) → per-order deny.
    books = {"tokYES": _book("tokYES", asks=[(0.05, 2000)], bids=[(0.03, 2000)])}
    result = _run_tick(
        _config(tmp_path, min_price=0.10),
        ledger,
        fetch_context_fn=_ctx_fn(ctx),
        books=books,
    )

    assert any(
        "RISK_DENY" in line and "price out of band" in line for line in result.lines
    )
    assert result.halted is False
    assert ledger.intents == []


def test_risk_hard_stop_halts_tick(tmp_path, monkeypatch) -> None:
    ledger = FakeLedger()
    ctx = _Ctx(
        _event(
            buckets=[
                _bucket("20-21C", yes_token="tokYES"),
                _bucket("22-23C", no_token="tokNO"),
            ]
        )
    )
    analysis = _Analysis(
        rows=[_Row("BUY_YES", "20-21C"), _Row("BUY_NO", "22-23C", side=SIDE_NO)]
    )
    monkeypatch.setattr("polytempo.live.node.analyze_event", lambda *a, **k: analysis)

    kill = tmp_path / "KILL"
    kill.write_text("halt")
    books = {
        "tokYES": _book("tokYES", asks=[(0.50, 200)], bids=[(0.48, 200)]),
        "tokNO": _book("tokNO", asks=[(0.50, 200)], bids=[(0.48, 200)]),
    }
    result = _run_tick(
        _config(tmp_path, kill_switch_file=kill),
        ledger,
        fetch_context_fn=_ctx_fn(ctx),
        books=books,
    )

    denies = [line for line in result.lines if "RISK_DENY" in line]
    assert result.halted is True
    assert len(denies) == 1  # stopped after the first row; the second never ran
    assert "kill switch" in denies[0]
    assert ledger.intents == []


def test_happy_path_fills_and_journals(tmp_path, monkeypatch) -> None:
    ledger = FakeLedger()
    ctx = _Ctx(_event(buckets=[_bucket("20-21C", yes_token="tokYES")]))
    analysis = _Analysis(rows=[_Row("BUY_YES", "20-21C")])
    monkeypatch.setattr("polytempo.live.node.analyze_event", lambda *a, **k: analysis)

    # 200 shares resting at 0.50; $10 stake buys 20 shares, fully filled.
    books = {"tokYES": _book("tokYES", asks=[(0.50, 200)], bids=[(0.48, 200)])}
    result = _run_tick(_config(tmp_path), ledger, fetch_context_fn=_ctx_fn(ctx), books=books)

    assert len(ledger.intents) == 1
    assert len(ledger.results) == 1
    assert ledger.results[0].state == "FILLED"
    assert ledger.results[0].filled_shares == 20.0
    fill_line = [line for line in result.lines if "FILLED" in line]
    assert len(fill_line) == 1
    assert "20.00/20.00sh" in fill_line[0]
    assert result.halted is False


def test_date_mismatch_skips(tmp_path, monkeypatch) -> None:
    ledger = FakeLedger()
    ctx = _Ctx(
        _event(buckets=[_bucket("20-21C", yes_token="tokYES")]), date_mismatch=True
    )
    monkeypatch.setattr(
        "polytempo.live.node.analyze_event",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("analyze on mismatch")),
    )

    result = _run_tick(_config(tmp_path), ledger, fetch_context_fn=_ctx_fn(ctx))

    assert any("settlement date mismatch" in line for line in result.lines)
    assert ledger.intents == []


def test_context_lookup_skipped_and_other_error_lined(tmp_path) -> None:
    # LookupError (no event listed) → silently skipped, no lines.
    ledger = FakeLedger()

    def lookup_miss(city, target_date, *, now):
        raise LookupError("no event")

    result = _run_tick(_config(tmp_path), ledger, fetch_context_fn=lookup_miss)
    assert result.lines == []

    # Any other exception → one ERROR line, tick still returns.
    def boom(city, target_date, *, now):
        raise RuntimeError("gamma down")

    result = _run_tick(_config(tmp_path), FakeLedger(), fetch_context_fn=boom)
    assert len(result.lines) == 1
    assert "ERROR" in result.lines[0] and "gamma down" in result.lines[0]


def test_fraction_of_mocked_balance_sizes_two_dollars(tmp_path, monkeypatch) -> None:
    ledger = FakeLedger()
    ctx = _Ctx(_event(buckets=[_bucket("20-21C", yes_token="tokYES")]))
    analysis = _Analysis(rows=[_Row("BUY_YES", "20-21C")])
    monkeypatch.setattr("polytempo.live.node.analyze_event", lambda *a, **k: analysis)

    books = {"tokYES": _book("tokYES", asks=[(0.50, 200)], bids=[(0.48, 200)])}
    result = _run_tick(
        _config(tmp_path, fraction=0.04),
        ledger,
        fetch_context_fn=_ctx_fn(ctx),
        books=books,
        starting_balance_usd=50.0,
    )

    assert len(ledger.intents) == 1
    assert ledger.results[0].state == "FILLED"
    assert ledger.results[0].filled_shares == 4.0
    assert any("4.00/4.00sh" in line for line in result.lines)


def test_fraction_skips_when_balance_is_none(tmp_path, monkeypatch) -> None:
    ledger = FakeLedger()
    ctx = _Ctx(_event(buckets=[_bucket("20-21C", yes_token="tokYES")]))
    analysis = _Analysis(rows=[_Row("BUY_YES", "20-21C")])
    monkeypatch.setattr("polytempo.live.node.analyze_event", lambda *a, **k: analysis)

    class _NoBalance(DryRunExecutionClient):
        def collateral_balance_usd(self) -> float | None:
            return None

    books = {"tokYES": _book("tokYES", asks=[(0.50, 200)], bids=[(0.48, 200)])}
    result = _run_tick(
        _config(tmp_path, fraction=0.04),
        ledger,
        fetch_context_fn=_ctx_fn(ctx),
        books=books,
        client=_NoBalance(lambda tid: books.get(tid)),
    )

    assert any("no collateral balance" in line for line in result.lines)
    assert ledger.intents == []


def test_bankroll_ref_scales_event_exposure_cap(tmp_path, monkeypatch) -> None:
    ledger = FakeLedger(event_exposure=6.0)
    ctx = _Ctx(_event(buckets=[_bucket("20-21C", yes_token="tokYES")]))
    analysis = _Analysis(rows=[_Row("BUY_YES", "20-21C")])
    monkeypatch.setattr("polytempo.live.node.analyze_event", lambda *a, **k: analysis)

    books = {"tokYES": _book("tokYES", asks=[(0.50, 200)], bids=[(0.48, 200)])}
    result = _run_tick(
        _config(
            tmp_path,
            fraction=0.04,
            bankroll_ref_usd=50.0,
            max_event_exposure_usd=5.0,
        ),
        ledger,
        fetch_context_fn=_ctx_fn(ctx),
        books=books,
        starting_balance_usd=100.0,
    )

    assert len(ledger.intents) == 1
    assert ledger.results[0].state == "FILLED"
    assert not any("RISK_DENY" in line for line in result.lines)


def test_whums_model_fallback_skips(tmp_path, monkeypatch) -> None:
    ledger = FakeLedger()
    ctx = _Ctx(_event(buckets=[_bucket("20-21C", yes_token="tokYES")]))
    analysis = _Analysis(
        rows=[_Row("BUY_YES", "20-21C")],
        fallback_reason="no calibration row",
        model_strategy="weighted_historical_market_sigma",
    )
    monkeypatch.setattr("polytempo.live.node.analyze_event", lambda *a, **k: analysis)

    result = _run_tick(
        _config(tmp_path, model_strategy="weighted_historical_market_sigma"),
        ledger,
        fetch_context_fn=_ctx_fn(ctx),
    )

    assert any(
        "MODEL_FALLBACK_SKIP" in line and "no calibration row" in line
        for line in result.lines
    )
    assert ledger.intents == []
