"""Data assembly for the Distribution Explorer page.

Pure-ish layer over the existing analysis/snapshot stack: given a city, settlement
date, and a snapshot instant, it assembles every distribution overlay the chart
draws. No Streamlit imports here so it stays testable. Gamma event lookups are
memoized so scrubbing the time slider does not re-hit the network per tick.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path

from polytempo.analysis import (
    MODEL_STRATEGIES,
    MODEL_STRATEGY_BEST_HISTORICAL,
    MODEL_STRATEGY_BEST_HISTORICAL_UPDATED,
    analyze_event,
)
from polytempo.markets.polymarket import (
    PolymarketEvent,
    fetch_event,
    fetch_weather_events,
    first_parseable_weather_event,
    is_lowest_temperature_event,
    strip_untradeable_bucket_prices,
    winning_label_from_event,
)
from polytempo.model.lead_time import lead_hours_to_end_of_target_day
from polytempo.storage.postgres import get_connection, resolve_database_url
from polytempo.storage.snapshot_reads import (
    fetch_nearest_clob_snapshot,
    fetch_nearest_open_meteo_forecast,
    fetch_nearest_wunderground_adjusted_tmax,
    hydrate_event_from_clob_snapshot,
)
from polytempo.weather.calibration_storage import WU_FORECAST_MODEL
from polytempo.visualizer.bucket_math import compute_market_implied_summary
from polytempo.weather.calibration_stats_csv import (
    DEFAULT_CALIBRATION_STATS_CSV_PATH,
    DEFAULT_UPDATED_CALIBRATION_STATS_CSV_PATH,
    CalibrationStatRow,
    lookup_lead_hours_for_calibration,
    read_calibration_stats_csv,
    resolve_lead_hours_anchor,
    select_ceiling_row,
    verified_init_lead_hours_by_model,
)
from polytempo.weather.schema import ForecastValues
from polytempo.weather.stations import get_station
from polytempo.weather.wu_live_forecast import append_wunderground_snapshot_forecast

# Mirrors profiles.load._calibration_path_for_strategy (single source of truth for
# which CSV each model strategy reads). Kept inline to avoid importing a private.
_UPDATED_CSV_STRATEGIES = frozenset(
    {
        "best_historical_updated",
        "weighted_historical_updated",
        "weighted_historical_market_sigma",
    }
)
_BEST_HISTORICAL_STRATEGIES = frozenset(
    {MODEL_STRATEGY_BEST_HISTORICAL, MODEL_STRATEGY_BEST_HISTORICAL_UPDATED}
)


def _csv_for_strategy(model_strategy: str) -> Path:
    if model_strategy in _UPDATED_CSV_STRATEGIES:
        return DEFAULT_UPDATED_CALIBRATION_STATS_CSV_PATH
    return DEFAULT_CALIBRATION_STATS_CSV_PATH


def _sigma_from_row(row: CalibrationStatRow) -> tuple[float, str] | None:
    """``(sigma, source)`` preferring error_std_c, falling back to rmse_c."""
    if math.isfinite(row.error_std_c) and row.error_std_c > 0:
        return row.error_std_c, "error_std_c"
    if math.isfinite(row.rmse_c) and row.rmse_c > 0:
        return row.rmse_c, "rmse_c"
    return None


@dataclass(frozen=True)
class ModelDistOverlay:
    """One metadata model's calibrated normal (mean = predicted - bias)."""

    model: str
    mean_c: float
    sigma_c: float
    sigma_source: str
    predicted_c: float
    bias_c: float
    lookup_lead_hours: float
    detail: str | None = None


@dataclass(frozen=True)
class StratDistOverlay:
    """One model-strategy distribution plus its per-bucket probabilities."""

    strategy: str
    mean_c: float
    sigma_c: float
    bucket_probs: list[tuple[str, float]]
    selected_model: str | None = None


@dataclass(frozen=True)
class MarketOverlay:
    """Market-implied per-bucket masses (yes_ask) and discrete moments."""

    bucket_asks: list[tuple[str, float | None]]
    implied_mean_c: float | None
    discrete_std_c: float | None


