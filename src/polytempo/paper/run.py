"""Paper trading pipeline orchestrator (profile-based, PostgreSQL-backed)."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from polytempo.analysis import (
    MODEL_STRATEGY_BEST_HISTORICAL,
    MODEL_STRATEGY_BEST_HISTORICAL_UPDATED,
    MODEL_STRATEGY_WEIGHTED_HISTORICAL_UPDATED,
    AnalysisResult,
    analyze_event,
)
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
from polytempo.weather.calibration_stats_csv import (
    calibration_stat_row_to_dict,
    effective_lead_hours_anchor,
    lookup_lead_hours_for_calibration,
    verified_init_lead_hours_by_model,
    weighted_contribution_to_dict,
)

logger = logging.getLogger(__name__)

_BEST_HISTORICAL_STRATEGIES = frozenset(
    {
        MODEL_STRATEGY_BEST_HISTORICAL,
        MODEL_STRATEGY_BEST_HISTORICAL_UPDATED,
        MODEL_STRATEGY_WEIGHTED_HISTORICAL_UPDATED,
    }
)


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
    audit_metadata = _analysis_audit_metadata(
        profile,
        analysis,
        forecast,
        lead_hours=lead_hours,
        station_id=_station_id_for_city(profile.city),
    )
    if (
        profile.model_strategy in _BEST_HISTORICAL_STRATEGIES
        and analysis.fallback_reason is not None
    ):
        logger.warning(
            "profile %s skipping open: %s -> %s (%s)",
            profile.id,
            profile.model_strategy,
            analysis.model_strategy,
            analysis.fallback_reason,
        )
        if isinstance(store, PostgresLedgerStore):
            store.log_gate_skip(
                profile.id,
                lead_hours=lead_hours,
                reason="model_strategy_fallback",
                metadata=audit_metadata,
            )
        return ProfileRunResult(
            profile_id=profile.id,
            action="MODEL_STRATEGY_SKIP",
            lead_hours=lead_hours,
            gate_passed=True,
            analysis=analysis,
        )

    opened = store.open_trades_from_analysis(
        profile.id,
        analysis,
        event.event_id,
        lead_hours=lead_hours,
        audit_metadata=audit_metadata,
    )
    action = "OPENED" if opened else "SKIP"
    if isinstance(store, PostgresLedgerStore):
        store.log_tick(
            profile.id,
            polymarket_event_id=event.event_id,
            lead_hours=lead_hours,
            model_strategy=analysis.model_strategy,
            trade_action=action,
            metadata=audit_metadata,
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


def _analysis_audit_metadata(
    profile: TradingProfile,
    analysis: AnalysisResult,
    forecast: ForecastValues,
    *,
    lead_hours: float | None,
    station_id: str,
) -> dict[str, object]:
    meta: dict[str, object] = {
        "requested_model_strategy": profile.model_strategy,
        "resolved_model_strategy": analysis.model_strategy,
        "fallback_reason": analysis.fallback_reason,
        "selected_model": analysis.selected_model,
        "calibration_sigma_source": analysis.calibration_sigma_source,
        "calibration_stats_path": profile.calibration_stats_path.name,
        "distribution_mean_c": analysis.distribution_mean_c,
        "distribution_sigma_c": analysis.distribution_sigma_c,
    }
    if lead_hours is not None:
        meta["wall_lead_hours"] = lead_hours

    if analysis.distribution_params is not None:
        meta["distribution_params"] = analysis.distribution_params

    if analysis.weighted_contributions:
        meta["weighted_contributions"] = [
            weighted_contribution_to_dict(c) for c in analysis.weighted_contributions
        ]

    row = analysis.calibration_row
    if row is not None:
        predicted_tmax_c: float | None = None
        if forecast.models and row.model in forecast.models:
            index = forecast.models.index(row.model)
            if index < len(forecast.values_c):
                predicted_tmax_c = forecast.values_c[index]

        init_leads = verified_init_lead_hours_by_model(forecast)
        anchor = effective_lead_hours_anchor(row)
        lookup_lead_hours = lookup_lead_hours_for_calibration(
            model=row.model,
            lead_hours_anchor=anchor,
            wall_lead_hours=lead_hours if lead_hours is not None else 0.0,
            init_lead_hours_by_model=init_leads,
        )
        if lookup_lead_hours is None and lead_hours is not None:
            lookup_lead_hours = lead_hours

        meta["calibration_selection"] = {
            "row": calibration_stat_row_to_dict(row),
            "predicted_tmax_c": predicted_tmax_c,
            "lookup_lead_hours": lookup_lead_hours,
        }

    return meta
