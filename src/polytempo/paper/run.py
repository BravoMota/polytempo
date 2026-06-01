"""Multi-strategy paper trading orchestrator.

One pipeline run = one event + forecast + N strategies. For each run:

- If the Polymarket event has resolved, settle every open position on it
  per strategy. No new trades are opened on a resolved event.
- Otherwise, run all strategies against the shared model+market output, and
  append OPEN records to each strategy's own JSONL ledger.

A compact, side-by-side row is also appended to ``<base_dir>/runs.jsonl`` so
strategy behavior can be compared across runs without replaying every ledger.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from polytempo.analysis import AnalysisResult, analyze_event_multi
from polytempo.markets.polymarket import (
    PolymarketEvent,
    is_event_resolved,
    winning_label_from_event,
)
from polytempo.model.calibration import CalibrationRule
from polytempo.paper.ledger import (
    DEFAULT_LEDGER_DIR,
    OpenTrade,
    ledger_path_for,
    open_trades_from_analysis,
    read_state,
    settle_event,
)
from polytempo.strategy.base import Strategy
from polytempo.weather.schema import ForecastValues

RUNS_FILENAME = "runs.jsonl"


@dataclass(frozen=True)
class StrategyRunResult:
    """What one strategy did on this run."""

    name: str
    action: str
    opened: list[OpenTrade] = field(default_factory=list)
    settled: list[OpenTrade] = field(default_factory=list)
    analysis: AnalysisResult | None = None


@dataclass(frozen=True)
class RunSummary:
    """Aggregate result of one pipeline run across all strategies."""

    ts: str
    event_id: str
    event_title: str
    resolved: bool
    winning_label: str | None
    strategies: list[StrategyRunResult]
    mode: str = "trade"


def has_open_trades_on_event(
    event_id: str,
    base_dir: Path = DEFAULT_LEDGER_DIR,
) -> bool:
    """True if any per-strategy ledger has an unsettled trade on this event."""
    if not base_dir.exists():
        return False
    for path in base_dir.glob("*.jsonl"):
        if path.name == RUNS_FILENAME:
            continue
        state = read_state(path)
        if any(t.event_id == event_id for t in state.open_trades):
            return True
    return False


def open_event_ids(base_dir: Path = DEFAULT_LEDGER_DIR) -> list[str]:
    """Distinct event ids that still have an open trade in any ledger.

    First-seen order is preserved so a sweep settles oldest-touched events first.
    """
    if not base_dir.exists():
        return []
    seen: dict[str, None] = {}
    for path in sorted(base_dir.glob("*.jsonl")):
        if path.name == RUNS_FILENAME:
            continue
        for trade in read_state(path).open_trades:
            seen.setdefault(trade.event_id, None)
    return list(seen)


def run_pipeline(
    forecast: ForecastValues,
    event: PolymarketEvent,
    strategies: list[Strategy],
    base_dir: Path = DEFAULT_LEDGER_DIR,
    calibration_rule: CalibrationRule | None = None,
    dedupe: bool = False,
    lead_hours: float | None = None,
    model_strategy: str = "ensemble_spread",
    station_id: str | None = None,
) -> RunSummary:
    """Settle if the event is resolved; otherwise open trades per strategy.

    When ``dedupe=True`` and any open trade already exists for ``event.event_id``
    across any per-strategy ledger, no new OPENs are written; each strategy
    reports ``DEDUPED_OPEN_TRADES_EXIST``.
    """
    ts = datetime.now(timezone.utc).isoformat()
    resolved = is_event_resolved(event)
    winning_label = winning_label_from_event(event) if resolved else None

    if resolved:
        results = _settle_all(event, strategies, winning_label, base_dir)
    elif dedupe and has_open_trades_on_event(event.event_id, base_dir):
        results = [
            StrategyRunResult(name=s.name, action="DEDUPED_OPEN_TRADES_EXIST")
            for s in strategies
        ]
    else:
        results = _open_all(
            forecast,
            event,
            strategies,
            base_dir,
            calibration_rule,
            lead_hours,
            model_strategy,
            station_id,
        )

    summary = RunSummary(
        ts=ts,
        event_id=event.event_id,
        event_title=event.title,
        resolved=resolved,
        winning_label=winning_label,
        strategies=results,
        mode="trade",
    )
    _append_runs_log(summary, base_dir)
    return summary


def preview_pipeline(
    forecast: ForecastValues,
    event: PolymarketEvent,
    strategies: list[Strategy],
    base_dir: Path = DEFAULT_LEDGER_DIR,
    calibration_rule: CalibrationRule | None = None,
    lead_hours: float | None = None,
    model_strategy: str = "ensemble_spread",
    station_id: str | None = None,
) -> RunSummary:
    """Run all strategies and append a runs.jsonl snapshot; write no OPENs."""
    from polytempo.analysis import analyze_event_multi

    ts = datetime.now(timezone.utc).isoformat()
    resolved = is_event_resolved(event)
    winning_label = winning_label_from_event(event) if resolved else None

    if resolved:
        results = [
            StrategyRunResult(name=s.name, action="RESOLVED")
            for s in strategies
        ]
    else:
        analyses = analyze_event_multi(
            forecast,
            event,
            strategies=strategies,
            calibration_rule=calibration_rule,
            lead_hours=lead_hours,
            model_strategy=model_strategy,
            station_id=station_id,
        )
        results = [
            StrategyRunResult(
                name=s.name,
                action="PREVIEW",
                analysis=analyses[s.name],
            )
            for s in strategies
        ]

    summary = RunSummary(
        ts=ts,
        event_id=event.event_id,
        event_title=event.title,
        resolved=resolved,
        winning_label=winning_label,
        strategies=results,
        mode="preview",
    )
    _append_runs_log(summary, base_dir)
    return summary


def _settle_all(
    event: PolymarketEvent,
    strategies: list[Strategy],
    winning_label: str | None,
    base_dir: Path,
) -> list[StrategyRunResult]:
    out: list[StrategyRunResult] = []
    for strategy in strategies:
        path = ledger_path_for(strategy.name, base_dir=base_dir)
        if winning_label is None:
            out.append(StrategyRunResult(
                name=strategy.name,
                action="RESOLVED_NO_WINNER",
            ))
            continue
        settled = settle_event(event.event_id, winning_label, path=path)
        out.append(StrategyRunResult(
            name=strategy.name,
            action="SETTLED" if settled else "NOTHING_TO_SETTLE",
            settled=settled,
        ))
    return out


def _open_all(
    forecast: ForecastValues,
    event: PolymarketEvent,
    strategies: list[Strategy],
    base_dir: Path,
    calibration_rule: CalibrationRule | None,
    lead_hours: float | None = None,
    model_strategy: str = "ensemble_spread",
    station_id: str | None = None,
) -> list[StrategyRunResult]:
    analyses = analyze_event_multi(
        forecast,
        event,
        strategies=strategies,
        calibration_rule=calibration_rule,
        lead_hours=lead_hours,
        model_strategy=model_strategy,
        station_id=station_id,
    )
    out: list[StrategyRunResult] = []
    for strategy in strategies:
        analysis = analyses[strategy.name]
        path = ledger_path_for(strategy.name, base_dir=base_dir)
        opened = open_trades_from_analysis(analysis, event_id=event.event_id, path=path)
        out.append(StrategyRunResult(
            name=strategy.name,
            action="OPENED" if opened else "SKIP",
            opened=opened,
            analysis=analysis,
        ))
    return out


def _append_runs_log(summary: RunSummary, base_dir: Path) -> None:
    path = base_dir / RUNS_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "ts": summary.ts,
        "mode": summary.mode,
        "event_id": summary.event_id,
        "event_title": summary.event_title,
        "resolved": summary.resolved,
        "winning_label": summary.winning_label,
        "strategies": {
            s.name: _strategy_to_record(s) for s in summary.strategies
        },
    }
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")


def _strategy_to_record(result: StrategyRunResult) -> dict:
    if result.opened:
        return {
            "action": result.action,
            "trades": [
                {
                    "bucket": t.bucket_label,
                    "side": t.side,
                    "entry_price": t.entry_price,
                    "stake_usd": t.stake_usd,
                    "shares": t.shares,
                    "edge_pp": t.edge_pp,
                }
                for t in result.opened
            ],
        }
    if result.settled:
        return {
            "action": result.action,
            "settled": [
                {
                    "bucket": t.bucket_label,
                    "side": t.side,
                    "stake_usd": t.stake_usd,
                    "shares": t.shares,
                }
                for t in result.settled
            ],
        }
    return {"action": result.action}
