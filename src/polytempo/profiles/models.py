"""Trading profile models."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from polytempo.strategy.base import Strategy
from polytempo.weather.calibration_stats_csv import DEFAULT_CALIBRATION_STATS_CSV_PATH


@dataclass(frozen=True)
class EntryGate:
    """Exact lead-hours instant when this profile may open (± tolerance)."""

    target_lead_hours: float
    tolerance_seconds: float = 90.0

    def __post_init__(self) -> None:
        if self.target_lead_hours < 0:
            raise ValueError(f"target_lead_hours must be non-negative, got {self.target_lead_hours}")
        if self.tolerance_seconds < 0:
            raise ValueError(f"tolerance_seconds must be non-negative, got {self.tolerance_seconds}")


@dataclass(frozen=True)
class ExitPolicy:
    """Active-sell bracket evaluated against a position's live mark-to-market.

    The mark is the sellable price (YES: ``yes_bid``; NO: ``1 - yes_ask``). A
    position closes when ``mark / entry_price`` reaches ``take_profit_ratio``
    (take profit) or falls to ``stop_loss_ratio`` (stop loss). ``None`` exit
    policy means hold to resolution (the default for all legacy profiles).
    """

    take_profit_ratio: float = 1.5
    stop_loss_ratio: float = 0.5

    def __post_init__(self) -> None:
        if not 0.0 < self.stop_loss_ratio < 1.0:
            raise ValueError(
                f"stop_loss_ratio must be in (0, 1), got {self.stop_loss_ratio}"
            )
        if self.take_profit_ratio <= 1.0:
            raise ValueError(
                f"take_profit_ratio must be > 1, got {self.take_profit_ratio}"
            )


@dataclass(frozen=True)
class ActiveParams:
    """Knobs for an active (edge-following) wallet.

    At each lead-gate tick the controller re-prices each open leg against a
    fresh forecast + live book: it scales in while the leg edge stays
    ``>= add_edge_pp`` (one ramp ticket per tick, capped so a leg's total stake
    never exceeds ``max_position_fraction`` of the wallet balance) and flattens
    the whole leg when its edge drops below ``exit_edge_pp``. A leg is skipped
    this tick when its quote is wider than ``max_spread`` or thinner than
    ``min_liquidity_usd``. Presence of this object marks a profile as active
    (no entry gate); ``None`` is a normal hold/gated profile.
    """

    add_edge_pp: float = 5.0
    exit_edge_pp: float = 0.0
    max_position_fraction: float = 0.15
    max_spread: float = 0.05
    min_liquidity_usd: float = 100.0

    def __post_init__(self) -> None:
        if self.add_edge_pp < self.exit_edge_pp:
            raise ValueError(
                f"add_edge_pp ({self.add_edge_pp}) must be >= exit_edge_pp ({self.exit_edge_pp})"
            )
        if not 0.0 < self.max_position_fraction <= 1.0:
            raise ValueError(
                f"max_position_fraction must be in (0, 1], got {self.max_position_fraction}"
            )


@dataclass(frozen=True)
class TradingProfile:
    """One global paper-trading profile (model + action + entry timing)."""

    id: str
    model_strategy: str
    trade_strategy: str
    entry_gate: EntryGate
    calibration_stats_path: Path = DEFAULT_CALIBRATION_STATS_CSV_PATH
    city: str = "london"
    target_day: str = "tomorrow"
    enabled: bool = True
    exit_policy: ExitPolicy | None = None
    active_params: ActiveParams | None = None

    def __post_init__(self) -> None:
        if not self.id or "/" in self.id or "\\" in self.id:
            raise ValueError(f"invalid profile id: {self.id!r}")

    @property
    def is_active(self) -> bool:
        """True for edge-following wallets managed by the active controller."""
        return self.active_params is not None

    def strategy_instance(self) -> Strategy:
        from polytempo.profiles.registry import trade_strategy_for_name

        return trade_strategy_for_name(self.trade_strategy)
