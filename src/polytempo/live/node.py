"""Live node tick: settle -> gate -> analyze -> size -> risk -> execute -> journal.

Reuses the paper pipeline for discovery, forecasts, analysis, and gate timing;
everything money-shaped goes through the write-ahead ledger and the risk engine.
Positions are held to settlement (no active resize — the paper data says both
resize variants lose). Fetchers are injectable for tests.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from polytempo.analysis import analyze_event
from polytempo.live.config import (
    LiveNodeConfig,
    resolve_stake_usd,
    scale_risk_config,
)
from polytempo.live.execution import ExecutionClient
from polytempo.live.ledger import LiveLedger
from polytempo.live.marketdata import fetch_book_depth
from polytempo.live.models import (
    SIDE_YES,
    OrderIntent,
)
from polytempo.live.orders import manage_order
from polytempo.live.risk import OpenCheckInputs, RiskEngine
from polytempo.live.sizing import size_buy, walk_cap
from polytempo.markets.polymarket import (
    PolymarketBucket,
    PolymarketEvent,
    fetch_event,
    is_event_resolved,
    winning_label_from_event,
)
from polytempo.model.lead_time import lead_hours_at_target
from polytempo.paper.bot import next_gate_wake_utc, work_units_for_profiles
from polytempo.paper.market_context import fetch_market_context
from polytempo.profiles.models import TradingProfile
from polytempo.weather.stations import get_station

logger = logging.getLogger(__name__)

# Risk reasons that stop the whole tick, not just one order.
_TICK_STOP_REASONS = frozenset({"kill switch", "ledger halted", "daily loss limit"})

# Model strategies that must skip when analysis falls back (mirrors paper/run.py).
_CALIBRATED_MODEL_STRATEGIES = frozenset(
    {
        "best_historical",
        "best_historical_updated",
        "weighted_historical_updated",
        "weighted_historical_market_sigma",
    }
)


@dataclass(frozen=True)
class NodeTickResult:
    """What one node tick did, plus when to wake next for a gate."""

    ts: str
    settled_events: list[str]
    lines: list[str]
    next_gate_wake: datetime | None
    halted: bool = False


@dataclass(frozen=True)
class _OpenOutcome:
    lines: list[str] = field(default_factory=list)
    stop_tick: bool = False


def _bucket_by_label(event: PolymarketEvent, label: str) -> PolymarketBucket | None:
    for bucket in event.buckets:
        if bucket.label == label:
            return bucket
    return None


def _token_for_side(bucket: PolymarketBucket, side: str) -> str | None:
    return bucket.yes_token_id if side == SIDE_YES else bucket.no_token_id


def settle_open_events(
    ledger: LiveLedger,
    *,
    fetch_event_fn=fetch_event,
) -> list[str]:
    """Record settlement for every open event Gamma reports resolved.

    Winning shares still require on-chain redemption — this journals the
    outcome; redemption remains a manual step reported by reconciliation.
    """
    settled: list[str] = []
    for event_id in ledger.open_event_ids():
        try:
            event = fetch_event_fn(event_id)
        except Exception:
            logger.exception("settle fetch failed for %s", event_id)
            continue
        if not is_event_resolved(event):
            continue
        winning_label = winning_label_from_event(event)
        if winning_label is None:
            continue
        if ledger.record_settlement(event.event_id, winning_label):
            settled.append(event.event_id)
    return settled


def _try_open_for_profile(
    config: LiveNodeConfig,
    ledger: LiveLedger,
    client: ExecutionClient,
    risk: RiskEngine,
    profile: TradingProfile,
    ctx,
    *,
    fetch_book_fn,
    now: datetime,
) -> _OpenOutcome:
    """Analyze one gated profile and execute its BUY rows through risk + sizing."""
    event = ctx.event
    lines: list[str] = []

    if ledger.has_open_on_event(event.event_id, profile.id):
        return _OpenOutcome(lines=[f"{profile.id}  DEDUPED event={event.event_id}"])

    analysis = analyze_event(
        ctx.forecast,
        event,
        strategy=profile.strategy_instance(),
        lead_hours=ctx.lead_hours,
        model_strategy=profile.model_strategy,
        station_id=get_station(profile.city).icao,
        calibration_stats_path=profile.calibration_stats_path,
    )
    if (
        profile.model_strategy in _CALIBRATED_MODEL_STRATEGIES
        and analysis.fallback_reason is not None
    ):
        return _OpenOutcome(
            lines=[f"{profile.id}  MODEL_FALLBACK_SKIP {analysis.fallback_reason}"]
        )

    buy_rows = [r for r in analysis.rows if r.action in ("BUY_YES", "BUY_NO")]
    if not buy_rows:
        return _OpenOutcome(lines=[f"{profile.id}  NO_BUY_ROWS"])

    tokens: list[str] = []
    for row in buy_rows:
        bucket = _bucket_by_label(event, row.label)
        token = _token_for_side(bucket, row.side) if bucket is not None else None
        if token:
            tokens.append(token)
    books = fetch_book_fn(tokens) if tokens else {}

    for row in buy_rows:
        bucket = _bucket_by_label(event, row.label)
        token = _token_for_side(bucket, row.side) if bucket is not None else None
        if token is None:
            lines.append(f"{profile.id}  SKIP {row.label}: no {row.side} token id")
            continue
        book = books.get(token)
        if book is None:
            lines.append(f"{profile.id}  SKIP {row.label}: no order book")
            continue

        balance_usd = client.collateral_balance_usd()
        stake_usd = resolve_stake_usd(config.stake, balance_usd)
        if stake_usd is None:
            lines.append(f"{profile.id}  SKIP {row.label}: no collateral balance")
            continue

        edge_pp = row.edge_yes_pp if row.side == SIDE_YES else row.edge_no_pp
        sized = size_buy(
            book,
            stake_usd,
            walk_cap(
                config.execution.max_slippage,
                edge_pp=edge_pp,
                edge_fraction=config.execution.slippage_edge_fraction,
            ),
            config.execution.min_depth_usd,
            config.risk.max_price,
        )
        if sized is None:
            lines.append(f"{profile.id}  SKIP {row.label}: unsizeable (thin/empty book)")
            continue

        tick_risk = (
            risk
            if config.risk.bankroll_ref_usd is None
            else RiskEngine(scale_risk_config(config.risk, balance_usd))
        )
        decision = tick_risk.check_open(
            OpenCheckInputs(
                stake_usd=sized.stake_usd,
                limit_price=sized.limit_price,
                spread=book.spread,
                depth_usd_available=sized.depth_usd_available,
                open_exposure_usd=ledger.open_exposure_usd(),
                event_exposure_usd=ledger.event_exposure_usd(event.event_id),
                realized_pnl_today_usd=ledger.realized_pnl_on_day(
                    now.date().isoformat()
                ),
                # Context is fetched fresh at the gate tick; the age check
                # guards any future cached-forecast path.
                forecast_age_hours=0.0,
                ledger_halted=ledger.is_halted(),
            )
        )
        if not decision.allowed:
            lines.append(f"{profile.id}  RISK_DENY {row.label}: {decision.reason}")
            if decision.reason in _TICK_STOP_REASONS:
                return _OpenOutcome(lines=lines, stop_tick=True)
            continue

        intent = OrderIntent(
            intent_id=uuid.uuid4().hex[:12],
            event_id=event.event_id,
            bucket_label=row.label,
            token_id=token,
            market_side=row.side,
            limit_price=sized.limit_price,
            shares=sized.shares,
            stake_usd=sized.stake_usd,
            knob_id=config.knob.id,
            mode=config.mode,
            ts_utc=now.isoformat(),
            edge_pp=row.edge_yes_pp if row.side == SIDE_YES else row.edge_no_pp,
            lead_hours=ctx.lead_hours,
            metadata={
                "profile_id": profile.id,
                "model_strategy": analysis.model_strategy,
                "trade_strategy": profile.trade_strategy,
            },
        )
        ledger.record_intent(intent)

        def _journal(state: str, status) -> None:
            ledger.record_order_state(
                intent,
                status.order_id if status is not None else None,
                state,
                filled_shares=status.filled_shares if status is not None else 0.0,
                avg_fill_price=status.avg_fill_price if status is not None else None,
            )

        result = manage_order(
            client,
            intent,
            timeout_seconds=config.execution.fill_timeout_seconds,
            on_transition=_journal,
        )
        ledger.record_result(result)
        lines.append(
            f"{profile.id}  {result.state} {row.label} "
            f"{result.filled_shares:.2f}/{intent.shares:.2f}sh "
            f"@{result.avg_fill_price if result.avg_fill_price is not None else '-'} "
            f"limit={intent.limit_price:.2f}"
        )
    return _OpenOutcome(lines=lines)


def run_node_tick(
    config: LiveNodeConfig,
    ledger: LiveLedger,
    client: ExecutionClient,
    risk: RiskEngine,
    *,
    now: datetime | None = None,
    fetch_context_fn=fetch_market_context,
    fetch_event_fn=fetch_event,
    fetch_book_fn=fetch_book_depth,
) -> NodeTickResult:
    """One tick: settle resolved events, then open at any due lead gate."""
    now = now if now is not None else datetime.now(timezone.utc)
    settled = settle_open_events(ledger, fetch_event_fn=fetch_event_fn)

    profiles = config.to_trading_profiles()
    lines: list[str] = []
    halted = False

    units = work_units_for_profiles(profiles, now=now)
    for unit in units:
        due = [
            p
            for p in profiles
            if p.city == unit.city
            and lead_hours_at_target(
                _lead_for(unit.target_date, now),
                p.entry_gate.target_lead_hours,
                p.entry_gate.tolerance_seconds,
            )
        ]
        if not due:
            continue
        try:
            ctx = fetch_context_fn(unit.city, unit.target_date, now=now)
        except LookupError:
            continue
        except Exception as exc:
            logger.exception("market context failed for %s %s", unit.city, unit.target_date)
            lines.append(f"target={unit.target_date.isoformat()}  ERROR {exc}")
            continue
        if ctx.date_mismatch:
            lines.append(
                f"target={unit.target_date.isoformat()}  SKIP settlement date mismatch"
            )
            continue
        for profile in due:
            outcome = _try_open_for_profile(
                config,
                ledger,
                client,
                risk,
                profile,
                ctx,
                fetch_book_fn=fetch_book_fn,
                now=now,
            )
            lines.extend(outcome.lines)
            if outcome.stop_tick:
                halted = True
                break
        if halted:
            break

    gate_wake, _ = next_gate_wake_utc(profiles, now)
    return NodeTickResult(
        ts=now.isoformat(),
        settled_events=settled,
        lines=lines,
        next_gate_wake=gate_wake,
        halted=halted,
    )


def _lead_for(target_date, now: datetime) -> float:
    from polytempo.model.lead_time import lead_hours_to_end_of_target_day

    return lead_hours_to_end_of_target_day(target_date, now=now)
