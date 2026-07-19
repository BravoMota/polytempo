"""Pre-trade risk gate for the live node.

Pure and deterministic — the only I/O is checking whether the kill-switch file
exists on disk. Every denial reason is a short stable string because it is
journaled onto the ORDER/RESULT row.
"""

from __future__ import annotations

from dataclasses import dataclass

from polytempo.live.config import RiskConfig


@dataclass(frozen=True)
class RiskDecision:
    allowed: bool
    reason: str | None = None


@dataclass(frozen=True)
class OpenCheckInputs:
    stake_usd: float
    limit_price: float
    spread: float | None
    depth_usd_available: float
    open_exposure_usd: float
    event_exposure_usd: float
    realized_pnl_today_usd: float
    forecast_age_hours: float | None
    ledger_halted: bool


class RiskEngine:
    def __init__(self, config: RiskConfig) -> None:
        self._config = config

    def check_open(self, inputs: OpenCheckInputs) -> RiskDecision:
        c = self._config
        if c.kill_switch_file.exists():
            return RiskDecision(False, "kill switch")
        if inputs.ledger_halted:
            return RiskDecision(False, "ledger halted")
        if inputs.realized_pnl_today_usd <= -c.max_daily_loss_usd:
            return RiskDecision(False, "daily loss limit")
        if inputs.limit_price < c.min_price or inputs.limit_price > c.max_price:
            return RiskDecision(False, "price out of band")
        if inputs.spread is None or inputs.spread > c.max_spread:
            return RiskDecision(False, "spread too wide")
        if inputs.depth_usd_available < inputs.stake_usd:
            return RiskDecision(False, "insufficient depth")
        if inputs.open_exposure_usd + inputs.stake_usd > c.max_open_exposure_usd:
            return RiskDecision(False, "open exposure limit")
        if inputs.event_exposure_usd + inputs.stake_usd > c.max_event_exposure_usd:
            return RiskDecision(False, "event exposure limit")
        if (
            inputs.forecast_age_hours is None
            or inputs.forecast_age_hours > c.max_forecast_age_hours
        ):
            return RiskDecision(False, "stale forecast")
        return RiskDecision(True)
