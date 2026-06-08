"""Tests for Open-Meteo forecast ingestion."""

import os
from datetime import date

import httpx
import pytest

from polytempo.weather.open_meteo import (
    DEFAULT_FORECAST_TIMEOUT_S,
    DEFAULT_MODELS,
    DailyMaxForecast,
    fetch_daily_max,
    fetch_for_station,
    parse_forecast_payload,
)
from polytempo.weather.schema import ForecastValues
from polytempo.weather.stations import get_station


def _payload() -> dict:
    return {
        "latitude": 40.4168,
        "longitude": -3.7038,
        "daily": {
            "time": ["2026-05-13", "2026-05-14", "2026-05-15"],
            "temperature_2m_max_ukmo_uk_deterministic_2km": [21.5, 23.1, 24.0],
            "temperature_2m_max_ukmo_seamless": ["22.0", 22.8, 23.6],
            "temperature_2m_max_ecmwf_ifs": [21.8, 23.0, 24.2],
            "temperature_2m_max_icon_seamless": [21.9, None, 24.4],
        },
    }


def test_parse_forecast_payload_collects_values_across_models() -> None:
    forecast = parse_forecast_payload(_payload(), date(2026, 5, 14))

    assert forecast.latitude == pytest.approx(40.4168)
    assert forecast.longitude == pytest.approx(-3.7038)
    assert forecast.target_date == date(2026, 5, 14)
    assert forecast.values_c == pytest.approx([23.1, 22.8, 23.0])
    assert forecast.models == [
        "ukmo_uk_deterministic_2km",
        "ukmo_seamless",
        "ecmwf_ifs",
    ]


def test_daily_max_forecast_to_forecast_values() -> None:
    forecast = parse_forecast_payload(_payload(), date(2026, 5, 14))
    values = forecast.to_forecast_values()

    assert isinstance(values, ForecastValues)
    assert values.source == "open_meteo"
    assert values.latitude == pytest.approx(forecast.latitude)
    assert values.longitude == pytest.approx(forecast.longitude)
    assert values.target_date == forecast.target_date
    assert values.values_c == pytest.approx(forecast.values_c)
    assert values.values_c is not forecast.values_c
    assert values.models == forecast.models
    assert values.models is not forecast.models


def test_daily_max_forecast_to_forecast_values_custom_source() -> None:
    forecast = parse_forecast_payload(_payload(), date(2026, 5, 14))
    assert forecast.to_forecast_values(source="open_meteo_ecmwf").source == "open_meteo_ecmwf"


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
    calls: list[tuple[str, dict, float | None]] = []

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return _payload()

    def fake_get(url: str, params: dict, timeout: float | None = None) -> FakeResponse:
        calls.append((url, params, timeout))
        return FakeResponse()

    monkeypatch.setattr(httpx, "get", fake_get)

    forecast = fetch_daily_max(
        latitude=40.4168,
        longitude=-3.7038,
        target_date=date(2026, 5, 14),
        timezone="Europe/Madrid",
        models=("ukmo_seamless", "ecmwf_ifs"),
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
                "models": "ukmo_seamless,ecmwf_ifs",
                "timezone": "Europe/Madrid",
                "start_date": "2026-05-14",
                "end_date": "2026-05-14",
            },
            DEFAULT_FORECAST_TIMEOUT_S,
        )
    ]


def test_fetch_daily_max_retries_on_read_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0
    sleeps: list[float] = []

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return _payload()

    def fake_get(url: str, params: dict, timeout: float | None = None) -> FakeResponse:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise httpx.ReadTimeout("timed out")
        return FakeResponse()

    monkeypatch.setattr(httpx, "get", fake_get)
    monkeypatch.setattr("polytempo.weather.open_meteo.time.sleep", lambda s: sleeps.append(s))

    forecast = fetch_daily_max(
        latitude=40.4168,
        longitude=-3.7038,
        target_date=date(2026, 5, 14),
        timezone="Europe/Madrid",
        base_url="https://example.test/v1/forecast",
    )

    assert isinstance(forecast, DailyMaxForecast)
    assert calls == 3
    assert sleeps == [2.0, 4.0]


def test_fetch_daily_max_retries_on_502(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    class FakeResponse:
        status_code: int

        def __init__(self, status_code: int) -> None:
            self.status_code = status_code

        def raise_for_status(self) -> None:
            if self.status_code >= 400:
                request = httpx.Request("GET", "https://example.test/v1/forecast")
                response = httpx.Response(self.status_code, request=request)
                raise httpx.HTTPStatusError("bad gateway", request=request, response=response)

        def json(self) -> dict:
            return _payload()

    def fake_get(url: str, params: dict, timeout: float | None = None) -> FakeResponse:
        nonlocal calls
        calls += 1
        if calls < 3:
            return FakeResponse(502)
        return FakeResponse(200)

    monkeypatch.setattr(httpx, "get", fake_get)
    monkeypatch.setattr("polytempo.weather.open_meteo.time.sleep", lambda _s: None)

    forecast = fetch_daily_max(
        latitude=40.4168,
        longitude=-3.7038,
        target_date=date(2026, 5, 14),
        timezone="Europe/Madrid",
        base_url="https://example.test/v1/forecast",
    )

    assert isinstance(forecast, DailyMaxForecast)
    assert calls == 3


def test_fetch_daily_max_max_retries_one_no_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0
    sleeps: list[float] = []

    def fake_get(url: str, params: dict, timeout: float | None = None) -> None:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("timed out")

    monkeypatch.setattr(httpx, "get", fake_get)
    monkeypatch.setattr("polytempo.weather.open_meteo.time.sleep", lambda s: sleeps.append(s))

    with pytest.raises(httpx.ReadTimeout):
        fetch_daily_max(
            latitude=40.4168,
            longitude=-3.7038,
            target_date=date(2026, 5, 14),
            timezone="Europe/Madrid",
            base_url="https://example.test/v1/forecast",
            max_retries=1,
        )

    assert calls == 1
    assert sleeps == []


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

    def fake_get(url: str, params: dict, timeout: float | None = None) -> FakeResponse:
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
