"""Tests for Wunderground HTML collector (no network)."""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path
import pytest

from polytempo.collectors.config import CollectorConfig, StationConfig
from polytempo.collectors.util import forecast_dates_for_station, lead_hours_to_day_end
from polytempo.collectors import wunderground as wu
from polytempo.storage.sqlite import get_connection, initialize_database, insert_station


def _icao_station() -> StationConfig:
    return StationConfig(
        station_id="EGLC",
        station_type="icao",
        name="London City Airport",
        timezone="Europe/London",
        lat=51.5053,
        lon=0.0553,
        country="gb",
        city_slug="london",
        pws_id=None,
    )


def _pws_station() -> StationConfig:
    return StationConfig(
        station_id="ILONDO288",
        station_type="pws",
        name="London PWS",
        timezone="Europe/London",
        lat=51.5053,
        lon=0.0553,
        country="gb",
        city_slug="london",
        pws_id="ILONDO288",
    )


def _state_html(state: dict) -> bytes:
    """Wrap an app-root-state dict in minimal Wunderground page HTML."""
    blob = json.dumps(state)
    return (
        b"<html><body>"
        b'<script id="app-root-state" type="application/json">'
        + blob.encode()
        + b"</script></body></html>"
    )


def _icao_obs_html() -> bytes:
    return _state_html(
        {
            "1": {
                "u": "https://api.weather.com/v3/wx/observations/current?units=e",
                "b": {
                    "temperature": 59,
                    "validTimeUtc": 1780525192,
                    "validTimeLocal": "2026-06-03T23:19:52+0100",
                },
            }
        }
    )


def _pws_obs_html() -> bytes:
    return _state_html(
        {
            "1": {
                "u": "https://api.weather.com/v2/pws/observations/all/1day?units=e",
                "b": {
                    "observations": [
                        {
                            "obsTimeUtc": "2026-06-03T22:00:00Z",
                            "obsTimeLocal": "2026-06-03 23:00:00",
                            "imperial": {"tempAvg": 59.4},
                        }
                    ]
                },
            }
        }
    )


def _hourly_html(target: date = date(2026, 6, 4)) -> bytes:
    iso = target.isoformat()
    return _state_html(
        {
            "1": {
                "u": "https://api.weather.com/v3/wx/forecast/hourly/15day?units=e",
                "b": {
                    "temperature": [59, 58],
                    "validTimeLocal": [
                        f"{iso}T00:00:00+0100",
                        f"{iso}T01:00:00+0100",
                    ],
                    "validTimeUtc": [1780527600, 1780531200],
                },
            }
        }
    )


def test_build_observation_url_icao() -> None:
    url = wu.build_observation_url(_icao_station())
    assert url == "https://www.wunderground.com/weather/gb/london/EGLC"


def test_build_observation_url_pws() -> None:
    url = wu.build_observation_url(_pws_station())
    assert url == "https://www.wunderground.com/dashboard/pws/ILONDO288"


def test_build_hourly_forecast_url() -> None:
    url = wu.build_hourly_forecast_url(_pws_station(), date(2026, 6, 4))
    assert url == "https://www.wunderground.com/hourly/gb/london/ILONDO288/date/2026-06-04"


def test_forecast_dates_for_station() -> None:
    now = datetime(2026, 6, 3, 22, 0, tzinfo=timezone.utc)
    today, tomorrow = forecast_dates_for_station("Europe/London", now)
    assert today == date(2026, 6, 3)
    assert tomorrow == date(2026, 6, 4)


def test_lead_hours_to_day_end() -> None:
    # 2026-01-15: Europe/London is on GMT (no DST offset).
    scraped = datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)
    lead = lead_hours_to_day_end(scraped, date(2026, 1, 16), "Europe/London")
    assert lead == pytest.approx(36.0)


def test_save_raw_response_writes_html_and_meta(tmp_path: Path) -> None:
    body = b"<html>test</html>"
    scraped = datetime(2026, 6, 3, 12, 0, tzinfo=timezone.utc)
    path, content_hash = wu.save_raw_response(
        tmp_path,
        "EGLC",
        "observation",
        scraped,
        body,
        "https://example.test",
    )
    assert path.exists()
    meta_path = path.with_suffix(".meta.json")
    assert meta_path.name.endswith(".meta.json")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    assert meta["content_hash"] == content_hash
    assert meta["page_kind"] == "observation"


def test_parse_observation_page_icao() -> None:
    scraped = datetime(2026, 6, 3, 22, 30, tzinfo=timezone.utc)
    obs = wu.parse_observation_page(_icao_obs_html(), _icao_station(), scraped)
    assert obs.temp_c == pytest.approx(15.0)
    assert obs.raw_temp_text == "59"
    assert obs.target_date_local == date(2026, 6, 3)
    assert obs.observed_at_utc == "2026-06-03T22:19:52Z"


def test_parse_observation_page_pws_decimal() -> None:
    scraped = datetime(2026, 6, 3, 22, 30, tzinfo=timezone.utc)
    obs = wu.parse_observation_page(_pws_obs_html(), _pws_station(), scraped)
    assert obs.temp_c == pytest.approx(15.22)
    assert obs.raw_temp_text == "59.4"
    assert obs.observed_at_local == "2026-06-03 23:00:00"


