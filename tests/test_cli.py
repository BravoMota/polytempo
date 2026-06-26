"""Tests for the CLI entry point."""

from datetime import date, datetime, timezone

import pytest
from typer.testing import CliRunner

from polytempo.model.lead_time import lead_hours_to_end_of_target_day
from polytempo.cli.main import app


def test_live_command_help_exits_zero() -> None:
    result = CliRunner().invoke(app, ["live", "--help"])
    assert result.exit_code == 0
    assert "--event-id" in result.stdout
    assert "--city" in result.stdout


def test_live_command_help_documents_model_strategy_flag() -> None:
    result = CliRunner().invoke(app, ["live", "--help"])
    assert result.exit_code == 0
    assert "--model-strategy" in result.stdout
    assert "--strategy" in result.stdout
    assert "ensemble_spread" in result.stdout
    assert "best_historical" in result.stdout
    assert "weighted" in result.stdout


def test_live_command_help_documents_days_ahead_flag() -> None:
    result = CliRunner().invoke(app, ["live", "--help"])
    assert result.exit_code == 0
    assert "--days-ahead" in result.stdout


def test_demo_command_exits_zero() -> None:
    result = CliRunner().invoke(app, ["demo"])
    assert result.exit_code == 0


def test_demo_command_prints_distribution_summary() -> None:
    result = CliRunner().invoke(app, ["demo"])
    assert "distribution:" in result.stdout
    assert "mean=" in result.stdout
    assert "sigma=" in result.stdout


def test_demo_command_prints_each_bucket_label() -> None:
    result = CliRunner().invoke(app, ["demo"])
    for label in ["22°C or below", "23°C", "24°C", "25°C", "26°C or higher"]:
        assert label in result.stdout


def test_demo_command_prints_an_action_column() -> None:
    result = CliRunner().invoke(app, ["demo"])
    assert ("BUY_YES" in result.stdout) or ("SKIP" in result.stdout)


# ---------------------------------------------------------------------------
# _lead_hours_to_end_of_target_day — anchor must match the offline
# `compute_lead_hours` convention (UTC midnight at END of target_date) so live
# best_historical lookups align with calibration_stats.csv. See
# docs/calibration-data.md "Lead-hours convention".
# ---------------------------------------------------------------------------


def test_lead_hours_anchors_at_end_of_same_day_target() -> None:
    target = date(2026, 5, 28)
    now = datetime(2026, 5, 28, 12, 0, tzinfo=timezone.utc)

    assert lead_hours_to_end_of_target_day(target, now=now) == pytest.approx(12.0)


def test_lead_hours_anchors_at_end_of_next_day_target() -> None:
    target = date(2026, 5, 29)
    now = datetime(2026, 5, 28, 12, 0, tzinfo=timezone.utc)

    assert lead_hours_to_end_of_target_day(target, now=now) == pytest.approx(36.0)


def test_lead_hours_clamps_to_zero_past_end_of_target() -> None:
    target = date(2026, 5, 28)
    now = datetime(2026, 5, 29, 0, 1, tzinfo=timezone.utc)

    assert lead_hours_to_end_of_target_day(target, now=now) == 0.0
