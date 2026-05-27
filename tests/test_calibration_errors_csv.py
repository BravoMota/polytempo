"""Tests for offline forecast-error / calibration-stats CSV builder."""

from __future__ import annotations

import csv
import importlib.util
import math
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

_SCRIPT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "6_compute_calibration_errors.py"
_SPEC = importlib.util.spec_from_file_location("compute_calibration_errors", _SCRIPT_PATH)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _MODULE
_SPEC.loader.exec_module(_MODULE)


def _forecast_record(
    *,
    station_id: str = "EGLC",
    model: str = "ecmwf_ifs025",
    run_time_utc: datetime = datetime(2026, 4, 1, 12, 0, tzinfo=timezone.utc),
    target_date: date = date(2026, 4, 2),
    lead_hours: float = 36.0,
    predicted_tmax_c: float = 17.0,
) -> dict[str, object]:
    return {
        "station_id": station_id,
        "model": model,
        "run_time_utc": run_time_utc,
        "target_date": target_date,
        "lead_hours": lead_hours,
        "predicted_tmax_c": predicted_tmax_c,
    }


def test_join_with_observations_skips_unmatched() -> None:
    records = [
        _forecast_record(target_date=date(2026, 4, 1), predicted_tmax_c=16.0),
        _forecast_record(target_date=date(2026, 4, 2), predicted_tmax_c=17.0),
        _forecast_record(target_date=date(2026, 4, 3), predicted_tmax_c=18.0),
    ]
    observations = {
        ("EGLC", date(2026, 4, 1)): 15.0,
        ("EGLC", date(2026, 4, 3)): 19.5,
    }

    rows = _MODULE.join_with_observations(records, observations)

    assert [row.target_date for row in rows] == [date(2026, 4, 1), date(2026, 4, 3)]


def test_join_with_observations_matches_on_station_id() -> None:
    records = [
        _forecast_record(station_id="EGLC", predicted_tmax_c=17.0),
        _forecast_record(station_id="KJFK", predicted_tmax_c=17.0),
    ]
    observations = {("KJFK", date(2026, 4, 2)): 16.5}

    rows = _MODULE.join_with_observations(records, observations)

    assert len(rows) == 1
    assert rows[0].station_id == "KJFK"
    assert rows[0].observed_tmax_c == pytest.approx(16.5)


def test_join_computes_error_columns() -> None:
    records = [_forecast_record(predicted_tmax_c=20.0)]
    observations = {("EGLC", date(2026, 4, 2)): 17.5}

    [row] = _MODULE.join_with_observations(records, observations)

    assert row.predicted_tmax_c == pytest.approx(20.0)
    assert row.observed_tmax_c == pytest.approx(17.5)
    assert row.error_c == pytest.approx(2.5)
    assert row.abs_error_c == pytest.approx(2.5)
    assert row.squared_error_c == pytest.approx(6.25)


def test_compute_calibration_stats_groups_by_exact_lead_hours() -> None:
    records = [
        _forecast_record(lead_hours=24.0, target_date=date(2026, 4, 1), predicted_tmax_c=18.0),
        _forecast_record(lead_hours=24.0, target_date=date(2026, 4, 2), predicted_tmax_c=21.0),
        _forecast_record(lead_hours=27.0, target_date=date(2026, 4, 3), predicted_tmax_c=22.0),
    ]
    observations = {
        ("EGLC", date(2026, 4, 1)): 16.0,
        ("EGLC", date(2026, 4, 2)): 22.0,
        ("EGLC", date(2026, 4, 3)): 20.0,
    }

    errors = _MODULE.join_with_observations(records, observations)
    stats = _MODULE.compute_calibration_stats(errors)

    assert [row.lead_hours for row in stats] == [24.0, 27.0]
    assert [row.n_samples for row in stats] == [2, 1]


def test_compute_calibration_stats_metric_values() -> None:
    errors = [
        _MODULE.ForecastError(
            station_id="EGLC",
            model="m",
            run_time_utc=datetime(2026, 4, 1, 0, 0, tzinfo=timezone.utc),
            target_date=date(2026, 4, 1),
            lead_hours=24.0,
            predicted_tmax_c=20.0,
            observed_tmax_c=18.0,
            error_c=2.0,
            abs_error_c=2.0,
            squared_error_c=4.0,
        ),
        _MODULE.ForecastError(
            station_id="EGLC",
            model="m",
            run_time_utc=datetime(2026, 4, 2, 0, 0, tzinfo=timezone.utc),
            target_date=date(2026, 4, 2),
            lead_hours=24.0,
            predicted_tmax_c=15.0,
            observed_tmax_c=20.0,
            error_c=-5.0,
            abs_error_c=5.0,
            squared_error_c=25.0,
        ),
        _MODULE.ForecastError(
            station_id="EGLC",
            model="m",
            run_time_utc=datetime(2026, 4, 3, 0, 0, tzinfo=timezone.utc),
            target_date=date(2026, 4, 3),
            lead_hours=24.0,
            predicted_tmax_c=22.0,
            observed_tmax_c=19.0,
            error_c=3.0,
            abs_error_c=3.0,
            squared_error_c=9.0,
        ),
    ]

    [stat] = _MODULE.compute_calibration_stats(errors)

    assert stat.n_samples == 3
    assert stat.bias_c == pytest.approx((2.0 - 5.0 + 3.0) / 3)
    assert stat.mae_c == pytest.approx((2.0 + 5.0 + 3.0) / 3)
    assert stat.rmse_c == pytest.approx(math.sqrt((4.0 + 25.0 + 9.0) / 3))
    # Sample standard deviation of [2, -5, 3] -> sqrt(((2-0)^2 + (-5-0)^2 + (3-0)^2) / (n - 1))
    assert stat.error_std_c == pytest.approx(math.sqrt(((2.0) ** 2 + (-5.0) ** 2 + (3.0) ** 2) / 2))


