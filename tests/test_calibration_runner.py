"""Tests for calibration_runner pure helpers."""

from __future__ import annotations

import csv
from datetime import date, datetime, timedelta
from datetime import timezone as dt_timezone
from pathlib import Path

import pytest

from polytempo.weather import calibration_runner
from polytempo.weather.calibration_config import (
    DEFAULT_CALIBRATION_CONFIG_PATH,
    load_calibration_config,
)
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


# --- per-station model sets: the ingest loop must not cross-product ----------


@pytest.fixture(scope="module")
def config():
    return load_calibration_config(DEFAULT_CALIBRATION_CONFIG_PATH)


def _record_fetch_calls(monkeypatch) -> list[tuple[str, str]]:
    """Stub the Single-Runs fetch and record (station_id, model) pairs."""
    calls: list[tuple[str, str]] = []

    def fake_fetch(latitude, longitude, model, forecast_run_time, **kwargs):
        calls.append((kwargs["station_id"], model))
        return {}

    monkeypatch.setattr(
        calibration_runner,
        "load_or_fetch_single_run_payload",
        fake_fetch,
    )
    return calls


def _record_full_fetch_calls(monkeypatch) -> list[tuple]:
    """As above, but keeping every request parameter the loop controls."""
    calls: list[tuple] = []

    def fake_fetch(latitude, longitude, model, forecast_run_time, **kwargs):
        calls.append(
            (
                kwargs["station_id"],
                model,
                forecast_run_time.isoformat(),
                kwargs["forecast_days"],
                kwargs["timezone"],
                latitude,
                longitude,
            )
        )
        return {}

    monkeypatch.setattr(
        calibration_runner,
        "load_or_fetch_single_run_payload",
        fake_fetch,
    )
    return calls


def _run_ingest(config, tmp_path: Path, *, days: int = 0) -> tuple[int, int]:
    # raw_dir is empty, so no cached payload is ever ingested and ``conn`` is
    # never touched.
    start = datetime(2026, 6, 8, 0, 0, tzinfo=dt_timezone.utc)
    end = start + timedelta(days=days)
    return calibration_runner.ingest_forecasts_for_range(
        None,
        config,
        run_start=start,
        run_end=end,
        client=None,
        raw_dir=tmp_path / "raw",
    )


def test_ingest_requests_each_stations_own_model_set(monkeypatch, config, tmp_path) -> None:
    calls = _record_fetch_calls(monkeypatch)
    _run_ingest(config, tmp_path)

    requested = {station: set() for station in ("EGLC", "LEMD")}
    for station_id, model in calls:
        requested[station_id].add(model)

    assert requested["EGLC"] == {m.name for m in config.models_for("EGLC")}
    assert requested["LEMD"] == {m.name for m in config.models_for("LEMD")}


def test_ingest_never_requests_uk_2km_at_madrid(monkeypatch, config, tmp_path) -> None:
    calls = _record_fetch_calls(monkeypatch)
    _run_ingest(config, tmp_path)
    assert ("LEMD", "ukmo_uk_deterministic_2km") not in calls


def test_ingest_never_requests_arpege_at_london(monkeypatch, config, tmp_path) -> None:
    calls = _record_fetch_calls(monkeypatch)
    _run_ingest(config, tmp_path)
    assert ("EGLC", "meteofrance_arpege_europe") not in calls


def test_ingest_requests_arpege_at_madrid(monkeypatch, config, tmp_path) -> None:
    calls = _record_fetch_calls(monkeypatch)
    _run_ingest(config, tmp_path)
    assert ("LEMD", "meteofrance_arpege_europe") in calls


def test_ingest_for_london_only_is_identical_to_the_pre_change_behaviour(
    monkeypatch, config, tmp_path
) -> None:
    """A London-only config produces exactly the old (station x global models) calls."""
    calls = _record_fetch_calls(monkeypatch)
    _run_ingest(config.subset_stations(["EGLC"]), tmp_path)

    expected = [("EGLC", m.name) for m in config.models]
    assert sorted(set(calls)) == sorted(set(expected))
    assert {station for station, _ in calls} == {"EGLC"}


def test_adding_madrid_does_not_change_a_single_london_request(
    monkeypatch, config, tmp_path
) -> None:
    """Strictly additive: London's ordered request sequence is untouched by LEMD.

    Compares the full (station, model, run_init, forecast_days, tz, lat, lon)
    sequence over a 3-day window from the two-station config against the same
    sequence from a London-only config.
    """
    both = _record_full_fetch_calls(monkeypatch)
    _run_ingest(config, tmp_path, days=3)
    london_from_both = [call for call in both if call[0] == "EGLC"]

    london_only = _record_full_fetch_calls(monkeypatch)
    _run_ingest(config.subset_stations(["EGLC"]), tmp_path, days=3)

    assert london_from_both == london_only
    assert london_only  # the window really did issue requests
    assert {call[1] for call in london_only} == {m.name for m in config.models}


