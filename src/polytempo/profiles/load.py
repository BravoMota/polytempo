"""Load trading profiles from YAML."""

from __future__ import annotations

from pathlib import Path

import yaml

from polytempo.analysis import MODEL_STRATEGIES
from polytempo.profiles.models import (
    ActiveParams,
    EntryGate,
    ExitPolicy,
    SIZING_MODE_BUDGET_NORMALIZE_WALLET_PERCENT,
    SIZING_MODE_LEGACY,
    TradingProfile,
)
from polytempo.profiles.registry import known_trade_strategies
from polytempo.weather.calibration_stats_csv import (
    DEFAULT_CALIBRATION_STATS_CSV_PATH,
    DEFAULT_UPDATED_CALIBRATION_STATS_CSV_PATH,
)
from polytempo.weather.data_dir import REPO_ROOT

DEFAULT_PROFILES_PATH = Path("config/paper_profiles.yaml")

_MODEL_ABBREV = {
    "best_historical": "bh",
    "best_historical_updated": "bhu",
    "weighted_historical_updated": "whu",
    "weighted_historical_market_sigma": "whums",
    "weighted_historical_updated_sharp": "whus",
    "ensemble_spread": "es",
}


def _resolve_repo_path(path: Path) -> Path:
    if path.is_absolute():
        return path.resolve()
    return (REPO_ROOT / path).resolve()


def _calibration_path_for_strategy(
    model_strategy: str,
    *,
    static_path: Path,
    updated_path: Path,
) -> Path:
    if model_strategy in (
        "best_historical_updated",
        "weighted_historical_updated",
        "weighted_historical_market_sigma",
        "weighted_historical_updated_sharp",
    ):
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


# Strategy knob ``event_budget``: which capital-allocation method to use.
# ``event_budget_fraction`` is an implementation param for
# ``budget_normalize_wallet_percent`` only — not a swept strategy dimension.
EVENT_BUDGET_LEGACY = SIZING_MODE_LEGACY
EVENT_BUDGET_BUDGET_NORMALIZE_WALLET_PERCENT = (
    SIZING_MODE_BUDGET_NORMALIZE_WALLET_PERCENT
)
EVENT_BUDGET_ALIASES = {
    "legacy": EVENT_BUDGET_LEGACY,
    "budget_normalize_wallet_percent": EVENT_BUDGET_BUDGET_NORMALIZE_WALLET_PERCENT,
    "bnwp": EVENT_BUDGET_BUDGET_NORMALIZE_WALLET_PERCENT,
}
DEFAULT_EVENT_BUDGETS = (
    EVENT_BUDGET_LEGACY,
    EVENT_BUDGET_BUDGET_NORMALIZE_WALLET_PERCENT,
)


def normalize_event_budget(raw: str) -> str:
    key = raw.strip()
    if key not in EVENT_BUDGET_ALIASES:
        raise ValueError(
            f"unknown event_budget {raw!r}; "
            f"known: {sorted(set(EVENT_BUDGET_ALIASES) - {'bnwp'})} "
            f"(alias: bnwp)"
        )
    return EVENT_BUDGET_ALIASES[key]


def parse_event_budgets(raw: dict) -> list[str]:
    """Read ``event_budgets`` list from YAML (default: legacy + bnwp)."""
    if raw.get("event_budgets") is None:
        return list(DEFAULT_EVENT_BUDGETS)
    values = [normalize_event_budget(str(x)) for x in raw["event_budgets"]]
    if not values:
        raise ValueError("event_budgets must be a non-empty list")
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def parse_event_budget_fraction(raw: dict) -> float:
    """Scalar pool size for budget_normalize_wallet_percent (not a strategy knob)."""
    value = float(raw.get("event_budget_fraction", 0.10))
    if not (0.0 < value <= 1.0):
        raise ValueError(f"event_budget_fraction must be in (0, 1], got {value}")
    return value


def _validate_trade_strategy_names(names: list[str]) -> None:
    known = set(known_trade_strategies())
    unknown = sorted({name for name in names if name not in known})
    if unknown:
        raise ValueError(
            f"unknown trade_strategies in paper_profiles.yaml: {unknown}; "
            f"register each name in profiles/registry.py "
            f"(known: {sorted(known)})"
        )


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
    static_path = _resolve_repo_path(calibration_stats_path)
    updated_path = _resolve_repo_path(updated_calibration_stats_path)
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
                            static_path=static_path,
                            updated_path=updated_path,
                        ),
                        city=city,
                        target_day=target_day,
                    )
                )
    return profiles


