"""Tests for offline historical forecast ingestion."""

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest
from typer.testing import CliRunner

from polytempo.cli.main import app
from polytempo.weather.historical_forecasts import (
    DEFAULT_MODEL_RUN_HOURS_UTC,
    HistoricalForecastJob,
    HistoricalForecastRecord,
    build_raw_forecast_filename,
    build_single_run_request_params,
    daily_tmax_series_from_payload,
    existing_record_keys,
    extract_predicted_tmax_c,
    fetch_historical_forecast_batch,
    fetch_historical_forecast_record,
    fetch_raw_forecast_runs,
    format_run_time_utc_for_filename,
    floor_run_time_to_init_grid,
    generate_forecast_run_times,
    generate_run_times_utc,
    job_key,
    load_or_fetch_single_run_payload,
    resolve_raw_fetch_run_times,
    parse_run_time_utc_from_raw_filename,
    parse_single_run_payload,
    plan_historical_forecast_requests,
    read_historical_forecasts_jsonl,
    read_raw_forecast_response,
    sanitize_filename_component,
    snap_run_time_to_model_init,
    write_historical_forecasts_jsonl,
    write_raw_forecast_response,
)


def _payload(*, model: str = "ukmo_seamless", tmax: float = 23.5) -> dict:
    return {
        "latitude": 51.5053,
        "longitude": 0.0553,
        "daily": {
            "time": ["2026-05-21", "2026-05-22"],
            f"temperature_2m_max_{model}": [22.0, tmax],
        },
    }


def test_generate_forecast_run_times_six_hour_cadence() -> None:
    runs = generate_forecast_run_times(
        date(2026, 5, 22),
        max_lead_hours=72,
        lead_step_hours=6,
        timezone="UTC",
    )

    assert [lead for _run, lead in runs] == [
        72,
        66,
        60,
        54,
        48,
        42,
        36,
        30,
        24,
        18,
        12,
        6,
    ]
    assert len(runs) == 12
    for run_utc, _lead in runs:
        assert run_utc.minute == 0
        assert run_utc.hour in DEFAULT_MODEL_RUN_HOURS_UTC


def test_snap_run_time_to_model_init_floors_to_synoptic_hour() -> None:
    snapped = snap_run_time_to_model_init(
        datetime(2026, 3, 29, 5, 0, tzinfo=timezone.utc)
    )
    assert snapped == datetime(2026, 3, 29, 0, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    ("run_time", "interval_hours", "expected"),
    [
        (
            datetime(2026, 6, 10, 1, 4, 17, tzinfo=timezone.utc),
            6.0,
            datetime(2026, 6, 10, 0, 0, tzinfo=timezone.utc),
        ),
        (
            datetime(2026, 6, 10, 1, 4, 17, tzinfo=timezone.utc),
            3.0,
            datetime(2026, 6, 10, 0, 0, tzinfo=timezone.utc),
        ),
        (
            datetime(2026, 6, 10, 7, 30, tzinfo=timezone.utc),
            6.0,
            datetime(2026, 6, 10, 6, 0, tzinfo=timezone.utc),
        ),
        (
            datetime(2026, 6, 10, 7, 30, tzinfo=timezone.utc),
            3.0,
            datetime(2026, 6, 10, 6, 0, tzinfo=timezone.utc),
        ),
    ],
)
def test_floor_run_time_to_init_grid(
    run_time: datetime,
    interval_hours: float,
    expected: datetime,
) -> None:
    assert floor_run_time_to_init_grid(run_time, interval_hours) == expected


def test_daily_incremental_run_times_stay_on_init_grid() -> None:
    """Wall-clock job finish must not produce fractional lead_hours run inits."""
    last_success = datetime(2026, 6, 10, 1, 4, 17, tzinfo=timezone.utc)
    run_end = datetime(2026, 6, 10, 23, 59, 59, tzinfo=timezone.utc)

    for interval_hours in (3.0, 6.0):
        grid_start = floor_run_time_to_init_grid(last_success, interval_hours)
        runs = generate_run_times_utc(grid_start, run_end, interval_hours)
        assert runs
        for run_utc in runs:
            assert run_utc.second == 0
            assert run_utc.microsecond == 0
            assert run_utc.minute == 0
            assert run_utc.hour % int(interval_hours) == 0


