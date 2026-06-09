"""Load trading profiles from YAML."""

from __future__ import annotations

from pathlib import Path

import yaml

from polytempo.analysis import MODEL_STRATEGIES
from polytempo.profiles.models import EntryGate, TradingProfile
from polytempo.profiles.registry import known_trade_strategies
from polytempo.weather.calibration_stats_csv import (
    DEFAULT_CALIBRATION_STATS_CSV_PATH,
    DEFAULT_UPDATED_CALIBRATION_STATS_CSV_PATH,
)

DEFAULT_PROFILES_PATH = Path("config/paper_profiles.yaml")

_MODEL_ABBREV = {
    "best_historical": "bh",
    "best_historical_updated": "bhu",
    "ensemble_spread": "es",
}


def _calibration_path_for_strategy(
    model_strategy: str,
    *,
    static_path: Path,
    updated_path: Path,
) -> Path:
    if model_strategy == "best_historical_updated":
        return updated_path
    return static_path


def _target_lead_hours_from_gate(gate: dict[str, float]) -> float:
    if "target_lead_hours" in gate:
        return float(gate["target_lead_hours"])
    if "max_lead_hours" in gate:
        return float(gate["max_lead_hours"])
    if "min_lead_hours" in gate:
        return float(gate["min_lead_hours"])
    raise ValueError("lead gate must define target_lead_hours")


def _profile_id(model: str, trade: str, lead_key: str) -> str:
    abbrev = _MODEL_ABBREV.get(model, model)
    return f"{abbrev}_{trade}_{lead_key}"


def generate_all_twelve_profiles(
    *,
    lead_gates: dict[str, dict[str, float]],
    model_strategies: list[str] | None = None,
    trade_strategies: list[str] | None = None,
    calibration_stats_path: Path = DEFAULT_CALIBRATION_STATS_CSV_PATH,
    updated_calibration_stats_path: Path = DEFAULT_UPDATED_CALIBRATION_STATS_CSV_PATH,
    city: str = "london",
    target_day: str = "tomorrow",
) -> list[TradingProfile]:
    models = list(model_strategies or MODEL_STRATEGIES)
    trades = list(trade_strategies or known_trade_strategies())
    profiles: list[TradingProfile] = []
    for model in models:
        for trade in trades:
            for lead_key, gate in lead_gates.items():
                profiles.append(
                    TradingProfile(
                        id=_profile_id(model, trade, lead_key),
                        model_strategy=model,
                        trade_strategy=trade,
                        entry_gate=EntryGate(
                            target_lead_hours=_target_lead_hours_from_gate(gate),
                            tolerance_seconds=float(gate.get("tolerance_seconds", 90.0)),
                        ),
                        calibration_stats_path=_calibration_path_for_strategy(
                            model,
                            static_path=calibration_stats_path,
                            updated_path=updated_calibration_stats_path,
                        ),
                        city=city,
                        target_day=target_day,
                    )
                )
    return profiles


def load_paper_profiles(
    path: Path = DEFAULT_PROFILES_PATH,
) -> list[TradingProfile]:
    """Load enabled profiles from YAML config."""
    if not path.is_file():
        raise FileNotFoundError(f"profiles config not found: {path}")

    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"expected mapping in {path}")

    lead_gates = raw.get("lead_gates") or {}
    if not lead_gates:
        raise ValueError("lead_gates must be defined in paper_profiles.yaml")

    cal_path = Path(
        raw.get("calibration_stats_path", DEFAULT_CALIBRATION_STATS_CSV_PATH)
    )
    updated_cal_path = Path(
        raw.get(
            "updated_calibration_stats_path",
            DEFAULT_UPDATED_CALIBRATION_STATS_CSV_PATH,
        )
    )
    city = str(raw.get("city", "london"))
    target_day = str(raw.get("target_day", "tomorrow"))
    models = raw.get("model_strategies")
    trades = raw.get("trade_strategies")

    all_profiles = generate_all_twelve_profiles(
        lead_gates=lead_gates,
        model_strategies=models,
        trade_strategies=trades,
        calibration_stats_path=cal_path,
        updated_calibration_stats_path=updated_cal_path,
        city=city,
        target_day=target_day,
    )
    by_id = {p.id: p for p in all_profiles}

    active = raw.get("active_profiles")
    if active == "all_twelve" or active is None:
        return [p for p in all_profiles if p.enabled]

    if isinstance(active, list):
        out: list[TradingProfile] = []
        for pid in active:
            if pid not in by_id:
                raise ValueError(f"unknown profile id in active_profiles: {pid!r}")
            out.append(by_id[pid])
        return out

    raise ValueError(f"invalid active_profiles: {active!r}")
