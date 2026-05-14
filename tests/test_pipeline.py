"""Opt-in live smoke tests for the pipeline built so far."""

import os
from datetime import date, timedelta

import pytest

from polytempo.analysis import analyze_event
from polytempo.markets.buckets import parse_temperature_bucket
from polytempo.markets.polymarket import PolymarketEvent, fetch_weather_events
from polytempo.weather.open_meteo import fetch_daily_max


def test_live_real_market_and_forecast_pipeline_smoke() -> None:
    """Run with POLYTEMPO_RUN_LIVE_API_TESTS=1 for manual pipeline inspection."""
    if os.environ.get("POLYTEMPO_RUN_LIVE_API_TESTS") != "1":
        pytest.skip("set POLYTEMPO_RUN_LIVE_API_TESTS=1 to run live pipeline test")

    events = fetch_weather_events(limit=20)
    event = _first_parseable_temperature_event(events)
    if event is None:
        pytest.skip(
            "No fetched weather event had bucket labels that the current Celsius "
            "bucket parser can parse. Missing next piece: normalize real "
            "Polymarket bucket labels/units and infer event location/date."
        )

    # Temporary real forecast input: Madrid, two days ahead. The pipeline can run
    # end-to-end, but event-specific city/date extraction is not implemented yet.
    target_date = date.today() + timedelta(days=2)
    daily = fetch_daily_max(
        latitude=40.4168,
        longitude=-3.7038,
        target_date=target_date,
        timezone="Europe/Madrid",
    )
    forecast = daily.to_forecast_values()

    result = analyze_event(forecast, event)

    print(f"\nEvent: {event.title} ({event.event_id})")
    print(f"Forecast proxy: Madrid {target_date.isoformat()} -> {forecast.values_c}")
    print(
        "Missing: event-specific location/date extraction before decisions are "
        "product-meaningful."
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


def _first_parseable_temperature_event(
    events: list[PolymarketEvent],
) -> PolymarketEvent | None:
    for event in events:
        if not event.buckets:
            continue
        try:
            for bucket in event.buckets:
                parse_temperature_bucket(bucket.label)
        except ValueError:
            continue
        return event
    return None
