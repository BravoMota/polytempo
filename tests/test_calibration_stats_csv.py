"""Tests for the calibration stats CSV reader and model selectors."""

from __future__ import annotations

from pathlib import Path

import pytest

from polytempo.weather.calibration_stats_csv import (
    CalibrationStatRow,
    read_calibration_stats_csv,
    select_best_model,
    select_ceiling_row,
)

_HEADER = "station_id,model,lead_hours,n_samples,bias_c,mae_c,rmse_c,error_std_c"


def _row(
    *,
    station_id: str = "EGLC",
    model: str = "ecmwf_ifs025",
    lead_hours: float = 24.0,
    n_samples: int = 50,
    bias_c: float = 0.0,
    mae_c: float = 1.0,
    rmse_c: float = 1.2,
    error_std_c: float = 1.1,
) -> CalibrationStatRow:
    return CalibrationStatRow(
        station_id=station_id,
        model=model,
        lead_hours=lead_hours,
        n_samples=n_samples,
        bias_c=bias_c,
        mae_c=mae_c,
        rmse_c=rmse_c,
        error_std_c=error_std_c,
    )


def test_read_calibration_stats_csv_missing_path_returns_empty(tmp_path: Path) -> None:
    assert read_calibration_stats_csv(tmp_path / "missing.csv") == []


