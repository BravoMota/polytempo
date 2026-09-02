"""Tests for server-side visualizer prefs."""

from __future__ import annotations

import json
from pathlib import Path

from polytempo.visualizer.prefs import (
    DEFAULT_ENABLED_STRATS,
    FilterPrefs,
    OverlayPrefs,
    VisualizerPrefs,
    add_csv_preset,
    default_models,
    enabled_strats_from_prefs,
    knob_options,
    load_prefs,
    normalize_csv_preset,
    prefs_from_dict,
    resolve_csv_preset,
    resolve_leads,
    resolve_models,
    save_filters,
    save_overlays,
    save_prefs,
)


def test_load_prefs_missing_file(tmp_path: Path) -> None:
    prefs = load_prefs(tmp_path / "missing.json")
    assert prefs.csv_presets == []
    assert prefs.filters is None
    assert prefs.overlays is None


def test_load_prefs_corrupt_json(tmp_path: Path) -> None:
    path = tmp_path / "prefs.json"
    path.write_text("{not-json", encoding="utf-8")
    prefs = load_prefs(path)
    assert prefs.csv_presets == []


def test_save_and_load_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "prefs.json"
    save_prefs(
        VisualizerPrefs(
            csv_presets=["reports/backtest/whums_daily.csv"],
            filters=FilterPrefs(models=["weighted_historical_market_sigma"]),
            overlays=OverlayPrefs(enabled_strats=["weighted_historical_updated_sharp"]),
        ),
        path,
    )
    loaded = load_prefs(path)
    assert loaded.csv_presets == ["reports/backtest/whums_daily.csv"]
    assert loaded.filters is not None
    assert loaded.filters.models == ["weighted_historical_market_sigma"]
    assert loaded.overlays is not None
    assert loaded.overlays.enabled_strats == ["weighted_historical_updated_sharp"]


def test_add_csv_preset_dedupes_and_skips_default(tmp_path: Path) -> None:
    prefs_path = tmp_path / "prefs.json"
    default_csv = tmp_path / "daily.csv"
    default_csv.write_text("x", encoding="utf-8")
    other = tmp_path / "backtest.csv"
    other.write_text("y", encoding="utf-8")

    add_csv_preset(default_csv, prefs_path=prefs_path, default_csv=default_csv)
    assert load_prefs(prefs_path).csv_presets == []

    add_csv_preset(other, prefs_path=prefs_path, default_csv=default_csv)
    add_csv_preset(other, prefs_path=prefs_path, default_csv=default_csv)
    presets = load_prefs(prefs_path).csv_presets
    assert presets == [str(other.resolve())]


def test_normalize_and_resolve_repo_relative(tmp_path: Path) -> None:
    csv_path = tmp_path / "reports" / "backtest" / "daily.csv"
    csv_path.parent.mkdir(parents=True)
    csv_path.write_text("x", encoding="utf-8")
    rel = normalize_csv_preset(csv_path, repo_root=tmp_path)
    assert rel == "reports/backtest/daily.csv"
    assert resolve_csv_preset(rel, repo_root=tmp_path) == csv_path.resolve()


def test_knob_options_from_source_values() -> None:
    assert knob_options(
        [
            "weighted_historical_market_sigma",
            "weighted_historical_updated",
            "",
            None,
            "weighted_historical_market_sigma",
            "nan",
        ]
    ) == [
        "weighted_historical_market_sigma",
        "weighted_historical_updated",
    ]


def test_resolve_models_first_run_and_restore() -> None:
    available = [
        "best_historical",
        "weighted_historical_market_sigma",
        "weighted_historical_updated",
    ]
    assert resolve_models(None, available) == ["weighted_historical_updated"]
    assert resolve_models(["weighted_historical_market_sigma"], available) == [
        "weighted_historical_market_sigma"
    ]
    assert resolve_models([], available) == []
    assert resolve_models(["missing"], available) == ["weighted_historical_updated"]


def test_default_models_falls_back_to_first() -> None:
    assert default_models(["ensemble_spread"]) == ["ensemble_spread"]
    assert default_models([]) == []


def test_resolve_leads_clamps_to_csv() -> None:
    assert resolve_leads(24, 48, [12, 24, 36, 54]) == (24, 36)
    assert resolve_leads(None, None, [12, 54]) == (12, 54)
    assert resolve_leads(10, 11, [24, 36]) == (24, 36)
    assert resolve_leads(1, 2, []) is None


def test_enabled_strats_from_prefs_drops_unknown() -> None:
    assert enabled_strats_from_prefs(None) == frozenset(DEFAULT_ENABLED_STRATS)
    overlays = OverlayPrefs(
        enabled_strats=["weighted_historical_market_sigma", "not_a_strat"]
    )
    assert enabled_strats_from_prefs(overlays) == frozenset(
        {"weighted_historical_market_sigma"}
    )


def test_save_filters_and_overlays_preserve_presets(tmp_path: Path) -> None:
    path = tmp_path / "prefs.json"
    save_prefs(VisualizerPrefs(csv_presets=["reports/a.csv"]), path)
    save_filters(FilterPrefs(models=["best_historical"]), prefs_path=path)
    save_overlays(OverlayPrefs(enabled_strats=["ensemble_spread"]), prefs_path=path)
    loaded = load_prefs(path)
    assert loaded.csv_presets == ["reports/a.csv"]
    assert loaded.filters is not None
    assert loaded.filters.models == ["best_historical"]
    assert loaded.overlays is not None
    assert loaded.overlays.enabled_strats == ["ensemble_spread"]


def test_prefs_from_dict_ignores_junk() -> None:
    prefs = prefs_from_dict(
        {
            "csv_presets": ["ok", 12, ""],
            "filters": {"models": ["whums"], "lead_lo": "24", "lead_hi": "nope"},
            "overlays": {"show_forecasts": 0, "enabled_strats": None},
        }
    )
    assert prefs.csv_presets == ["ok", "12"]
    assert prefs.filters is not None
    assert prefs.filters.lead_lo == 24
    assert prefs.filters.lead_hi is None
    assert prefs.overlays is not None
    assert prefs.overlays.show_forecasts is False
    assert prefs.overlays.enabled_strats == list(DEFAULT_ENABLED_STRATS)


def test_save_prefs_writes_json_object(tmp_path: Path) -> None:
    path = tmp_path / "prefs.json"
    save_prefs(VisualizerPrefs(), path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["csv_presets"] == []
    assert raw["filters"] is None
    assert raw["overlays"] is None