def test_compute_calibration_stats_one_sample_group_returns_zero_std() -> None:
    records = [_forecast_record(predicted_tmax_c=20.0)]
    observations = {("EGLC", date(2026, 4, 2)): 17.0}

    errors = _MODULE.join_with_observations(records, observations)
    [stat] = _MODULE.compute_calibration_stats(errors)

    assert stat.n_samples == 1
    assert stat.error_std_c == 0.0


def test_load_forecast_records_skips_invalid_rows(tmp_path: Path) -> None:
    csv_path = tmp_path / "forecast_records.csv"
    csv_path.write_text(
        "station_id,model,run_time_utc,target_date,lead_hours,predicted_tmax_c,forecast_lat,forecast_lon\n"
        "EGLC,m,2026-04-01T12:00:00Z,2026-04-02,36,17.0,51.5,0.0\n"
        ",m,2026-04-01T12:00:00Z,2026-04-02,36,17.0,51.5,0.0\n"
        "EGLC,m,bad-date,2026-04-02,36,17.0,51.5,0.0\n"
        "EGLC,m,2026-04-01T12:00:00Z,2026-04-02,36,not-a-number,51.5,0.0\n"
        "EGLC,m,2026-04-01T12:00:00Z,2026-04-02,36,nan,51.5,0.0\n",
        encoding="utf-8",
    )

    rows = _MODULE.load_forecast_records(csv_path)

    assert len(rows) == 1
    assert rows[0]["station_id"] == "EGLC"
    assert rows[0]["predicted_tmax_c"] == pytest.approx(17.0)


def test_load_observations_map_skips_invalid_rows(tmp_path: Path) -> None:
    csv_path = tmp_path / "observed_tmax.csv"
    csv_path.write_text(
        "station_id,target_date,observed_tmax_c,source\n"
        "EGLC,2026-04-01,18.0,wunderground\n"
        "EGLC,2026-04-02,,wunderground\n"
        "EGLC,bad,19.0,wunderground\n",
        encoding="utf-8",
    )

    observations = _MODULE.load_observations_map(csv_path)

    assert observations == {("EGLC", date(2026, 4, 1)): 18.0}


def test_write_forecast_errors_csv_round_trip(tmp_path: Path) -> None:
    rows = [
        _MODULE.ForecastError(
            station_id="EGLC",
            model="ecmwf_ifs025",
            run_time_utc=datetime(2026, 4, 1, 12, 0, tzinfo=timezone.utc),
            target_date=date(2026, 4, 2),
            lead_hours=36.0,
            predicted_tmax_c=17.0,
            observed_tmax_c=15.0,
            error_c=2.0,
            abs_error_c=2.0,
            squared_error_c=4.0,
        )
    ]
    out_path = tmp_path / "forecast_errors.csv"

    _MODULE.write_forecast_errors_csv(rows, out_path)

    with out_path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        assert reader.fieldnames == list(_MODULE.FORECAST_ERROR_COLUMNS)
        loaded = list(reader)

    assert len(loaded) == 1
    assert loaded[0]["run_time_utc"] == "2026-04-01T12:00:00Z"
    assert loaded[0]["target_date"] == "2026-04-02"
    assert loaded[0]["lead_hours"] == "36"
    assert loaded[0]["error_c"] == "2.0"
    assert loaded[0]["squared_error_c"] == "4.0"


def test_write_calibration_stats_csv_columns(tmp_path: Path) -> None:
    rows = [
        _MODULE.CalibrationStatRow(
            station_id="EGLC",
            model="ecmwf_ifs025",
            lead_hours=24.0,
            n_samples=3,
            bias_c=0.0,
            mae_c=3.33,
            rmse_c=3.91,
            error_std_c=4.36,
        )
    ]
    out_path = tmp_path / "calibration_stats.csv"

    _MODULE.write_calibration_stats_csv(rows, out_path)

    with out_path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        assert reader.fieldnames == list(_MODULE.CALIBRATION_STAT_COLUMNS)
        loaded = list(reader)

    assert loaded[0]["lead_hours"] == "24"
    assert loaded[0]["n_samples"] == "3"
    assert "error_std_c" in loaded[0]
    assert "std_error_c" not in loaded[0]
