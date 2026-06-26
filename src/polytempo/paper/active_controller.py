"""Active edge-following position controller.

At each lead-gate tick (the hold wallets' decision clock — lead hours
12/15/18/24/30/36/42/48/54), re-price each active wallet's open legs against a
*fresh* forecast + *live* order book and either scale in (edge still positive)
or flatten the leg (edge flipped negative). One wallet per ``model x trade
strategy``; lead hours are the decision clock, not a partition.

Entry (which leg/side to open) is the base trade strategy's job; once a leg is
held, management follows that leg's own edge: add a ramp ticket while it stays
``>= add_edge_pp`` (capped per leg), flatten when it drops below
``exit_edge_pp``. Re-entry is allowed if the edge returns. Fills are taker
(YES adds at ask / sells at bid; NO adds at ``1-bid`` / sells at ``1-ask``);
legs with a wide spread or thin book are skipped this tick.

Only events in the discovery window (today..today+2, lead in ``(0, 54]``) are
managed; positions on already-settling events are left to the settle sweep.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone

from polytempo.analysis import AnalysisResult, AnalysisRow, analyze_event
from polytempo.markets.polymarket import (
    MarketPrice,
    is_event_resolved,
    to_market_prices,
)
from polytempo.paper.ledger import OpenTrade, PostgresLedgerStore, stake_fraction
from polytempo.paper.market_context import (
    MarketContext,
    fetch_market_context,
    preview_settlement_dates,
)
from polytempo.profiles.models import ActiveParams, TradingProfile
from polytempo.weather.stations import get_station

logger = logging.getLogger(__name__)

ACTIVE_WINDOW_MAX_LEAD_HOURS = 54.0
DISCOVERY_DAYS = 3
MIN_TICKET_USD = 0.50


@dataclass(frozen=True)
class ActiveDecision:
    """One would-be (or executed) action for a wallet's leg this sweep."""

    profile_id: str
    event_id: str
    bucket_label: str
    side: str
    action: str  # OPEN | ADD | FLATTEN | HOLD | SKIP_STALE
    edge_pp: float | None
    stake_usd: float | None = None
    exit_price: float | None = None
    reason: str = ""


@dataclass(frozen=True)
class ActiveSweepResult:
    evaluated: int = 0
    opened: int = 0
    added: int = 0
    flattened: int = 0
    skipped: int = 0
    decisions: list[ActiveDecision] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"active: {self.opened} opened, {self.added} added, "
            f"{self.flattened} flattened, {self.skipped} skipped "
            f"(evaluated {self.evaluated})"
        )


