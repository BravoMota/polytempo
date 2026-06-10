"""Paper trading ledger (PostgreSQL-backed).

Append-only OPEN and SETTLE events in ``paper_events``. Demo account starts at
$1000 per profile. Stake per BUY is 2-5% of current balance, scaled linearly
by model edge; a decision's ``stake_usd`` (flat ticket) or ``stake_fraction``
(fraction of current balance) overrides the ramp. CLOSE is reserved for
future early-exit support.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

from polytempo.analysis import AnalysisResult
from polytempo.storage.paper_postgres import (
    PaperEventRow,
    STARTING_BALANCE_USD,
    get_paper_connection,
    insert_paper_event,
    profile_has_open_on_event,
    resolve_paper_database_url,
)

MIN_STAKE_FRAC = 0.02
MAX_STAKE_FRAC = 0.05
EDGE_FLOOR_PP = 7.0
EDGE_CEILING_PP = 15.0


@dataclass(frozen=True)
class OpenTrade:
    """One unsettled position."""

    trade_id: str
    event_id: str
    bucket_label: str
    yes_ask: float
    edge_pp: float
    stake_usd: float
    shares: float
    side: str = "YES"
    entry_price: float | None = None


@dataclass(frozen=True)
class LedgerState:
    """Replayed ledger snapshot for one profile."""

    balance_usd: float
    open_trades: list[OpenTrade]
    settled_count: int
    realized_pnl_usd: float


class LedgerStore(Protocol):
    """Persistence for one profile's paper ledger."""

    def read_state(self, profile_id: str) -> LedgerState: ...

    def has_open_on_event(self, profile_id: str, event_id: str) -> bool: ...

    def open_trades_from_analysis(
        self,
        profile_id: str,
        analysis: AnalysisResult,
        event_id: str,
        *,
        lead_hours: float | None = None,
        model_strategy: str | None = None,
    ) -> list[OpenTrade]: ...

    def settle_event(
        self,
        profile_id: str,
        event_id: str,
        winning_label: str,
    ) -> list[OpenTrade]: ...

    def append_close(self, profile_id: str, trade_id: str) -> None:
        """Reserved for future early-exit; not implemented in v1."""
        ...


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


