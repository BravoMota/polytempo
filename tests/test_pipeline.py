"""Opt-in live smoke tests for the pipeline built so far."""

import os
from datetime import date, timedelta

import pytest

from polytempo.analysis import analyze_event
from polytempo.markets.polymarket import fetch_weather_events, first_parseable_weather_event
from polytempo.weather.open_meteo import fetch_daily_max
from polytempo.weather.stations import get_station


def test_live_real_market_and_forecast_pipeline_smoke() -> None:
    """Run with POLYTEMPO_RUN_LIVE_API_TESTS=1 for manual pipeline inspection."""
    if os.environ.get("POLYTEMPO_RUN_LIVE_API_TESTS") != "1":
        pytest.skip("set POLYTEMPO_RUN_LIVE_API_TESTS=1 to run live pipeline test")

    target_date = date.today() + timedelta(days=1)

    events = fetch_weather_events(limit=20, end_on_date=target_date)
    event = first_parseable_weather_event(
        events,
        city="london",
        settlement_date=target_date,
    )
    if event is None:
        pytest.skip(
            "No London-titled weather event with matching Gamma end date, "
            "parseable Celsius buckets, and within fetch limit. "
            "Try a larger limit or a different day (--days-ahead)."
        )

    london = get_station("london")
    daily = fetch_daily_max(
        latitude=london.latitude,
        longitude=london.longitude,
        target_date=target_date,
        timezone=london.timezone,
    )
    forecast = daily.to_forecast_values()

    result = analyze_event(forecast, event)

    print(f"\nEvent: {event.title} ({event.event_id})")
    print(
        f"Forecast: London EGLC {target_date.isoformat()} -> {forecast.values_c}"
    )
    print(
        "Event: Gamma list for that end date + London title/slug + parseable buckets; "
        "forecast: same calendar day at EGLC."
    )
    for row in result.rows:
        print(
            {
                "label": row.label,
                "probability": row.probability,
                "yes_ask": row.yes_ask,
                "edge_yes_pp": row.edge_yes_pp,
                "action": row.action,
                "reason": row.reason,
                "warnings": row.warnings,
            }
        )

    assert result.rows