def manage_active_wallets(
    store: PostgresLedgerStore,
    profiles: list[TradingProfile],
    *,
    now: datetime | None = None,
    events_limit: int = 20,
    dry_run: bool = False,
) -> ActiveSweepResult:
    """Run one active-management sweep over the active wallets in ``profiles``."""
    now = now if now is not None else datetime.now(timezone.utc)
    active = [p for p in profiles if p.active_params is not None]
    if not active:
        return ActiveSweepResult()

    contexts = _fetch_active_contexts(active, now=now, events_limit=events_limit)
    if not contexts:
        return ActiveSweepResult()

    analysis_cache: dict[tuple, AnalysisResult] = {}
    decisions: list[ActiveDecision] = []
    opened = added = flattened = skipped = evaluated = 0

    for profile in active:
        params = profile.active_params
        assert params is not None  # narrowed by the active filter
        state = store.read_state(profile.id)
        balance = state.balance_usd
        bankroll = state.balance_usd  # cap reference, fixed for this sweep
        open_by_event: dict[str, list[OpenTrade]] = defaultdict(list)
        for trade in state.open_trades:
            open_by_event[trade.event_id].append(trade)

        for ctx in contexts:
            event = ctx.event
            if is_event_resolved(event):
                continue
            evaluated += 1
            analysis = _cached_analysis(analysis_cache, profile, ctx)
            mp_by_label = {mp.label: mp for mp in to_market_prices(event)}
            row_by_label = {row.label: row for row in analysis.rows}
            legs = _legs_for_event(open_by_event.get(event.event_id, []))

            # 1) Manage held legs: add while edge persists, flatten when it dies.
            for (label, side), trades in legs.items():
                row = row_by_label.get(label)
                mp = mp_by_label.get(label)
                if row is None or _is_stale(mp, params):
                    skipped += 1
                    decisions.append(
                        _decision(profile, event.event_id, label, side,
                                  "SKIP_STALE", None, reason="stale/illiquid")
                    )
                    continue
                edge = _leg_edge_pp(row, side)
                if edge is None:
                    skipped += 1
                    continue
                exposure = sum(t.stake_usd for t in trades)

                if edge < params.exit_edge_pp:
                    exit_price = _exit_price(row, side)
                    if exit_price is None:
                        skipped += 1
                        continue
                    reason = _flatten_reason(trades, exit_price)
                    if not dry_run:
                        store.flatten_leg(
                            profile.id, event.event_id, label, side,
                            exit_price, reason=reason,
                        )
                    flattened += 1
                    decisions.append(
                        _decision(profile, event.event_id, label, side,
                                  "FLATTEN", edge, exit_price=exit_price, reason=reason)
                    )
                    continue

                if edge >= params.add_edge_pp:
                    stake = _add_stake(edge, bankroll, exposure, balance, params)
                    if stake is None:
                        decisions.append(
                            _decision(profile, event.event_id, label, side,
                                      "HOLD", edge, reason="at cap / no balance")
                        )
                        continue
                    entry = _entry_price(row, side)
                    if entry is None or entry <= 0:
                        skipped += 1
                        continue
                    if not dry_run:
                        store.add_position(
                            profile.id, event.event_id,
                            bucket_label=label, side=side, stake=stake,
                            entry_price=entry, edge_pp=edge,
                            yes_bid=row.yes_bid, yes_ask=row.yes_ask,
                            lead_hours=ctx.lead_hours,
                            model_strategy=analysis.model_strategy,
                            trade_action="ACTIVE_ADD",
                        )
                    balance -= stake
                    added += 1
                    decisions.append(
                        _decision(profile, event.event_id, label, side,
                                  "ADD", edge, stake_usd=stake)
                    )
                else:
                    decisions.append(
                        _decision(profile, event.event_id, label, side,
                                  "HOLD", edge, reason="below add floor")
                    )

            # 2) Open new legs the base strategy currently favours.
            for row in analysis.rows:
                if row.action not in ("BUY_YES", "BUY_NO"):
                    continue
                leg = (row.label, row.side)
                if leg in legs:
                    continue  # already held → handled above
                if row.edge_yes_pp is None or row.edge_yes_pp < params.add_edge_pp:
                    continue
                mp = mp_by_label.get(row.label)
                if _is_stale(mp, params):
                    skipped += 1
                    continue
                stake = _add_stake(row.edge_yes_pp, bankroll, 0.0, balance, params)
                if stake is None:
                    continue
                entry = _entry_price(row, row.side)
                if entry is None or entry <= 0:
                    continue
                if not dry_run:
                    store.add_position(
                        profile.id, event.event_id,
                        bucket_label=row.label, side=row.side, stake=stake,
                        entry_price=entry, edge_pp=row.edge_yes_pp,
                        yes_bid=row.yes_bid, yes_ask=row.yes_ask,
                        lead_hours=ctx.lead_hours,
                        model_strategy=analysis.model_strategy,
                        trade_action="ACTIVE_OPEN",
                    )
                balance -= stake
                opened += 1
                decisions.append(
                    _decision(profile, event.event_id, row.label, row.side,
                              "OPEN", row.edge_yes_pp, stake_usd=stake)
                )

    return ActiveSweepResult(
        evaluated=evaluated,
        opened=opened,
        added=added,
        flattened=flattened,
        skipped=skipped,
        decisions=decisions,
    )