def test_generate_forecast_run_times_uses_timezone_anchor() -> None:
    runs = generate_forecast_run_times(
        date(2026, 5, 22),
        max_lead_hours=24,
        lead_step_hours=24,
        timezone="Europe/London",
    )
    run_utc, lead = runs[0]
    assert run_utc == datetime(2026, 5, 20, 18, 0, tzinfo=timezone.utc)
    assert lead == pytest.approx(29.0)


def test_parse_single_run_payload_model_suffixed_key() -> None:
    run_time = datetime(2026, 5, 19, 0, 0, tzinfo=timezone.utc)
    record = parse_single_run_payload(
        _payload(tmax=24.1),
        station_id="EGLC",
        source="open_meteo_single_runs",
        model="ukmo_seamless",
        target_date=date(2026, 5, 22),
        forecast_run_time=run_time,
        lead_hours=72.0,
    )

    assert record.predicted_tmax_c == pytest.approx(24.1)
    assert record.station_id == "EGLC"
    assert record.lead_hours == 72.0


def test_parse_single_run_payload_plain_key() -> None:
    payload = {
        "daily": {
            "time": ["2026-05-22"],
            "temperature_2m_max": [18.2],
        }
    }
    record = parse_single_run_payload(
        payload,
        station_id="EGLC",
        source="open_meteo_single_runs",
        model="ukmo_seamless",
        target_date=date(2026, 5, 22),
        forecast_run_time=datetime(2026, 5, 21, 0, 0, tzinfo=timezone.utc),
        lead_hours=24.0,
    )
    assert record.predicted_tmax_c == pytest.approx(18.2)


def _hourly_payload(
    first_stamp: str,
    hours: int,
    values: list[float | None],
    *,
    key: str = "temperature_2m",
) -> dict:
    """Local-stamped hourly payload starting at ``first_stamp`` for ``hours`` steps."""
    start = datetime.fromisoformat(first_stamp)
    times = [
        (start + timedelta(hours=offset)).strftime("%Y-%m-%dT%H:%M")
        for offset in range(hours)
    ]
    return {
        "latitude": 51.5,
        "longitude": 0.0625,
        "timezone": "Europe/London",
        "utc_offset_seconds": 3600,
        "hourly": {"time": times, key: values},
    }


def test_daily_tmax_series_reads_cached_daily_payload() -> None:
    """Raw files cached before the hourly switch must still parse unchanged."""
    times, values = daily_tmax_series_from_payload(_payload(tmax=24.1), "ukmo_seamless")
    assert times == ["2026-05-21", "2026-05-22"]
    assert values == [22.0, 24.1]


def test_daily_tmax_series_aggregates_hourly_to_local_day_max() -> None:
    payload = _hourly_payload(
        "2026-06-16T00:00",
        48,
        [float(hour % 24) for hour in range(48)],
    )
    times, values = daily_tmax_series_from_payload(payload, "icon_eu")
    assert times == ["2026-06-16", "2026-06-17"]
    assert values == [pytest.approx(23.0), pytest.approx(23.0)]


def test_daily_tmax_series_keeps_leading_part_day_and_drops_trailing_one() -> None:
    """A 06:00Z run covers 07:00 local onward; ``daily=`` reported the same days."""
    values: list[float | None] = [10.0] * 48
    values[10] = 21.9  # 2026-06-15T17:00 local
    values[30] = 23.1  # 2026-06-16T13:00 local
    values[45] = 99.0  # 2026-06-17T04:00 local — trailing part-day, must be dropped
    payload = _hourly_payload("2026-06-15T07:00", 48, values)

    times, maxima = daily_tmax_series_from_payload(payload, "icon_eu")

    assert times == ["2026-06-15", "2026-06-16"]
    assert maxima == [pytest.approx(21.9), pytest.approx(23.1)]


