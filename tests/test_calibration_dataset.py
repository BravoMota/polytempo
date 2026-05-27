"""Tests for calibration join and stats computation."""

from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from polytempo.weather.calibration_dataset import (
    CalibrationStats,
    ForecastErrorRecord,
    compute_calibration_stats,
    join_forecasts_with_observations,
    lead_bucket_for_hours,
    read_calibration_stats_json,
    write_calibration_stats_json,
)
from polytempo.weather.historical_forecasts import HistoricalForecastRecord
from polytempo.weather.observations import ObservedTmax


def test_lead_bucket_boundaries() -> None:
    assert lead_bucket_for_hours(0.0) == "0-6h"
    assert lead_bucket_for_hours(5.9) == "0-6h"
    assert lead_bucket_for_hours(6.0) == "6-12h"
    assert lead_bucket_for_hours(11.9) == "6-12h"
    assert lead_bucket_for_hours(12.0) == "12-24h"
    assert lead_bucket_for_hours(71.9) == "48-72h"
    assert lead_bucket_for_hours(72.0) == "72h+"


def test_join_forecasts_with_observations() -> None:
    forecasts = [
        HistoricalForecastRecord(
            station_id="EGLC",
            source="open_meteo_single_runs",
            model="ukmo_seamless",
            target_date=date(2026, 5, 14),
            forecast_run_time=datetime(2026, 5, 11, 0, 0, tzinfo=timezone.utc),
            lead_hours=72.0,
            predicted_tmax_c=24.0,
        ),
        HistoricalForecastRecord(
            station_id="EGLC",
            source="open_meteo_single_runs",
            model="ukmo_seamless",
            target_date=date(2026, 5, 15),
            forecast_run_time=datetime(2026, 5, 12, 0, 0, tzinfo=timezone.utc),
            lead_hours=72.0,
            predicted_tmax_c=20.0,
        ),
    ]
    observations = [
        ObservedTmax("EGLC", date(2026, 5, 14), 22.0, "manual"),
    ]

    joined = join_forecasts_with_observations(forecasts, observations)

    assert len(joined) == 1
    assert joined[0].error_c == pytest.approx(2.0)
    assert joined[0].observed_tmax_c == pytest.approx(22.0)


def test_compute_calibration_stats_metrics() -> None:
    errors = [
        ForecastErrorRecord(
            station_id="EGLC",
            source="open_meteo_single_runs",
            model="ukmo_seamless",
            target_date=date(2026, 5, 14),
            lead_hours=60.0,
            predicted_tmax_c=24.0,
            observed_tmax_c=22.0,
            error_c=2.0,
        ),
        ForecastErrorRecord(
            station_id="EGLC",
            source="open_meteo_single_runs",
            model="ukmo_seamless",
            target_date=date(2026, 5, 15),
            lead_hours=66.0,
            predicted_tmax_c=21.0,
            observed_tmax_c=22.0,
            error_c=-1.0,
        ),
    ]

    stats = compute_calibration_stats(errors)
    row = next(item for item in stats if item.lead_bucket == "48-72h")

    assert row.n_samples == 2
    assert row.bias_c == pytest.approx(0.5)
    assert row.mae_c == pytest.approx(1.5)
    assert row.rmse_c == pytest.approx((5.0 / 2) ** 0.5)


def test_calibration_stats_json_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "calibration_stats.json"
    stats = [
        CalibrationStats(
            station_id="EGLC",
            source="open_meteo_single_runs",
            model="ukmo_seamless",
            lead_bucket="48-72h",
            bias_c=0.5,
            rmse_c=1.58,
            mae_c=1.5,
            n_samples=2,
        )
    ]

    write_calibration_stats_json(stats, path)
    loaded = read_calibration_stats_json(path)

    assert len(loaded) == 1
    assert loaded[0].station_id == "EGLC"
    assert loaded[0].n_samples == 2
