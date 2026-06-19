"""Tests for calibration_runner pure helpers."""

from __future__ import annotations

from datetime import date

from polytempo.weather.wunderground import _observed_tmax_from_celsius


def test_observed_tmax_from_celsius_rounds_to_integer() -> None:
    row = _observed_tmax_from_celsius(
        station_id="EGLC",
        target_date=date(2026, 6, 8),
        temp_c=16.4,
    )
    assert row.observed_tmax_c == 16.0
    assert row.observed_tmax_f is None


def test_observed_tmax_from_celsius_keeps_integer() -> None:
    row = _observed_tmax_from_celsius(
        station_id="EGLC",
        target_date=date(2026, 6, 8),
        temp_c=17.0,
    )
    assert row.observed_tmax_c == 17.0
    assert row.observed_tmax_f is None
