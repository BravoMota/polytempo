"""Tests for Open-Meteo rolling metadata parsing and live bundle."""

from __future__ import annotations

from datetime import date, datetime, timezone

import httpx
import pytest

from polytempo.weather.open_meteo import (
    availability_lag_hours,
    daily_to_forecast_values,
    fetch_open_meteo_live_bundle,
    format_run_time_utc,
    parse_rolling_meta_payload,
)

# Tue Jun 10 2026 16:00 UTC / avail 20:57 UTC (from live UKMO sample)
_META_UKMO = {
    "last_run_initialisation_time": 1781107200,
    "last_run_availability_time": 1781125021,
    "last_run_modification_time": 1781125021,
    "update_interval_seconds": 3600,
    "temporal_resolution_seconds": 3600,
    "data_end_time": 1781154000,
}


def _forecast_payload() -> dict:
    return {
        "latitude": 51.507725,
        "longitude": 0.042434692,
        "daily": {
            "time": ["2026-06-10", "2026-06-11"],
            "temperature_2m_max_ukmo_uk_deterministic_2km": [16.5, 16.4],
            "temperature_2m_max_icon_eu": [17.0, 16.8],
        },
    }


def test_parse_rolling_meta_payload() -> None:
    meta = parse_rolling_meta_payload("ukmo_uk_deterministic_2km", _META_UKMO)

    assert meta.model == "ukmo_uk_deterministic_2km"
    assert meta.run_init_utc == datetime(2026, 6, 10, 16, 0, tzinfo=timezone.utc)
    assert meta.run_available_utc == datetime(2026, 6, 10, 20, 57, 1, tzinfo=timezone.utc)
    assert meta.update_interval_seconds == 3600
    assert meta.data_end_utc == datetime(2026, 6, 11, 5, 0, tzinfo=timezone.utc)
    assert availability_lag_hours(meta) == pytest.approx(4.950277777, rel=1e-6)


def test_format_run_time_utc_matches_calibration() -> None:
    init = datetime(2026, 6, 10, 16, 0, tzinfo=timezone.utc)
    assert format_run_time_utc(init) == "2026-06-10T16:00:00Z"


def test_fetch_open_meteo_live_bundle_staleness_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    meta_calls = 0
    init_before = 1781107200
    init_after = 1781110800  # +1h

    class FakeResponse:
        def __init__(self, payload: dict) -> None:
            self._payload = payload

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return self._payload

    class FakeClient:
        def get(self, url: str, params: dict | None = None, timeout: float | None = None) -> FakeResponse:
            nonlocal meta_calls
            if url.endswith("meta.json"):
                meta_calls += 1
                payload = dict(_META_UKMO)
                payload["last_run_initialisation_time"] = (
                    init_before if meta_calls <= 2 else init_after
                )
                return FakeResponse(payload)
            return FakeResponse(_forecast_payload())

        def close(self) -> None:
            return None

    monkeypatch.setattr(httpx, "Client", FakeClient)

    fetched_at = datetime(2026, 6, 10, 22, 0, tzinfo=timezone.utc)
    bundle = fetch_open_meteo_live_bundle(
        latitude=51.5053,
        longitude=0.0553,
        timezone="Europe/London",
        models=("ukmo_uk_deterministic_2km", "icon_eu"),
        target_dates=[date(2026, 6, 10), date(2026, 6, 11)],
        fetched_at_utc=fetched_at,
    )

    assert bundle.meta_staleness_detected is True
    assert bundle.meta_by_model["ukmo_uk_deterministic_2km"].run_init_utc == datetime(
        2026, 6, 10, 17, 0, tzinfo=timezone.utc
    )
    assert ("ukmo_uk_deterministic_2km", date(2026, 6, 10)) in bundle.init_lead_hours
    assert ("ukmo_uk_deterministic_2km", date(2026, 6, 10)) in bundle.wall_clock_lead_hours


def test_fetch_open_meteo_live_bundle_skips_missing_meta(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeResponse:
        def __init__(self, payload: dict | None, *, status_code: int = 200) -> None:
            self._payload = payload
            self.status_code = status_code

        def raise_for_status(self) -> None:
            if self.status_code >= 400:
                request = httpx.Request("GET", "https://example.test/meta.json")
                response = httpx.Response(self.status_code, request=request)
                raise httpx.HTTPStatusError("error", request=request, response=response)

        def json(self) -> dict:
            assert self._payload is not None
            return self._payload

    class FakeClient:
        def get(self, url: str, params: dict | None = None, timeout: float | None = None) -> FakeResponse:
            if url.endswith("icon_eu/static/meta.json"):
                return FakeResponse(None, status_code=404)
            if url.endswith("meta.json"):
                return FakeResponse(_META_UKMO)
            return FakeResponse(_forecast_payload())

        def close(self) -> None:
            return None

    monkeypatch.setattr(httpx, "Client", FakeClient)

    target = date(2026, 6, 10)
    fetched_at = datetime(2026, 6, 10, 22, 0, tzinfo=timezone.utc)
    bundle = fetch_open_meteo_live_bundle(
        latitude=51.5053,
        longitude=0.0553,
        timezone="Europe/London",
        models=("ukmo_uk_deterministic_2km", "icon_eu"),
        target_dates=[target],
        fetched_at_utc=fetched_at,
    )

    assert "icon_eu" not in bundle.meta_by_model
    assert ("ukmo_uk_deterministic_2km", target) in bundle.init_lead_hours
    assert ("icon_eu", target) in bundle.wall_clock_lead_hours
    assert ("icon_eu", target) not in bundle.init_lead_hours

    forecast = daily_to_forecast_values(bundle, target)
    ukmo_index = forecast.models.index("ukmo_uk_deterministic_2km")
    icon_index = forecast.models.index("icon_eu")
    assert forecast.init_lead_hours[ukmo_index] == bundle.init_lead_hours[
        ("ukmo_uk_deterministic_2km", target)
    ]
    assert forecast.init_lead_hours[icon_index] == bundle.wall_clock_lead_hours[
        ("icon_eu", target)
    ]
    assert forecast.model_run_init_utc[icon_index] == ""