@dataclass(frozen=True)
class DistributionView:
    """Everything the distribution chart needs for one (city, date, at_utc)."""

    city: str
    settlement_date: date
    at_utc: datetime
    lead_hours: float
    bucket_labels: list[str]
    model_overlays: list[ModelDistOverlay]
    strat_overlays: list[StratDistOverlay]
    market: MarketOverlay | None
    resolved_label: str | None
    warnings: list[str]
    om_fetched_at_utc: str | None
    clob_poll_slot_utc: str | None
    wu_scraped_at_utc: str | None


@lru_cache(maxsize=256)
def _cached_event(event_id: str) -> PolymarketEvent:
    """Memoized Gamma fetch (event metadata is independent of the slider time)."""
    return strip_untradeable_bucket_prices(fetch_event(event_id))


def list_cities(weather_url: str) -> list[str]:
    """Distinct city slugs that have CLOB snapshots."""
    with get_connection(resolve_database_url(override=weather_url)) as conn:
        rows = conn.execute(
            "SELECT DISTINCT city_slug FROM clob_bucket_snapshots ORDER BY city_slug"
        ).fetchall()
    return [str(r["city_slug"]) for r in rows]


def list_resolution_dates(city: str, weather_url: str) -> list[date]:
    """Settlement dates for ``city`` with CLOB snapshots, newest first."""
    with get_connection(resolve_database_url(override=weather_url)) as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT settlement_date
            FROM clob_bucket_snapshots
            WHERE city_slug = %(city)s
            ORDER BY settlement_date DESC
            """,
            {"city": city},
        ).fetchall()
    return [date.fromisoformat(str(r["settlement_date"])[:10]) for r in rows]


def event_id_for(city: str, settlement_date: date, weather_url: str) -> str | None:
    """Resolve the highest-temperature event id for one (city, settlement date).

    Live Gamma discovery is the source of truth so the page self-heals once correct
    CLOB snapshots exist. If discovery fails (offline / API error), fall back to the
    most-sampled stored event id, skipping any lowest-temperature events that earlier
    collector bugs may have persisted.
    """
    try:
        events = fetch_weather_events(end_on_date=settlement_date, city=city)
        found = first_parseable_weather_event(
            events, city=city, settlement_date=settlement_date
        )
        if found is not None:
            return found.event_id
    except Exception:
        pass

    with get_connection(resolve_database_url(override=weather_url)) as conn:
        rows = conn.execute(
            """
            SELECT polymarket_event_id, COUNT(*) AS n
            FROM clob_bucket_snapshots
            WHERE city_slug = %(city)s AND settlement_date = %(date)s
            GROUP BY polymarket_event_id
            ORDER BY n DESC
            """,
            {"city": city, "date": settlement_date.isoformat()},
        ).fetchall()

    for row in rows:
        event_id = str(row["polymarket_event_id"])
        try:
            if is_lowest_temperature_event(_cached_event(event_id)):
                continue
        except Exception:
            pass
        return event_id
    return None


def model_overlays(
    forecast: ForecastValues,
    *,
    station_id: str,
    lead_hours: float,
    calibration_rows: list[CalibrationStatRow],
) -> list[ModelDistOverlay]:
    """Per-model calibrated normals — the full expansion of ``select_best_model``.

    For every live model that has a ceiling calibration row, draw its bias-corrected
    mean and empirical sigma instead of keeping only the lowest-sigma winner.
    """
    if not forecast.models:
        return []
    init_lead_by_model = verified_init_lead_hours_by_model(forecast)
    predicted_by_model = {
        model: forecast.values_c[i]
        for i, model in enumerate(forecast.models)
        if i < len(forecast.values_c)
    }

    overlays: list[ModelDistOverlay] = []
    for model, predicted in predicted_by_model.items():
        anchor = resolve_lead_hours_anchor(
            calibration_rows, station_id=station_id, model=model
        )
        lookup_lead = lookup_lead_hours_for_calibration(
            model=model,
            lead_hours_anchor=anchor,
            wall_lead_hours=lead_hours,
            init_lead_hours_by_model=init_lead_by_model,
        )
        if lookup_lead is None:
            continue
        row = select_ceiling_row(
            calibration_rows, station_id, model, lookup_lead, lead_hours_anchor=anchor
        )
        if row is None:
            continue
        sigma_info = _sigma_from_row(row)
        if sigma_info is None:
            continue
        sigma, source = sigma_info
        overlays.append(
            ModelDistOverlay(
                model=model,
                mean_c=predicted - row.bias_c,
                sigma_c=sigma,
                sigma_source=source,
                predicted_c=predicted,
                bias_c=row.bias_c,
                lookup_lead_hours=lookup_lead,
            )
        )
    return sorted(overlays, key=lambda o: o.model)


def wunderground_model_overlay(
    *,
    predicted_c: float,
    station_id: str,
    lead_hours: float,
    calibration_rows: list[CalibrationStatRow],
    scraped_at_utc: str,
) -> ModelDistOverlay | None:
    """Calibrated WU forecast from ``forecast_snapshots`` (scraped_at lead lookup)."""
    anchor = resolve_lead_hours_anchor(
        calibration_rows, station_id=station_id, model=WU_FORECAST_MODEL
    )
    lookup_lead = lookup_lead_hours_for_calibration(
        model=WU_FORECAST_MODEL,
        lead_hours_anchor=anchor,
        wall_lead_hours=lead_hours,
        init_lead_hours_by_model=None,
    )
    if lookup_lead is None:
        return None
    row = select_ceiling_row(
        calibration_rows,
        station_id,
        WU_FORECAST_MODEL,
        lookup_lead,
        lead_hours_anchor=anchor,
    )
    if row is None:
        return None
    sigma_info = _sigma_from_row(row)
    if sigma_info is None:
        return None
    sigma, source = sigma_info
    return ModelDistOverlay(
        model=WU_FORECAST_MODEL,
        mean_c=predicted_c - row.bias_c,
        sigma_c=sigma,
        sigma_source=source,
        predicted_c=predicted_c,
        bias_c=row.bias_c,
        lookup_lead_hours=lookup_lead,
        detail=(
            f"forecast_snapshots · scrape `{scraped_at_utc}` · "
            f"scraped_at anchor `{anchor}`"
        ),
    )


def strat_overlays(
    forecast: ForecastValues,
    event: PolymarketEvent,
    *,
    station_id: str,
    lead_hours: float,
) -> tuple[list[StratDistOverlay], list[str]]:
    """One distribution per model strategy; returns (overlays, skipped warnings)."""
    overlays: list[StratDistOverlay] = []
    skipped: list[str] = []
    for model_strategy in MODEL_STRATEGIES:
        try:
            result = analyze_event(
                forecast,
                event,
                lead_hours=lead_hours,
                model_strategy=model_strategy,
                station_id=station_id,
                calibration_stats_path=_csv_for_strategy(model_strategy),
            )
        except Exception as exc:  # noqa: BLE001 - surface, do not crash the page
            skipped.append(f"{model_strategy}: {exc}")
            continue
        mean = result.distribution_mean_c
        sigma = result.distribution_sigma_c
        if not (math.isfinite(mean) and math.isfinite(sigma) and sigma > 0):
            reason = result.fallback_reason or "no distribution"
            skipped.append(f"{model_strategy}: {reason}")
            continue
        overlays.append(
            StratDistOverlay(
                strategy=model_strategy,
                mean_c=mean,
                sigma_c=sigma,
                bucket_probs=[(r.label, r.probability) for r in result.rows],
                selected_model=(
                    result.selected_model
                    if model_strategy in _BEST_HISTORICAL_STRATEGIES
                    else None
                ),
            )
        )
    return overlays, skipped


def market_overlay(event: PolymarketEvent) -> MarketOverlay | None:
    """Per-bucket yes_ask masses and their discrete mean/spread."""
    bucket_asks = [(b.label, b.yes_ask) for b in event.buckets]
    if not bucket_asks:
        return None
    labels = [label for label, _ in bucket_asks]
    asks = [ask for _, ask in bucket_asks]
    summary = compute_market_implied_summary(labels, asks)
    return MarketOverlay(
        bucket_asks=bucket_asks,
        implied_mean_c=summary.mean_c if summary else None,
        discrete_std_c=summary.discrete_std_c if summary else None,
    )


def build_distribution_view(
    *,
    city: str,
    settlement_date: date,
    at_utc: datetime,
    weather_url: str,
    calibration_source: Path = DEFAULT_CALIBRATION_STATS_CSV_PATH,
) -> DistributionView:
    """Assemble all overlays for one (city, settlement date, snapshot instant)."""
    station = get_station(city)
    lead_hours = lead_hours_to_end_of_target_day(settlement_date, now=at_utc)
    warnings: list[str] = []

    forecast_bundle = fetch_nearest_open_meteo_forecast(
        station, settlement_date, at_utc, database_url=weather_url
    )
    forecast = forecast_bundle.forecast if forecast_bundle else None
    if forecast is None:
        warnings.append("No Open-Meteo forecast snapshot near this time.")

    wu_snapshot = fetch_nearest_wunderground_adjusted_tmax(
        station, settlement_date, at_utc, database_url=weather_url
    )
    if wu_snapshot is None:
        warnings.append("No Wunderground forecast snapshot near this time.")

    event_id = event_id_for(city, settlement_date, weather_url)
    event = None
    clob_poll_slot: str | None = None
    if event_id is None:
        warnings.append("No CLOB event found for this city/date.")
    else:
        event = _cached_event(event_id)
        clob = fetch_nearest_clob_snapshot(event_id, at_utc, database_url=weather_url)
        if clob is not None:
            event = hydrate_event_from_clob_snapshot(event, clob.rows)
            clob_poll_slot = clob.poll_slot_utc
        else:
            warnings.append("No CLOB price snapshot near this time (using last known).")

    overlays_models: list[ModelDistOverlay] = []
    overlays_strats: list[StratDistOverlay] = []
    market: MarketOverlay | None = None
    resolved_label: str | None = None
    bucket_labels: list[str] = []

    if event is not None:
        bucket_labels = [b.label for b in event.buckets]
        market = market_overlay(event)
        resolved_label = winning_label_from_event(event)

    if forecast is not None:
        calibration_rows = read_calibration_stats_csv(calibration_source)
        om_overlays = model_overlays(
            forecast,
            station_id=station.icao,
            lead_hours=lead_hours,
            calibration_rows=calibration_rows,
        )
        overlays_models = list(om_overlays)
        if not om_overlays:
            warnings.append("No Open-Meteo calibration rows matched this lead.")

    if wu_snapshot is not None:
        wu_cal_rows = read_calibration_stats_csv(DEFAULT_UPDATED_CALIBRATION_STATS_CSV_PATH)
        wu_overlay = wunderground_model_overlay(
            predicted_c=wu_snapshot.predicted_tmax_c,
            station_id=station.icao,
            lead_hours=lead_hours,
            calibration_rows=wu_cal_rows,
            scraped_at_utc=wu_snapshot.scraped_at_utc,
        )
        if wu_overlay is not None:
            overlays_models = sorted([*overlays_models, wu_overlay], key=lambda o: o.model)
        else:
            warnings.append("No WU calibration row matched this lead.")

    if event is not None and forecast is not None:
        forecast_for_strats = forecast
        if wu_snapshot is not None:
            forecast_for_strats = append_wunderground_snapshot_forecast(
                forecast,
                predicted_tmax_c=wu_snapshot.predicted_tmax_c,
                as_of_utc=at_utc,
                observed_running_max_c=wu_snapshot.observed_running_max_c,
            )
        overlays_strats, skipped = strat_overlays(
            forecast_for_strats,
            event,
            station_id=station.icao,
            lead_hours=lead_hours,
        )
        warnings.extend(f"strat skipped — {s}" for s in skipped)

    return DistributionView(
        city=city,
        settlement_date=settlement_date,
        at_utc=at_utc,
        lead_hours=lead_hours,
        bucket_labels=bucket_labels,
        model_overlays=overlays_models,
        strat_overlays=overlays_strats,
        market=market,
        resolved_label=resolved_label,
        warnings=warnings,
        om_fetched_at_utc=forecast_bundle.fetched_at_utc if forecast_bundle else None,
        clob_poll_slot_utc=clob_poll_slot,
        wu_scraped_at_utc=wu_snapshot.scraped_at_utc if wu_snapshot else None,
    )