def test_station_subset_limits_ingest_to_madrid(monkeypatch, config, tmp_path) -> None:
    calls = _record_fetch_calls(monkeypatch)
    _run_ingest(config.subset_stations(["LEMD"]), tmp_path)
    assert {station for station, _ in calls} == {"LEMD"}


# --- the stats CSV writer is never station scoped ----------------------------


class _Row(dict):
    """Minimal stand-in for a psycopg dict row."""


class _FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class _FakeConn:
    """Serves the two SELECTs recompute_updated_stats issues, for both stations."""

    def __init__(self, observations, forecasts):
        self._observations = observations
        self._forecasts = forecasts

    def execute(self, sql, params=None):
        if "calibration_observed_tmax" in sql:
            return _FakeCursor(self._observations)
        if "model <> ALL" in sql:
            excluded = set(params[0])
            return _FakeCursor(
                [r for r in self._forecasts if r["model"] not in excluded]
            )
        if "WHERE model = " in sql:
            return _FakeCursor([r for r in self._forecasts if r["model"] == params[0]])
        return _FakeCursor(list(self._forecasts))


def _two_station_store():
    observations = [
        _Row(station_id="EGLC", target_date="2026-06-08", observed_tmax_c=20.0),
        _Row(station_id="EGLC", target_date="2026-06-09", observed_tmax_c=21.0),
        _Row(station_id="LEMD", target_date="2026-06-08", observed_tmax_c=30.0),
        _Row(station_id="LEMD", target_date="2026-06-09", observed_tmax_c=31.0),
    ]
    forecasts = []
    for station_id, model, bias in (
        ("EGLC", "ukmo_uk_deterministic_2km", 0.5),
        ("LEMD", "meteofrance_arpege_europe", -0.5),
    ):
        for day, observed in (("2026-06-08", 20.0), ("2026-06-09", 21.0)):
            forecasts.append(
                _Row(
                    station_id=station_id,
                    model=model,
                    run_time_utc="2026-06-07T00:00:00+00:00",
                    target_date=day,
                    lead_hours=36.0,
                    predicted_tmax_c=(observed if station_id == "EGLC" else observed + 10.0)
                    + bias,
                    forecast_lat=None,
                    forecast_lon=None,
                )
            )
    return observations, forecasts


def _stations_in_csv(path: Path) -> set[str]:
    with path.open(encoding="utf-8", newline="") as handle:
        return {row["station_id"] for row in csv.DictReader(handle)}


def test_recompute_writes_every_station_in_the_store(tmp_path) -> None:
    observations, forecasts = _two_station_store()
    out = tmp_path / "calibration_stats_updated.csv"
    calibration_runner.recompute_updated_stats(_FakeConn(observations, forecasts), out)
    assert _stations_in_csv(out) == {"EGLC", "LEMD"}


def test_madrid_only_run_leaves_london_rows_in_the_csv(monkeypatch, config, tmp_path) -> None:
    """`--station LEMD` scopes ingest only; the CSV keeps every London row."""
    observations, forecasts = _two_station_store()
    out = tmp_path / "calibration_stats_updated.csv"

    # First write: the full (both-station) CSV that lives in git today.
    calibration_runner.recompute_updated_stats(_FakeConn(observations, forecasts), out)
    before = out.read_text(encoding="utf-8")
    london_before = [line for line in before.splitlines() if line.startswith("EGLC,")]
    assert london_before

    # Now the Madrid-scoped run: ingest is narrowed, recompute is not.
    madrid_only = config.subset_stations(["LEMD"])
    assert madrid_only.station_ids == ["LEMD"]
    calls = _record_fetch_calls(monkeypatch)
    _run_ingest(madrid_only, tmp_path)
    assert {station for station, _ in calls} == {"LEMD"}

    calibration_runner.recompute_updated_stats(_FakeConn(observations, forecasts), out)
    after = out.read_text(encoding="utf-8")

    assert [line for line in after.splitlines() if line.startswith("EGLC,")] == london_before
    assert after == before
    assert _stations_in_csv(out) == {"EGLC", "LEMD"}


def test_recompute_takes_no_station_argument() -> None:
    """Structural guard: nothing can accidentally narrow the CSV to one station."""
    import inspect

    params = inspect.signature(calibration_runner.recompute_updated_stats).parameters
    assert "station" not in " ".join(params)
