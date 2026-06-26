"""Paper trading bot core loop (schedule-driven by lead-time gates)."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from polytempo.markets.polymarket import (
    PolymarketBucket,
    PolymarketEvent,
    fetch_event,
    hydrate_prices,
    is_event_resolved,
    winning_label_from_event,
)
from polytempo.model.lead_time import (
    gate_target_utc,
    lead_hours_at_target,
    lead_hours_before_target,
    lead_hours_missed_target,
    lead_hours_to_end_of_target_day,
)
from polytempo.paper.active_controller import manage_active_wallets
from polytempo.paper.bot_log import format_profile_line, format_preview_report, format_tick_box, PreviewDateSection
from polytempo.paper.ledger import LedgerStore, OpenTrade, PostgresLedgerStore
from polytempo.paper.market_context import (
    fetch_market_context,
    preview_settlement_dates,
    settlement_dates_for_profile,
)
from polytempo.paper.run import open_event_ids, run_profile, run_profiles
from polytempo.profiles.load import load_paper_profiles
from polytempo.profiles.models import TradingProfile
from polytempo.storage.paper_postgres import fetch_bot_state, upsert_bot_state

logger = logging.getLogger(__name__)

SETTLE_SWEEP_INTERVAL = timedelta(minutes=15)
GATE_RETRY_INTERVAL = timedelta(seconds=30)
PREVIEW_DAYS = 3

# Active-sell exit monitoring. Resolution-day price convergence is fast (tens of
# cents in under an hour around the afternoon peak), so when an active-sell
# position is open on an event settling today we poll well below the entry-gate
# lead floor at this cadence instead of the 15-minute settle sweep.
ACTIVE_EXIT_POLL_INTERVAL = timedelta(minutes=3)
LONDON_TZ = ZoneInfo("Europe/London")
# Local hour from which the peak window opens (covers ~12:00-14:00 peak + settle).
ACTIVE_EXIT_WINDOW_START_HOUR = 10

# Edge-following active wallets re-evaluate at each lead-gate instant — the same
# decision clock as the hold wallets (lead hours 12/15/18/24/30/36/42/48/54),
# deduped on the gate instant so each tick fires the sweep exactly once.
ACTIVE_SWEEP_STATE_KEY = "active_sweep_instant"
ACTIVE_SWEEP_LOOKBACK_DAYS = 1
ACTIVE_SWEEP_LOOKAHEAD_DAYS = 3


@dataclass
class BotState:
    profiles: list[TradingProfile] = field(default_factory=list)
    last_settle_wake: datetime | None = None
    next_settle_wake: datetime | None = None


@dataclass(frozen=True)
class WorkUnit:
    city: str
    target_date: date


@dataclass(frozen=True)
class TickResult:
    box: str
    gate_retry_at: datetime | None = None
    active_exit_poll_at: datetime | None = None


@dataclass(frozen=True)
class ActiveExitResult:
    """Outcome of one active-sell sweep."""

    closed: int = 0
    fast_poll: bool = False


def reload_profiles(config_path: Path) -> list[TradingProfile]:
    from polytempo.profiles.calibration_ready import filter_profiles_by_calibration

    loaded = [p for p in load_paper_profiles(config_path) if p.enabled]
    enabled, warnings = filter_profiles_by_calibration(loaded)
    for message in warnings:
        logger.warning(message)
    return enabled


def _profile_settlement_dates(profile: TradingProfile, now: datetime) -> list[date]:
    gate = profile.entry_gate
    return settlement_dates_for_profile(
        profile.target_day,
        gate.target_lead_hours,
        now=now,
        tolerance_seconds=gate.tolerance_seconds,
    )


def work_units_for_profiles(
    profiles: list[TradingProfile],
    now: datetime,
) -> list[WorkUnit]:
    seen: set[tuple[str, date]] = set()
    units: list[WorkUnit] = []
    for profile in profiles:
        for target in _profile_settlement_dates(profile, now):
            key = (profile.city, target)
            if key not in seen:
                seen.add(key)
                units.append(WorkUnit(city=profile.city, target_date=target))
    return units


def next_gate_wake_utc(
    profiles: list[TradingProfile],
    now: datetime,
) -> tuple[datetime | None, str | None]:
    """Earliest UTC instant a profile should open (exact target lead hours)."""
    candidates: list[tuple[datetime, str]] = []
    for profile in profiles:
        gate = profile.entry_gate
        for target in _profile_settlement_dates(profile, now):
            lead = lead_hours_to_end_of_target_day(target, now=now)
            if lead_hours_at_target(lead, gate.target_lead_hours, gate.tolerance_seconds):
                continue
            if lead_hours_missed_target(lead, gate.target_lead_hours, gate.tolerance_seconds):
                continue
            wake = gate_target_utc(target, gate.target_lead_hours)
            if wake > now:
                candidates.append((wake, profile.id))
    if not candidates:
        return None, None
    wake, pid = min(candidates, key=lambda x: x[0])
    return wake, pid


def _profile_at_entry_target(
    profile: TradingProfile,
    lead_hours: float | None,
) -> bool:
    if lead_hours is None:
        return False
    gate = profile.entry_gate
    return lead_hours_at_target(lead_hours, gate.target_lead_hours, gate.tolerance_seconds)


def compute_next_wake(
    state: BotState,
    now: datetime,
) -> datetime:
    wakes = [state.next_settle_wake]
    gated = [p for p in state.profiles if p.active_params is None]
    gate_wake, _ = next_gate_wake_utc(gated, now)
    if gate_wake is not None:
        wakes.append(gate_wake)
    valid = [w for w in wakes if w is not None and w > now]
    if not valid:
        return now + SETTLE_SWEEP_INTERVAL
    return min(valid)


def settle_resolved_open_events(store: LedgerStore, profiles: list[TradingProfile]) -> int:
    """Settle every open event that Gamma reports resolved."""
    total = 0
    for eid in open_event_ids(store):
        try:
            event = fetch_event(eid)
        except Exception:
            logger.exception("fetch_event failed for %s", eid)
            continue
        if not is_event_resolved(event):
            continue
        winning_label = winning_label_from_event(event)
        if winning_label is None:
            continue
        for profile in profiles:
            if store.has_open_on_event(profile.id, eid):
                settled = store.settle_event(profile.id, eid, winning_label)
                total += len(settled)
    return total


def _mark_to_market(trade: OpenTrade, bucket: PolymarketBucket) -> float | None:
    """Sellable price for an open position given the live book, or None if unmarkable."""
    if trade.side == "NO":
        return None if bucket.yes_ask is None else 1.0 - bucket.yes_ask
    return bucket.yes_bid


def _bucket_by_label(event: PolymarketEvent, label: str) -> PolymarketBucket | None:
    for bucket in event.buckets:
        if bucket.label == label:
            return bucket
    return None


def _in_active_exit_window(now: datetime, settlement_date: date | None) -> bool:
    """True when ``now`` is on the event's resolution day (London local) in the peak window."""
    if settlement_date is None:
        return False
    local = now.astimezone(LONDON_TZ)
    return local.date() == settlement_date and local.hour >= ACTIVE_EXIT_WINDOW_START_HOUR


