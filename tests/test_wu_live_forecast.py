"""Tests for live Wunderground forecast merge."""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from polytempo.weather.calibration_stats_csv import (
    LEAD_HOURS_ANCHOR_RUN_INIT,
    LEAD_HOURS_ANCHOR_SCRAPED_AT,
    CalibrationStatRow,
    select_best_model,
)
from polytempo.weather.calibration_storage import WU_FORECAST_MODEL
from polytempo.weather.schema import ForecastValues
from polytempo.weather.stations import get_station
from polytempo.weather.wu_live_forecast import (
    append_wunderground_forecast,
    parse_hourly_forecast_metric_payload,
)


def test_parse_hourly_forecast_metric_payload_filters_target_date() -> None:
    payload = {
        "temperature": [14.0, 15.0, 16.0],
        "validTimeLocal": [
            "2026-06-09T23:00:00+0100",
            "2026-06-10T00:00:00+0100",
            "2026-06-10T01:00:00+0100",
        ],
        "validTimeUtc": [1781050800, 1781054400, 1781058000],
    }
    rows = parse_hourly_forecast_metric_payload(payload, date(2026, 6, 10))
    assert len(rows) == 2
    assert rows[0].temp_c == pytest.approx(15.0)
    assert rows[1].temp_c == pytest.approx(16.0)


def test_append_wunderground_forecast_adds_model(monkeypatch: pytest.MonkeyPatch) -> None:
    base = ForecastValues(
        source="open_meteo",
        latitude=51.5,
        longitude=0.05,
        target_date=date(2026, 6, 10),
        values_c=[20.0],
        models=["ecmwf_ifs025"],
        init_lead_hours=[36.0],
        model_run_init_utc=["2026-06-09T10:00:00Z"],
    )
    station = get_station("london")
    as_of = datetime(2026, 6, 9, 12, 0, tzinfo=timezone.utc)

    monkeypatch.setattr(
        "polytempo.weather.wu_live_forecast.fetch_wu_adjusted_tmax_c",
        lambda *args, **kwargs: 22.5,
    )

    merged = append_wunderground_forecast(base, station, as_of_utc=as_of)
    assert merged.models == ["ecmwf_ifs025", WU_FORECAST_MODEL]
    assert merged.values_c == [20.0, 22.5]
    assert merged.model_run_init_utc == ["2026-06-09T10:00:00Z", ""]


def test_append_wunderground_snapshot_forecast_adds_model() -> None:
    from polytempo.weather.wu_live_forecast import append_wunderground_snapshot_forecast

    base = ForecastValues(
        source="open_meteo_snapshot",
        latitude=51.5,
        longitude=0.05,
        target_date=date(2026, 6, 10),
        values_c=[20.0],
        models=["ecmwf_ifs025"],
        init_lead_hours=[36.0],
        model_run_init_utc=["2026-06-09T10:00:00Z"],
    )
    as_of = datetime(2026, 6, 9, 12, 0, tzinfo=timezone.utc)

    merged = append_wunderground_snapshot_forecast(
        base,
        predicted_tmax_c=22.5,
        as_of_utc=as_of,
    )
    assert merged.models == ["ecmwf_ifs025", WU_FORECAST_MODEL]
    assert merged.values_c == [20.0, 22.5]
    assert merged.model_run_init_utc == ["2026-06-09T10:00:00Z", ""]


def test_append_wunderground_forecast_without_run_init_utc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DB snapshots omit model_run_init_utc; append must not synthesize a short list."""
    base = ForecastValues(
        source="open_meteo_snapshot",
        latitude=51.5,
        longitude=0.05,
        target_date=date(2026, 6, 10),
        values_c=[20.0, 21.0, 22.0, 23.0, 24.0],
        models=[
            "ecmwf_ifs025",
            "gfs_seamless",
            "icon_eu",
            "ukmo_global_deterministic_10km",
            "ukmo_uk_deterministic_2km",
        ],
        init_lead_hours=[36.0, 36.0, 36.0, 36.0, 36.0],
        model_run_init_utc=None,
    )
    station = get_station("london")
    as_of = datetime(2026, 6, 9, 12, 0, tzinfo=timezone.utc)

    monkeypatch.setattr(
        "polytempo.weather.wu_live_forecast.fetch_wu_adjusted_tmax_c",
        lambda *args, **kwargs: 22.5,
    )

    merged = append_wunderground_forecast(base, station, as_of_utc=as_of)
    assert merged.models is not None
    assert len(merged.models) == 6
    assert merged.model_run_init_utc is None
    assert merged.init_lead_hours is not None
    assert len(merged.init_lead_hours) == 6


def test_select_best_model_picks_wunderground_with_scraped_at_lead() -> None:
    rows = [
        CalibrationStatRow(
            station_id="EGLC",
            model="ecmwf_ifs025",
            lead_hours=36.0,
            n_samples=50,
            bias_c=0.0,
            mae_c=1.0,
            rmse_c=1.2,
            error_std_c=1.5,
            lead_hours_anchor=LEAD_HOURS_ANCHOR_RUN_INIT,
        ),
        CalibrationStatRow(
            station_id="EGLC",
            model=WU_FORECAST_MODEL,
            lead_hours=36.0,
            n_samples=40,
            bias_c=0.0,
            mae_c=0.8,
            rmse_c=0.9,
            error_std_c=0.6,
            lead_hours_anchor=LEAD_HOURS_ANCHOR_SCRAPED_AT,
        ),
    ]

    chosen = select_best_model(
        rows,
        station_id="EGLC",
        available_models=["ecmwf_ifs025", WU_FORECAST_MODEL],
        current_lead_hours=36.0,
        init_lead_hours_by_model={"ecmwf_ifs025": 38.0},
    )
    assert chosen is not None
    assert chosen[0].model == WU_FORECAST_MODEL
    assert chosen[0].lead_hours == 36.0
