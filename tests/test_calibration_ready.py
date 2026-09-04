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


HEADER = "station_id,model,lead_hours,n_samples,bias_c,mae_c,rmse_c,error_std_c\n"
EGLC_ROW = "EGLC,ukmo_uk_deterministic_2km,12,10,0.1,0.2,0.3,0.4\n"
LEMD_ROW = "LEMD,ukmo_uk_deterministic_2km,12,10,0.1,0.2,0.3,0.4\n"


def _profile(
    model_strategy: str,
    cal_path: Path,
    *,
    profile_id: str = "test_profile",
    city: str = "london",
) -> TradingProfile:
    return TradingProfile(
        id=profile_id,
        model_strategy=model_strategy,
        trade_strategy="argmax_yes",
        entry_gate=EntryGate(target_lead_hours=12.0),
        calibration_stats_path=cal_path,
        city=city,
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


# --------------------------------------------------------------------------- #
# Station-aware readiness: a profile needs rows for its OWN contract station
# --------------------------------------------------------------------------- #
def test_calibration_csv_ready_requires_rows_for_the_given_station(tmp_path: Path) -> None:
    path = tmp_path / "stats.csv"
    path.write_text(HEADER + EGLC_ROW)
    assert calibration_csv_ready(path, station_id="EGLC") == (True, "")
    assert calibration_csv_ready(path, station_id="LEMD") == (
        False,
        "no_calibration_rows_for_station",
    )


def test_calibration_csv_ready_without_station_id_is_unchanged(tmp_path: Path) -> None:
    """London-unchanged proof at the primitive: no station_id -> old behaviour."""
    path = tmp_path / "stats.csv"
    path.write_text(HEADER + EGLC_ROW)
    assert calibration_csv_ready(path) == (True, "")


def test_filter_drops_madrid_profiles_on_a_london_only_csv(tmp_path: Path) -> None:
    """Madrid is filtered out; London on the SAME csv stays ready (unchanged)."""
    static = tmp_path / "static.csv"
    static.write_text(HEADER + EGLC_ROW)
    updated = tmp_path / "updated.csv"
    updated.write_text(HEADER + EGLC_ROW)
    profiles = [
        _profile("best_historical", static, profile_id="bh_london"),
        _profile(
            MODEL_STRATEGY_BEST_HISTORICAL_UPDATED, updated, profile_id="bhu_london"
        ),
        _profile(
            MODEL_STRATEGY_WEIGHTED_HISTORICAL_UPDATED, updated, profile_id="whu_london"
        ),
        _profile("best_historical", static, profile_id="bh_madrid", city="madrid"),
        _profile(
            MODEL_STRATEGY_BEST_HISTORICAL_UPDATED,
            updated,
            profile_id="bhu_madrid",
            city="madrid",
        ),
        _profile(
            MODEL_STRATEGY_WEIGHTED_HISTORICAL_UPDATED,
            updated,
            profile_id="whu_madrid",
            city="madrid",
        ),
    ]
    enabled, warnings = filter_profiles_by_calibration(profiles)
    assert [p.id for p in enabled] == ["bh_london", "bhu_london", "whu_london"]
    assert warnings
    assert all("no_calibration_rows_for_station" in w for w in warnings)


def test_filter_keeps_madrid_profiles_once_lemd_rows_exist(tmp_path: Path) -> None:
    static = tmp_path / "static.csv"
    static.write_text(HEADER + EGLC_ROW + LEMD_ROW)
    updated = tmp_path / "updated.csv"
    updated.write_text(HEADER + EGLC_ROW + LEMD_ROW)
    profiles = [
        _profile("best_historical", static, profile_id="bh_madrid", city="madrid"),
        _profile(
            MODEL_STRATEGY_BEST_HISTORICAL_UPDATED,
            updated,
            profile_id="bhu_madrid",
            city="madrid",
        ),
    ]
    enabled, warnings = filter_profiles_by_calibration(profiles)
    assert [p.id for p in enabled] == ["bh_madrid", "bhu_madrid"]
    assert warnings == []
