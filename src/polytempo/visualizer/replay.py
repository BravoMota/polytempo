"""Replay decision-time analysis for the performance viewer."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from polytempo.analysis import AnalysisResult, analyze_event
from polytempo.markets.polymarket import (
    PolymarketEvent,
    fetch_event,
    strip_untradeable_bucket_prices,
)
from polytempo.paper.settlement_reporting import build_event_settlement_dates
from polytempo.profiles.load import DEFAULT_PROFILES_PATH, load_paper_profiles
from polytempo.profiles.models import TradingProfile
from polytempo.storage.snapshot_reads import (
    fetch_nearest_clob_snapshot,
    fetch_nearest_open_meteo_forecast,
    hydrate_event_from_clob_snapshot,
    missing_open_meteo_forecast_reason,
    overlay_open_trade_prices,
)
from polytempo.weather.stations import get_station


def _parse_ts(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def profile_by_id(profile_id: str, *, config_path=DEFAULT_PROFILES_PATH) -> TradingProfile | None:
    profiles = load_paper_profiles(config_path)
    return next((p for p in profiles if p.id == profile_id), None)


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

    resolved_strategy = model_strategy or profile.model_strategy
    analysis = analyze_event(
        om_bundle.forecast,
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