def test_read_calibration_stats_csv_skips_invalid_rows(tmp_path: Path) -> None:
    path = tmp_path / "calibration_stats.csv"
    path.write_text(
        "\n".join(
            [
                _HEADER,
                "EGLC,ecmwf_ifs025,24,50,-0.1,0.9,1.2,1.1",
                ",ecmwf_ifs025,24,50,0,1,1,1",
                "EGLC,ecmwf_ifs025,bad,50,0,1,1,1",
                "EGLC,ecmwf_ifs025,24,0,0,1,1,1",
                "EGLC,ecmwf_ifs025,24,-3,0,1,1,1",
                "EGLC,ecmwf_ifs025,24,5,nan,1,1,1",
                "EGLC,icon_eu,24,4,0,,1,1",
                "EGLC,icon_eu,36,7,0,1,1,1",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    rows = read_calibration_stats_csv(path)

    assert [(r.model, r.lead_hours) for r in rows] == [
        ("ecmwf_ifs025", 24.0),
        ("icon_eu", 36.0),
    ]


def test_select_ceiling_row_returns_smallest_lead_at_or_above_current() -> None:
    rows = [
        _row(model="m", lead_hours=12.0),
        _row(model="m", lead_hours=24.0),
        _row(model="m", lead_hours=36.0),
    ]

    chosen = select_ceiling_row(rows, station_id="EGLC", model="m", current_lead_hours=19.0)

    assert chosen is not None
    assert chosen.lead_hours == 24.0


def test_select_ceiling_row_does_not_pick_closer_but_lower_lead() -> None:
    rows = [
        _row(model="m", lead_hours=18.0),
        _row(model="m", lead_hours=36.0),
    ]

    chosen = select_ceiling_row(rows, station_id="EGLC", model="m", current_lead_hours=19.0)

    assert chosen is not None
    # 18h is closer numerically but below current, so must NOT be chosen.
    assert chosen.lead_hours == 36.0


def test_select_ceiling_row_returns_none_when_no_row_at_or_above() -> None:
    rows = [_row(model="m", lead_hours=18.0)]

    assert (
        select_ceiling_row(rows, station_id="EGLC", model="m", current_lead_hours=24.0)
        is None
    )


def test_select_ceiling_row_filters_by_station_and_model() -> None:
    rows = [
        _row(station_id="EGLC", model="m", lead_hours=24.0),
        _row(station_id="LFPG", model="m", lead_hours=24.0),
        _row(station_id="EGLC", model="other", lead_hours=24.0),
    ]

    chosen = select_ceiling_row(rows, station_id="EGLC", model="m", current_lead_hours=12.0)

    assert chosen is not None
    assert chosen.station_id == "EGLC" and chosen.model == "m"


def test_select_best_model_prefers_lowest_error_std_c() -> None:
    rows = [
        _row(model="alpha", lead_hours=24.0, error_std_c=1.5, rmse_c=2.0),
        _row(model="beta", lead_hours=24.0, error_std_c=0.9, rmse_c=2.0),
        _row(model="gamma", lead_hours=24.0, error_std_c=1.2, rmse_c=0.5),
    ]

    chosen = select_best_model(
        rows,
        station_id="EGLC",
        available_models=["alpha", "beta", "gamma"],
        current_lead_hours=12.0,
    )

    assert chosen is not None
    row, sigma_source = chosen
    assert row.model == "beta"
    assert sigma_source == "error_std_c"


def test_select_best_model_falls_back_to_rmse_when_std_invalid() -> None:
    rows = [
        _row(model="alpha", lead_hours=24.0, error_std_c=0.0, rmse_c=1.4),
        _row(model="beta", lead_hours=24.0, error_std_c=0.0, rmse_c=0.9),
    ]

    chosen = select_best_model(
        rows,
        station_id="EGLC",
        available_models=["alpha", "beta"],
        current_lead_hours=12.0,
    )

    assert chosen is not None
    row, sigma_source = chosen
    assert row.model == "beta"
    assert sigma_source == "rmse_c"


def test_select_best_model_drops_models_without_ceiling_row() -> None:
    rows = [
        _row(model="alpha", lead_hours=18.0, error_std_c=0.5),
        _row(model="beta", lead_hours=24.0, error_std_c=1.4),
    ]

    chosen = select_best_model(
        rows,
        station_id="EGLC",
        available_models=["alpha", "beta"],
        current_lead_hours=20.0,
    )

    assert chosen is not None
    row, _ = chosen
    # alpha's only row is 18h < 20h current, so alpha is dropped.
    assert row.model == "beta"


def test_select_best_model_returns_none_when_no_candidate_qualifies() -> None:
    rows = [_row(model="alpha", lead_hours=10.0, error_std_c=0.5)]

    chosen = select_best_model(
        rows,
        station_id="EGLC",
        available_models=["alpha", "beta"],
        current_lead_hours=20.0,
    )

    assert chosen is None


def test_select_best_model_returns_none_when_sigma_unusable_everywhere() -> None:
    rows = [
        _row(model="alpha", lead_hours=24.0, error_std_c=0.0, rmse_c=0.0),
        _row(model="beta", lead_hours=24.0, error_std_c=0.0, rmse_c=float("-inf")),
    ]

    # Pre-filter happens in read_*; assume rows already valid except sigma helpers.
    # Direct call: rmse=0 is rejected, -inf was never read by reader; force via fixture:
    rows[1] = _row(model="beta", lead_hours=24.0, error_std_c=0.0, rmse_c=0.0)

    assert (
        select_best_model(
            rows,
            station_id="EGLC",
            available_models=["alpha", "beta"],
            current_lead_hours=12.0,
        )
        is None
    )


def test_select_best_model_uses_per_model_ceiling_row() -> None:
    # alpha has a finer cadence than beta; the picker must use each model's
    # OWN ceiling, not a single global ceiling.
    rows = [
        _row(model="alpha", lead_hours=21.0, error_std_c=1.5),
        _row(model="alpha", lead_hours=24.0, error_std_c=2.0),
        _row(model="beta", lead_hours=30.0, error_std_c=1.7),
    ]

    chosen = select_best_model(
        rows,
        station_id="EGLC",
        available_models=["alpha", "beta"],
        current_lead_hours=20.0,
    )

    assert chosen is not None
    row, _ = chosen
    # alpha's ceiling at lead=20 is 21h (std 1.5) — beats beta's 30h (std 1.7).
    assert row.model == "alpha"
    assert row.lead_hours == 21.0


def test_select_best_model_returns_none_when_no_models_available() -> None:
    rows = [_row(model="alpha", lead_hours=24.0)]

    assert (
        select_best_model(
            rows,
            station_id="EGLC",
            available_models=[],
            current_lead_hours=12.0,
        )
        is None
    )


def test_select_best_model_skips_unknown_models_entirely() -> None:
    rows = [_row(model="alpha", lead_hours=24.0, error_std_c=0.7)]

    chosen = select_best_model(
        rows,
        station_id="EGLC",
        available_models=["zeta"],
        current_lead_hours=12.0,
    )

    assert chosen is None


def test_select_best_model_uses_per_model_init_lead_hours() -> None:
    rows = [
        _row(model="alpha", lead_hours=30.0, error_std_c=1.0),
        _row(model="alpha", lead_hours=34.0, error_std_c=2.0),
        _row(model="beta", lead_hours=28.0, error_std_c=0.8),
        _row(model="beta", lead_hours=30.0, error_std_c=1.5),
    ]

    wall_clock_winner = select_best_model(
        rows,
        station_id="EGLC",
        available_models=["alpha", "beta"],
        current_lead_hours=30.0,
    )
    assert wall_clock_winner is not None
    assert wall_clock_winner[0].model == "alpha"
    assert wall_clock_winner[0].lead_hours == 30.0

    init_winner = select_best_model(
        rows,
        station_id="EGLC",
        available_models=["alpha", "beta"],
        current_lead_hours=30.0,
        init_lead_hours_by_model={"alpha": 34.0, "beta": 28.0},
    )
    assert init_winner is not None
    assert init_winner[0].model == "beta"
    assert init_winner[0].lead_hours == 28.0


def test_select_best_model_excludes_models_missing_from_init_lead_map() -> None:
    rows = [
        _row(model="alpha", lead_hours=24.0, error_std_c=1.0),
        _row(model="beta", lead_hours=12.0, error_std_c=0.8),
    ]

    chosen = select_best_model(
        rows,
        station_id="EGLC",
        available_models=["alpha", "beta"],
        current_lead_hours=11.0,
        init_lead_hours_by_model={"alpha": 24.0},
    )

    assert chosen is not None
    assert chosen[0].model == "alpha"
    assert chosen[0].lead_hours == 24.0


def test_read_calibration_stats_csv_smoke_real_file() -> None:
    # Sanity: the real generated CSV (if present) loads without error.
    from polytempo.weather.calibration_stats_csv import DEFAULT_CALIBRATION_STATS_CSV_PATH

    rows = read_calibration_stats_csv(DEFAULT_CALIBRATION_STATS_CSV_PATH)
    for row in rows:
        assert row.n_samples > 0
        assert row.lead_hours >= 0


# Quiet pyflakes / linters about unused pytest import when no parametrize.
_ = pytest
