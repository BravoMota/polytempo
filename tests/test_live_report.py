"""Tests for concise polytempo live report formatting."""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

from polytempo.analysis import AnalysisResult, AnalysisRow
from polytempo.model.distribution import DistributionBuildInfo
from polytempo.reports.live_report import (
    format_calibration_selection_md,
    format_distribution_md,
    format_open_meteo_md,
    resolve_calibration_selection,
)
from polytempo.weather.open_meteo import DailyMaxForecast, ModelRunMeta, OpenMeteoLiveBundle
from polytempo.weather.schema import ForecastValues
from polytempo.weather.stations import get_station


def test_resolve_calibration_selection_uses_init_lead(tmp_path: Path) -> None:
    csv_path = tmp_path / "calibration_stats.csv"
    csv_path.write_text(
        "station_id,model,lead_hours,n_samples,bias_c,mae_c,rmse_c,error_std_c\n"
        "EGLC,ecmwf_ifs025,36,90,-0.1,1.0,1.2,1.1\n"
        "EGLC,gfs_seamless,36,90,-0.2,1.5,1.6,1.4\n",
        encoding="utf-8",
    )
    forecast = ForecastValues(
        source="open_meteo",
        latitude=51.5,
        longitude=0.05,
        target_date=date(2026, 6, 14),
        values_c=[20.0, 21.0],
        models=["ecmwf_ifs025", "gfs_seamless"],
        init_lead_hours=[30.0, 48.0],
    )
    selection = resolve_calibration_selection(
        csv_path=csv_path,
        label="static",
        station_id="EGLC",
        forecast=forecast,
        wall_lead_hours=40.0,
    )
    assert selection.row is not None
    assert selection.row.model == "ecmwf_ifs025"
    assert selection.lookup_lead_hours == 30.0
    assert selection.predicted_tmax_c == 20.0


def test_format_calibration_per_model_md_includes_predicted_tmax(tmp_path: Path) -> None:
    from polytempo.reports.live_report import format_calibration_per_model_md

    csv_path = tmp_path / "calibration_stats.csv"
    csv_path.write_text(
        "station_id,model,lead_hours,n_samples,bias_c,mae_c,rmse_c,error_std_c\n"
        "EGLC,alpha,24,40,0.0,1.0,1.0,1.0\n",
        encoding="utf-8",
    )
    forecast = ForecastValues(
        source="open_meteo",
        latitude=51.5,
        longitude=0.05,
        target_date=date(2026, 6, 14),
        values_c=[21.5],
        models=["alpha"],
        init_lead_hours=[24.0],
        model_run_init_utc=["2026-06-14T00:00:00Z"],
    )
    md = format_calibration_per_model_md(
        csv_path=csv_path,
        station_id="EGLC",
        forecast=forecast,
        wall_lead_hours=11.0,
    )
    assert "predicted_tmax_c" in md
    assert "21.50" in md


def test_format_calibration_selection_md_shows_row() -> None:
    md = format_calibration_selection_md(
        resolve_calibration_selection(
            csv_path=Path("data/weather/statistical/calibration_stats.csv"),
            label="best_historical",
            station_id="EGLC",
            forecast=ForecastValues(
                source="open_meteo",
                latitude=51.5,
                longitude=0.05,
                target_date=date(2026, 6, 14),
                values_c=[20.0],
                models=["ecmwf_ifs025"],
                init_lead_hours=[36.0],
            ),
            wall_lead_hours=36.0,
        )
    )
    assert "selected_model:" in md
    assert "lookup_lead_hours:" in md
    assert "open_meteo_predicted_tmax_c:" in md


def test_format_open_meteo_md_includes_init_and_wall_leads() -> None:
    station = get_station("london")
    target = date(2026, 6, 14)
    run_init = datetime(2026, 6, 13, 0, 0, tzinfo=timezone.utc)
    bundle = OpenMeteoLiveBundle(
        fetched_at_utc=datetime(2026, 6, 13, 12, 0, tzinfo=timezone.utc),
        requested_lat=station.latitude,
        requested_lon=station.longitude,
        returned_lat=station.latitude,
        returned_lon=station.longitude,
        daily_by_date={
            target: DailyMaxForecast(
                target_date=target,
                latitude=station.latitude,
                longitude=station.longitude,
                values_c=[22.5],
                models=["ecmwf_ifs025"],
            ),
        },
        meta_by_model={
            "ecmwf_ifs025": ModelRunMeta(
                model="ecmwf_ifs025",
                run_init_utc=run_init,
                run_available_utc=run_init,
                run_modified_utc=run_init,
                update_interval_seconds=3600,
                temporal_resolution_seconds=3600,
                data_end_utc=None,
            ),
        },
        init_lead_hours={("ecmwf_ifs025", target): 36.0},
        wall_clock_lead_hours={("ecmwf_ifs025", target): 48.0},
        meta_staleness_detected=False,
    )
    forecast = ForecastValues(
        source="open_meteo",
        latitude=station.latitude,
        longitude=station.longitude,
        target_date=target,
        values_c=[22.5],
        models=["ecmwf_ifs025"],
        init_lead_hours=[36.0],
        model_run_init_utc=["2026-06-13T00:00:00Z"],
    )
    md = format_open_meteo_md(
        station=station,
        target_date=target,
        bundle=bundle,
        forecast=forecast,
    )
    assert "run_init_utc" in md
    assert "predicted_tmax_c" in md
    assert "22.50" in md
    assert "36.0" in md
    assert "48.0" in md
    assert "Rolling metadata" in md


def test_format_distribution_md_includes_signed_edge_table() -> None:
    result = AnalysisResult(
        distribution_mean_c=22.0,
        distribution_sigma_c=1.0,
        distribution_build=DistributionBuildInfo(
            values_used_c=[22.0],
            default_sigma_c=1.0,
            lead_hours=None,
            lead_hours_sigma_floor_c=None,
            ensemble_stdev_c=None,
            mean_c=22.0,
            sigma_c=1.0,
            method="calibrated_single_model",
        ),
        rows=[
            AnalysisRow(
                label="21°C",
                probability=0.35,
                yes_ask=0.20,
                edge_yes_pp=15.0,
                action="SKIP",
                reason="",
                confidence="",
                warnings=[],
            ),
            AnalysisRow(
                label="22°C",
                probability=0.10,
                yes_ask=0.33,
                edge_yes_pp=-23.0,
                action="SKIP",
                reason="",
                confidence="",
                warnings=[],
            ),
        ],
        model_strategy="best_historical",
    )
    md = format_distribution_md(
        result.distribution_build,
        model_strategy="best_historical",
        result=result,
    )
    assert "| bucket | yes_ask | dist_prob | edge |" in md
    assert "0.3500" in md
    assert "+0.1500" in md
    assert "-0.2300" in md
