"""Tests for raw Single Runs JSON -> processed forecast_records.csv builder."""

from __future__ import annotations

import csv
import importlib.util
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

_SCRIPT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "4_build_forecast_records_csv.py"
_SPEC = importlib.util.spec_from_file_location("build_forecast_records_csv", _SCRIPT_PATH)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _MODULE
_SPEC.loader.exec_module(_MODULE)


def test_compute_lead_hours_spec_example() -> None:
    run_time = datetime(2026, 4, 1, 12, 0, tzinfo=timezone.utc)
    assert _MODULE.compute_lead_hours(run_time, date(2026, 4, 2)) == pytest.approx(36.0)


def test_compute_lead_hours_same_day_and_next_day() -> None:
    run_time = datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc)
    assert _MODULE.compute_lead_hours(run_time, date(2026, 5, 22)) == pytest.approx(12.0)
    assert _MODULE.compute_lead_hours(run_time, date(2026, 5, 23)) == pytest.approx(36.0)


def test_iter_forecast_rows_from_payload_skips_null_and_negative_lead() -> None:
    payload = {
        "latitude": 51.5,
        "longitude": 0.0,
        "daily": {
            "time": ["2026-05-22", "2026-05-23", "2026-05-24"],
            "temperature_2m_max": [27.7, None, 31.2],
        },
    }
    run_time = datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc)

    rows = list(
        _MODULE.iter_forecast_rows_from_payload(
            payload,
            station_id="EGLC",
            model="ecmwf_ifs025",
            run_time_utc=run_time,
        )
    )

    assert len(rows) == 2
    assert rows[0].target_date == date(2026, 5, 22)
    assert rows[0].predicted_tmax_c == pytest.approx(27.7)
    assert rows[0].lead_hours == pytest.approx(12.0)
    assert rows[0].forecast_lat == pytest.approx(51.5)
    assert rows[0].forecast_lon == pytest.approx(0.0)
    assert rows[1].target_date == date(2026, 5, 24)
    assert rows[1].predicted_tmax_c == pytest.approx(31.2)
    assert rows[1].lead_hours == pytest.approx(60.0)


def test_build_forecast_rows_reads_raw_files(tmp_path: Path) -> None:
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()

    payload_a = {
        "latitude": 51.507725,
        "longitude": 0.042434692,
        "daily": {
            "time": ["2026-05-02", "2026-05-03"],
            "temperature_2m_max": [22.6, 18.3],
        },
    }
    payload_b = {
        "latitude": 51.5,
        "longitude": 0.0,
        "daily": {
            "time": ["2026-05-22", "2026-05-23", "2026-05-24"],
            "temperature_2m_max": [27.7, 29.6, None],
        },
    }

    (raw_dir / "EGLC_ukmo_uk_deterministic_2km_2026-05-02T030000Z.json").write_text(
        json.dumps(payload_a),
        encoding="utf-8",
    )
    (raw_dir / "EGLC_ecmwf_ifs025_2026-05-22T120000Z.json").write_text(
        json.dumps(payload_b),
        encoding="utf-8",
    )

    rows = _MODULE.build_forecast_rows(raw_dir)

    assert len(rows) == 4
    assert rows[0].station_id == "EGLC"
    assert rows[0].model == "ecmwf_ifs025"
    assert rows[0].run_time_utc == datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc)
    assert rows[0].target_date == date(2026, 5, 22)
    assert rows[1].target_date == date(2026, 5, 23)
    assert rows[2].station_id == "EGLC"
    assert rows[2].model == "ukmo_uk_deterministic_2km"
    assert rows[2].run_time_utc == datetime(2026, 5, 2, 3, 0, tzinfo=timezone.utc)
    assert rows[2].target_date == date(2026, 5, 2)
    assert rows[3].target_date == date(2026, 5, 3)


def test_write_forecast_records_csv_round_trip(tmp_path: Path) -> None:
    rows = [
        _MODULE.ForecastRow(
            station_id="EGLC",
            model="ecmwf_ifs025",
            run_time_utc=datetime(2026, 4, 1, 12, 0, tzinfo=timezone.utc),
            target_date=date(2026, 4, 2),
            lead_hours=36.0,
            predicted_tmax_c=17.0,
            forecast_lat=51.5,
            forecast_lon=0.0,
        )
    ]
    out_path = tmp_path / "forecast_records.csv"

    _MODULE.write_forecast_records_csv(rows, out_path)

    with out_path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        assert reader.fieldnames == list(_MODULE.FORECAST_RECORD_COLUMNS)
        loaded = list(reader)

    assert len(loaded) == 1
    assert loaded[0]["station_id"] == "EGLC"
    assert loaded[0]["model"] == "ecmwf_ifs025"
    assert loaded[0]["run_time_utc"] == "2026-04-01T12:00:00Z"
    assert loaded[0]["target_date"] == "2026-04-02"
    assert loaded[0]["lead_hours"] == "36"
    assert loaded[0]["predicted_tmax_c"] == "17.0"
    assert loaded[0]["forecast_lat"] == "51.5"
    assert loaded[0]["forecast_lon"] == "0.0"
