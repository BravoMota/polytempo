"""Post-decision stake allocation (legacy bankroll ramp vs bnwp)."""

from __future__ import annotations

from collections.abc import Sequence

from polytempo.analysis import AnalysisRow
from polytempo.profiles.models import (
    SIZING_MODE_BUDGET_NORMALIZE_WALLET_PERCENT,
    SIZING_MODE_LEGACY,
)

MIN_STAKE_FRAC = 0.02
MAX_STAKE_FRAC = 0.05
EDGE_FLOOR_PP = 7.0
EDGE_CEILING_PP = 15.0

# budget_normalize_wallet_percent ticket floors (Polymarket-style minimums)
BUDGET_DUST_USD = 0.50  # skip legs below this after normalize
BUDGET_MIN_TICKET_USD = 1.00  # floor for legs that clear the dust cut


def stake_fraction(edge_pp: float) -> float:
    """Linear ramp: 2% at edge=7pp, 5% at edge>=15pp, clamped."""
    if edge_pp <= EDGE_FLOOR_PP:
        return MIN_STAKE_FRAC
    if edge_pp >= EDGE_CEILING_PP:
        return MAX_STAKE_FRAC
    span = EDGE_CEILING_PP - EDGE_FLOOR_PP
    return MIN_STAKE_FRAC + (edge_pp - EDGE_FLOOR_PP) / span * (
        MAX_STAKE_FRAC - MIN_STAKE_FRAC
    )


def implied_stake_usd(row: AnalysisRow, balance: float) -> float:
    """Dollar stake implied by a decision row under legacy sizing rules."""
    if row.stake_usd is not None:
        return float(row.stake_usd)
    if row.stake_fraction is not None:
        return balance * float(row.stake_fraction)
    if row.edge_yes_pp is None:
        return 0.0
    return balance * stake_fraction(row.edge_yes_pp)


def allocate_stakes(
    rows: Sequence[AnalysisRow],
    balance: float,
    *,
    sizing_mode: str = SIZING_MODE_LEGACY,
    event_budget_fraction: float | None = None,
) -> list[tuple[AnalysisRow, float]]:
    """Map BUY rows to stake dollars under the profile sizing policy.

    ``legacy``: sequential bankroll sizing (same as historical ledger behavior).
    ``budget_normalize_wallet_percent``: implied stakes renormalized onto
    ``pool = event_budget_fraction × balance``, then dust/min-ticket floors.
    """
    if sizing_mode == SIZING_MODE_BUDGET_NORMALIZE_WALLET_PERCENT:
        return _allocate_budget_normalize_wallet_percent(
            rows, balance, event_budget_fraction
        )
    if sizing_mode != SIZING_MODE_LEGACY:
        raise ValueError(f"unknown sizing_mode: {sizing_mode!r}")
    return _allocate_legacy(rows, balance)


def _allocate_legacy(
    rows: Sequence[AnalysisRow],
    balance: float,
) -> list[tuple[AnalysisRow, float]]:
    remaining = balance
    out: list[tuple[AnalysisRow, float]] = []
    for row in rows:
        if row.action not in ("BUY_YES", "BUY_NO"):
            continue
        if remaining <= 0:
            break
        stake = round(implied_stake_usd(row, remaining), 2)
        if stake <= 0 or stake > remaining:
            continue
        out.append((row, stake))
        remaining -= stake
    return out


def _apply_budget_ticket_floors(stake: float) -> float | None:
    """Return adjusted stake, or None to skip (dust)."""
    if stake < BUDGET_DUST_USD:
        return None
    if stake < BUDGET_MIN_TICKET_USD:
        return BUDGET_MIN_TICKET_USD
    return round(stake, 2)


def _allocate_budget_normalize_wallet_percent(
    rows: Sequence[AnalysisRow],
    balance: float,
    event_budget_fraction: float | None,
) -> list[tuple[AnalysisRow, float]]:
    if event_budget_fraction is None or not (0.0 < event_budget_fraction <= 1.0):
        raise ValueError(
            "event_budget_fraction must be in (0, 1] for "
            "budget_normalize_wallet_percent"
        )
    if balance <= 0:
        return []

    candidates: list[tuple[AnalysisRow, float]] = []
    for row in rows:
        if row.action not in ("BUY_YES", "BUY_NO"):
            continue
        implied = implied_stake_usd(row, balance)
        if implied <= 0:
            continue
        candidates.append((row, implied))
    if not candidates:
        return []

    pool = float(event_budget_fraction) * balance
    total_implied = sum(implied for _, implied in candidates)
    if total_implied <= 0 or pool <= 0:
        return []

    raw = [pool * implied / total_implied for _, implied in candidates]

    out: list[tuple[AnalysisRow, float]] = []
    remaining = balance
    for (row, _), stake in zip(candidates, raw):
        adjusted = _apply_budget_ticket_floors(stake)
        if adjusted is None:
            continue
        if adjusted > remaining:
            continue
        out.append((row, adjusted))
        remaining -= adjusted
    return out
