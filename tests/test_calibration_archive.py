"""Tests for calibration stats CSV archive before nightly overwrite."""

from __future__ import annotations

from pathlib import Path

from polytempo.weather.calibration_compute import archive_calibration_stats_csv_before_write


def test_archive_calibration_stats_csv_before_write_copies_existing(tmp_path: Path) -> None:
    live = tmp_path / "calibration_stats_updated.csv"
    live.write_text("station_id,model\nEGLC,ukmo\n", encoding="utf-8")

    archived = archive_calibration_stats_csv_before_write(live)

    assert archived is not None
    assert archived.parent == tmp_path / "historic"
    assert archived.name.startswith("calibration_stats_updated_")
    assert archived.name.endswith(".csv")
    assert archived.read_text(encoding="utf-8") == live.read_text(encoding="utf-8")
    assert live.is_file()


def test_archive_calibration_stats_csv_before_write_noop_when_missing(tmp_path: Path) -> None:
    missing = tmp_path / "calibration_stats_updated.csv"
    assert archive_calibration_stats_csv_before_write(missing) is None
    assert not (tmp_path / "historic").exists()
