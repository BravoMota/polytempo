"""Live node configuration loaded from ``config/live_node.yaml``.

The knob (model x trade strategy x lead gates) is a deliberate placeholder —
the node boots with it in dry_run, but the values are undecided pending the
model A/B rerun. ``live`` mode is refused unless credentials and an explicit
``POLYTEMPO_LIVE_CONFIRM=1`` are present in the environment.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from polytempo.analysis import MODEL_STRATEGIES
from polytempo.live.models import MODE_DRY_RUN, MODE_LIVE
from polytempo.profiles.models import EntryGate, TradingProfile
from polytempo.profiles.registry import trade_strategy_for_name

DEFAULT_LIVE_CONFIG_PATH = (
    Path(__file__).resolve().parents[3] / "config" / "live_node.yaml"
)

ENV_PRIVATE_KEY = "POLYMARKET_PRIVATE_KEY"
ENV_WALLET_ADDRESS = "POLYMARKET_WALLET_ADDRESS"
ENV_LIVE_CONFIRM = "POLYTEMPO_LIVE_CONFIRM"


@dataclass(frozen=True)
class KnobConfig:
    """The strategy knob: which model x trade strategy x lead gates to trade."""

    id: str
    model_strategy: str
    trade_strategy: str
    lead_gates: tuple[float, ...]

    def __post_init__(self) -> None:
        if self.model_strategy not in MODEL_STRATEGIES:
            raise ValueError(
                f"unknown model_strategy {self.model_strategy!r}, "
                f"expected one of {MODEL_STRATEGIES}"
            )
        trade_strategy_for_name(self.trade_strategy)  # raises on unknown
        if not self.lead_gates:
            raise ValueError("knob.lead_gates must not be empty")
        if any(g <= 0 for g in self.lead_gates):
            raise ValueError(f"knob.lead_gates must be positive, got {self.lead_gates}")


@dataclass(frozen=True)
class StakeConfig:
    fixed_usd: float

    def __post_init__(self) -> None:
        if self.fixed_usd <= 0:
            raise ValueError(f"stake.fixed_usd must be positive, got {self.fixed_usd}")


@dataclass(frozen=True)
class ExecutionConfig:
    max_slippage: float
    fill_timeout_seconds: float
    min_depth_usd: float

    def __post_init__(self) -> None:
        if not 0.0 <= self.max_slippage < 1.0:
            raise ValueError(f"execution.max_slippage must be in [0, 1), got {self.max_slippage}")
        if self.fill_timeout_seconds <= 0:
            raise ValueError("execution.fill_timeout_seconds must be positive")
        if self.min_depth_usd < 0:
            raise ValueError("execution.min_depth_usd must be non-negative")


@dataclass(frozen=True)
class RiskConfig:
    kill_switch_file: Path
    max_daily_loss_usd: float
    max_open_exposure_usd: float
    max_event_exposure_usd: float
    min_price: float
    max_price: float
    max_spread: float
    max_forecast_age_hours: float

    def __post_init__(self) -> None:
        if not 0.0 <= self.min_price < self.max_price <= 1.0:
            raise ValueError(
                f"require 0 <= min_price < max_price <= 1, got "
                f"{self.min_price}/{self.max_price}"
            )
        for name in (
            "max_daily_loss_usd",
            "max_open_exposure_usd",
            "max_event_exposure_usd",
            "max_spread",
            "max_forecast_age_hours",
        ):
            if getattr(self, name) <= 0:
                raise ValueError(f"risk.{name} must be positive")


@dataclass(frozen=True)
class LiveNodeConfig:
    mode: str
    city: str
    target_day: str
    knob: KnobConfig
    stake: StakeConfig
    execution: ExecutionConfig
    risk: RiskConfig

    def __post_init__(self) -> None:
        if self.mode not in (MODE_DRY_RUN, MODE_LIVE):
            raise ValueError(f"mode must be {MODE_DRY_RUN!r} or {MODE_LIVE!r}, got {self.mode!r}")

    def to_trading_profiles(self) -> list[TradingProfile]:
        """One gated profile per lead gate, reusing the paper gate machinery."""
        from polytempo.weather.calibration_stats_csv import (
            DEFAULT_CALIBRATION_STATS_CSV_PATH,
            DEFAULT_UPDATED_CALIBRATION_STATS_CSV_PATH,
        )

        # Same model -> calibration CSV mapping as profiles/load.py.
        if self.knob.model_strategy in (
            "best_historical_updated",
            "weighted_historical_updated",
        ):
            stats_path = DEFAULT_UPDATED_CALIBRATION_STATS_CSV_PATH
        else:
            stats_path = DEFAULT_CALIBRATION_STATS_CSV_PATH
        return [
            TradingProfile(
                id=f"live_{self.knob.id}_lead{int(gate)}",
                model_strategy=self.knob.model_strategy,
                trade_strategy=self.knob.trade_strategy,
                entry_gate=EntryGate(target_lead_hours=gate),
                calibration_stats_path=stats_path,
                city=self.city,
                target_day=self.target_day,
            )
            for gate in self.knob.lead_gates
        ]


@dataclass(frozen=True)
class LiveCredentials:
    """Execution credentials pulled from the environment (live mode only).

    ``wallet_address`` is optional: the SDK falls back to the signer's own
    deposit wallet and derives the wallet type itself.
    """

    private_key: str
    wallet_address: str | None = None
    extra: dict[str, str] = field(default_factory=dict)


def load_live_node_config(path: Path | None = None) -> LiveNodeConfig:
    """Load and validate the live node YAML config."""
    config_path = path if path is not None else DEFAULT_LIVE_CONFIG_PATH
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"invalid live node config: {config_path}")

    knob_raw = raw.get("knob") or {}
    knob = KnobConfig(
        id=str(knob_raw.get("id", "")),
        model_strategy=str(knob_raw.get("model_strategy", "")),
        trade_strategy=str(knob_raw.get("trade_strategy", "")),
        lead_gates=tuple(float(g) for g in knob_raw.get("lead_gates", [])),
    )
    if not knob.id:
        raise ValueError("knob.id is required")

    stake_raw = raw.get("stake") or {}
    execution_raw = raw.get("execution") or {}
    risk_raw = raw.get("risk") or {}
    kill_file = Path(str(risk_raw.get("kill_switch_file", "config/KILL_LIVE")))
    if not kill_file.is_absolute():
        kill_file = config_path.resolve().parent.parent / kill_file

    return LiveNodeConfig(
        mode=str(raw.get("mode", MODE_DRY_RUN)),
        city=str(raw.get("city", "london")),
        target_day=str(raw.get("target_day", "tomorrow")),
        knob=knob,
        stake=StakeConfig(fixed_usd=float(stake_raw.get("fixed_usd", 0.0))),
        execution=ExecutionConfig(
            max_slippage=float(execution_raw.get("max_slippage", 0.02)),
            fill_timeout_seconds=float(execution_raw.get("fill_timeout_seconds", 90.0)),
            min_depth_usd=float(execution_raw.get("min_depth_usd", 50.0)),
        ),
        risk=RiskConfig(
            kill_switch_file=kill_file,
            max_daily_loss_usd=float(risk_raw.get("max_daily_loss_usd", 50.0)),
            max_open_exposure_usd=float(risk_raw.get("max_open_exposure_usd", 120.0)),
            max_event_exposure_usd=float(risk_raw.get("max_event_exposure_usd", 40.0)),
            min_price=float(risk_raw.get("min_price", 0.02)),
            max_price=float(risk_raw.get("max_price", 0.90)),
            max_spread=float(risk_raw.get("max_spread", 0.10)),
            max_forecast_age_hours=float(risk_raw.get("max_forecast_age_hours", 6.0)),
        ),
    )


def resolve_live_credentials() -> LiveCredentials:
    """Read execution credentials from the environment; raise if incomplete.

    Called only when mode == live. Also enforces the explicit
    ``POLYTEMPO_LIVE_CONFIRM=1`` opt-in so a config edit alone can never
    place real orders.
    """
    if os.environ.get(ENV_LIVE_CONFIRM) != "1":
        raise RuntimeError(f"live mode requires {ENV_LIVE_CONFIRM}=1")
    private_key = os.environ.get(ENV_PRIVATE_KEY)
    if not private_key:
        raise RuntimeError(f"live mode requires {ENV_PRIVATE_KEY}")
    return LiveCredentials(
        private_key=private_key,
        wallet_address=os.environ.get(ENV_WALLET_ADDRESS),
    )
