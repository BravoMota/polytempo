"""Paper trading bot core loop (schedule-driven by lead-time gates)."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from polytempo.markets.polymarket import fetch_event, is_event_resolved, winning_label_from_event
from polytempo.model.lead_time import (
    gate_target_utc,
    lead_hours_at_target,
    lead_hours_before_target,
    lead_hours_missed_target,
    lead_hours_to_end_of_target_day,
)
from polytempo.paper.bot_log import format_profile_line, format_preview_report, format_tick_box, PreviewDateSection
from polytempo.paper.ledger import LedgerStore, PostgresLedgerStore
from polytempo.paper.market_context import (
    fetch_market_context,
    preview_settlement_dates,
    settlement_dates_for_profile,
)
from polytempo.paper.run import open_event_ids, run_profile, run_profiles
from polytempo.profiles.load import load_paper_profiles
from polytempo.profiles.models import TradingProfile
from polytempo.storage.paper_postgres import upsert_bot_state

logger = logging.getLogger(__name__)

SETTLE_SWEEP_INTERVAL = timedelta(minutes=15)
GATE_RETRY_INTERVAL = timedelta(seconds=30)
PREVIEW_DAYS = 3


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


def reload_profiles(config_path: Path) -> list[TradingProfile]:
    return [p for p in load_paper_profiles(config_path) if p.enabled]


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
    gate_wake, _ = next_gate_wake_utc(state.profiles, now)
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
    settle_count = settle_resolved_open_events(store, profiles)

    profile_lines: list[str] = []
    no_event_dates: list[date] = []
    gate_retry_at: datetime | None = None
    units = work_units_for_profiles(profiles, now=now)

    for unit in units:
        unit_profiles = [
            p
            for p in profiles
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

    gate_wake, gate_pid = next_gate_wake_utc(profiles, now)
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
                "profile_count": len(profiles),
            },
            ts,
        )
        conn.commit()

    return TickResult(box=box, gate_retry_at=gate_retry_at)
