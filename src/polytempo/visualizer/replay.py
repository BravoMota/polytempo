"""Replay decision-time analysis for the performance viewer."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path

from polytempo.analysis import AnalysisResult, analyze_event
from polytempo.markets.polymarket import (
    PolymarketEvent,
    fetch_event,
    strip_untradeable_bucket_prices,
)
from polytempo.model.lead_time import gate_target_utc
from polytempo.paper.settlement_reporting import build_event_settlement_dates
from polytempo.profiles.load import DEFAULT_PROFILES_PATH, load_paper_profiles
from polytempo.profiles.models import TradingProfile
from polytempo.visualizer.distribution_data import event_id_for
from polytempo.visualizer.paths import BACKTEST_PROFILES
from polytempo.storage.snapshot_reads import (
    fetch_nearest_clob_snapshot,
    fetch_nearest_open_meteo_forecast,
    fetch_nearest_wunderground_adjusted_tmax,
    hydrate_event_from_clob_snapshot,
    missing_open_meteo_forecast_reason,
    overlay_open_trade_prices,
)
from polytempo.weather.stations import get_station
from polytempo.weather.wu_live_forecast import append_wunderground_snapshot_forecast


def _parse_ts(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


@lru_cache(maxsize=4)
def _profiles_by_id(config_path: str) -> dict[str, TradingProfile]:
    """Paper wallets first; backtest-only ids (whums / whus / es / …) fill the gaps."""
    by_id: dict[str, TradingProfile] = {}
    if BACKTEST_PROFILES.is_file():
        by_id.update({p.id: p for p in load_paper_profiles(BACKTEST_PROFILES)})
    by_id.update({p.id: p for p in load_paper_profiles(Path(config_path))})
    return by_id


def profile_by_id(profile_id: str, *, config_path=DEFAULT_PROFILES_PATH) -> TradingProfile | None:
    return _profiles_by_id(str(Path(config_path))).get(profile_id)


@dataclass(frozen=True)
class ReplaySources:
    fetch_cycle_id: int | None
    fetch_cycle_fetched_at: str | None
    clob_poll_slot: str | None
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class ReplayResult:
    opened_at_utc: str
    snapshot_sources: ReplaySources
    analysis: AnalysisResult
    event: PolymarketEvent


def replay_event_analysis(
    *,
    profile_id: str,
    polymarket_event_id: str,
    opened_at_utc: str,
    lead_hours: float | None,
    model_strategy: str | None,
    open_bucket_prices: dict[str, tuple[float | None, float | None]] | None = None,
    weather_database_url: str | None = None,
) -> tuple[ReplayResult | None, str | None]:
    """Rebuild ``analyze_event`` inputs from snapshots at ``opened_at_utc``.

    Returns ``(result, error_message)``. On partial snapshot coverage the result
    may still be returned with warnings in ``snapshot_sources``.
    """
    profile = profile_by_id(profile_id)
    if profile is None:
        return None, f"unknown profile_id: {profile_id!r}"

    at_utc = _parse_ts(opened_at_utc)
    station = get_station(profile.city)
    event_dates = build_event_settlement_dates({polymarket_event_id})
    settlement_date = event_dates.get(polymarket_event_id)
    if settlement_date is None:
        return None, f"no settlement date for event {polymarket_event_id!r}"

    warnings: list[str] = []
    om_bundle = fetch_nearest_open_meteo_forecast(
        station,
        settlement_date,
        at_utc,
        database_url=weather_database_url,
    )
    if om_bundle is None:
        return None, missing_open_meteo_forecast_reason(
            station,
            settlement_date,
            at_utc,
            database_url=weather_database_url,
        )

    clob_bundle = fetch_nearest_clob_snapshot(
        polymarket_event_id,
        at_utc,
        database_url=weather_database_url,
    )

    try:
        event = strip_untradeable_bucket_prices(fetch_event(polymarket_event_id))
    except Exception as exc:
        return None, f"Gamma fetch failed: {exc}"

    if clob_bundle is not None:
        event = hydrate_event_from_clob_snapshot(event, clob_bundle.rows)
    elif open_bucket_prices:
        event = overlay_open_trade_prices(event, bucket_prices=open_bucket_prices)
        warnings.append(
            "partial market prices (OPEN rows only; no CLOB snapshot at trade time)"
        )
    else:
        return None, "no CLOB snapshot at trade time and no OPEN prices to overlay"

    forecast = om_bundle.forecast
    wu_snapshot = fetch_nearest_wunderground_adjusted_tmax(
        station,
        settlement_date,
        at_utc,
        database_url=weather_database_url,
    )
    if wu_snapshot is not None:
        forecast = append_wunderground_snapshot_forecast(
            forecast,
            predicted_tmax_c=wu_snapshot.predicted_tmax_c,
            as_of_utc=at_utc,
            observed_running_max_c=wu_snapshot.observed_running_max_c,
        )

    resolved_strategy = model_strategy or profile.model_strategy
    analysis = analyze_event(
        forecast,
        event,
        strategy=profile.strategy_instance(),
        lead_hours=lead_hours,
        model_strategy=resolved_strategy,
        station_id=station.icao,
        calibration_stats_path=profile.calibration_stats_path,
    )

    sources = ReplaySources(
        fetch_cycle_id=om_bundle.fetch_cycle_id,
        fetch_cycle_fetched_at=om_bundle.fetched_at_utc,
        clob_poll_slot=clob_bundle.poll_slot_utc if clob_bundle else None,
        warnings=tuple(warnings),
    )
    return (
        ReplayResult(
            opened_at_utc=opened_at_utc,
            snapshot_sources=sources,
            analysis=analysis,
            event=event,
        ),
        None,
    )


def replay_at_settlement(
    *,
    profile_id: str,
    settlement_date: date,
    weather_database_url: str,
    lead_hours: float | None = None,
    model_strategy: str | None = None,
) -> tuple[ReplayResult | None, str | None]:
    """Replay the profile's model at its lead gate for a settlement date.

    Used when the paper ledger has no fills (backtest / research knobs). Event
    id and CLOB/OM snapshots come from the weather DB; there are no OPEN rows.
    """
    profile = profile_by_id(profile_id)
    if profile is None:
        return None, f"unknown profile_id: {profile_id!r}"

    hours = lead_hours
    if hours is None:
        hours = profile.entry_gate.target_lead_hours
    event_id = event_id_for(profile.city, settlement_date, weather_database_url)
    if event_id is None:
        return None, (
            f"no CLOB event for {profile.city} {settlement_date.isoformat()}"
        )
    at_utc = gate_target_utc(settlement_date, hours)
    return replay_event_analysis(
        profile_id=profile_id,
        polymarket_event_id=event_id,
        opened_at_utc=at_utc.isoformat(),
        lead_hours=hours,
        model_strategy=model_strategy or profile.model_strategy,
        weather_database_url=weather_database_url,
    )
