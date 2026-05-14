"""Tests for Open-Meteo forecast ingestion."""

import os
from datetime import date

import httpx
import pytest

from polytempo.weather.open_meteo import (
    DEFAULT_MODELS,
    DailyMaxForecast,
    fetch_daily_max,
    fetch_for_station,
    parse_forecast_payload,
)
from polytempo.weather.stations import get_station


def _payload() -> dict:
    return {
        "latitude": 40.4168,
        "longitude": -3.7038,
        "daily": {
            "time": ["2026-05-13", "2026-05-14", "2026-05-15"],
            "temperature_2m_max_ecmwf_ifs025": [21.5, 23.1, 24.0],
            "temperature_2m_max_gfs_seamless": ["22.0", 22.8, 23.6],
            "temperature_2m_max_icon_seamless": [21.9, None, 24.4],
        },
    }


def test_parse_forecast_payload_collects_values_across_models() -> None:
    forecast = parse_forecast_payload(_payload(), date(2026, 5, 14))

    assert forecast.latitude == pytest.approx(40.4168)
    assert forecast.longitude == pytest.approx(-3.7038)
    assert forecast.target_date == date(2026, 5, 14)
    assert forecast.values_c == pytest.approx([23.1, 22.8])
    assert forecast.models == ["ecmwf_ifs025", "gfs_seamless"]


def test_parse_forecast_payload_supports_single_model_key() -> None:
    payload = {
        "latitude": 0.0,
        "longitude": 0.0,
        "daily": {
            "time": ["2026-05-14"],
            "temperature_2m_max": [19.5],
        },
    }

    forecast = parse_forecast_payload(payload, date(2026, 5, 14))

    assert forecast.values_c == pytest.approx([19.5])
    assert forecast.models == ["default"]


@pytest.mark.parametrize(
    "payload",
    [
        {"longitude": 0.0, "daily": {"time": ["2026-05-14"], "temperature_2m_max": [1.0]}},
        {"latitude": 0.0, "daily": {"time": ["2026-05-14"], "temperature_2m_max": [1.0]}},
        {"latitude": 0.0, "longitude": 0.0},
        {"latitude": 0.0, "longitude": 0.0, "daily": {"time": []}},
    ],
)
def test_parse_forecast_payload_missing_required_fields_raises(payload: dict) -> None:
    with pytest.raises(ValueError):
        parse_forecast_payload(payload, date(2026, 5, 14))


def test_parse_forecast_payload_unknown_target_date_raises() -> None:
    with pytest.raises(ValueError):
        parse_forecast_payload(_payload(), date(2030, 1, 1))


def test_parse_forecast_payload_no_values_for_date_raises() -> None:
    payload = {
        "latitude": 0.0,
        "longitude": 0.0,
        "daily": {
            "time": ["2026-05-14"],
            "temperature_2m_max_gfs_seamless": [None],
        },
    }

    with pytest.raises(ValueError):
        parse_forecast_payload(payload, date(2026, 5, 14))


@pytest.mark.parametrize("bad_value", [-50.0, 75.0])
def test_parse_forecast_payload_rejects_implausible_temperature(bad_value: float) -> None:
    payload = {
        "latitude": 0.0,
        "longitude": 0.0,
        "daily": {
            "time": ["2026-05-14"],
            "temperature_2m_max": [bad_value],
        },
    }

    with pytest.raises(ValueError):
        parse_forecast_payload(payload, date(2026, 5, 14))


def test_fetch_daily_max_calls_expected_url_and_params(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict]] = []

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return _payload()

    def fake_get(url: str, params: dict) -> FakeResponse:
        calls.append((url, params))
        return FakeResponse()

    monkeypatch.setattr(httpx, "get", fake_get)

    forecast = fetch_daily_max(
        latitude=40.4168,
        longitude=-3.7038,
        target_date=date(2026, 5, 14),
        timezone="Europe/Madrid",
        models=("ecmwf_ifs025", "gfs_seamless"),
        base_url="https://example.test/v1/forecast",
    )

    assert isinstance(forecast, DailyMaxForecast)
    assert calls == [
        (
            "https://example.test/v1/forecast",
            {
                "latitude": 40.4168,
                "longitude": -3.7038,
                "daily": "temperature_2m_max",
                "temperature_unit": "celsius",
                "models": "ecmwf_ifs025,gfs_seamless",
                "timezone": "Europe/Madrid",
                "start_date": "2026-05-14",
                "end_date": "2026-05-14",
            },
        )
    ]


def test_fetch_daily_max_rejects_empty_models() -> None:
    with pytest.raises(ValueError):
        fetch_daily_max(
            latitude=0.0,
            longitude=0.0,
            target_date=date(2026, 5, 14),
            timezone="UTC",
            models=(),
        )


def test_fetch_daily_max_rejects_empty_timezone() -> None:
    with pytest.raises(ValueError):
        fetch_daily_max(
            latitude=0.0,
            longitude=0.0,
            target_date=date(2026, 5, 14),
            timezone="  ",
        )


def test_fetch_for_station_uses_station_coordinates_and_timezone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict] = []

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return _payload()

    def fake_get(url: str, params: dict) -> FakeResponse:
        calls.append(params)
        return FakeResponse()

    monkeypatch.setattr(httpx, "get", fake_get)

    london = get_station("London")
    fetch_for_station(london, target_date=date(2026, 5, 14))

    assert calls[0]["latitude"] == pytest.approx(london.latitude)
    assert calls[0]["longitude"] == pytest.approx(london.longitude)
    assert calls[0]["timezone"] == "Europe/London"


def test_live_open_meteo_daily_max() -> None:
    if os.environ.get("POLYTEMPO_RUN_LIVE_API_TESTS") != "1":
        pytest.skip("set POLYTEMPO_RUN_LIVE_API_TESTS=1 to run live Open-Meteo smoke test")

    london = get_station("London")
    forecast = fetch_for_station(london, target_date=date.today(), models=DEFAULT_MODELS)

    assert forecast.values_c
    assert all(isinstance(value, float) for value in forecast.values_c)