class PostgresLedgerStore:
    """PostgreSQL implementation of ``LedgerStore``."""

    def __init__(self, database_url: str | None = None) -> None:
        self._database_url = database_url or resolve_paper_database_url()

    @property
    def database_url(self) -> str:
        return self._database_url

    def read_state(self, profile_id: str) -> LedgerState:
        with get_paper_connection(self._database_url) as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM paper_events
                WHERE profile_id = %(pid)s
                ORDER BY id ASC
                """,
                {"pid": profile_id},
            ).fetchall()

        balance = STARTING_BALANCE_USD
        open_by_id: dict[str, dict] = {}
        settled = 0
        realized = 0.0

        for record in rows:
            kind = record["event_type"]
            if kind == "OPEN":
                balance -= float(record["stake_usd"])
                open_by_id[record["trade_id"]] = record
            elif kind in ("SETTLE", "CLOSE"):
                trade = open_by_id.pop(record["trade_id"], None)
                payout = float(record["payout_usd"] or 0.0)
                balance += payout
                if trade is not None:
                    realized += payout - float(trade["stake_usd"])
                settled += 1

        open_trades = [_open_trade_from_row(r) for r in open_by_id.values()]
        return LedgerState(
            balance_usd=round(balance, 4),
            open_trades=open_trades,
            settled_count=settled,
            realized_pnl_usd=round(realized, 4),
        )

    def has_open_on_event(self, profile_id: str, event_id: str) -> bool:
        with get_paper_connection(self._database_url) as conn:
            return profile_has_open_on_event(conn, profile_id, event_id)

    def open_trades_from_analysis(
        self,
        profile_id: str,
        analysis: AnalysisResult,
        event_id: str,
        *,
        lead_hours: float | None = None,
        model_strategy: str | None = None,
    ) -> list[OpenTrade]:
        state = self.read_state(profile_id)
        balance = state.balance_usd
        held = {
            (t.event_id, t.bucket_label, t.side) for t in state.open_trades
        }
        opened: list[OpenTrade] = []
        ts = _now_iso()

        with get_paper_connection(self._database_url) as conn:
            for row in analysis.rows:
                if row.action not in ("BUY_YES", "BUY_NO"):
                    continue
                if (event_id, row.label, row.side) in held:
                    continue
                if row.edge_yes_pp is None:
                    continue
                entry_price = _entry_price(row)
                if entry_price is None or entry_price <= 0:
                    continue
                if balance <= 0:
                    break
                if row.stake_usd is not None:
                    stake = round(row.stake_usd, 2)
                elif row.stake_fraction is not None:
                    stake = round(balance * row.stake_fraction, 2)
                else:
                    frac = stake_fraction(row.edge_yes_pp)
                    stake = round(balance * frac, 2)
                if stake <= 0 or stake > balance:
                    continue
                shares = round(stake / entry_price, 4)
                trade = OpenTrade(
                    trade_id=uuid.uuid4().hex[:12],
                    event_id=event_id,
                    bucket_label=row.label,
                    yes_ask=row.yes_ask if row.yes_ask is not None else 0.0,
                    edge_pp=row.edge_yes_pp,
                    stake_usd=stake,
                    shares=shares,
                    side=row.side,
                    entry_price=entry_price,
                )
                insert_paper_event(
                    conn,
                    PaperEventRow(
                        profile_id=profile_id,
                        event_type="OPEN",
                        trade_id=trade.trade_id,
                        ts_utc=ts,
                        polymarket_event_id=event_id,
                        bucket_label=trade.bucket_label,
                        side=trade.side,
                        entry_price=trade.entry_price,
                        stake_usd=trade.stake_usd,
                        shares=trade.shares,
                        edge_pp=trade.edge_pp,
                        yes_bid=row.yes_bid,
                        yes_ask=row.yes_ask,
                        lead_hours=lead_hours,
                        model_strategy=model_strategy or analysis.model_strategy,
                        trade_action=row.action,
                    ),
                )
                opened.append(trade)
                balance -= stake
            conn.commit()
        return opened

    def settle_event(
        self,
        profile_id: str,
        event_id: str,
        winning_label: str,
    ) -> list[OpenTrade]:
        state = self.read_state(profile_id)
        settled: list[OpenTrade] = []
        ts = _now_iso()

        with get_paper_connection(self._database_url) as conn:
            for trade in state.open_trades:
                if trade.event_id != event_id:
                    continue
                bucket_won = trade.bucket_label == winning_label
                won = bucket_won if trade.side == "YES" else not bucket_won
                payout = round(trade.shares, 4) if won else 0.0
                insert_paper_event(
                    conn,
                    PaperEventRow(
                        profile_id=profile_id,
                        event_type="SETTLE",
                        trade_id=trade.trade_id,
                        ts_utc=ts,
                        polymarket_event_id=event_id,
                        bucket_label=trade.bucket_label,
                        side=trade.side,
                        winning_label=winning_label,
                        payout_usd=payout,
                        outcome="YES" if won else "NO",
                    ),
                )
                settled.append(trade)
            conn.commit()
        return settled

    def append_close(self, profile_id: str, trade_id: str) -> None:
        raise NotImplementedError("position close is reserved for a future phase")

    def log_tick(
        self,
        profile_id: str,
        *,
        polymarket_event_id: str | None,
        lead_hours: float | None,
        model_strategy: str | None,
        trade_action: str,
        metadata: dict | None = None,
    ) -> None:
        with get_paper_connection(self._database_url) as conn:
            insert_paper_event(
                conn,
                PaperEventRow(
                    profile_id=profile_id,
                    event_type="TICK",
                    ts_utc=_now_iso(),
                    polymarket_event_id=polymarket_event_id,
                    lead_hours=lead_hours,
                    model_strategy=model_strategy,
                    trade_action=trade_action,
                    metadata=metadata,
                ),
            )
            conn.commit()

    def log_gate_skip(
        self,
        profile_id: str,
        *,
        lead_hours: float | None,
        reason: str,
        metadata: dict | None = None,
    ) -> None:
        meta = dict(metadata or {})
        meta["reason"] = reason
        with get_paper_connection(self._database_url) as conn:
            insert_paper_event(
                conn,
                PaperEventRow(
                    profile_id=profile_id,
                    event_type="GATE_SKIP",
                    ts_utc=_now_iso(),
                    lead_hours=lead_hours,
                    trade_action=reason,
                    metadata=meta,
                ),
            )
            conn.commit()


def default_ledger_store(database_url: str | None = None) -> PostgresLedgerStore:
    return PostgresLedgerStore(database_url=database_url)


def _entry_price(row) -> float | None:  # type: ignore[no-untyped-def]
    if row.side == "NO":
        return None if row.yes_bid is None else 1.0 - row.yes_bid
    return row.yes_ask


def _open_trade_from_row(record: dict) -> OpenTrade:
    side = record.get("side") or "YES"
    raw_yes_ask = record.get("yes_ask")
    yes_ask = float(raw_yes_ask) if raw_yes_ask is not None else 0.0
    entry_price = record.get("entry_price")
    return OpenTrade(
        trade_id=record["trade_id"],
        event_id=record["polymarket_event_id"],
        bucket_label=record["bucket_label"],
        yes_ask=yes_ask,
        edge_pp=float(record["edge_pp"]),
        stake_usd=float(record["stake_usd"]),
        shares=float(record["shares"]),
        side=side,
        entry_price=float(entry_price) if entry_price is not None else None,
    )


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
