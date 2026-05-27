"""Tests for observed Tmax JSONL -> CSV conversion script."""

from __future__ import annotations

import csv
import importlib.util
import sys
from datetime import date
from pathlib import Path

from polytempo.weather.observations import ObservedTmax, write_observations_jsonl

_SCRIPT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "5_build_observed_tmax_csv.py"
_SPEC = importlib.util.spec_from_file_location("build_observed_tmax_csv", _SCRIPT_PATH)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _MODULE
_SPEC.loader.exec_module(_MODULE)


def test_write_observed_tmax_csv_round_trip(tmp_path: Path) -> None:
    in_path = tmp_path / "observed_tmax.jsonl"
    out_path = tmp_path / "observed_tmax.csv"
    rows = [
        ObservedTmax(
            station_id="EGLC",
            target_date=date(2026, 5, 14),
            observed_tmax_c=22.0,
            source="wunderground",
        ),
        ObservedTmax(
            station_id="EGLC",
            target_date=date(2026, 5, 15),
            observed_tmax_c=20.0,
            source="wunderground",
        ),
    ]
    write_observations_jsonl(rows, in_path)

    count = _MODULE.build_observed_tmax_csv(in_path, out_path)

    assert count == 2
    with out_path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        assert reader.fieldnames == list(_MODULE.OBSERVED_TMAX_COLUMNS)
        loaded = list(reader)

    assert len(loaded) == 2
    assert loaded[0]["station_id"] == "EGLC"
    assert loaded[0]["target_date"] == "2026-05-14"
    assert loaded[0]["observed_tmax_c"] == "22.0"
    assert loaded[0]["source"] == "wunderground"
    assert loaded[1]["target_date"] == "2026-05-15"


def test_build_observed_tmax_csv_empty_jsonl_writes_header_only(tmp_path: Path) -> None:
    in_path = tmp_path / "observed_tmax.jsonl"
    out_path = tmp_path / "observed_tmax.csv"
    in_path.write_text("", encoding="utf-8")

    count = _MODULE.build_observed_tmax_csv(in_path, out_path)

    assert count == 0
    with out_path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        assert reader.fieldnames == list(_MODULE.OBSERVED_TMAX_COLUMNS)
        assert list(reader) == []


def test_main_returns_2_when_no_observations(
    tmp_path: Path,
    monkeypatch,
) -> None:
    in_path = tmp_path / "missing.jsonl"
    out_path = tmp_path / "observed_tmax.csv"
    monkeypatch.setattr(_MODULE, "IN_PATH", in_path)
    monkeypatch.setattr(_MODULE, "OUT_PATH", out_path)

    assert _MODULE.main() == 2
    assert not out_path.exists()
