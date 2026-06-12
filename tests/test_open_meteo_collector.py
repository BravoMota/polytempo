"""Tests for Open-Meteo collector DB persistence."""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from polytempo.collectors.config import CollectorConfig, StationConfig
from polytempo.collectors import open_meteo as om_collector
from polytempo.storage.postgres import get_connection, insert_station
from polytempo.weather.open_meteo import (
    DailyMaxForecast,
    ModelRunMeta,
    OpenMeteoLiveBundle,
)


def _station() -> StationConfig:
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


def _collector() -> CollectorConfig:
    return CollectorConfig(
        name="open_meteo",
        enabled=True,
        source="open_meteo",
        observations_interval_seconds=86400,
        observations_anchor_time_utc="00:00",
        forecast_interval_seconds=600,
        forecast_anchor_time_utc="00:00",
        stations=[_station()],
        models=("ukmo_uk_deterministic_2km", "icon_eu"),
        target_horizon_days=2,
    )


def _bundle() -> OpenMeteoLiveBundle:
    fetched_at = datetime(2026, 6, 10, 22, 0, tzinfo=timezone.utc)
    init = datetime(2026, 6, 10, 16, 0, tzinfo=timezone.utc)
    avail = datetime(2026, 6, 10, 20, 57, 1, tzinfo=timezone.utc)
    meta = ModelRunMeta(
        model="ukmo_uk_deterministic_2km",
        run_init_utc=init,
        run_available_utc=avail,
        run_modified_utc=avail,
        update_interval_seconds=3600,
        temporal_resolution_seconds=3600,
        data_end_utc=None,
    )
    daily = DailyMaxForecast(
        target_date=date(2026, 6, 11),
        latitude=51.507725,
        longitude=0.042434692,
        values_c=[16.4, 16.8],
        models=["ukmo_uk_deterministic_2km", "icon_eu"],
    )
    return OpenMeteoLiveBundle(
        fetched_at_utc=fetched_at,
        requested_lat=51.5053,
        requested_lon=0.0553,
        returned_lat=51.507725,
        returned_lon=0.042434692,
        daily_by_date={date(2026, 6, 11): daily},
        meta_by_model={
            "ukmo_uk_deterministic_2km": meta,
            "icon_eu": ModelRunMeta(
                model="icon_eu",
                run_init_utc=init,
                run_available_utc=avail,
                run_modified_utc=avail,
                update_interval_seconds=10_800,
                temporal_resolution_seconds=3600,
                data_end_utc=None,
            ),
        },
        init_lead_hours={
            ("ukmo_uk_deterministic_2km", date(2026, 6, 11)): 28.0,
            ("icon_eu", date(2026, 6, 11)): 28.0,
        },
        wall_clock_lead_hours={
            ("ukmo_uk_deterministic_2km", date(2026, 6, 11)): 26.0,
            ("icon_eu", date(2026, 6, 11)): 26.0,
        },
        meta_staleness_detected=False,
    )


def test_open_meteo_run_station_forecasts_persists_rows(
    weather_db_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = _bundle()

    monkeypatch.setattr(
        "polytempo.collectors.open_meteo.fetch_open_meteo_live_bundle",
        lambda **kwargs: bundle,
    )

    with get_connection(weather_db_url) as conn:
        insert_station(
            conn,
            station_id="EGLC",
            name="London City Airport",
            timezone="Europe/London",
            lat=51.5053,
            lon=0.0553,
            country="gb",
        )
        conn.commit()

        class FakeClient:
            def close(self) -> None:
                return None

        om_collector.run_station_forecasts(
            conn,
            _collector(),
            _station(),
            client=FakeClient(),
            now_utc=bundle.fetched_at_utc,
        )

        cycle_count = conn.execute("SELECT COUNT(*) AS n FROM open_meteo_fetch_cycles").fetchone()
        meta_count = conn.execute(
            "SELECT COUNT(*) AS n FROM open_meteo_model_meta_snapshots"
        ).fetchone()
        fc_count = conn.execute(
            "SELECT COUNT(*) AS n FROM open_meteo_forecast_snapshots"
        ).fetchone()

        assert cycle_count is not None and int(cycle_count["n"]) == 1
        assert meta_count is not None and int(meta_count["n"]) == 2
        assert fc_count is not None and int(fc_count["n"]) == 2

        row = conn.execute(
            """
            SELECT run_init_utc, predicted_tmax_c, init_lead_hours, wall_clock_lead_hours
            FROM open_meteo_forecast_snapshots
            WHERE model = 'ukmo_uk_deterministic_2km'
            """
        ).fetchone()
        assert row is not None
        assert row["run_init_utc"] == "2026-06-10T16:00:00Z"
        assert row["predicted_tmax_c"] == pytest.approx(16.4)
        assert row["init_lead_hours"] == pytest.approx(28.0)
        assert row["wall_clock_lead_hours"] == pytest.approx(26.0)
