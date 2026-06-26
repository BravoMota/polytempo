"""PostgreSQL helpers for paper trading ledger and bot state."""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Generator

import psycopg
from psycopg import Connection
from psycopg.rows import dict_row

DEFAULT_PAPER_SCHEMA_PATH = (
    Path(__file__).resolve().parent / "schema_paper_postgres.sql"
)

STARTING_BALANCE_USD = 1000.0


def resolve_paper_database_url(*, override: str | None = None) -> str:
    """Resolve paper Postgres URL from override or env."""
    if override:
        return override
    url = os.environ.get("POLYTEMPO_PAPER_DATABASE_URL")
    if not url:
        raise RuntimeError("Set POLYTEMPO_PAPER_DATABASE_URL")
    return url


@contextmanager
def get_paper_connection(database_url: str) -> Generator[Connection, None, None]:
    """Open a PostgreSQL connection with dict rows."""
    conn = psycopg.connect(database_url, row_factory=dict_row, autocommit=False)
    try:
        yield conn
    finally:
        conn.close()


def _execute_sql_script(conn: Connection, sql: str) -> None:
    statement = ""
    for line in sql.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("--"):
            continue
        statement += line + "\n"
        if stripped.endswith(";"):
            conn.execute(statement)
            statement = ""
    if statement.strip():
        conn.execute(statement)


def initialize_paper_database(
    database_url: str,
    schema_path: Path = DEFAULT_PAPER_SCHEMA_PATH,
) -> None:
    """Create or upgrade the paper database schema. Safe to run multiple times."""
    if not schema_path.is_file():
        raise FileNotFoundError(f"schema not found: {schema_path}")
    sql = schema_path.read_text(encoding="utf-8")
    with get_paper_connection(database_url) as conn:
        _execute_sql_script(conn, sql)
        conn.commit()


def truncate_paper_tables(conn: Connection) -> None:
    """Clear paper tables (tests only)."""
    conn.execute("TRUNCATE TABLE paper_events, paper_bot_state RESTART IDENTITY CASCADE")


@dataclass(frozen=True)
class PaperEventRow:
    """One row to insert into paper_events."""

    profile_id: str
    event_type: str
    ts_utc: str
    trade_id: str | None = None
    polymarket_event_id: str | None = None
    bucket_label: str | None = None
    side: str | None = None
    entry_price: float | None = None
    stake_usd: float | None = None
    shares: float | None = None
    edge_pp: float | None = None
    yes_bid: float | None = None
    yes_ask: float | None = None
    winning_label: str | None = None
    payout_usd: float | None = None
    outcome: str | None = None
    lead_hours: float | None = None
    model_strategy: str | None = None
    trade_action: str | None = None
    metadata: dict[str, Any] | None = None


def insert_paper_event(conn: Connection, row: PaperEventRow) -> None:
    """Append one paper event."""
    conn.execute(
        """
        INSERT INTO paper_events (
            profile_id, event_type, trade_id, ts_utc, polymarket_event_id,
            bucket_label, side, entry_price, stake_usd, shares, edge_pp,
            yes_bid, yes_ask, winning_label, payout_usd, outcome,
            lead_hours, model_strategy, trade_action, metadata
        ) VALUES (
            %(profile_id)s, %(event_type)s, %(trade_id)s, %(ts_utc)s,
            %(polymarket_event_id)s, %(bucket_label)s, %(side)s,
            %(entry_price)s, %(stake_usd)s, %(shares)s, %(edge_pp)s,
            %(yes_bid)s, %(yes_ask)s, %(winning_label)s, %(payout_usd)s,
            %(outcome)s, %(lead_hours)s, %(model_strategy)s, %(trade_action)s,
            %(metadata)s::jsonb
        )
        """,
        {
            "profile_id": row.profile_id,
            "event_type": row.event_type,
            "trade_id": row.trade_id,
            "ts_utc": row.ts_utc,
            "polymarket_event_id": row.polymarket_event_id,
            "bucket_label": row.bucket_label,
            "side": row.side,
            "entry_price": row.entry_price,
            "stake_usd": row.stake_usd,
            "shares": row.shares,
            "edge_pp": row.edge_pp,
            "yes_bid": row.yes_bid,
            "yes_ask": row.yes_ask,
            "winning_label": row.winning_label,
            "payout_usd": row.payout_usd,
            "outcome": row.outcome,
            "lead_hours": row.lead_hours,
            "model_strategy": row.model_strategy,
            "trade_action": row.trade_action,
            "metadata": json.dumps(row.metadata or {}),
        },
    )


def upsert_bot_state(conn: Connection, key: str, value: dict[str, Any], ts_utc: str) -> None:
    conn.execute(
        """
        INSERT INTO paper_bot_state (key, value_json, updated_at_utc)
        VALUES (%(key)s, %(value)s::jsonb, %(ts)s)
        ON CONFLICT (key) DO UPDATE SET
            value_json = EXCLUDED.value_json,
            updated_at_utc = EXCLUDED.updated_at_utc
        """,
        {"key": key, "value": json.dumps(value), "ts": ts_utc},
    )


def fetch_bot_state(conn: Connection, key: str) -> dict[str, Any] | None:
    """Read one ``paper_bot_state`` value (jsonb → dict), or None if absent."""
    row = conn.execute(
        "SELECT value_json FROM paper_bot_state WHERE key = %(k)s",
        {"k": key},
    ).fetchone()
    return row["value_json"] if row else None


def fetch_open_event_ids(conn: Connection, profile_id: str | None = None) -> list[str]:
    """Distinct polymarket event ids with open positions."""
    if profile_id:
        rows = conn.execute(
            """
            SELECT DISTINCT polymarket_event_id AS event_id
            FROM paper_open_positions
            WHERE profile_id = %(pid)s AND polymarket_event_id IS NOT NULL
            ORDER BY polymarket_event_id
            """,
            {"pid": profile_id},
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT DISTINCT polymarket_event_id AS event_id
            FROM paper_open_positions
            WHERE polymarket_event_id IS NOT NULL
            ORDER BY polymarket_event_id
            """
        ).fetchall()
    return [str(r["event_id"]) for r in rows]


def profile_has_open_on_event(
    conn: Connection,
    profile_id: str,
    polymarket_event_id: str,
) -> bool:
    row = conn.execute(
        """
        SELECT 1
        FROM paper_open_positions
        WHERE profile_id = %(pid)s AND polymarket_event_id = %(eid)s
        LIMIT 1
        """,
        {"pid": profile_id, "eid": polymarket_event_id},
    ).fetchone()
    return row is not None
