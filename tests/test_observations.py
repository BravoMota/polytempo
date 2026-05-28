"""Tests for observed Tmax JSONL helpers and Wunderground script parsing."""

from datetime import date

import pytest

from polytempo.weather import wunderground as _WU_MODULE
from polytempo.weather.observations import (
    ObservedTmax,
    parse_observation_records,
    read_observations_jsonl,
    write_observations_jsonl,
)


def test_parse_observation_records_validates_required_keys() -> None:
    with pytest.raises(ValueError, match="missing"):
        parse_observation_records([{"station_id": "EGLC"}])


def test_parse_observation_records_parses_rows() -> None:
    rows = parse_observation_records(
        [
            {
                "station_id": "EGLC",
                "target_date": "2026-05-14",
                "observed_tmax_c": 22.4,
                "source": "manual",
            }
        ]
    )
    assert rows == [
        ObservedTmax(
            station_id="EGLC",
            target_date=date(2026, 5, 14),
            observed_tmax_c=22.4,
            source="manual",
        )
    ]


def test_observations_jsonl_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "observed_tmax.jsonl"
    row = ObservedTmax(
        station_id="EGLC",
        target_date=date(2026, 5, 14),
        observed_tmax_c=21.0,
        source="manual",
    )

    write_observations_jsonl([row], path)
    loaded = read_observations_jsonl(path)

    assert loaded == [row]


def test_read_observations_jsonl_missing_file_returns_empty(tmp_path: Path) -> None:
    assert read_observations_jsonl(tmp_path / "missing.jsonl") == []


def test_build_wunderground_history_url() -> None:
    url = _WU_MODULE.build_wunderground_history_url(
        "EGLC", date(2026, 5, 14), country_code="GB"
    )
    assert "api.weather.com" in url
    assert "/v1/location/EGLC:9:GB/observations/historical.json" in url
    assert "startDate=20260514" in url
    assert "endDate=20260514" in url
    assert "units=m" in url
    assert "apiKey=" in url


def test_parse_wunderground_daily_high_takes_max_temp_from_observations() -> None:
    payload = (
        '{"metadata":{"status_code":200},'
        '"observations":['
        '{"valid_time_gmt":1748000000,"temp":15},'
        '{"valid_time_gmt":1748010000,"temp":22},'
        '{"valid_time_gmt":1748020000,"temp":null},'
        '{"valid_time_gmt":1748030000,"temp":18}'
        "]}"
    )
    row = _WU_MODULE.parse_wunderground_daily_high(
        payload,
        station_id="EGLC",
        target_date=date(2025, 5, 24),
    )
    assert row == ObservedTmax(
        station_id="EGLC",
        target_date=date(2025, 5, 24),
        observed_tmax_c=22.0,
        source="wunderground",
    )


def test_parse_wunderground_daily_high_raises_when_missing() -> None:
    with pytest.raises(ValueError, match="daily high not found"):
        _WU_MODULE.parse_wunderground_daily_high(
            '{"observations":[]}',
            station_id="EGLC",
            target_date=date(2026, 5, 14),
        )


def test_parse_wunderground_daily_high_raises_when_all_temps_null() -> None:
    payload = '{"observations":[{"temp":null},{"temp":null}]}'
    with pytest.raises(ValueError, match="daily high not found"):
        _WU_MODULE.parse_wunderground_daily_high(
            payload,
            station_id="EGLC",
            target_date=date(2026, 5, 14),
        )


def test_parse_wunderground_daily_high_raises_on_non_json() -> None:
    with pytest.raises(ValueError, match="daily high not found"):
        _WU_MODULE.parse_wunderground_daily_high(
            "<html><body>No weather numbers here</body></html>",
            station_id="EGLC",
            target_date=date(2026, 5, 14),
        )


def test_fetch_wunderground_observed_tmax_uses_user_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeResponse:
        text = '{"observations":[{"temp":15},{"temp":21},{"temp":17}]}'

        def raise_for_status(self) -> None:
            return None

    called: dict[str, object] = {}

    def fake_get(url: str, headers: dict[str, str], timeout: float):
        called["url"] = url
        called["headers"] = headers
        called["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr(_WU_MODULE.httpx, "get", fake_get)

    row = _WU_MODULE.fetch_wunderground_observed_tmax(
        "EGLC", date(2026, 5, 14), country_code="GB"
    )

    assert row.observed_tmax_c == pytest.approx(21.0)
    assert row.source == "wunderground"
    assert "User-Agent" in called["headers"]
    assert "EGLC:9:GB" in str(called["url"])
    assert "startDate=20260514" in str(called["url"])
    assert "endDate=20260514" in str(called["url"])


def test_fetch_wunderground_observations_range_skips_failures(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fake_fetch(station_id: str, target_date: date, **_kwargs):
        if target_date == date(2026, 5, 15):
            raise ValueError("no daily high")
        return ObservedTmax(
            station_id=station_id,
            target_date=target_date,
            observed_tmax_c=20.0,
            source="wunderground",
        )

    monkeypatch.setattr(_WU_MODULE, "fetch_wunderground_observed_tmax", fake_fetch)

    rows = _WU_MODULE.fetch_wunderground_observations_range(
        "EGLC",
        start_date=date(2026, 5, 14),
        end_date=date(2026, 5, 16),
        country_code="GB",
    )

    assert [row.target_date for row in rows] == [date(2026, 5, 14), date(2026, 5, 16)]
    stderr = capsys.readouterr().err
    assert "2026-05-15" in stderr
