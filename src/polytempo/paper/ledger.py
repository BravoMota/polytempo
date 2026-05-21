"""Paper trading ledger.

Append-only JSONL of OPEN and SETTLE events. Demo account starts at
$1000. Stake per BUY_YES bucket is 2-5% of current balance, scaled
linearly by model edge. No real orders, no live trading.

Record shapes:

    OPEN
      {"type": "OPEN", "trade_id", "ts", "event_id", "bucket_label",
       "yes_ask", "edge_pp", "stake_usd", "shares"}

    SETTLE
      {"type": "SETTLE", "trade_id", "ts", "event_id", "bucket_label",
       "winning_label", "outcome", "payout_usd"}

Balance is always derived by replaying the log; the file is the
source of truth.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from polytempo.analysis import AnalysisResult

STARTING_BALANCE_USD = 1000.0
MIN_STAKE_FRAC = 0.02
MAX_STAKE_FRAC = 0.05
EDGE_FLOOR_PP = 7.0
EDGE_CEILING_PP = 15.0
DEFAULT_LEDGER_DIR = Path("paper_ledger")
DEFAULT_LEDGER_PATH = Path("paper_ledger.jsonl")


def ledger_path_for(
    strategy_name: str,
    base_dir: Path = DEFAULT_LEDGER_DIR,
) -> Path:
    """Per-strategy JSONL path: ``<base_dir>/<strategy_name>.jsonl``.

    Each strategy keeps an independent bankroll, so they must not share a file.
    """
    if not strategy_name or "/" in strategy_name or "\\" in strategy_name:
        raise ValueError(f"invalid strategy name: {strategy_name!r}")
    return base_dir / f"{strategy_name}.jsonl"


@dataclass(frozen=True)
class OpenTrade:
    """One unsettled position. ``side`` is YES or NO; ``entry_price`` is what
    one share costs (``yes_ask`` for YES, ``1 - yes_bid`` for NO)."""

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
    """Replayed ledger snapshot."""

    balance_usd: float
    open_trades: list[OpenTrade]
    settled_count: int
    realized_pnl_usd: float


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


def read_state(path: Path = DEFAULT_LEDGER_PATH) -> LedgerState:
    """Replay the ledger file to derive balance and open positions."""
    balance = STARTING_BALANCE_USD
    open_by_id: dict[str, dict] = {}
    settled = 0
    realized = 0.0
    for record in _iter_records(path):
        kind = record.get("type")
        if kind == "OPEN":
            balance -= float(record["stake_usd"])
            open_by_id[record["trade_id"]] = record
        elif kind == "SETTLE":
            trade = open_by_id.pop(record["trade_id"], None)
            payout = float(record["payout_usd"])
            balance += payout
            if trade is not None:
                realized += payout - float(trade["stake_usd"])
            settled += 1
    open_trades = [_open_trade_from_record(r) for r in open_by_id.values()]
    return LedgerState(
        balance_usd=round(balance, 4),
        open_trades=open_trades,
        settled_count=settled,
        realized_pnl_usd=round(realized, 4),
    )


def open_trades_from_analysis(
    analysis: AnalysisResult,
    event_id: str,
    path: Path = DEFAULT_LEDGER_PATH,
) -> list[OpenTrade]:
    """Append OPEN records for every BUY_YES row, sized off live balance."""
    state = read_state(path)
    balance = state.balance_usd
    held = {
        (t.event_id, t.bucket_label, t.side)
        for t in state.open_trades
    }
    opened: list[OpenTrade] = []
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
        else:
            frac = stake_fraction(row.edge_yes_pp)
            stake = round(balance * frac, 2)
        if stake <= 0:
            continue
        if stake > balance:
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
        _append(
            path,
            {
                "type": "OPEN",
                "trade_id": trade.trade_id,
                "ts": _now_iso(),
                "event_id": trade.event_id,
                "bucket_label": trade.bucket_label,
                "side": trade.side,
                "entry_price": trade.entry_price,
                "yes_bid": row.yes_bid,
                "yes_ask": row.yes_ask,
                "edge_pp": trade.edge_pp,
                "stake_usd": trade.stake_usd,
                "shares": trade.shares,
            },
        )
        opened.append(trade)
        balance -= stake
    return opened


def _entry_price(row) -> float | None:  # type: ignore[no-untyped-def]
    """Cost-per-share for the row's side. YES = yes_ask, NO = 1 - yes_bid."""
    if row.side == "NO":
        return None if row.yes_bid is None else 1.0 - row.yes_bid
    return row.yes_ask


def settle_event(
    event_id: str,
    winning_label: str,
    path: Path = DEFAULT_LEDGER_PATH,
) -> list[OpenTrade]:
    """Settle every open trade on this event against the winning bucket.

    YES share pays $1 if its bucket is the winning_label, else $0.
    Returns the trades that were settled.
    """
    state = read_state(path)
    settled: list[OpenTrade] = []
    for trade in state.open_trades:
        if trade.event_id != event_id:
            continue
        bucket_won = trade.bucket_label == winning_label
        won = bucket_won if trade.side == "YES" else not bucket_won
        payout = round(trade.shares, 4) if won else 0.0
        _append(
            path,
            {
                "type": "SETTLE",
                "trade_id": trade.trade_id,
                "ts": _now_iso(),
                "event_id": event_id,
                "bucket_label": trade.bucket_label,
                "side": trade.side,
                "winning_label": winning_label,
                "outcome": "YES" if won else "NO",
                "payout_usd": payout,
            },
        )
        settled.append(trade)
    return settled


def _open_trade_from_record(record: dict) -> OpenTrade:
    side = record.get("side", "YES")
    raw_yes_ask = record.get("yes_ask")
    yes_ask = float(raw_yes_ask) if raw_yes_ask is not None else 0.0
    entry_price = record.get("entry_price")
    return OpenTrade(
        trade_id=record["trade_id"],
        event_id=record["event_id"],
        bucket_label=record["bucket_label"],
        yes_ask=yes_ask,
        edge_pp=float(record["edge_pp"]),
        stake_usd=float(record["stake_usd"]),
        shares=float(record["shares"]),
        side=side,
        entry_price=float(entry_price) if entry_price is not None else None,
    )


def _iter_records(path: Path) -> Iterable[dict]:
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                yield json.loads(line)


def _append(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
