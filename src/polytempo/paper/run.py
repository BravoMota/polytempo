"""Paper trading pipeline orchestrator (profile-based, PostgreSQL-backed)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from polytempo.analysis import AnalysisResult, analyze_event
from polytempo.markets.polymarket import (
    PolymarketEvent,
    is_event_resolved,
    winning_label_from_event,
)
from polytempo.model.calibration import CalibrationRule
from polytempo.model.lead_time import lead_hours_at_target
from polytempo.paper.ledger import LedgerStore, OpenTrade, PostgresLedgerStore
from polytempo.profiles.models import TradingProfile
from polytempo.weather.schema import ForecastValues


@dataclass(frozen=True)
class ProfileRunResult:
    """What one profile did on this run."""

    profile_id: str
    action: str
    lead_hours: float | None = None
    gate_passed: bool | None = None
    opened: list[OpenTrade] = field(default_factory=list)
    settled: list[OpenTrade] = field(default_factory=list)
    analysis: AnalysisResult | None = None


@dataclass(frozen=True)
class RunSummary:
    """Aggregate result of one pipeline tick."""

    ts: str
    event_id: str
    event_title: str
    target_date: str | None
    resolved: bool
    winning_label: str | None
    profiles: list[ProfileRunResult]
    mode: str = "trade"


def open_event_ids(store: LedgerStore) -> list[str]:
    """Distinct event ids with open trades in any profile."""
    from polytempo.storage.paper_postgres import fetch_open_event_ids, get_paper_connection

    if not isinstance(store, PostgresLedgerStore):
        raise TypeError("open_event_ids requires PostgresLedgerStore")
    with get_paper_connection(store.database_url) as conn:
        return fetch_open_event_ids(conn)


def run_profile(
    store: LedgerStore,
    profile: TradingProfile,
    forecast: ForecastValues,
    event: PolymarketEvent,
    *,
    lead_hours: float | None = None,
    calibration_rule: CalibrationRule | None = None,
    dedupe: bool = True,
    enforce_gate: bool = True,
) -> ProfileRunResult:
    """Settle, gate-check, or open trades for a single profile."""
    resolved = is_event_resolved(event)
    winning_label = winning_label_from_event(event) if resolved else None

    if resolved:
        if winning_label is None:
            return ProfileRunResult(
                profile_id=profile.id,
                action="RESOLVED_NO_WINNER",
                lead_hours=lead_hours,
            )
        settled = store.settle_event(profile.id, event.event_id, winning_label)
        return ProfileRunResult(
            profile_id=profile.id,
            action="SETTLED" if settled else "NOTHING_TO_SETTLE",
            lead_hours=lead_hours,
            settled=settled,
        )

    if enforce_gate and lead_hours is not None:
        gate = profile.entry_gate
        if not lead_hours_at_target(
            lead_hours,
            gate.target_lead_hours,
            gate.tolerance_seconds,
        ):
            if isinstance(store, PostgresLedgerStore):
                store.log_gate_skip(
                    profile.id,
                    lead_hours=lead_hours,
                    reason="outside_entry_gate",
                    metadata={
                        "target_lead_hours": gate.target_lead_hours,
                        "tolerance_seconds": gate.tolerance_seconds,
                    },
                )
            return ProfileRunResult(
                profile_id=profile.id,
                action="GATE_SKIP",
                lead_hours=lead_hours,
                gate_passed=False,
            )

    if dedupe and store.has_open_on_event(profile.id, event.event_id):
        return ProfileRunResult(
            profile_id=profile.id,
            action="DEDUPED_OPEN_TRADES_EXIST",
            lead_hours=lead_hours,
            gate_passed=True,
        )

    strategy = profile.strategy_instance()
    analysis = analyze_event(
        forecast,
        event,
        strategy=strategy,
        calibration_rule=calibration_rule,
        lead_hours=lead_hours,
        model_strategy=profile.model_strategy,
        station_id=_station_id_for_city(profile.city),
        calibration_stats_path=profile.calibration_stats_path,
    )
    opened = store.open_trades_from_analysis(
        profile.id,
        analysis,
        event.event_id,
        lead_hours=lead_hours,
        model_strategy=profile.model_strategy,
    )
    action = "OPENED" if opened else "SKIP"
    if isinstance(store, PostgresLedgerStore):
        store.log_tick(
            profile.id,
            polymarket_event_id=event.event_id,
            lead_hours=lead_hours,
            model_strategy=profile.model_strategy,
            trade_action=action,
        )
    return ProfileRunResult(
        profile_id=profile.id,
        action=action,
        lead_hours=lead_hours,
        gate_passed=True,
        opened=opened,
        analysis=analysis,
    )


def run_profiles(
    store: LedgerStore | None,
    profiles: list[TradingProfile],
    forecast: ForecastValues,
    event: PolymarketEvent,
    *,
    lead_hours: float | None = None,
    calibration_rule: CalibrationRule | None = None,
    dedupe: bool = True,
    enforce_gate: bool = True,
    mode: str = "trade",
) -> RunSummary:
    """Run all profiles against shared market context."""
    if mode != "preview" and store is None:
        raise ValueError("store is required when mode is trade")
    ts = datetime.now(timezone.utc).isoformat()
    resolved = is_event_resolved(event)
    winning_label = winning_label_from_event(event) if resolved else None

    results: list[ProfileRunResult] = []
    for profile in profiles:
        if mode == "preview" and not resolved:
            strategy = profile.strategy_instance()
            analysis = analyze_event(
                forecast,
                event,
                strategy=strategy,
                calibration_rule=calibration_rule,
                lead_hours=lead_hours,
                model_strategy=profile.model_strategy,
                station_id=_station_id_for_city(profile.city),
                calibration_stats_path=profile.calibration_stats_path,
            )
            results.append(
                ProfileRunResult(
                    profile_id=profile.id,
                    action="PREVIEW",
                    lead_hours=lead_hours,
                    analysis=analysis,
                )
            )
            continue

        results.append(
            run_profile(
                store,
                profile,
                forecast,
                event,
                lead_hours=lead_hours,
                calibration_rule=calibration_rule,
                dedupe=dedupe,
                enforce_gate=enforce_gate,
            )
        )

    settlement_date = (
        event.settlement_date.isoformat() if event.settlement_date else None
    )
    return RunSummary(
        ts=ts,
        event_id=event.event_id,
        event_title=event.title,
        target_date=settlement_date,
        resolved=resolved,
        winning_label=winning_label,
        profiles=results,
        mode=mode,
    )


def _station_id_for_city(city: str) -> str:
    from polytempo.weather.stations import get_station

    return get_station(city).icao
