"""Tests for preview report formatting."""

from datetime import date, datetime, timezone

from polytempo.analysis import AnalysisResult
from polytempo.model.distribution import DistributionBuildInfo
from polytempo.paper.bot_log import PreviewDateSection, format_calibration_lead, format_preview_report
from polytempo.paper.run import ProfileRunResult, RunSummary
from polytempo.profiles.models import EntryGate, TradingProfile
from polytempo.weather.calibration_stats_csv import CalibrationStatRow


def _distribution_build() -> DistributionBuildInfo:
    return DistributionBuildInfo(
        values_used_c=[17.0],
        default_sigma_c=1.0,
        lead_hours=30.0,
        lead_hours_sigma_floor_c=None,
        ensemble_stdev_c=None,
        mean_c=17.0,
        sigma_c=1.0,
        method="calibrated",
    )


def test_format_calibration_lead_from_stats_row() -> None:
    analysis = AnalysisResult(
        distribution_mean_c=17.0,
        distribution_sigma_c=1.0,
        distribution_build=_distribution_build(),
        rows=[],
        model_strategy="best_historical",
        selected_model="ecmwf_ifs",
        calibration_row=CalibrationStatRow(
            station_id="EGLC",
            model="ecmwf_ifs",
            lead_hours=48.0,
            n_samples=100,
            bias_c=0.1,
            mae_c=0.5,
            rmse_c=0.6,
            error_std_c=0.55,
        ),
    )
    assert format_calibration_lead(analysis) == "48h/ecmwf_ifs"


def test_format_calibration_lead_fallback() -> None:
    analysis = AnalysisResult(
        distribution_mean_c=17.0,
        distribution_sigma_c=1.0,
        distribution_build=_distribution_build(),
        rows=[],
        fallback_reason="no_calibration_csv",
    )
    assert format_calibration_lead(analysis) == "fallback:no_calibration_csv"


def test_format_preview_report_groups_by_date() -> None:
    profile = TradingProfile(
        id="bh_dist_arb_lead30",
        model_strategy="best_historical",
        trade_strategy="dist_arb",
        entry_gate=EntryGate(target_lead_hours=30),
    )
    now = datetime(2026, 6, 7, 18, 0, tzinfo=timezone.utc)
    summary = RunSummary(
        ts=now.isoformat(),
        event_id="evt-1",
        event_title="London max temp",
        target_date="2026-06-08",
        resolved=False,
        winning_label=None,
        profiles=[
            ProfileRunResult(
                profile_id="bh_dist_arb_lead30",
                action="PREVIEW",
                lead_hours=30.0,
                analysis=None,
            )
        ],
        mode="preview",
    )
    text = format_preview_report(
        now=now,
        sections=[
            PreviewDateSection(
                target_date=date(2026, 6, 8),
                lead_hours=30.0,
                summary=summary,
            ),
            PreviewDateSection(
                target_date=date(2026, 6, 10),
                lead_hours=None,
                missing_reason="No weather event",
            ),
        ],
        profiles_by_id={profile.id: profile},
    )
    assert "dry-run" in text
    assert "2026-06-08" in text
    assert "2026-06-10" in text
    assert "no Polymarket event" in text
    assert "30h" in text
