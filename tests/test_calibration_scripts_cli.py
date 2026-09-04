"""The --station flag on the calibration scripts scopes ingest, never the CSV."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"


def _load_script(name: str):
    spec = importlib.util.spec_from_file_location(f"_script_{name}", SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


SCRIPT_NAMES = (
    "bootstrap_calibration_store",
    "bootstrap_wu_calibration_store",
    "run_daily_calibration",
)


@pytest.fixture(params=SCRIPT_NAMES)
def script(request):
    return _load_script(request.param)


def _run_main(script, monkeypatch, argv: list[str]) -> dict:
    """Invoke main() with the DB/runner boundary stubbed out."""
    seen: dict = {}

    monkeypatch.setattr(sys, "argv", [script.__name__, *argv])
    monkeypatch.setattr(script, "resolve_database_url", lambda override=None: "stub://db")
    if hasattr(script, "initialize_database"):
        monkeypatch.setattr(script, "initialize_database", lambda url: None)

    def capture(config, database_url, **kwargs):
        seen["station_ids"] = list(config.station_ids)
        seen["stations"] = [s.station_id for s in config.stations]
        seen["updated_stats_csv"] = config.updated_stats_csv
        seen["eglc_models"] = [m.name for m in config.models_for("EGLC")]
        seen["lemd_models"] = [m.name for m in config.models_for("LEMD")]
        return 0

    for runner in ("run_bootstrap", "run_wu_bootstrap", "run_daily", "run_wu_daily"):
        if hasattr(script, runner):
            monkeypatch.setattr(script, runner, capture)

    assert script.main() == 0
    return seen


def _base_argv(script) -> list[str]:
    return ["--once"] if script.__name__.endswith("run_daily_calibration") else []


def test_default_covers_every_configured_station(script, monkeypatch) -> None:
    seen = _run_main(script, monkeypatch, _base_argv(script))
    assert seen["station_ids"] == ["EGLC", "LEMD"]


def test_station_flag_scopes_ingest_to_madrid(script, monkeypatch) -> None:
    seen = _run_main(script, monkeypatch, [*_base_argv(script), "--station", "LEMD"])
    assert seen["station_ids"] == ["LEMD"]
    assert seen["stations"] == ["LEMD"]


def test_station_id_alias_works(script, monkeypatch) -> None:
    seen = _run_main(script, monkeypatch, [*_base_argv(script), "--station-id", "EGLC"])
    assert seen["station_ids"] == ["EGLC"]


def test_station_flag_is_repeatable(script, monkeypatch) -> None:
    seen = _run_main(
        script,
        monkeypatch,
        [*_base_argv(script), "--station", "LEMD", "--station", "EGLC"],
    )
    assert seen["station_ids"] == ["EGLC", "LEMD"]


def test_station_flag_does_not_repoint_or_narrow_the_stats_csv(script, monkeypatch) -> None:
    """The CSV target is identical with and without --station."""
    full = _run_main(script, monkeypatch, _base_argv(script))
    scoped = _run_main(script, monkeypatch, [*_base_argv(script), "--station", "LEMD"])
    assert scoped["updated_stats_csv"] == full["updated_stats_csv"]


def test_station_flag_never_alters_the_resolved_model_sets(script, monkeypatch) -> None:
    full = _run_main(script, monkeypatch, _base_argv(script))
    scoped = _run_main(script, monkeypatch, [*_base_argv(script), "--station", "LEMD"])
    assert scoped["eglc_models"] == full["eglc_models"]
    assert scoped["lemd_models"] == full["lemd_models"]


def test_unconfigured_station_is_rejected(script, monkeypatch) -> None:
    with pytest.raises(ValueError, match="not configured for calibration"):
        _run_main(script, monkeypatch, [*_base_argv(script), "--station", "LIMC"])