def test_daily_tmax_series_nulls_a_day_that_is_missing_an_hour() -> None:
    """``daily=`` reported null past the model horizon; a partial max must not leak."""
    values: list[float | None] = [12.0] * 48
    values[30] = 23.1  # 2026-06-17T06:00 local
    for index in range(32, 48):  # rest of 2026-06-17 beyond the model horizon
        values[index] = None
    payload = _hourly_payload("2026-06-16T00:00", 48, values)

    times, maxima = daily_tmax_series_from_payload(payload, "icon_eu")

    assert times == ["2026-06-16", "2026-06-17"]
    assert maxima == [pytest.approx(12.0), None]
    with pytest.raises(ValueError):
        extract_predicted_tmax_c(payload, "icon_eu", date(2026, 6, 17))


def test_daily_tmax_series_prefers_model_suffixed_hourly_key() -> None:
    payload = _hourly_payload(
        "2026-06-16T00:00",
        24,
        [12.0] * 23 + [19.5],
        key="temperature_2m_icon_eu",
    )
    times, maxima = daily_tmax_series_from_payload(payload, "icon_eu")
    assert times == ["2026-06-16"]
    assert maxima == [pytest.approx(19.5)]


def test_daily_tmax_series_rejects_payload_without_daily_or_hourly() -> None:
    with pytest.raises(ValueError):
        daily_tmax_series_from_payload({"latitude": 51.5}, "icon_eu")


def test_parse_single_run_payload_hourly_shape() -> None:
    payload = _hourly_payload("2026-05-19T07:00", 96, [15.0] * 96)
    payload["hourly"]["temperature_2m"][60] = 24.3  # 2026-05-21T19:00 local

    record = parse_single_run_payload(
        payload,
        station_id="EGLC",
        source="open_meteo_single_runs",
        model="ukmo_seamless",
        target_date=date(2026, 5, 21),
        forecast_run_time=datetime(2026, 5, 19, 6, 0, tzinfo=timezone.utc),
        lead_hours=42.0,
    )

    assert record.predicted_tmax_c == pytest.approx(24.3)
    assert record.target_date == date(2026, 5, 21)


def test_historical_forecasts_jsonl_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "forecasts.jsonl"
    run_time = datetime(2026, 5, 19, 12, 0, tzinfo=timezone.utc)
    record = HistoricalForecastRecord(
        station_id="EGLC",
        source="open_meteo_single_runs",
        model="ukmo_seamless",
        target_date=date(2026, 5, 22),
        forecast_run_time=run_time,
        lead_hours=66.0,
        predicted_tmax_c=23.9,
    )

    write_historical_forecasts_jsonl([record], path)
    loaded = read_historical_forecasts_jsonl(path)

    assert len(loaded) == 1
    assert loaded[0] == record