def generate_xsell_profiles(
    spec: dict | None,
    *,
    lead_gates: dict[str, dict[str, float]],
    calibration_stats_path: Path = DEFAULT_CALIBRATION_STATS_CSV_PATH,
    updated_calibration_stats_path: Path = DEFAULT_UPDATED_CALIBRATION_STATS_CSV_PATH,
    city: str = "london",
    target_day: str = "tomorrow",
) -> list[TradingProfile]:
    """Expand the ``xsell_wallets`` block into active-sell profile variants.

    Each variant duplicates an existing ``{model} x {trade} x {gate}`` combo with
    an id suffixed ``_xsell`` and an attached ``ExitPolicy``, so it A/B's against
    its hold-to-settle twin.
    """
    if not spec:
        return []
    static_path = _resolve_repo_path(calibration_stats_path)
    updated_path = _resolve_repo_path(updated_calibration_stats_path)
    policy_raw = spec.get("exit_policy") or {}
    exit_policy = ExitPolicy(
        take_profit_ratio=float(policy_raw.get("take_profit_ratio", 1.5)),
        stop_loss_ratio=float(policy_raw.get("stop_loss_ratio", 0.5)),
    )
    models = list(spec.get("models") or [])
    trades = list(spec.get("trade_strategies") or [])
    gate_keys = list(spec.get("lead_gates") or [])
    _validate_trade_strategy_names(trades)

    profiles: list[TradingProfile] = []
    for model in models:
        for trade in trades:
            for lead_key in gate_keys:
                gate = lead_gates.get(lead_key)
                if gate is None:
                    raise ValueError(
                        f"xsell_wallets references unknown lead gate {lead_key!r}"
                    )
                profiles.append(
                    TradingProfile(
                        id=f"{_profile_id(model, trade, lead_key)}_xsell",
                        model_strategy=model,
                        trade_strategy=trade,
                        entry_gate=EntryGate(
                            target_lead_hours=_target_lead_hours_from_gate(gate),
                            tolerance_seconds=float(gate.get("tolerance_seconds", 90.0)),
                        ),
                        calibration_stats_path=_calibration_path_for_strategy(
                            model,
                            static_path=static_path,
                            updated_path=updated_path,
                        ),
                        city=city,
                        target_day=target_day,
                        exit_policy=exit_policy,
                    )
                )
    return profiles


def generate_active_profiles(
    spec: dict | None,
    *,
    calibration_stats_path: Path = DEFAULT_CALIBRATION_STATS_CSV_PATH,
    updated_calibration_stats_path: Path = DEFAULT_UPDATED_CALIBRATION_STATS_CSV_PATH,
    city: str = "london",
    target_day: str = "tomorrow",
) -> list[TradingProfile]:
    """Expand the ``active_wallets`` block into edge-following profiles.

    Each ``{model} x {trade}`` combo becomes one ``{abbrev}_{trade}_active``
    wallet with shared :class:`ActiveParams` and **no** lead gate (the active
    controller manages it across every lead tick, not at a single instant).
    """
    if not spec:
        return []
    static_path = _resolve_repo_path(calibration_stats_path)
    updated_path = _resolve_repo_path(updated_calibration_stats_path)
    knobs = spec.get("knobs") or {}
    params = ActiveParams(
        add_edge_pp=float(knobs.get("add_edge_pp", 5.0)),
        exit_edge_pp=float(knobs.get("exit_edge_pp", 0.0)),
        max_position_fraction=float(knobs.get("max_position_fraction", 0.15)),
        max_spread=float(knobs.get("max_spread", 0.05)),
        min_liquidity_usd=float(knobs.get("min_liquidity_usd", 100.0)),
    )
    models = list(spec.get("models") or [])
    trades = list(spec.get("trade_strategies") or [])
    _validate_trade_strategy_names(trades)

    profiles: list[TradingProfile] = []
    for model in models:
        for trade in trades:
            abbrev = _MODEL_ABBREV.get(model, model)
            profiles.append(
                TradingProfile(
                    id=f"{abbrev}_{trade}_active",
                    model_strategy=model,
                    trade_strategy=trade,
                    # Placeholder gate (active wallets are not gate-driven; the
                    # controller ignores entry_gate and works the 6h cadence).
                    entry_gate=EntryGate(target_lead_hours=54.0),
                    calibration_stats_path=_calibration_path_for_strategy(
                        model,
                        static_path=static_path,
                        updated_path=updated_path,
                    ),
                    city=city,
                    target_day=target_day,
                    active_params=params,
                )
            )
    return profiles