def test_parse_hourly_forecast_page_filters_to_target_date() -> None:
    scraped = datetime(2026, 6, 3, 22, 30, tzinfo=timezone.utc)
    hours = wu.parse_hourly_forecast_page(
        _hourly_html(), _icao_station(), date(2026, 6, 4), scraped
    )
    assert len(hours) == 2
    assert hours[0].target_time_local == "2026-06-04T00:00:00+0100"
    assert hours[0].target_time_utc == "2026-06-03T23:00:00Z"
    assert hours[0].temp_c == pytest.approx(15.0)


def test_parse_observation_page_missing_state_raises() -> None:
    scraped = datetime(2026, 6, 3, 12, 0, tzinfo=timezone.utc)
    with pytest.raises(ValueError):
        wu.parse_observation_page(b"<html></html>", _icao_station(), scraped)


def test_run_station_cycle_saves_files_and_inserts_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "test.db"
    initialize_database(db_path)
    raw_base = tmp_path / "raw"

    with get_connection(db_path) as conn:
        insert_station(
            conn,
            station_id="EGLC",
            name="London City Airport",
            timezone="Europe/London",
        )
        conn.commit()

    def fake_fetch(url: str, *, client: object = None) -> bytes:
        if "/hourly/" in url:
            return _hourly_html(date.fromisoformat(url.rsplit("/", 1)[-1]))
        return _icao_obs_html()

    monkeypatch.setattr(wu, "fetch_raw_page", fake_fetch)

    collector = CollectorConfig(
        name="wunderground",
        enabled=True,
        source="wunderground",
        interval_seconds=300,
        anchor_time_local=None,
        stations=[_icao_station()],
    )

    with get_connection(db_path) as conn:
        wu.run_station_cycle(
            conn,
            collector,
            _icao_station(),
            raw_base,
            now_utc=datetime(2026, 6, 3, 12, 0, tzinfo=timezone.utc),
        )

    html_files = list((raw_base / "wunderground").glob("*.html"))
    meta_files = list((raw_base / "wunderground").glob("*.meta.json"))
    assert len(html_files) == 3
    assert len(meta_files) == 3

    with get_connection(db_path) as conn:
        state = conn.execute(
            "SELECT success_count FROM collector_state WHERE station_id = 'EGLC'"
        ).fetchone()
        obs_count = conn.execute(
            "SELECT COUNT(*) AS n FROM observation_snapshots WHERE station_id = 'EGLC'"
        ).fetchone()
        fc_count = conn.execute(
            "SELECT COUNT(*) AS n FROM forecast_snapshots WHERE station_id = 'EGLC'"
        ).fetchone()
    assert state["success_count"] == 1
    assert obs_count["n"] == 1
    # Two hourly pages (today + tomorrow), each yielding two rows for its own date.
    assert fc_count["n"] == 4


def test_run_station_cycle_one_failure_still_saves_others(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "test.db"
    initialize_database(db_path)
    raw_base = tmp_path / "raw"

    def fake_fetch(url: str, *, client: object = None) -> bytes:
        if "dashboard" in url or "/weather/" in url:
            raise RuntimeError("observation down")
        return b"forecast ok"

    monkeypatch.setattr(wu, "fetch_raw_page", fake_fetch)

    collector = CollectorConfig(
        name="wunderground",
        enabled=True,
        source="wunderground",
        interval_seconds=300,
        anchor_time_local=None,
        stations=[_icao_station()],
    )

    with get_connection(db_path) as conn:
        wu.run_station_cycle(
            conn,
            collector,
            _icao_station(),
            raw_base,
            now_utc=datetime(2026, 6, 3, 12, 0, tzinfo=timezone.utc),
        )

    html_files = list((raw_base / "wunderground").glob("*.html"))
    assert len(html_files) == 2

    with get_connection(db_path) as conn:
        row = conn.execute(
            "SELECT error_count, last_error_message FROM collector_state WHERE station_id = 'EGLC'"
        ).fetchone()
    assert row["error_count"] == 1
    assert "observation" in row["last_error_message"]


def test_run_cycle_isolates_station_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "test.db"
    initialize_database(db_path)
    raw_base = tmp_path / "raw"

    calls: list[str] = []

    def fake_run_station_cycle(
        conn: object,
        collector: CollectorConfig,
        station: StationConfig,
        raw_base_dir: Path,
        **kwargs: object,
    ) -> None:
        calls.append(station.station_id)
        if station.station_id == "EGLC":
            raise RuntimeError("station boom")

    monkeypatch.setattr(wu, "run_station_cycle", fake_run_station_cycle)

    from polytempo.collectors.config import WeatherCollectorsConfig

    config = WeatherCollectorsConfig(
        weather_db_path=db_path,
        raw_base_dir=raw_base,
        collectors=[],
    )
    collector = CollectorConfig(
        name="wunderground",
        enabled=True,
        source="wunderground",
        interval_seconds=300,
        anchor_time_local=None,
        stations=[_icao_station(), _pws_station()],
    )

    with get_connection(db_path) as conn:
        wu.run_cycle(conn, config, collector)

    assert calls == ["EGLC", "ILONDO288"]

    with get_connection(db_path) as conn:
        err_row = conn.execute(
            "SELECT error_count FROM collector_state WHERE station_id = 'EGLC'"
        ).fetchone()
    assert err_row["error_count"] == 1