def _fetch_live_event(event_id: str) -> PolymarketEvent | None:
    try:
        return hydrate_prices(fetch_event(event_id))
    except Exception:
        logger.exception("active-exit fetch failed for %s", event_id)
        return None


def sweep_active_exits(
    store: LedgerStore,
    profiles: list[TradingProfile],
    *,
    now: datetime | None = None,
) -> ActiveExitResult:
    """Mark active-sell positions to the live book and close any that hit the bracket.

    Acts only on profiles with an ``exit_policy``; closes a position when its live
    mark / entry reaches ``take_profit_ratio`` (TP) or falls to ``stop_loss_ratio``
    (SL). Reports ``fast_poll`` when an active-sell position is still open on an
    event resolving today within the peak window, so the runner polls fast.
    """
    now = now if now is not None else datetime.now(timezone.utc)
    active = [p for p in profiles if p.exit_policy is not None]
    if not active:
        return ActiveExitResult()

    events: dict[str, PolymarketEvent | None] = {}
    closed = 0
    fast_poll = False
    for profile in active:
        policy = profile.exit_policy
        assert policy is not None  # narrowed by the filter above
        for trade in store.read_state(profile.id).open_trades:
            eid = trade.event_id
            if eid not in events:
                events[eid] = _fetch_live_event(eid)
            event = events[eid]
            if event is None or is_event_resolved(event):
                continue
            if _in_active_exit_window(now, event.settlement_date):
                fast_poll = True
            bucket = _bucket_by_label(event, trade.bucket_label)
            if bucket is None or trade.entry_price in (None, 0):
                continue
            mark = _mark_to_market(trade, bucket)
            if mark is None:
                continue
            ratio = mark / trade.entry_price
            if ratio >= policy.take_profit_ratio:
                reason = "TP"
            elif ratio <= policy.stop_loss_ratio:
                reason = "SL"
            else:
                continue
            if store.close_position(profile.id, trade.trade_id, mark, reason=reason):
                closed += 1
    return ActiveExitResult(closed=closed, fast_poll=fast_poll)