def generate_budget_normalize_wallet_percent_profiles(
    hold_profiles: list[TradingProfile],
    *,
    event_budget_fraction: float,
) -> list[TradingProfile]:
    """Clone hold profiles as ``_bnwp`` twins (budget_normalize_wallet_percent).

    Does not clone xsell or active wallets — pass only legacy hold profiles.
    ``event_budget_fraction`` is an implementation detail of this method.
    """
    if not (0.0 < event_budget_fraction <= 1.0):
        raise ValueError(
            f"event_budget_fraction must be in (0, 1], got {event_budget_fraction}"
        )
    profiles: list[TradingProfile] = []
    for base in hold_profiles:
        if base.exit_policy is not None or base.active_params is not None:
            continue
        if base.sizing_mode != SIZING_MODE_LEGACY:
            continue
        profiles.append(
            TradingProfile(
                id=f"{base.id}_bnwp",
                model_strategy=base.model_strategy,
                trade_strategy=base.trade_strategy,
                entry_gate=base.entry_gate,
                calibration_stats_path=base.calibration_stats_path,
                city=base.city,
                target_day=base.target_day,
                enabled=base.enabled,
                sizing_mode=SIZING_MODE_BUDGET_NORMALIZE_WALLET_PERCENT,
                event_budget_fraction=event_budget_fraction,
            )
        )
    return profiles


def expand_event_budgets(
    legacy_profiles: list[TradingProfile],
    *,
    event_budgets: list[str],
    event_budget_fraction: float,
) -> list[TradingProfile]:
    """Expand the hold grid for the selected ``event_budget`` strategies."""
    wanted = {normalize_event_budget(b) for b in event_budgets}
    out: list[TradingProfile] = []
    if EVENT_BUDGET_LEGACY in wanted:
        out.extend(legacy_profiles)
    if EVENT_BUDGET_BUDGET_NORMALIZE_WALLET_PERCENT in wanted:
        out.extend(
            generate_budget_normalize_wallet_percent_profiles(
                legacy_profiles,
                event_budget_fraction=event_budget_fraction,
            )
        )
    return out


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

    cal_path = _resolve_repo_path(
        Path(raw.get("calibration_stats_path", DEFAULT_CALIBRATION_STATS_CSV_PATH))
    )
    updated_cal_path = _resolve_repo_path(
        Path(
            raw.get(
                "updated_calibration_stats_path",
                DEFAULT_UPDATED_CALIBRATION_STATS_CSV_PATH,
            )
        )
    )
    city = str(raw.get("city", "london"))
    target_day = str(raw.get("target_day", "tomorrow"))
    models = raw.get("model_strategies")
    trades = raw.get("trade_strategies")
    if trades is not None:
        _validate_trade_strategy_names(list(trades))

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

    xsell = generate_xsell_profiles(
        raw.get("xsell_wallets"),
        lead_gates=lead_gates,
        calibration_stats_path=cal_path,
        updated_calibration_stats_path=updated_cal_path,
        city=city,
        target_day=target_day,
    )

    active_wallets = generate_active_profiles(
        raw.get("active_wallets"),
        calibration_stats_path=cal_path,
        updated_calibration_stats_path=updated_cal_path,
        city=city,
        target_day=target_day,
    )

    event_budgets = parse_event_budgets(raw)
    event_budget_fraction = parse_event_budget_fraction(raw)
    hold_enabled = [p for p in all_profiles if p.enabled]
    hold_expanded = expand_event_budgets(
        hold_enabled,
        event_budgets=event_budgets,
        event_budget_fraction=event_budget_fraction,
    )

    active = raw.get("active_profiles")
    if active == "all_twelve" or active is None:
        return hold_expanded + xsell + active_wallets

    if isinstance(active, list):
        out: list[TradingProfile] = []
        for pid in active:
            if pid not in by_id:
                raise ValueError(f"unknown profile id in active_profiles: {pid!r}")
            out.append(by_id[pid])
        return (
            expand_event_budgets(
                out,
                event_budgets=event_budgets,
                event_budget_fraction=event_budget_fraction,
            )
            + xsell
            + active_wallets
        )

    raise ValueError(f"invalid active_profiles: {active!r}")
