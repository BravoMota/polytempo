"""Tests for observed Tmax JSONL helpers and Wunderground script parsing."""

from datetime import date
from pathlib import Path

import pytest

from polytempo.weather import wunderground as _WU_MODULE
from polytempo.weather.observations import (
    ObservedTmax,
    parse_observation_records,
    read_observations_jsonl,
    write_observations_jsonl,
)


def _history_page_html(temp_f: int) -> str:
    body = (
        '{"temperatureMax":['
        f"{temp_f}"
        '],"validTimeLocal":["2026-05-14T07:00:00+0100"]}'
    )
    state = (
        '{"x":{"u":"https://api.weather.com/v3/wx/conditions/historical/dailysummary",'
        f'"b":{body}'
        "}}"
    )
    return f'<html><script id="app-root-state" type="application/json">{state}</script></html>'


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


def test_build_wunderground_history_page_url() -> None:
    url = _WU_MODULE.build_wunderground_history_page_url(
        "EGLC",
        date(2026, 5, 14),
        country="gb",
        city_slug="london",
    )
    assert "wunderground.com/history/daily/gb/london/EGLC/date/2026-05-14" in url


def test_parse_wunderground_daily_high_uses_reported_temperature_max() -> None:
    payload = {
        "validTimeLocal": ["2026-05-14T07:00:00+0100"],
        "temperatureMax": [72],
    }
    row = _WU_MODULE.parse_dailysummary_payload(
        payload,
        station_id="EGLC",
        target_date=date(2026, 5, 14),
    )
    assert row == ObservedTmax(
        station_id="EGLC",
        target_date=date(2026, 5, 14),
        observed_tmax_c=22.22,
        observed_tmax_f=72.0,
        source="wunderground",
    )


def test_parse_wunderground_daily_high_from_embedded_page_json() -> None:
    row = _WU_MODULE.parse_wunderground_daily_high(
        _history_page_html(72),
        station_id="EGLC",
        target_date=date(2026, 5, 14),
    )
    assert row.observed_tmax_f == 72.0


def test_parse_summary_table_high_temp() -> None:
    html = (
        '<div class="summary-table"><table aria-labelledby="History summary">'
        "<tbody><tr><th>High Temp</th><td>61</td><td>69</td></tr></tbody></table></div>"
    )
    assert _WU_MODULE._parse_summary_table_high_temp_f(html) == 61.0


def test_parse_wunderground_daily_high_does_not_use_hourly_observations() -> None:
    payload = (
        '{"x":{"u":"https://api.weather.com/v1/location/EGLC/observations/historical.json",'
        '"b":{"observations":[{"temp":99},{"temp":50}]}}}'
    )
    html = f'<html><script id="app-root-state" type="application/json">{payload}</script></html>'
    with pytest.raises(ValueError, match="temperatureMax missing"):
        _WU_MODULE.parse_wunderground_daily_high(
            html,
            station_id="EGLC",
            target_date=date(2026, 5, 14),
        )


def test_parse_wunderground_daily_high_raises_when_missing() -> None:
    empty_summary = (
        '<html><script id="app-root-state" type="application/json">'
        '{"x":{"u":"https://api.weather.com/v3/wx/conditions/historical/dailysummary","b":{}}}'
        "</script></html>"
    )
    with pytest.raises(ValueError, match="daily high not found"):
        _WU_MODULE.parse_wunderground_daily_high(
            empty_summary,
            station_id="EGLC",
            target_date=date(2026, 5, 14),
        )


def test_fetch_wunderground_observed_tmax_uses_station_hourly_max(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_hourly_max(
        station_id: str,
        target_date: date,
        *,
        country_code: str,
        client=None,
        api_key=None,
    ) -> float:
        assert station_id == "EGLC"
        assert target_date == date(2026, 5, 14)
        assert country_code == "GB"
        return 22.0

    monkeypatch.setattr(_WU_MODULE, "fetch_dailysummary_30day_map", lambda *a, **k: {})
    monkeypatch.setattr(
        _WU_MODULE, "_fetch_v1_historical_daily_high_c", fake_hourly_max
    )

    row = _WU_MODULE.fetch_wunderground_observed_tmax(
        "EGLC",
        date(2026, 5, 14),
        country_code="GB",
        city_slug="london",
        lat=51.5053,
        lon=0.0553,
    )

    assert row.observed_tmax_c == pytest.approx(22.0)
    assert row.observed_tmax_f is None
    assert row.source == "wunderground"


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
