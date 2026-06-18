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

    def __post_init__(self) -> None:
        if not self.id or "/" in self.id or "\\" in self.id:
            raise ValueError(f"invalid profile id: {self.id!r}")

    def strategy_instance(self) -> Strategy:
        from polytempo.profiles.registry import trade_strategy_for_name

        return trade_strategy_for_name(self.trade_strategy)
