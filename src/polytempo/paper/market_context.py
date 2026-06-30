"""Fetch Polymarket event + Open-Meteo forecast for one target settlement day."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from polytempo.markets.polymarket import (
    PolymarketEvent,
    fetch_event,
    fetch_weather_events,
    first_parseable_weather_event,
    hydrate_prices,
)
from polytempo.model.lead_time import (
    lead_hours_at_target,
    lead_hours_before_target,
    lead_hours_to_end_of_target_day,
)
from polytempo.weather.open_meteo import (
    DEFAULT_MODELS,
    DailyMaxForecast,
    daily_to_forecast_values,
    fetch_open_meteo_live_bundle,
)
from polytempo.weather.schema import ForecastValues
from polytempo.weather.stations import Station, get_station
from polytempo.weather.wu_live_forecast import append_wunderground_forecast

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MarketContext:
    """Resolved inputs for one city + target settlement date."""

    city: str
    station: Station
    target_date: date
    event: PolymarketEvent
    forecast: ForecastValues
    daily: DailyMaxForecast
    lead_hours: float
    date_mismatch: bool


def resolve_target_dates(target_day: str, *, today: date | None = None) -> list[date]:
    """Map profile ``target_day`` to calendar date(s) (legacy relative helper)."""
    today = today if today is not None else date.today()
    key = target_day.strip().lower()
    if key == "today":
        return [today]
    if key == "tomorrow":
        return [today + timedelta(days=1)]
    if key == "both":
        return [today, today + timedelta(days=1)]
    raise ValueError(f"unknown target_day {target_day!r}; expected today, tomorrow, or both")


def preview_settlement_dates(*, now: datetime, days: int = 3) -> list[date]:
    """Calendar settlement dates for dry-run preview: today through today+N-1."""
    if days < 1:
        raise ValueError("days must be >= 1")
    today = now.date()
    return [today + timedelta(days=offset) for offset in range(days)]


def _target_day_base_offsets(target_day: str) -> list[int]:
    key = target_day.strip().lower()
    if key == "today":
        return [0]
    if key == "tomorrow":
        return [1]
    if key == "both":
        return [0, 1]
    raise ValueError(f"unknown target_day {target_day!r}; expected today, tomorrow, or both")


def settlement_date_span_days(target_lead_hours: float) -> int:
    """Calendar days to scan ahead so long lead gates (e.g. 54h) reach today+N markets."""
    return max(1, int((target_lead_hours + 23) // 24) + 1)


def settlement_dates_for_profile(
    profile_target_day: str,
    target_lead_hours: float,
    *,
    now: datetime,
    tolerance_seconds: float = 90.0,
) -> list[date]:
    """Settlement dates to fetch for one profile given its lead gate and ``target_day``.

    Combines ``target_day`` offsets with a lead-dependent horizon, and always
    includes any date whose gate is active or still upcoming at ``now``. This
    covers gates that fire on the settlement day itself (e.g. 20h → 04:00 UTC)
    and long leads (e.g. 54h → today+2 when ``target_day`` is tomorrow).
    """
    today = now.date()
    span = settlement_date_span_days(target_lead_hours)
    dates: set[date] = set()

    for base in _target_day_base_offsets(profile_target_day):
        for extra in range(span + 1):
            dates.add(today + timedelta(days=base + extra))

    for k in range(-1, span + 2):
        candidate = today + timedelta(days=k)
        lead = lead_hours_to_end_of_target_day(candidate, now=now)
        if lead_hours_at_target(lead, target_lead_hours, tolerance_seconds):
            dates.add(candidate)
            continue
        if lead_hours_before_target(lead, target_lead_hours, tolerance_seconds):
            dates.add(candidate)

    return sorted(dates)


def fetch_market_context(
    city: str,
    target_date: date,
    *,
    event_id: str | None = None,
    events_limit: int = 20,
    now: datetime | None = None,
) -> MarketContext:
    """Discover event and forecast for ``target_date``, with CLOB-hydrated prices."""
    station = get_station(city)
    if event_id:
        event = hydrate_prices(fetch_event(event_id.strip()))
    else:
        events = fetch_weather_events(
            limit=events_limit,
            end_on_date=target_date,
            city=city,
        )
        found = first_parseable_weather_event(
            events,
            city=city,
            settlement_date=target_date,
        )
        if found is None:
            raise LookupError(
                f"No weather event for city={city!r}, settlement_date={target_date.isoformat()}"
            )
        event = hydrate_prices(found)

    date_mismatch = (
        event.settlement_date is not None and event.settlement_date != target_date
    )
    fetched_at = now if now is not None else datetime.now(timezone.utc)
    bundle = fetch_open_meteo_live_bundle(
        latitude=station.latitude,
        longitude=station.longitude,
        timezone=station.timezone,
        models=DEFAULT_MODELS,
        target_dates=[target_date],
        fetched_at_utc=fetched_at,
    )
    if bundle.meta_staleness_detected:
        logger.warning(
            "Open-Meteo meta changed during forecast fetch for %s %s",
            city,
            target_date.isoformat(),
        )
    if target_date not in bundle.daily_by_date:
        raise LookupError(
            f"No Open-Meteo forecast for city={city!r}, target_date={target_date.isoformat()}"
        )
    daily = bundle.daily_by_date[target_date]
    forecast = daily_to_forecast_values(bundle, target_date)
    forecast = append_wunderground_forecast(
        forecast,
        station,
        as_of_utc=fetched_at,
    )
    lead_hours = lead_hours_to_end_of_target_day(target_date, now=now)

    return MarketContext(
        city=city,
        station=station,
        target_date=target_date,
        event=event,
        forecast=forecast,
        daily=daily,
        lead_hours=lead_hours,
        date_mismatch=date_mismatch,
    )