def test_fetch_batch_skips_existing_keys(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    out_path = tmp_path / "forecasts.jsonl"
    run_time = datetime(2026, 5, 19, 0, 0, tzinfo=timezone.utc)
    existing = HistoricalForecastRecord(
        station_id="EGLC",
        source="open_meteo_single_runs",
        model="ukmo_seamless",
        target_date=date(2026, 5, 22),
        forecast_run_time=run_time,
        lead_hours=72.0,
        predicted_tmax_c=23.0,
    )
    write_historical_forecasts_jsonl([existing], out_path)

    job = HistoricalForecastJob(
        station_id="EGLC",
        latitude=51.5053,
        longitude=0.0553,
        model="ukmo_seamless",
        target_date=date(2026, 5, 22),
        forecast_run_time=run_time,
        lead_hours=72.0,
        timezone="UTC",
    )

    def fail_get(*_args, **_kwargs):
        raise AssertionError("httpx.get should not be called for duplicate key")

    monkeypatch.setattr(httpx, "get", fail_get)

    fetched, skipped, failed = fetch_historical_forecast_batch([job], out_path=out_path)

    assert fetched == 0
    assert skipped == 1
    assert failed == 0
    assert len(read_historical_forecasts_jsonl(out_path)) == 1


def test_fetch_batch_appends_new_record(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    out_path = tmp_path / "forecasts.jsonl"
    run_time = datetime(2026, 5, 19, 0, 0, tzinfo=timezone.utc)
    job = HistoricalForecastJob(
        station_id="EGLC",
        latitude=51.5053,
        longitude=0.0553,
        model="ukmo_seamless",
        target_date=date(2026, 5, 22),
        forecast_run_time=run_time,
        lead_hours=72.0,
        timezone="UTC",
    )

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return _payload(tmax=25.0)

    monkeypatch.setattr(httpx, "get", lambda *_a, **_k: FakeResponse())

    fetched, skipped, failed = fetch_historical_forecast_batch([job], out_path=out_path)

    assert fetched == 1
    assert skipped == 0
    assert failed == 0
    rows = read_historical_forecasts_jsonl(out_path)
    assert rows[0].predicted_tmax_c == pytest.approx(25.0)


def test_plan_historical_forecast_requests_dedupes_runs() -> None:
    jobs = plan_historical_forecast_requests(
        station_id="EGLC",
        latitude=51.5053,
        longitude=0.0553,
        model="ukmo_seamless",
        start_date=date(2026, 5, 20),
        end_date=date(2026, 5, 21),
        max_lead_hours=72,
        lead_step_hours=6,
        timezone="UTC",
    )
    assert len(jobs) == 24
    assert len({job_key(job) for job in jobs}) == len(jobs)


def test_fetch_record_falls_back_when_primary_tmax_is_null(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = HistoricalForecastJob(
        station_id="EGLC",
        latitude=51.5053,
        longitude=0.0553,
        model="ukmo_seamless",
        target_date=date(2026, 5, 22),
        forecast_run_time=datetime(2026, 5, 19, 6, 0, tzinfo=timezone.utc),
        lead_hours=65.0,
        timezone="Europe/London",
    )
    calls: list[str] = []

    class FakeResponse:
        def __init__(self, payload: dict) -> None:
            self._payload = payload

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return self._payload

    def fake_get(_url: str, params: dict, **_kwargs: object) -> FakeResponse:
        calls.append(params["run"])
        if params["run"] == "2026-05-19T06:00":
            return FakeResponse(
                {
                    "daily": {
                        "time": ["2026-05-22"],
                        "temperature_2m_max": [None],
                    }
                }
            )
        return FakeResponse(
            {
                "daily": {
                    "time": ["2026-05-22"],
                    "temperature_2m_max": [24.3],
                }
            }
        )

    monkeypatch.setattr(httpx, "get", fake_get)

    record = fetch_historical_forecast_record(
        job,
        base_url="https://example.test",
        raw_dir=tmp_path / "raw",
    )

    assert calls == ["2026-05-19T06:00", "2026-05-19T12:00"]
    assert record.predicted_tmax_c == pytest.approx(24.3)
    assert record.forecast_run_time == datetime(2026, 5, 19, 12, 0, tzinfo=timezone.utc)


def test_existing_record_keys_matches_job_key(tmp_path: Path) -> None:
    run_time = datetime(2026, 5, 19, 0, 0, tzinfo=timezone.utc)
    record = HistoricalForecastRecord(
        station_id="EGLC",
        source="open_meteo_single_runs",
        model="ukmo_seamless",
        target_date=date(2026, 5, 22),
        forecast_run_time=run_time,
        lead_hours=72.0,
        predicted_tmax_c=23.0,
    )
    path = tmp_path / "forecasts.jsonl"
    write_historical_forecasts_jsonl([record], path)

    job = HistoricalForecastJob(
        station_id="EGLC",
        latitude=51.5053,
        longitude=0.0553,
        model="ukmo_seamless",
        target_date=date(2026, 5, 22),
        forecast_run_time=run_time,
        lead_hours=72.0,
        timezone="UTC",
    )
    keys = existing_record_keys(path)
    assert job_key(job) in keys


def test_generate_run_times_utc_steps_inclusive() -> None:
    start = datetime(2026, 5, 1, 0, 0, tzinfo=timezone.utc)
    end = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
    runs = generate_run_times_utc(start, end, interval_hours=6.0)
    assert runs == [
        datetime(2026, 5, 1, 0, 0, tzinfo=timezone.utc),
        datetime(2026, 5, 1, 6, 0, tzinfo=timezone.utc),
        datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc),
    ]


def test_resolve_raw_fetch_run_times_merges_explicit_and_range() -> None:
    runs = resolve_raw_fetch_run_times(
        ["2026-05-01T00:00:00Z"],
        run_time_start="2026-05-01T06:00:00Z",
        run_time_end="2026-05-01T12:00:00Z",
        run_interval_hours=6.0,
    )
    assert len(runs) == 3
    assert runs[0] == datetime(2026, 5, 1, 0, 0, tzinfo=timezone.utc)
    assert runs[-1] == datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)


def test_build_single_run_request_params_shape() -> None:
    params = build_single_run_request_params(
        51.5053,
        0.0553,
        "ukmo_seamless",
        date(2026, 5, 22),
        datetime(2026, 5, 19, 0, 0, tzinfo=timezone.utc),
        timezone="Europe/London",
        forecast_days=14,
    )
    assert params["hourly"] == "temperature_2m"
    assert "daily" not in params
    assert params["models"] == "ukmo_seamless"
    assert params["run"] == "2026-05-19T00:00"
    assert params["forecast_days"] == 14
    assert "start_date" not in params
    assert "end_date" not in params


def test_build_raw_forecast_filename_includes_station_model_runtime() -> None:
    run_time = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
    name = build_raw_forecast_filename(
        "EGLC",
        "ukmo_uk_deterministic_2km",
        run_time,
    )
    assert name == "EGLC_ukmo_uk_deterministic_2km_2026-05-01T120000Z.json"
    assert parse_run_time_utc_from_raw_filename(name) == run_time


def test_raw_forecast_filename_is_filesystem_safe() -> None:
    run_time = datetime(2026, 5, 1, 12, 30, 45, tzinfo=timezone.utc)
    name = build_raw_forecast_filename(
        "EGLC",
        "model/with spaces:bad",
        run_time,
    )
    assert ":" not in name
    assert " " not in name
    assert "/" not in name
    assert "\\" not in name
    assert sanitize_filename_component("model/with spaces:bad") == "model_with_spaces_bad"
    assert format_run_time_utc_for_filename(run_time) == "2026-05-01T123045Z"


def test_raw_forecast_response_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "EGLC_ecmwf_ifs_2026-05-01T000000Z.json"
    payload = _payload(model="ecmwf_ifs")
    write_raw_forecast_response(payload, path)
    loaded = read_raw_forecast_response(path)
    assert loaded == payload


def test_fetch_batch_writes_raw_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    out_path = tmp_path / "forecasts.jsonl"
    raw_dir = tmp_path / "raw"
    run_time = datetime(2026, 5, 19, 0, 0, tzinfo=timezone.utc)
    job = HistoricalForecastJob(
        station_id="EGLC",
        latitude=51.5053,
        longitude=0.0553,
        model="ukmo_seamless",
        target_date=date(2026, 5, 22),
        forecast_run_time=run_time,
        lead_hours=72.0,
        timezone="UTC",
    )

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return _payload(tmax=25.0)

    monkeypatch.setattr(httpx, "get", lambda *_a, **_k: FakeResponse())

    fetched, skipped, failed = fetch_historical_forecast_batch(
        [job],
        out_path=out_path,
        raw_dir=raw_dir,
    )

    assert fetched == 1
    assert skipped == 0
    assert failed == 0
    raw_path = raw_dir / build_raw_forecast_filename("EGLC", "ukmo_seamless", run_time)
    assert raw_path.exists()
    assert read_raw_forecast_response(raw_path)["daily"]["time"]


def test_load_or_fetch_single_run_payload_uses_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_dir = tmp_path / "raw"
    run_time = datetime(2026, 5, 19, 0, 0, tzinfo=timezone.utc)
    path = raw_dir / build_raw_forecast_filename("EGLC", "ukmo_seamless", run_time)
    write_raw_forecast_response(_payload(tmax=21.0), path)

    def fail_get(*_args, **_kwargs):
        raise AssertionError("httpx.get should not be called when raw cache exists")

    monkeypatch.setattr(httpx, "get", fail_get)

    payload = load_or_fetch_single_run_payload(
        51.5053,
        0.0553,
        "ukmo_seamless",
        run_time,
        station_id="EGLC",
        timezone="UTC",
        raw_dir=raw_dir,
    )
    assert payload["daily"]["time"]


def test_fetch_raw_forecast_runs_writes_multiple_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_dir = tmp_path / "raw"
    runs = [
        datetime(2026, 5, 19, 0, 0, tzinfo=timezone.utc),
        datetime(2026, 5, 19, 6, 0, tzinfo=timezone.utc),
    ]

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return _payload()

    monkeypatch.setattr(httpx, "get", lambda *_a, **_k: FakeResponse())

    fetched, skipped, failed = fetch_raw_forecast_runs(
        "EGLC",
        51.5053,
        0.0553,
        "ukmo_seamless",
        runs,
        timezone="UTC",
        raw_dir=raw_dir,
    )

    assert fetched == 2
    assert skipped == 0
    assert failed == 0
    assert len(list(raw_dir.glob("*.json"))) == 2


def test_fetch_historical_forecasts_dry_run_does_not_call_api(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(httpx, "get", MagicMock())

    result = CliRunner().invoke(
        app,
        [
            "fetch-historical-forecasts",
            "--station-id",
            "EGLC",
            "--latitude",
            "51.5053",
            "--longitude",
            "0.0553",
            "--model",
            "ukmo_seamless",
            "--start-date",
            "2026-05-20",
            "--end-date",
            "2026-05-21",
            "--timezone",
            "Europe/London",
            "--dry-run",
        ],
    )

    assert result.exit_code == 0
    assert "planned_requests=" in result.stdout
    httpx.get.assert_not_called()


def test_fetch_historical_forecasts_run_range_dry_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(httpx, "get", MagicMock())

    result = CliRunner().invoke(
        app,
        [
            "fetch-historical-forecasts",
            "--station-id",
            "EGLC",
            "--latitude",
            "51.5053",
            "--longitude",
            "0.0553",
            "--model",
            "ukmo_seamless",
            "--timezone",
            "Europe/London",
            "--run-time-start",
            "2026-05-01T00:00:00Z",
            "--run-time-end",
            "2026-05-01T12:00:00Z",
            "--run-interval-hours",
            "6",
            "--forecast-days",
            "10",
            "--dry-run",
        ],
    )

    assert result.exit_code == 0
    assert "planned_raw_requests=3" in result.stdout
    assert "forecast_days=10" in result.stdout
    httpx.get.assert_not_called()


def test_fetch_historical_forecasts_explicit_run_times_dry_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(httpx, "get", MagicMock())

    result = CliRunner().invoke(
        app,
        [
            "fetch-historical-forecasts",
            "--station-id",
            "EGLC",
            "--latitude",
            "51.5053",
            "--longitude",
            "0.0553",
            "--model",
            "ukmo_seamless",
            "--timezone",
            "Europe/London",
            "--run-time",
            "2026-05-19T00:00:00Z",
            "--run-time",
            "2026-05-19T06:00:00Z",
            "--dry-run",
        ],
    )

    assert result.exit_code == 0
    assert "planned_raw_requests=2" in result.stdout
    httpx.get.assert_not_called()