def latest_due_gate_instant(
    profiles: list[TradingProfile],
    now: datetime,
) -> datetime | None:
    """Most recent lead-gate instant at/just before ``now``.

    Uses the hold wallets' gate values (lead hours) across the in-window target
    days, so the active sweep shares the hold wallets' decision clock. Returns
    ``None`` if no gate instant has passed yet.
    """
    gate_values = {
        p.entry_gate.target_lead_hours for p in profiles if p.active_params is None
    }
    if not gate_values:
        return None
    start = now.date() - timedelta(days=ACTIVE_SWEEP_LOOKBACK_DAYS)
    span = ACTIVE_SWEEP_LOOKBACK_DAYS + ACTIVE_SWEEP_LOOKAHEAD_DAYS + 1
    passed: list[datetime] = []
    for offset in range(span):
        target = start + timedelta(days=offset)
        for lead in gate_values:
            instant = gate_target_utc(target, lead)
            if instant <= now:
                passed.append(instant)
    return max(passed) if passed else None


def run_active_sweep(
    store: PostgresLedgerStore,
    profiles: list[TradingProfile],
    *,
    now: datetime,
):
    """Run the active-wallet sweep once per lead-gate instant; else None.

    Fires when a new lead-gate instant has passed since the last sweep (caught at
    the gate wake, or within the next 15-min settle sweep), deduped on the instant.
    """
    active = [p for p in profiles if p.active_params is not None]
    if not active:
        return None
    instant = latest_due_gate_instant(profiles, now)
    if instant is None:
        return None
    key = instant.isoformat()
    from polytempo.storage.paper_postgres import get_paper_connection

    with get_paper_connection(store.database_url) as conn:
        last = fetch_bot_state(conn, ACTIVE_SWEEP_STATE_KEY)
    if last is not None and last.get("instant") == key:
        return None
    try:
        result = manage_active_wallets(store, active, now=now)
    except Exception:
        logger.exception("active sweep failed")
        return None
    with get_paper_connection(store.database_url) as conn:
        upsert_bot_state(conn, ACTIVE_SWEEP_STATE_KEY, {"instant": key}, now.isoformat())
        conn.commit()
    return result


def run_preview(
    profiles: list[TradingProfile],
    *,
    events_limit: int = 20,
    preview_days: int = PREVIEW_DAYS,
    now: datetime | None = None,
) -> str:
    """Dry-run snapshot for today..today+N-1. No DB writes, no gate enforcement."""
    now = now if now is not None else datetime.now(timezone.utc)
    city = profiles[0].city if profiles else "london"
    profiles_by_id = {p.id: p for p in profiles}
    sections: list[PreviewDateSection] = []

    for target_date in preview_settlement_dates(now=now, days=preview_days):
        try:
            ctx = fetch_market_context(
                city,
                target_date,
                events_limit=events_limit,
                now=now,
            )
        except LookupError as exc:
            sections.append(
                PreviewDateSection(
                    target_date=target_date,
                    lead_hours=None,
                    missing_reason=str(exc),
                )
            )
            continue
        except Exception:
            logger.exception("preview failed for %s %s", city, target_date)
            sections.append(
                PreviewDateSection(
                    target_date=target_date,
                    lead_hours=None,
                    missing_reason="fetch error",
                )
            )
            continue

        if ctx.date_mismatch:
            sections.append(
                PreviewDateSection(
                    target_date=target_date,
                    lead_hours=ctx.lead_hours,
                    missing_reason="settlement date mismatch",
                )
            )
            continue

        summary = run_profiles(
            None,
            profiles,
            ctx.forecast,
            ctx.event,
            lead_hours=ctx.lead_hours,
            mode="preview",
            enforce_gate=False,
        )
        sections.append(
            PreviewDateSection(
                target_date=target_date,
                lead_hours=ctx.lead_hours,
                summary=summary,
            )
        )

    return format_preview_report(
        now=now,
        sections=sections,
        profiles_by_id=profiles_by_id,
    )


