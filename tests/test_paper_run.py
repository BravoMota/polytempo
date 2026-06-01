"""Tests for the multi-strategy paper pipeline orchestrator."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from polytempo.markets.polymarket import PolymarketBucket, PolymarketEvent
from polytempo.paper.ledger import (
    STARTING_BALANCE_USD,
    ledger_path_for,
    read_state,
)
from polytempo.paper.run import (
    RUNS_FILENAME,
    has_open_trades_on_event,
    open_event_ids,
    preview_pipeline,
    run_pipeline,
)
from polytempo.strategy import ArgmaxYesStrategy, DistArbStrategy, MidBandStrategy
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


def _strategies() -> list:
    return [ArgmaxYesStrategy(), DistArbStrategy(), MidBandStrategy()]


def test_open_run_writes_trades_to_each_strategy_ledger(tmp_path: Path) -> None:
    forecast = _forecast([24.0])
    event = _event(
        [
            _bucket("23°C", yes_ask=0.30, yes_bid=0.25),
            _bucket("24°C", yes_ask=0.40, yes_bid=0.35),
            _bucket("25°C", yes_ask=0.25, yes_bid=0.20),
        ]
    )

    summary = run_pipeline(forecast, event, _strategies(), base_dir=tmp_path)

    assert summary.resolved is False
    assert summary.winning_label is None
    names = [s.name for s in summary.strategies]
    assert names == ["argmax_yes", "dist_arb", "mid_band"]

    for s in summary.strategies:
        path = ledger_path_for(s.name, base_dir=tmp_path)
        assert path.exists() or s.action == "SKIP"


def test_runs_log_appended_with_one_row_per_run(tmp_path: Path) -> None:
    forecast = _forecast([24.0])
    event = _event([_bucket("24°C", yes_ask=0.30, yes_bid=0.25)])

    run_pipeline(forecast, event, _strategies(), base_dir=tmp_path)
    run_pipeline(forecast, event, _strategies(), base_dir=tmp_path)

    runs_path = tmp_path / RUNS_FILENAME
    lines = [json.loads(line) for line in runs_path.read_text().splitlines() if line]
    assert len(lines) == 2
    assert all(row["event_id"] == "evt-1" for row in lines)
    assert set(lines[0]["strategies"].keys()) == {
        "argmax_yes",
        "dist_arb",
        "mid_band",
    }


def test_resolved_event_only_settles_and_does_not_open(tmp_path: Path) -> None:
    forecast = _forecast([24.0])

    open_event = _event(
        [
            _bucket("23°C", yes_ask=0.30, yes_bid=0.25),
            _bucket("24°C", yes_ask=0.40, yes_bid=0.35),
        ]
    )
    run_pipeline(forecast, open_event, _strategies(), base_dir=tmp_path)

    starting_balances = {
        name: read_state(ledger_path_for(name, base_dir=tmp_path)).balance_usd
        for name in ("argmax_yes", "dist_arb", "mid_band")
    }

    resolved_event = _event(
        [
            _bucket("23°C", yes_ask=0.30, yes_bid=0.25, resolved=True, outcome="NO"),
            _bucket(
                "24°C",
                yes_ask=0.40,
                yes_bid=0.35,
                resolved=True,
                outcome="YES",
            ),
        ]
    )
    summary = run_pipeline(forecast, resolved_event, _strategies(), base_dir=tmp_path)

    assert summary.resolved is True
    assert summary.winning_label == "24°C"

    for s in summary.strategies:
        assert s.action in ("SETTLED", "NOTHING_TO_SETTLE")
        assert s.opened == []
        state = read_state(ledger_path_for(s.name, base_dir=tmp_path))
        assert state.open_trades == []
        if s.action == "SETTLED":
            assert state.balance_usd != starting_balances[s.name]


def test_resolved_with_no_winner_records_action(tmp_path: Path) -> None:
    forecast = _forecast([24.0])
    weird_event = _event(
        [
            _bucket("23°C", resolved=True, outcome="NO"),
            _bucket("24°C", resolved=True, outcome="NO"),
        ]
    )

    summary = run_pipeline(forecast, weird_event, _strategies(), base_dir=tmp_path)

    assert summary.resolved is True
    assert summary.winning_label is None
    for s in summary.strategies:
        assert s.action == "RESOLVED_NO_WINNER"


def test_has_open_trades_on_event_false_when_empty(tmp_path: Path) -> None:
    assert has_open_trades_on_event("evt-1", base_dir=tmp_path) is False


def test_has_open_trades_on_event_true_after_open(tmp_path: Path) -> None:
    forecast = _forecast([24.0])
    event = _event([_bucket("24°C", yes_ask=0.30, yes_bid=0.25)])
    run_pipeline(forecast, event, _strategies(), base_dir=tmp_path)
    assert has_open_trades_on_event("evt-1", base_dir=tmp_path) is True
    assert has_open_trades_on_event("evt-other", base_dir=tmp_path) is False


def test_open_event_ids_empty_when_no_ledgers(tmp_path: Path) -> None:
    assert open_event_ids(base_dir=tmp_path) == []


def test_open_event_ids_collects_distinct_open_events(tmp_path: Path) -> None:
    forecast = _forecast([24.0])
    event_a = _event([_bucket("24°C", yes_ask=0.30, yes_bid=0.25)], event_id="evt-a")
    event_b = _event([_bucket("24°C", yes_ask=0.30, yes_bid=0.25)], event_id="evt-b")
    run_pipeline(forecast, event_a, _strategies(), base_dir=tmp_path)
    run_pipeline(forecast, event_b, _strategies(), base_dir=tmp_path)

    assert set(open_event_ids(base_dir=tmp_path)) == {"evt-a", "evt-b"}


def test_open_event_ids_drops_settled_events(tmp_path: Path) -> None:
    forecast = _forecast([24.0])
    event = _event([_bucket("24°C", yes_ask=0.30, yes_bid=0.25)], event_id="evt-a")
    run_pipeline(forecast, event, _strategies(), base_dir=tmp_path)

    resolved = _event(
        [_bucket("24°C", resolved=True, outcome="YES")], event_id="evt-a"
    )
    run_pipeline(forecast, resolved, _strategies(), base_dir=tmp_path)

    assert open_event_ids(base_dir=tmp_path) == []


def test_dedupe_blocks_second_open(tmp_path: Path) -> None:
    forecast = _forecast([24.0])
    event = _event([_bucket("24°C", yes_ask=0.30, yes_bid=0.25)])

    first = run_pipeline(forecast, event, _strategies(), base_dir=tmp_path, dedupe=True)
    assert any(s.action == "OPENED" for s in first.strategies)

    second = run_pipeline(forecast, event, _strategies(), base_dir=tmp_path, dedupe=True)
    for s in second.strategies:
        assert s.action == "DEDUPED_OPEN_TRADES_EXIST"
        assert s.opened == []


def test_preview_pipeline_writes_runs_log_no_opens(tmp_path: Path) -> None:
    forecast = _forecast([24.0])
    event = _event([_bucket("24°C", yes_ask=0.30, yes_bid=0.25)])

    summary = preview_pipeline(forecast, event, _strategies(), base_dir=tmp_path)

    assert summary.mode == "preview"
    for s in summary.strategies:
        assert s.action == "PREVIEW"
        assert s.opened == []
        path = ledger_path_for(s.name, base_dir=tmp_path)
        assert not path.exists()

    runs_path = tmp_path / RUNS_FILENAME
    rows = [json.loads(line) for line in runs_path.read_text().splitlines() if line]
    assert len(rows) == 1
    assert rows[0]["mode"] == "preview"


def test_run_summary_mode_trade_in_runs_log(tmp_path: Path) -> None:
    forecast = _forecast([24.0])
    event = _event([_bucket("24°C", yes_ask=0.30, yes_bid=0.25)])
    run_pipeline(forecast, event, _strategies(), base_dir=tmp_path)
    runs_path = tmp_path / RUNS_FILENAME
    row = json.loads(runs_path.read_text().splitlines()[0])
    assert row["mode"] == "trade"


def test_skip_when_strategy_opens_nothing(tmp_path: Path) -> None:
    forecast = _forecast([24.0])
    # yes_ask=None → no executable edge → SKIP for every strategy
    event = _event([_bucket("24°C", yes_ask=None, yes_bid=0.0, liquidity=250.0)])

    summary = run_pipeline(forecast, event, [ArgmaxYesStrategy()], base_dir=tmp_path)

    [result] = summary.strategies
    assert result.action == "SKIP"
    assert result.opened == []
    state = read_state(ledger_path_for("argmax_yes", base_dir=tmp_path))
    assert state.balance_usd == STARTING_BALANCE_USD
