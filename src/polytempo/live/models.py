"""Shared dataclasses and order-state vocabulary for the live trading node.

This module is the contract between execution, sizing, risk, ledger, and the
node loop. It must stay dependency-free (stdlib only) so every live module can
import it without cycles.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# ── Order lifecycle ────────────────────────────────────────────────────────────
# PENDING    intent recorded in the ledger, order not yet sent (write-ahead).
# SUBMITTED  sent to the exchange, no acknowledgement yet.
# OPEN       acknowledged and resting on the book (possibly partially filled).
# FILLED     fully filled (terminal).
# CANCELED   canceled by us, possibly after a partial fill (terminal).
# REJECTED   refused by the exchange (terminal).
# FAILED     local/transport error; true exchange state unknown until
#            reconciliation resolves it (terminal in the ledger).
STATE_PENDING = "PENDING"
STATE_SUBMITTED = "SUBMITTED"
STATE_OPEN = "OPEN"
STATE_FILLED = "FILLED"
STATE_CANCELED = "CANCELED"
STATE_REJECTED = "REJECTED"
STATE_FAILED = "FAILED"

ORDER_STATES = frozenset(
    {
        STATE_PENDING,
        STATE_SUBMITTED,
        STATE_OPEN,
        STATE_FILLED,
        STATE_CANCELED,
        STATE_REJECTED,
        STATE_FAILED,
    }
)
TERMINAL_STATES = frozenset({STATE_FILLED, STATE_CANCELED, STATE_REJECTED, STATE_FAILED})

# Valid state transitions (old -> allowed new states).
ORDER_TRANSITIONS: dict[str, frozenset[str]] = {
    STATE_PENDING: frozenset({STATE_SUBMITTED, STATE_FAILED}),
    STATE_SUBMITTED: frozenset({STATE_OPEN, STATE_FILLED, STATE_REJECTED, STATE_FAILED}),
    STATE_OPEN: frozenset({STATE_FILLED, STATE_CANCELED, STATE_FAILED}),
    STATE_FILLED: frozenset(),
    STATE_CANCELED: frozenset(),
    STATE_REJECTED: frozenset(),
    STATE_FAILED: frozenset(),
}

SIDE_YES = "YES"
SIDE_NO = "NO"

MODE_DRY_RUN = "dry_run"
MODE_LIVE = "live"


@dataclass(frozen=True)
class BookLevel:
    """One price level of a CLOB order book. ``size`` is in shares."""

    price: float
    size: float


@dataclass(frozen=True)
class BookDepth:
    """Full-depth order book for one token.

    ``bids`` sorted best-first (descending price), ``asks`` best-first
    (ascending price).
    """

    token_id: str
    bids: tuple[BookLevel, ...]
    asks: tuple[BookLevel, ...]
    ts_utc: str

    @property
    def best_bid(self) -> float | None:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> float | None:
        return self.asks[0].price if self.asks else None

    @property
    def spread(self) -> float | None:
        if self.best_bid is None or self.best_ask is None:
            return None
        return self.best_ask - self.best_bid


@dataclass(frozen=True)
class SizedOrder:
    """Depth-aware sizing result: what to actually quote."""

    limit_price: float
    shares: float
    stake_usd: float
    depth_usd_available: float
    levels_consumed: int


@dataclass(frozen=True)
class OrderIntent:
    """Immutable record of what the node decided to buy, written before sending."""

    intent_id: str
    event_id: str
    bucket_label: str
    token_id: str
    market_side: str  # SIDE_YES / SIDE_NO — which outcome token is being bought
    limit_price: float
    shares: float
    stake_usd: float
    knob_id: str
    mode: str  # MODE_DRY_RUN / MODE_LIVE
    ts_utc: str
    edge_pp: float | None = None
    lead_hours: float | None = None
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class PlacedOrder:
    """Exchange acknowledgement of a submitted order."""

    order_id: str
    intent_id: str
    state: str  # one of ORDER_STATES (already normalized by the client)
    raw: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class OrderStatus:
    """Normalized snapshot of one exchange order."""

    order_id: str
    state: str  # one of ORDER_STATES
    filled_shares: float
    avg_fill_price: float | None
    raw: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class ExecutionResult:
    """Terminal outcome of managing one intent (recorded to the ledger)."""

    intent: OrderIntent
    state: str  # one of TERMINAL_STATES
    order_id: str | None
    filled_shares: float
    avg_fill_price: float | None
    message: str | None = None


@dataclass(frozen=True)
class LivePosition:
    """One outcome-token position as the exchange reports it."""

    token_id: str
    shares: float
    avg_price: float | None = None


def assert_transition(old: str, new: str) -> None:
    """Raise ``ValueError`` on an illegal order-state transition."""
    if old not in ORDER_TRANSITIONS:
        raise ValueError(f"unknown order state: {old!r}")
    if new not in ORDER_STATES:
        raise ValueError(f"unknown order state: {new!r}")
    if new not in ORDER_TRANSITIONS[old]:
        raise ValueError(f"illegal order transition {old} -> {new}")