def run_tick(
    store: PostgresLedgerStore,
    profiles: list[TradingProfile],
    *,
    events_limit: int = 20,
    enforce_gate: bool = True,
) -> TickResult:
    """One bot iteration: settle, then attempt opens for gated profiles."""
    now = datetime.now(timezone.utc)
    # Active wallets are managed by the edge controller, not the entry-gate
    # path; keep them out of work-unit discovery and gate-wake scheduling.
    gated_profiles = [p for p in profiles if p.active_params is None]
    settle_count = settle_resolved_open_events(store, profiles)
    exit_result = sweep_active_exits(store, profiles, now=now)
    active_result = run_active_sweep(store, profiles, now=now)

    profile_lines: list[str] = []
    if exit_result.closed:
        profile_lines.append(f"active-sell: closed {exit_result.closed} position(s)")
    if active_result is not None and (
        active_result.opened or active_result.added or active_result.flattened
    ):
        profile_lines.append(active_result.summary())
    no_event_dates: list[date] = []
    gate_retry_at: datetime | None = None
    units = work_units_for_profiles(gated_profiles, now=now)

    for unit in units:
        unit_profiles = [
            p
            for p in gated_profiles
            if p.city == unit.city
            and unit.target_date in _profile_settlement_dates(p, now)
        ]
        try:
            ctx = fetch_market_context(
                unit.city,
                unit.target_date,
                events_limit=events_limit,
                now=now,
            )
        except LookupError:
            no_event_dates.append(unit.target_date)
            continue
        except Exception as exc:
            logger.exception("market context failed for %s %s", unit.city, unit.target_date)
            profile_lines.append(
                f"target={unit.target_date.isoformat()}  ERROR   {exc}"
            )
            lead_hours = lead_hours_to_end_of_target_day(unit.target_date, now=now)
            if any(_profile_at_entry_target(p, lead_hours) for p in unit_profiles):
                retry_at = now + GATE_RETRY_INTERVAL
                if gate_retry_at is None or retry_at < gate_retry_at:
                    gate_retry_at = retry_at
            continue

        if ctx.date_mismatch:
            profile_lines.append(
                f"target={unit.target_date.isoformat()}  SKIP    settlement date mismatch"
            )
            continue

        for profile in unit_profiles:
            if enforce_gate and not _profile_at_entry_target(profile, ctx.lead_hours):
                continue
            result = run_profile(
                store,
                profile,
                ctx.forecast,
                ctx.event,
                lead_hours=ctx.lead_hours,
                dedupe=True,
                enforce_gate=enforce_gate,
            )
            gate_hint = None
            if result.action == "GATE_SKIP":
                gate_hint = f"target={profile.entry_gate.target_lead_hours:.0f}h"
            profile_lines.append(
                format_profile_line(
                    profile.id,
                    result,
                    lead_hours=ctx.lead_hours,
                    gate_label=gate_hint,
                )
            )

    # Skip NO_EVENT noise when this tick had nothing else to report.
    if not profile_lines and no_event_dates:
        pass
    elif no_event_dates and profile_lines:
        skipped = ", ".join(d.isoformat() for d in sorted(set(no_event_dates)))
        profile_lines.append(f"(skipped unlisted dates: {skipped})")

    gate_wake, gate_pid = next_gate_wake_utc(gated_profiles, now)
    next_settle = now + SETTLE_SWEEP_INTERVAL
    box = format_tick_box(
        now=now,
        settle_count=settle_count,
        profile_lines=profile_lines,
        next_settle_wake=next_settle,
        next_gate_wake=gate_wake,
        next_gate_label=gate_pid,
    )

    ts = now.isoformat()
    from polytempo.storage.paper_postgres import get_paper_connection

    with get_paper_connection(store.database_url) as conn:
        upsert_bot_state(
            conn,
            "last_tick",
            {
                "ts": ts,
                "settle_count": settle_count,
                "exit_close_count": exit_result.closed,
                "profile_count": len(profiles),
            },
            ts,
        )
        conn.commit()

    active_exit_poll_at = (
        now + ACTIVE_EXIT_POLL_INTERVAL if exit_result.fast_poll else None
    )
    return TickResult(
        box=box,
        gate_retry_at=gate_retry_at,
        active_exit_poll_at=active_exit_poll_at,
    )
