"""Tests for Open-Meteo Single Runs request helpers."""

from datetime import date, datetime, timezone

import httpx
import pytest

from polytempo.weather.historical_forecasts import (
    build_single_run_request_params,
    fetch_single_run_payload,
)

# Keep in sync with @variables in http/open_meteo_single_runs.http
_HTTP_EGLC_LATITUDE = 51.5053
_HTTP_EGLC_LONGITUDE = 0.0553
_HTTP_MODEL = "ukmo_seamless"
_HTTP_RUN_TIME = datetime(2026, 3, 29, 0, 0, tzinfo=timezone.utc)
_HTTP_TARGET_DATE = date(2026, 4, 1)
_HTTP_TIMEZONE = "Europe/London"


def test_open_meteo_single_runs_http_scratch_matches_request_builder() -> None:
    """http/open_meteo_single_runs.http must match build_single_run_request_params."""
    params = build_single_run_request_params(
        latitude=_HTTP_EGLC_LATITUDE,
        longitude=_HTTP_EGLC_LONGITUDE,
        model=_HTTP_MODEL,
        target_date=_HTTP_TARGET_DATE,
        forecast_run_time=_HTTP_RUN_TIME,
        timezone=_HTTP_TIMEZONE,
    )
    assert params == {
        "latitude": _HTTP_EGLC_LATITUDE,
        "longitude": _HTTP_EGLC_LONGITUDE,
        "hourly": "temperature_2m",
        "temperature_unit": "celsius",
        "models": _HTTP_MODEL,
        "timezone": _HTTP_TIMEZONE,
        "run": "2026-03-29T00:00",
        "forecast_days": 7,
    }
    assert "daily" not in params
    assert "start_date" not in params
    assert "end_date" not in params


def test_build_single_run_request_params_includes_run_and_dates() -> None:
    params = build_single_run_request_params(
        latitude=51.5053,
        longitude=0.0553,
        model="ecmwf_ifs",
        target_date=date(2026, 5, 22),
        forecast_run_time=datetime(2026, 5, 20, 6, 0, tzinfo=timezone.utc),
        timezone="UTC",
    )

    assert params == {
        "latitude": 51.5053,
        "longitude": 0.0553,
        "hourly": "temperature_2m",
        "temperature_unit": "celsius",
        "models": "ecmwf_ifs",
        "timezone": "UTC",
        "run": "2026-05-20T06:00",
        "forecast_days": 7,
    }


def test_fetch_single_run_payload_success(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, dict]] = []

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {
                "hourly": {
                    "time": ["2026-05-22T00:00", "2026-05-22T12:00"],
                    "temperature_2m": [11.0, 20.0],
                }
            }

    def fake_get(url: str, params: dict, **kwargs: object) -> FakeResponse:
        calls.append((url, params))
        return FakeResponse()

    monkeypatch.setattr(httpx, "get", fake_get)

    payload = fetch_single_run_payload(
        51.5053,
        0.0553,
        "ukmo_seamless",
        date(2026, 5, 22),
        datetime(2026, 5, 19, 0, 0, tzinfo=timezone.utc),
        timezone="Europe/London",
        base_url="https://example.test/v1/forecast",
    )

    assert payload["hourly"]["time"][0] == "2026-05-22T00:00"
    assert calls[0][0] == "https://example.test/v1/forecast"
    assert calls[0][1]["models"] == "ukmo_seamless"
    assert calls[0][1]["hourly"] == "temperature_2m"


def test_fetch_single_run_payload_non_2xx_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeResponse:
        def raise_for_status(self) -> None:
            raise httpx.HTTPStatusError(
                "bad",
                request=httpx.Request("GET", "https://example.test"),
                response=httpx.Response(500),
            )

        def json(self) -> dict:
            return {}

    monkeypatch.setattr(httpx, "get", lambda *_a, **_k: FakeResponse())

    with pytest.raises(httpx.HTTPStatusError):
        fetch_single_run_payload(
            0.0,
            0.0,
            "ukmo_seamless",
            date(2026, 5, 22),
            datetime(2026, 5, 19, 0, 0, tzinfo=timezone.utc),
            timezone="UTC",
            base_url="https://example.test/v1/forecast",
        )
