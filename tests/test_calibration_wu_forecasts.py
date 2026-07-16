"""Tests for WU forecast calibration helpers."""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from polytempo.weather.calibration_storage import WuHistoryObservationRow
from polytempo.weather.calibration_wu_forecasts import (
    adjusted_predicted_tmax_c,
    bucket_wall_clock_lead_hours,
    is_oclock_scrape,
    normalize_scrape_time,
    observed_running_max_c,
)


def test_is_oclock_scrape() -> None:
    on_hour = datetime(2026, 6, 7, 14, 0, 37, tzinfo=timezone.utc)
    off_hour = datetime(2026, 6, 7, 14, 10, 0, tzinfo=timezone.utc)
    assert is_oclock_scrape(on_hour) is True
    assert is_oclock_scrape(off_hour) is False


def test_normalize_scrape_time_zeros_seconds() -> None:
    scraped = datetime(2026, 6, 7, 14, 0, 37, tzinfo=timezone.utc)
    assert normalize_scrape_time(scraped) == datetime(2026, 6, 7, 14, 0, tzinfo=timezone.utc)


def test_bucket_wall_clock_lead_hours() -> None:
    assert bucket_wall_clock_lead_hours(59.9) == 59
    assert bucket_wall_clock_lead_hours(60.0) == 60
    assert bucket_wall_clock_lead_hours(60.1) == 60
    assert bucket_wall_clock_lead_hours(61.0) is None
    assert bucket_wall_clock_lead_hours(-0.1) is None


def test_adjusted_predicted_tmax_future_target_uses_forecast_only() -> None:
    target = date(2026, 6, 8)
    as_of = datetime(2026, 6, 7, 12, 0, tzinfo=timezone.utc)
    from polytempo.weather.calibration_wu_forecasts import ForecastHourRow

    predicted = adjusted_predicted_tmax_c(
        target_date=target,
        as_of_utc=as_of,
        hourly_forecast_rows=[
            ForecastHourRow(
                target_time_utc=datetime(2026, 6, 8, 13, 0, tzinfo=timezone.utc),
                temp_c=20.0,
            ),
            ForecastHourRow(
                target_time_utc=datetime(2026, 6, 8, 15, 0, tzinfo=timezone.utc),
                temp_c=24.0,
            ),
        ],
        history_observations=[],
    )
    assert predicted == pytest.approx(24.0)


def test_adjusted_predicted_tmax_same_day_obs_beats_remaining_fc() -> None:
    target = date(2026, 6, 8)
    as_of = datetime(2026, 6, 8, 12, 0, tzinfo=timezone.utc)
    from polytempo.weather.calibration_wu_forecasts import ForecastHourRow

    predicted = adjusted_predicted_tmax_c(
        target_date=target,
        as_of_utc=as_of,
        hourly_forecast_rows=[
            ForecastHourRow(
                target_time_utc=datetime(2026, 6, 8, 15, 0, tzinfo=timezone.utc),
                temp_c=22.0,
            ),
        ],
        history_observations=[
            WuHistoryObservationRow(
                station_id="EGLC",
                target_date=target,
                observed_at_utc=datetime(2026, 6, 8, 11, 0, tzinfo=timezone.utc),
                observed_at_local=None,
                temp_c=25.0,
            ),
        ],
    )
    assert predicted == pytest.approx(25.0)


def test_adjusted_predicted_tmax_combines_obs_and_fc() -> None:
    target = date(2026, 6, 8)
    as_of = datetime(2026, 6, 8, 12, 0, tzinfo=timezone.utc)
    from polytempo.weather.calibration_wu_forecasts import ForecastHourRow

    predicted = adjusted_predicted_tmax_c(
        target_date=target,
        as_of_utc=as_of,
        hourly_forecast_rows=[
            ForecastHourRow(
                target_time_utc=datetime(2026, 6, 8, 15, 0, tzinfo=timezone.utc),
                temp_c=26.0,
            ),
        ],
        history_observations=[
            WuHistoryObservationRow(
                station_id="EGLC",
                target_date=target,
                observed_at_utc=datetime(2026, 6, 8, 11, 0, tzinfo=timezone.utc),
                observed_at_local=None,
                temp_c=20.0,
            ),
        ],
    )
    assert predicted == pytest.approx(26.0)


def test_observed_running_max_c_respects_as_of_cutoff() -> None:
    target = date(2026, 6, 8)
    history = [
        WuHistoryObservationRow(
            station_id="EGLC",
            target_date=target,
            observed_at_utc=datetime(2026, 6, 8, 10, 0, tzinfo=timezone.utc),
            observed_at_local=None,
            temp_c=20.0,
        ),
        WuHistoryObservationRow(
            station_id="EGLC",
            target_date=target,
            observed_at_utc=datetime(2026, 6, 8, 12, 0, tzinfo=timezone.utc),
            observed_at_local=None,
            temp_c=23.0,
        ),
        WuHistoryObservationRow(
            station_id="EGLC",
            target_date=target,
            observed_at_utc=datetime(2026, 6, 8, 14, 0, tzinfo=timezone.utc),
            observed_at_local=None,
            temp_c=25.0,
        ),
    ]
    as_of = datetime(2026, 6, 8, 12, 30, tzinfo=timezone.utc)
    assert observed_running_max_c(
        history, target_date=target, as_of_utc=as_of
    ) == pytest.approx(23.0)


def test_observed_running_max_c_empty_when_no_obs() -> None:
    assert (
        observed_running_max_c(
            [],
            target_date=date(2026, 6, 8),
            as_of_utc=datetime(2026, 6, 8, 12, 0, tzinfo=timezone.utc),
        )
        is None
    )
