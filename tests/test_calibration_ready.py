"""Tests for calibration CSV readiness checks."""

from __future__ import annotations

from pathlib import Path

from polytempo.analysis import (
    MODEL_STRATEGY_BEST_HISTORICAL_UPDATED,
    MODEL_STRATEGY_WEIGHTED_HISTORICAL_UPDATED,
)
from polytempo.profiles.calibration_ready import (
    calibration_csv_ready,
    filter_profiles_by_calibration,
)
from polytempo.profiles.models import EntryGate, TradingProfile


def _profile(
    model_strategy: str,
    cal_path: Path,
    *,
    profile_id: str = "test_profile",
) -> TradingProfile:
    return TradingProfile(
        id=profile_id,
        model_strategy=model_strategy,
        trade_strategy="argmax_yes",
        entry_gate=EntryGate(target_lead_hours=12.0),
        calibration_stats_path=cal_path,
    )


def test_calibration_csv_ready_missing(tmp_path: Path) -> None:
    ready, reason = calibration_csv_ready(tmp_path / "nope.csv")
    assert ready is False
    assert reason == "missing_calibration_csv"


def test_calibration_csv_ready_empty_file(tmp_path: Path) -> None:
    path = tmp_path / "empty.csv"
    path.write_text("station_id,model,lead_hours,n_samples,bias_c,mae_c,rmse_c,error_std_c\n")
    ready, reason = calibration_csv_ready(path)
    assert ready is False
    assert reason == "empty_calibration_csv"


def test_calibration_csv_ready_stale(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "stats.csv"
    path.write_text(
        "station_id,model,lead_hours,n_samples,bias_c,mae_c,rmse_c,error_std_c\n"
        "EGLC,ukmo_uk_deterministic_2km,12,10,0.1,0.2,0.3,0.4\n"
    )
    old = path.stat().st_mtime
    monkeypatch.setattr(
        "polytempo.profiles.calibration_ready.time.time",
        lambda: old + 49 * 3600,
    )
    ready, reason = calibration_csv_ready(path, max_age_hours=48.0)
    assert ready is False
    assert reason == "stale_calibration_csv"


def test_filter_profiles_disables_bhu_when_updated_csv_missing(tmp_path: Path) -> None:
    static = tmp_path / "static.csv"
    static.write_text(
        "station_id,model,lead_hours,n_samples,bias_c,mae_c,rmse_c,error_std_c\n"
        "EGLC,ukmo_uk_deterministic_2km,12,10,0.1,0.2,0.3,0.4\n"
    )
    profiles = [
        _profile("best_historical", static, profile_id="bh"),
        _profile(
            MODEL_STRATEGY_BEST_HISTORICAL_UPDATED,
            tmp_path / "missing.csv",
            profile_id="bhu",
        ),
        _profile(
            MODEL_STRATEGY_WEIGHTED_HISTORICAL_UPDATED,
            tmp_path / "missing.csv",
            profile_id="whu",
        ),
    ]
    enabled, warnings = filter_profiles_by_calibration(profiles)
    assert [p.id for p in enabled] == ["bh"]
    assert len(warnings) == 2
    assert all("missing_calibration_csv" in w for w in warnings)
