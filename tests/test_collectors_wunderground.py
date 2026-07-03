"""Tests for Wunderground HTML collector (no network)."""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path
import pytest

from polytempo.collectors.config import CollectorConfig, StationConfig, WeatherCollectorsConfig
from polytempo.collectors.util import forecast_dates_for_station, lead_hours_to_day_end
from polytempo.collectors import wunderground as wu
from polytempo.storage.postgres import get_connection, insert_station


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


def _madrid_station() -> StationConfig:
    return StationConfig(
        station_id="LEMD",
        station_type="icao",
        name="Madrid-Barajas",
        timezone="Europe/Madrid",
        lat=40.4936,
        lon=-3.5668,
        country="es",
        city_slug="madrid",
        pws_id=None,
    )


def _milan_station() -> StationConfig:
    return StationConfig(
        station_id="LIMC",
        station_type="icao",
        name="Milan Malpensa",
        timezone="Europe/Rome",
        lat=45.6306,
        lon=8.7281,
        country="it",
        city_slug="milan",
        pws_id=None,
    )


def _collector(**kwargs: object) -> CollectorConfig:
    defaults: dict[str, object] = {
        "name": "wunderground",
        "enabled": True,
        "source": "wunderground",
        "observations_interval_seconds": 300,
        "observations_anchor_time_utc": "00:00",
        "forecast_interval_seconds": 3600,
        "forecast_anchor_time_utc": "00:00",
        "stations": [_icao_station()],
    }
    defaults.update(kwargs)
    return CollectorConfig(**defaults)  # type: ignore[arg-type]


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


def _icao_obs_metric() -> dict:
    return {"temperature": 15.0}


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


def _pws_obs_metric() -> dict:
    return {
        "observations": [
            {
                "obsTimeUtc": "2026-06-03T22:00:00Z",
                "obsTimeLocal": "2026-06-03 23:00:00",
                "metric": {"tempAvg": 15.2},
            }
        ]
    }


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


def _hourly_metric() -> dict:
    return {
        "temperature": [15.0, 14.4],
        "validTimeUtc": [1780527600, 1780531200],
    }


