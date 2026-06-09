"""Tests for calibration join/aggregate logic."""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from polytempo.weather.calibration_compute import (
    ForecastRecord,
    compute_calibration_stats,
    join_with_observations,
)


def _record(
    *,
    station_id: str = "EGLC",
    model: str = "ecmwf_ifs025",
    target_date: date = date(2026, 4, 2),
    lead_hours: float = 36.0,
    predicted_tmax_c: float = 17.0,
) -> ForecastRecord:
    return ForecastRecord(
        station_id=station_id,
        model=model,
        run_time_utc=datetime(2026, 4, 1, 12, 0, tzinfo=timezone.utc),
        target_date=target_date,
        lead_hours=lead_hours,
        predicted_tmax_c=predicted_tmax_c,
    )


def test_join_with_observations_inner_join_only() -> None:
    forecasts = [
        _record(target_date=date(2026, 4, 2), predicted_tmax_c=17.0),
        _record(target_date=date(2026, 4, 3), predicted_tmax_c=18.0),
    ]
    observations = {( "EGLC", date(2026, 4, 2)): 16.0}

    errors = join_with_observations(forecasts, observations)
    assert len(errors) == 1
    assert errors[0].error_c == pytest.approx(1.0)


def test_compute_calibration_stats_groups_by_exact_lead_hours() -> None:
    forecasts = [
        _record(model="m1", lead_hours=36.0, predicted_tmax_c=17.0),
        _record(model="m1", lead_hours=39.0, predicted_tmax_c=18.0),
    ]
    observations = {
        ("EGLC", date(2026, 4, 2)): 16.0,
    }
    errors = join_with_observations(forecasts, observations)
    stats = compute_calibration_stats(errors)
    assert len(stats) == 2
    lead_hours = {row.lead_hours for row in stats}
    assert lead_hours == {36.0, 39.0}
