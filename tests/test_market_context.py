"""Tests for paper trading market context fetch."""

from __future__ import annotations

from datetime import date, datetime, timezone
from unittest.mock import MagicMock

import pytest

from polytempo.paper.market_context import fetch_market_context
from polytempo.weather.open_meteo import (
    DEFAULT_MODELS,
    DailyMaxForecast,
    ModelRunMeta,
    OpenMeteoLiveBundle,
)


def test_fetch_market_context_populates_init_lead_and_wall_clock_lead(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target_date = date(2026, 6, 10)
    now = datetime(2026, 6, 9, 12, 0, tzinfo=timezone.utc)
    init_utc = datetime(2026, 6, 9, 10, 0, tzinfo=timezone.utc)

    daily = DailyMaxForecast(
        target_date=target_date,
        latitude=51.5053,
        longitude=0.0553,
        values_c=[16.5, 17.0],
        models=["alpha", "beta"],
    )
    bundle = OpenMeteoLiveBundle(
        fetched_at_utc=now,
        requested_lat=51.5053,
        requested_lon=0.0553,
        returned_lat=51.5077,
        returned_lon=0.0424,
        daily_by_date={target_date: daily},
        meta_by_model={
            "alpha": ModelRunMeta(
                model="alpha",
                run_init_utc=init_utc,
                run_available_utc=init_utc,
                run_modified_utc=init_utc,
                update_interval_seconds=3600,
                temporal_resolution_seconds=3600,
                data_end_utc=None,
            ),
            "beta": ModelRunMeta(
                model="beta",
                run_init_utc=init_utc,
                run_available_utc=init_utc,
                run_modified_utc=init_utc,
                update_interval_seconds=3600,
                temporal_resolution_seconds=3600,
                data_end_utc=None,
            ),
        },
        init_lead_hours={
            ("alpha", target_date): 38.0,
            ("beta", target_date): 36.0,
        },
        wall_clock_lead_hours={
            ("alpha", target_date): 36.0,
            ("beta", target_date): 36.0,
        },
        meta_staleness_detected=False,
    )

    event = MagicMock()
    event.settlement_date = target_date
    event.event_id = "evt-1"

    monkeypatch.setattr(
        "polytempo.paper.market_context.fetch_weather_events",
        lambda **kwargs: [event],
    )
    monkeypatch.setattr(
        "polytempo.paper.market_context.first_parseable_weather_event",
        lambda events, **kwargs: events[0],
    )
    monkeypatch.setattr(
        "polytempo.paper.market_context.hydrate_prices",
        lambda event: event,
    )
    monkeypatch.setattr(
        "polytempo.paper.market_context.fetch_open_meteo_live_bundle",
        lambda **kwargs: bundle,
    )
    monkeypatch.setattr(
        "polytempo.paper.market_context.append_wunderground_forecast",
        lambda forecast, station, **kwargs: forecast,
    )

    ctx = fetch_market_context("london", target_date, now=now)

    assert ctx.lead_hours == pytest.approx(36.0)
    assert ctx.forecast.init_lead_hours == [38.0, 36.0]
    assert ctx.forecast.model_run_init_utc == [
        "2026-06-09T10:00:00Z",
        "2026-06-09T10:00:00Z",
    ]
    assert ctx.daily is daily


def _stub_market_context(
    monkeypatch: pytest.MonkeyPatch,
    target_date: date,
    now: datetime,
) -> list[dict]:
    """Patch out network calls; return the list that records bundle-fetch kwargs."""
    daily = DailyMaxForecast(
        target_date=target_date,
        latitude=51.5053,
        longitude=0.0553,
        values_c=[16.5],
        models=["alpha"],
    )
    bundle = OpenMeteoLiveBundle(
        fetched_at_utc=now,
        requested_lat=51.5053,
        requested_lon=0.0553,
        returned_lat=51.5053,
        returned_lon=0.0553,
        daily_by_date={target_date: daily},
        meta_by_model={},
        init_lead_hours={},
        wall_clock_lead_hours={("alpha", target_date): 36.0},
        meta_staleness_detected=False,
    )

    event = MagicMock()
    event.settlement_date = target_date
    event.event_id = "evt-1"

    calls: list[dict] = []

    def _capture(**kwargs):
        calls.append(kwargs)
        return bundle

    monkeypatch.setattr(
        "polytempo.paper.market_context.fetch_weather_events",
        lambda **kwargs: [event],
    )
    monkeypatch.setattr(
        "polytempo.paper.market_context.first_parseable_weather_event",
        lambda events, **kwargs: events[0],
    )
    monkeypatch.setattr(
        "polytempo.paper.market_context.hydrate_prices",
        lambda event: event,
    )
    monkeypatch.setattr(
        "polytempo.paper.market_context.fetch_open_meteo_live_bundle",
        _capture,
    )
    monkeypatch.setattr(
        "polytempo.paper.market_context.append_wunderground_forecast",
        lambda forecast, station, **kwargs: forecast,
    )
    return calls


def test_fetch_market_context_london_requests_default_models(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """London must keep requesting the unchanged 8-model DEFAULT_MODELS tuple."""
    target_date = date(2026, 6, 10)
    now = datetime(2026, 6, 9, 12, 0, tzinfo=timezone.utc)
    calls = _stub_market_context(monkeypatch, target_date, now)

    fetch_market_context("london", target_date, now=now)

    assert calls[0]["models"] == DEFAULT_MODELS
    assert calls[0]["models"] == (
        "ukmo_global_deterministic_10km",
        "icon_eu",
        "gfs_seamless",
        "ecmwf_ifs025",
        "ukmo_uk_deterministic_2km",
        "ukmo_seamless",
        "ecmwf_ifs",
        "icon_seamless",
    )


def test_fetch_market_context_madrid_requests_station_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target_date = date(2026, 6, 10)
    now = datetime(2026, 6, 9, 12, 0, tzinfo=timezone.utc)
    calls = _stub_market_context(monkeypatch, target_date, now)

    fetch_market_context("madrid", target_date, now=now)

    assert calls[0]["models"] == (
        "meteofrance_arpege_europe",
        "icon_eu",
        "ecmwf_ifs025",
        "gfs_seamless",
        "ukmo_global_deterministic_10km",
    )