def _install_metric_api_mocks(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_current_observation_payload(**kwargs: object) -> dict:
        station_type = kwargs.get("station_type")
        if station_type == "pws":
            return _pws_obs_metric()
        return _icao_obs_metric()

    def fake_hourly_forecast_payload(geocode: str, **kwargs: object) -> dict:
        _ = geocode
        return {
            "temperature": [16.0, 15.5, 15.0, 14.4],
            "validTimeUtc": [1780441200, 1780444800, 1780527600, 1780531200],
        }

    monkeypatch.setattr(wu, "fetch_current_observation_payload", fake_current_observation_payload)
    monkeypatch.setattr(wu, "fetch_hourly_forecast_payload", fake_hourly_forecast_payload)


def test_build_observation_url_icao() -> None:
    url = wu.build_observation_url(_icao_station())
    assert url == "https://www.wunderground.com/weather/gb/london/EGLC"


def test_build_observation_url_madrid() -> None:
    url = wu.build_observation_url(_madrid_station())
    assert url == "https://www.wunderground.com/weather/es/madrid/LEMD"


def test_build_observation_url_milan() -> None:
    url = wu.build_observation_url(_milan_station())
    assert url == "https://www.wunderground.com/weather/it/milan/LIMC"


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


def test_compute_content_hash() -> None:
    body = b"<html>test</html>"
    assert wu.compute_content_hash(body) == wu.compute_content_hash(body)
    assert wu.compute_content_hash(body) != wu.compute_content_hash(b"other")


def test_parse_observation_page_icao() -> None:
    scraped = datetime(2026, 6, 3, 22, 30, tzinfo=timezone.utc)
    obs = wu.parse_observation_page(
        _icao_obs_html(), _icao_station(), scraped, metric_body=_icao_obs_metric()
    )
    assert obs.temp_f == pytest.approx(59.0)
    assert obs.temp_c == pytest.approx(15.0)
    assert obs.raw_temp_text == "59"
    assert obs.target_date_local == date(2026, 6, 3)
    assert obs.observed_at_utc == "2026-06-03T22:19:52Z"


def test_parse_observation_page_pws_decimal() -> None:
    scraped = datetime(2026, 6, 3, 22, 30, tzinfo=timezone.utc)
    obs = wu.parse_observation_page(
        _pws_obs_html(), _pws_station(), scraped, metric_body=_pws_obs_metric()
    )
    assert obs.temp_f == pytest.approx(59.4)
    assert obs.temp_c == pytest.approx(15.2)
    assert obs.raw_temp_text == "59.4"
    assert obs.observed_at_local == "2026-06-03 23:00:00"


def test_parse_hourly_forecast_page_filters_to_target_date() -> None:
    scraped = datetime(2026, 6, 3, 22, 30, tzinfo=timezone.utc)
    hours = wu.parse_hourly_forecast_page(
        _hourly_html(),
        _icao_station(),
        date(2026, 6, 4),
        scraped,
        metric_body=_hourly_metric(),
    )
    assert len(hours) == 2
    assert hours[0].target_time_local == "2026-06-04T00:00:00+0100"
    assert hours[0].target_time_utc == "2026-06-03T23:00:00Z"
    assert hours[0].temp_f == pytest.approx(59.0)
    assert hours[0].temp_c == pytest.approx(15.0)


def test_parse_hourly_forecast_page_skips_missing_metric_hour() -> None:
    scraped = datetime(2026, 6, 3, 22, 30, tzinfo=timezone.utc)
    metric_body = {"temperature": [15.0], "validTimeUtc": [1780527600]}
    hours = wu.parse_hourly_forecast_page(
        _hourly_html(),
        _icao_station(),
        date(2026, 6, 4),
        scraped,
        metric_body=metric_body,
    )
    assert len(hours) == 1
    assert hours[0].temp_f == pytest.approx(59.0)
    assert hours[0].temp_c == pytest.approx(15.0)


def test_parse_observation_page_missing_state_raises() -> None:
    scraped = datetime(2026, 6, 3, 12, 0, tzinfo=timezone.utc)
    with pytest.raises(ValueError):
        wu.parse_observation_page(
            b"<html></html>", _icao_station(), scraped, metric_body=_icao_obs_metric()
        )


def test_run_station_cycle_inserts_rows(
    weather_db_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw_base = tmp_path / "raw"

    with get_connection(weather_db_url) as conn:
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
    _install_metric_api_mocks(monkeypatch)

    collector = _collector()

    with get_connection(weather_db_url) as conn:
        wu.run_station_cycle(
            conn,
            collector,
            _icao_station(),
            raw_base,
            now_utc=datetime(2026, 6, 3, 12, 0, tzinfo=timezone.utc),
        )

    assert not (raw_base / "wunderground").exists()

    with get_connection(weather_db_url) as conn:
        state = conn.execute(
            "SELECT success_count FROM collector_state WHERE station_id = 'EGLC'"
        ).fetchone()
        obs_count = conn.execute(
            "SELECT COUNT(*) AS n FROM observation_snapshots WHERE station_id = 'EGLC'"
        ).fetchone()
        fc_count = conn.execute(
            "SELECT COUNT(*) AS n FROM forecast_snapshots WHERE station_id = 'EGLC'"
        ).fetchone()
        fc_row = conn.execute(
            """
            SELECT temp_f, temp_c, raw_temp_text
            FROM forecast_snapshots WHERE station_id = 'EGLC' LIMIT 1
            """
        ).fetchone()
        obs_row = conn.execute(
            "SELECT temp_f, temp_c FROM observation_snapshots WHERE station_id = 'EGLC'"
        ).fetchone()
    assert state["success_count"] == 2
    assert obs_count["n"] == 1
    # Two hourly pages (today + tomorrow), each yielding two rows for its own date.
    assert fc_count["n"] == 4
    assert fc_row["temp_f"] == pytest.approx(59.0)
    assert fc_row["temp_c"] == pytest.approx(15.0)
    assert fc_row["raw_temp_text"] == "59"
    assert obs_row["temp_f"] == pytest.approx(59.0)
    assert obs_row["temp_c"] == pytest.approx(15.0)


def test_run_station_observations_only(
    weather_db_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw_base = tmp_path / "raw"

    with get_connection(weather_db_url) as conn:
        insert_station(
            conn,
            station_id="EGLC",
            name="London City Airport",
            timezone="Europe/London",
        )
        conn.commit()

    monkeypatch.setattr(wu, "fetch_raw_page", lambda url, *, client=None: _icao_obs_html())
    _install_metric_api_mocks(monkeypatch)

    with get_connection(weather_db_url) as conn:
        wu.run_station_cycle(
            conn,
            _collector(),
            _icao_station(),
            raw_base,
            now_utc=datetime(2026, 6, 3, 12, 0, tzinfo=timezone.utc),
            fetch_observations=True,
            fetch_forecasts=False,
        )

    with get_connection(weather_db_url) as conn:
        obs_count = conn.execute(
            "SELECT COUNT(*) AS n FROM observation_snapshots WHERE station_id = 'EGLC'"
        ).fetchone()
        fc_count = conn.execute(
            "SELECT COUNT(*) AS n FROM forecast_snapshots WHERE station_id = 'EGLC'"
        ).fetchone()
    assert obs_count["n"] == 1
    assert fc_count["n"] == 0


def test_run_station_cycle_one_failure_still_inserts_others(
    weather_db_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_base = tmp_path / "raw"

    with get_connection(weather_db_url) as conn:
        insert_station(
            conn,
            station_id="EGLC",
            name="London City Airport",
            timezone="Europe/London",
        )
        conn.commit()

    def fake_fetch(url: str, *, client: object = None) -> bytes:
        if "dashboard" in url or "/weather/" in url:
            raise RuntimeError("observation down")
        if "/hourly/" in url:
            return _hourly_html(date.fromisoformat(url.rsplit("/", 1)[-1]))
        return _icao_obs_html()

    monkeypatch.setattr(wu, "fetch_raw_page", fake_fetch)
    _install_metric_api_mocks(monkeypatch)

    collector = _collector()

    with get_connection(weather_db_url) as conn:
        wu.run_station_cycle(
            conn,
            collector,
            _icao_station(),
            raw_base,
            now_utc=datetime(2026, 6, 3, 12, 0, tzinfo=timezone.utc),
        )

    assert not (raw_base / "wunderground").exists()

    with get_connection(weather_db_url) as conn:
        row = conn.execute(
            "SELECT error_count, success_count, last_error_message FROM collector_state WHERE station_id = 'EGLC'"
        ).fetchone()
    assert row["error_count"] == 1
    assert row["success_count"] == 1
    assert "observation" in row["last_error_message"]


def test_run_cycle_isolates_station_failures(
    weather_db_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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

    config = WeatherCollectorsConfig(
        raw_base_dir=raw_base,
        collectors=[],
    )
    collector = _collector(stations=[_icao_station(), _pws_station()])

    with get_connection(weather_db_url) as conn:
        wu.run_cycle(conn, config, collector)

    assert calls == ["EGLC", "ILONDO288"]

    with get_connection(weather_db_url) as conn:
        err_row = conn.execute(
            "SELECT error_count FROM collector_state WHERE station_id = 'EGLC'"
        ).fetchone()
    assert err_row["error_count"] == 1
