"""Tests for OPEN audit metadata enrichment."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from polytempo.analysis import (
    MODEL_STRATEGY_BEST_HISTORICAL_UPDATED,
    MODEL_STRATEGY_ENSEMBLE_SPREAD,
    AnalysisResult,
    AnalysisRow,
)
from polytempo.model.distribution import DistributionBuildInfo
from polytempo.paper.run import _analysis_audit_metadata
from polytempo.profiles.models import EntryGate, TradingProfile
from polytempo.weather.calibration_stats_csv import CalibrationStatRow
from polytempo.weather.schema import ForecastValues


def _distribution_build() -> DistributionBuildInfo:
    return DistributionBuildInfo(
        values_used_c=[36.0],
        default_sigma_c=1.0,
        lead_hours=48.0,
        lead_hours_sigma_floor_c=1.0,
        ensemble_stdev_c=None,
        mean_c=35.8,
        sigma_c=1.2,
        method="calibrated",
    )


def _profile(*, model_strategy: str, cal_path: Path) -> TradingProfile:
    return TradingProfile(
        id="bhu_dist_arb_tight_lead48",
        model_strategy=model_strategy,
        trade_strategy="dist_arb_tight",
        entry_gate=EntryGate(target_lead_hours=48.0),
        calibration_stats_path=cal_path,
        city="london",
    )


def _forecast() -> ForecastValues:
    return ForecastValues(
        source="open_meteo",
        latitude=51.5,
        longitude=0.1,
        target_date=date(2026, 6, 25),
        values_c=[36.5, 35.0],
        models=["ukmo_uk_deterministic_2km", "ecmwf_ifs025"],
        init_lead_hours=[53.0, 60.0],
    )


def test_analysis_audit_metadata_includes_calibration_selection() -> None:
    cal_path = Path("data/weather/statistical/calibration_stats_updated.csv")
    row = CalibrationStatRow(
        station_id="EGLC",
        model="ukmo_uk_deterministic_2km",
        lead_hours=54.0,
        n_samples=42,
        bias_c=0.7,
        mae_c=1.1,
        rmse_c=1.3,
        error_std_c=1.2,
    )
    analysis = AnalysisResult(
        distribution_mean_c=35.8,
        distribution_sigma_c=1.2,
        distribution_build=_distribution_build(),
        rows=[
            AnalysisRow(
                label="36°C",
                probability=0.3,
                yes_ask=0.2,
                edge_yes_pp=5.0,
                action="SKIP",
                reason="test",
                confidence="low",
                warnings=[],
            )
        ],
        model_strategy=MODEL_STRATEGY_BEST_HISTORICAL_UPDATED,
        selected_model="ukmo_uk_deterministic_2km",
        calibration_row=row,
        calibration_sigma_source="error_std_c",
        fallback_reason=None,
    )

    meta = _analysis_audit_metadata(
        _profile(model_strategy=MODEL_STRATEGY_BEST_HISTORICAL_UPDATED, cal_path=cal_path),
        analysis,
        _forecast(),
        lead_hours=48.0,
        station_id="EGLC",
    )

    assert meta["calibration_stats_path"] == "calibration_stats_updated.csv"
    assert meta["wall_lead_hours"] == 48.0
    assert meta["distribution_mean_c"] == 35.8
    assert meta["distribution_sigma_c"] == 1.2

    selection = meta["calibration_selection"]
    assert isinstance(selection, dict)
    assert selection["predicted_tmax_c"] == 36.5
    assert selection["lookup_lead_hours"] == 53.0
    assert selection["row"] == {
        "station_id": "EGLC",
        "model": "ukmo_uk_deterministic_2km",
        "lead_hours": 54.0,
        "n_samples": 42,
        "bias_c": 0.7,
        "mae_c": 1.1,
        "rmse_c": 1.3,
        "error_std_c": 1.2,
    }


def test_analysis_audit_metadata_omits_calibration_selection_for_ensemble() -> None:
    cal_path = Path("data/weather/statistical/calibration_stats.csv")
    analysis = AnalysisResult(
        distribution_mean_c=24.0,
        distribution_sigma_c=1.0,
        distribution_build=_distribution_build(),
        rows=[
            AnalysisRow(
                label="24°C",
                probability=0.5,
                yes_ask=0.3,
                edge_yes_pp=10.0,
                action="BUY_YES",
                reason="test",
                confidence="medium",
                warnings=[],
            )
        ],
        model_strategy=MODEL_STRATEGY_ENSEMBLE_SPREAD,
        selected_model=None,
        calibration_row=None,
        calibration_sigma_source=None,
        fallback_reason=None,
    )

    meta = _analysis_audit_metadata(
        _profile(model_strategy=MODEL_STRATEGY_ENSEMBLE_SPREAD, cal_path=cal_path),
        analysis,
        _forecast(),
        lead_hours=30.0,
        station_id="EGLC",
    )

    assert "calibration_selection" not in meta
    assert meta["calibration_stats_path"] == "calibration_stats.csv"
    assert meta["distribution_mean_c"] == 24.0
