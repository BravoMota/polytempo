"""Tests for the CLI entry point."""

from typer.testing import CliRunner

from polytempo.cli.main import app


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
