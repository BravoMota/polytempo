"""Realized trade queries for the performance viewer drill-down."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from polytempo.paper.settlement_reporting import (
    build_event_settlement_dates,
    realization_day,
)
from polytempo.storage.paper_postgres import (
    get_paper_connection,
    resolve_paper_database_url,
)


def _parse_ts(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _collect_event_ids(rows: list[dict]) -> set[str]:
    ids: set[str] = set()
    for row in rows:
        eid = row.get("polymarket_event_id")
        if eid:
            ids.add(str(eid))
    return ids


@dataclass(frozen=True)
class RealizedTrade:
    """One OPEN joined with its SETTLE or CLOSE for a reporting day."""

    trade_id: str
    polymarket_event_id: str
    settlement_date: date
    realization_type: str
    bucket_label: str
    side: str
    entry_price: float
    stake_usd: float
    shares: float
    yes_bid: float | None
    yes_ask: float | None
    edge_pp: float | None
    lead_hours: float | None
    model_strategy: str | None
    trade_action: str | None
    opened_at_utc: str
    open_metadata: dict[str, Any]
    winning_label: str | None
    outcome: str | None
    payout_usd: float
    realized_at_utc: str
    exit_metadata: dict[str, Any]
    pnl_usd: float


@dataclass(frozen=True)
class RealizedEventGroup:
    """Trades realized on one settlement date for one Polymarket event."""

    polymarket_event_id: str
    settlement_date: date
    trades: tuple[RealizedTrade, ...]

    @property
    def resolution_label(self) -> str:
        for trade in self.trades:
            if trade.realization_type == "SETTLE" and trade.winning_label:
                return trade.winning_label
        if all(t.realization_type == "CLOSE" for t in self.trades):
            return "early exit"
        return "—"


P_FROM_TRADES_COL = "P (from trades)"
P_REPLAY_TOLERANCE = 1e-4


def model_p_from_trade(trade: RealizedTrade) -> float | None:
    """Reconstruct model P at OPEN from ledger edge_pp and executable prices."""
    if trade.edge_pp is None:
        return None
    edge = float(trade.edge_pp) / 100.0
    if trade.side == "YES":
        if trade.yes_ask is None:
            return None
        return float(trade.yes_ask) + edge
    if trade.side == "NO":
        if trade.yes_bid is None:
            return None
        return float(trade.yes_bid) - edge
    return None


def bucket_probs_from_trades(trades: list[RealizedTrade] | tuple[RealizedTrade, ...]) -> dict[str, float]:
    """Map bucket label → model P registered on OPEN (traded buckets only)."""
    out: dict[str, float] = {}
    for trade in trades:
        p = model_p_from_trade(trade)
        if p is None or not trade.bucket_label:
            continue
        out.setdefault(trade.bucket_label, p)
    return out


def _fetch_profile_events(conn, profile_id: str) -> list[dict]:
    return conn.execute(
        """
        SELECT id, profile_id, event_type, trade_id, ts_utc, polymarket_event_id,
               bucket_label, side, entry_price, stake_usd, shares, edge_pp,
               yes_bid, yes_ask, winning_label, payout_usd, outcome,
               lead_hours, model_strategy, trade_action, metadata
        FROM paper_events
        WHERE profile_id = %(profile_id)s
        ORDER BY id ASC
        """,
        {"profile_id": profile_id},
    ).fetchall()


def _metadata_dict(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    return {}


def realized_trades_from_rows(
    rows: list[dict],
    settlement_date: date,
    *,
    event_settlement_dates: dict[str, date] | None = None,
) -> list[RealizedTrade]:
    """Build realized trades for one settlement date from ledger rows."""
    if event_settlement_dates is None:
        event_settlement_dates = build_event_settlement_dates(_collect_event_ids(rows))

    opens: dict[str, dict] = {}
    open_event_ids: dict[str, str] = {}

    for row in rows:
        if row["event_type"] != "OPEN":
            continue
        trade_id = row["trade_id"]
        if not trade_id:
            continue
        opens[trade_id] = row
        eid = row.get("polymarket_event_id")
        if eid:
            open_event_ids[trade_id] = str(eid)

    realized: list[RealizedTrade] = []
    for row in rows:
        if row["event_type"] not in ("SETTLE", "CLOSE"):
            continue
        trade_id = row["trade_id"]
        if not trade_id:
            continue
        open_row = opens.get(trade_id)
        if open_row is None:
            continue

        ts = _parse_ts(row["ts_utc"])
        eid = row.get("polymarket_event_id") or open_event_ids.get(trade_id)
        if not eid:
            continue
        eid_str = str(eid)
        reporting_day = realization_day(
            ts,
            polymarket_event_id=eid_str,
            event_settlement_dates=event_settlement_dates,
        )
        if reporting_day != settlement_date:
            continue

        stake = float(open_row["stake_usd"] or 0.0)
        payout = float(row["payout_usd"] or 0.0)
        realized.append(
            RealizedTrade(
                trade_id=trade_id,
                polymarket_event_id=eid_str,
                settlement_date=reporting_day,
                realization_type=row["event_type"],
                bucket_label=str(open_row.get("bucket_label") or ""),
                side=str(open_row.get("side") or ""),
                entry_price=float(open_row.get("entry_price") or 0.0),
                stake_usd=stake,
                shares=float(open_row.get("shares") or 0.0),
                yes_bid=open_row.get("yes_bid"),
                yes_ask=open_row.get("yes_ask"),
                edge_pp=open_row.get("edge_pp"),
                lead_hours=open_row.get("lead_hours"),
                model_strategy=open_row.get("model_strategy"),
                trade_action=open_row.get("trade_action"),
                opened_at_utc=str(open_row["ts_utc"]),
                open_metadata=_metadata_dict(open_row.get("metadata")),
                winning_label=row.get("winning_label"),
                outcome=row.get("outcome"),
                payout_usd=payout,
                realized_at_utc=str(row["ts_utc"]),
                exit_metadata=_metadata_dict(row.get("metadata")),
                pnl_usd=round(payout - stake, 4),
            )
        )
    return realized


def group_by_event(trades: list[RealizedTrade]) -> list[RealizedEventGroup]:
    """Group realized trades by polymarket event id."""
    buckets: dict[str, list[RealizedTrade]] = defaultdict(list)
    settlement: date | None = None
    for trade in trades:
        buckets[trade.polymarket_event_id].append(trade)
        settlement = trade.settlement_date
    if settlement is None:
        return []
    return [
        RealizedEventGroup(
            polymarket_event_id=eid,
            settlement_date=settlement,
            trades=tuple(group),
        )
        for eid, group in sorted(buckets.items())
    ]


def fetch_realized_trades(
    profile_id: str,
    settlement_date: date,
    *,
    database_url: str | None = None,
    event_settlement_dates: dict[str, date] | None = None,
) -> list[RealizedTrade]:
    """Load realized trades for one wallet on one settlement date."""
    url = resolve_paper_database_url(override=database_url)
    with get_paper_connection(url) as conn:
        rows = _fetch_profile_events(conn, profile_id)
    return realized_trades_from_rows(
        rows,
        settlement_date,
        event_settlement_dates=event_settlement_dates,
    )