def _fetch_active_contexts(
    active: list[TradingProfile],
    *,
    now: datetime,
    events_limit: int,
) -> list[MarketContext]:
    cities = sorted({p.city for p in active})
    contexts: list[MarketContext] = []
    for city in cities:
        for target_date in preview_settlement_dates(now=now, days=DISCOVERY_DAYS):
            try:
                ctx = fetch_market_context(
                    city, target_date, events_limit=events_limit, now=now
                )
            except LookupError:
                continue
            except Exception:
                logger.exception("active context fetch failed for %s %s", city, target_date)
                continue
            if ctx.date_mismatch:
                continue
            if not 0.0 < ctx.lead_hours <= ACTIVE_WINDOW_MAX_LEAD_HOURS:
                continue
            contexts.append(ctx)
    return contexts


def _cached_analysis(
    cache: dict[tuple, AnalysisResult],
    profile: TradingProfile,
    ctx: MarketContext,
) -> AnalysisResult:
    key = (
        ctx.event.event_id,
        profile.model_strategy,
        str(profile.calibration_stats_path),
        profile.trade_strategy,
    )
    if key not in cache:
        cache[key] = analyze_event(
            ctx.forecast,
            ctx.event,
            strategy=profile.strategy_instance(),
            lead_hours=ctx.lead_hours,
            model_strategy=profile.model_strategy,
            station_id=get_station(profile.city).icao,
            calibration_stats_path=profile.calibration_stats_path,
        )
    return cache[key]


def _legs_for_event(trades: list[OpenTrade]) -> dict[tuple[str, str], list[OpenTrade]]:
    legs: dict[tuple[str, str], list[OpenTrade]] = defaultdict(list)
    for trade in trades:
        legs[(trade.bucket_label, trade.side)].append(trade)
    return legs


def _leg_edge_pp(row: AnalysisRow, side: str) -> float | None:
    """Current edge (pp) of holding ``side`` on this bucket, from fresh inputs."""
    if side == "YES":
        if row.yes_ask is None:
            return None
        return (row.probability - row.yes_ask) * 100.0
    if row.edge_no_pp is not None:
        return row.edge_no_pp
    if row.yes_bid is None:
        return None
    return (row.yes_bid - row.probability) * 100.0


def _entry_price(row: AnalysisRow, side: str) -> float | None:
    if side == "NO":
        return None if row.yes_bid is None else 1.0 - row.yes_bid
    return row.yes_ask


def _exit_price(row: AnalysisRow, side: str) -> float | None:
    if side == "NO":
        return None if row.yes_ask is None else 1.0 - row.yes_ask
    return row.yes_bid


def _is_stale(mp: MarketPrice | None, params: ActiveParams) -> bool:
    if mp is None or mp.yes_bid is None or mp.yes_ask is None:
        return True
    spread = mp.spread if mp.spread is not None else mp.yes_ask - mp.yes_bid
    if spread is None or spread > params.max_spread:
        return True
    if mp.liquidity_usd is None or mp.liquidity_usd < params.min_liquidity_usd:
        return True
    return False


def _add_stake(
    edge_pp: float,
    bankroll: float,
    exposure: float,
    balance: float,
    params: ActiveParams,
) -> float | None:
    """One ramp ticket, capped so leg exposure stays under the per-leg cap and
    within available balance. None when there's no room for a meaningful add."""
    cap = params.max_position_fraction * bankroll
    headroom = min(cap - exposure, balance)
    if headroom < MIN_TICKET_USD:
        return None
    stake = min(stake_fraction(edge_pp) * bankroll, headroom)
    if stake < MIN_TICKET_USD:
        return None
    return round(stake, 2)


def _flatten_reason(trades: list[OpenTrade], exit_price: float) -> str:
    cost = sum(t.stake_usd for t in trades)
    proceeds = sum(t.shares for t in trades) * exit_price
    return "TAKE_PROFIT" if proceeds >= cost else "CUT_LOSS"


def _decision(
    profile: TradingProfile,
    event_id: str,
    label: str,
    side: str,
    action: str,
    edge_pp: float | None,
    *,
    stake_usd: float | None = None,
    exit_price: float | None = None,
    reason: str = "",
) -> ActiveDecision:
    return ActiveDecision(
        profile_id=profile.id,
        event_id=event_id,
        bucket_label=label,
        side=side,
        action=action,
        edge_pp=edge_pp,
        stake_usd=stake_usd,
        exit_price=exit_price,
        reason=reason,
    )
