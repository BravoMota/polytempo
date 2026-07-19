"""PostgreSQL helpers for the live trading journal and node state."""

from __future__ import annotations

import json
import math
import os
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Generator

import psycopg
from psycopg import Connection
from psycopg.rows import dict_row

DEFAULT_LIVE_SCHEMA_PATH = (
    Path(__file__).resolve().parent / "schema_live_postgres.sql"
)


def resolve_live_database_url(*, override: str | None = None) -> str:
    """Resolve live Postgres URL from override or env."""
    if override:
        return override
    url = os.environ.get("POLYTEMPO_LIVE_DATABASE_URL")
    if not url:
        raise RuntimeError("Set POLYTEMPO_LIVE_DATABASE_URL")
    return url


@contextmanager
def get_live_connection(database_url: str) -> Generator[Connection, None, None]:
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


def initialize_live_database(
    database_url: str,
    schema_path: Path = DEFAULT_LIVE_SCHEMA_PATH,
) -> None:
    """Create or upgrade the live database schema. Safe to run multiple times."""
    if not schema_path.is_file():
        raise FileNotFoundError(f"schema not found: {schema_path}")
    sql = schema_path.read_text(encoding="utf-8")
    with get_live_connection(database_url) as conn:
        _execute_sql_script(conn, sql)
        conn.commit()


def truncate_live_tables(conn: Connection) -> None:
    """Clear live tables (tests only)."""
    conn.execute("TRUNCATE TABLE live_events, live_node_state RESTART IDENTITY CASCADE")


def sanitize_json(value: Any) -> Any:
    """Recursively replace non-finite floats (nan/inf) with None.

    Python's ``json.dumps`` emits the bare tokens ``NaN``/``Infinity`` which
    Postgres ``jsonb`` rejects. Every value bound to a jsonb column must pass
    through here first (see docs/project-audit-2026-07-06.md §2).
    """
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {k: sanitize_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize_json(v) for v in value]
    return value


@dataclass(frozen=True)
class LiveEventRow:
    """One row to insert into live_events."""

    event_type: str
    ts_utc: str
    intent_id: str | None = None
    order_id: str | None = None
    polymarket_event_id: str | None = None
    bucket_label: str | None = None
    token_id: str | None = None
    market_side: str | None = None
    limit_price: float | None = None
    shares: float | None = None
    stake_usd: float | None = None
    filled_shares: float | None = None
    avg_fill_price: float | None = None
    state: str | None = None
    knob_id: str | None = None
    mode: str | None = None
    edge_pp: float | None = None
    lead_hours: float | None = None
    payout_usd: float | None = None
    winning_label: str | None = None
    metadata: dict[str, Any] | None = None


def insert_live_event(conn: Connection, row: LiveEventRow) -> None:
    """Append one live journal event."""
    conn.execute(
        """
        INSERT INTO live_events (
            event_type, intent_id, order_id, ts_utc, polymarket_event_id,
            bucket_label, token_id, market_side, limit_price, shares, stake_usd,
            filled_shares, avg_fill_price, state, knob_id, mode, edge_pp,
            lead_hours, payout_usd, winning_label, metadata
        ) VALUES (
            %(event_type)s, %(intent_id)s, %(order_id)s, %(ts_utc)s,
            %(polymarket_event_id)s, %(bucket_label)s, %(token_id)s,
            %(market_side)s, %(limit_price)s, %(shares)s, %(stake_usd)s,
            %(filled_shares)s, %(avg_fill_price)s, %(state)s, %(knob_id)s,
            %(mode)s, %(edge_pp)s, %(lead_hours)s, %(payout_usd)s,
            %(winning_label)s, %(metadata)s::jsonb
        )
        """,
        {
            "event_type": row.event_type,
            "intent_id": row.intent_id,
            "order_id": row.order_id,
            "ts_utc": row.ts_utc,
            "polymarket_event_id": row.polymarket_event_id,
            "bucket_label": row.bucket_label,
            "token_id": row.token_id,
            "market_side": row.market_side,
            "limit_price": row.limit_price,
            "shares": row.shares,
            "stake_usd": row.stake_usd,
            "filled_shares": row.filled_shares,
            "avg_fill_price": row.avg_fill_price,
            "state": row.state,
            "knob_id": row.knob_id,
            "mode": row.mode,
            "edge_pp": row.edge_pp,
            "lead_hours": row.lead_hours,
            "payout_usd": row.payout_usd,
            "winning_label": row.winning_label,
            "metadata": json.dumps(sanitize_json(row.metadata or {})),
        },
    )


def fetch_node_state(conn: Connection, key: str) -> dict[str, Any] | None:
    """Read one ``live_node_state`` value (jsonb → dict), or None if absent."""
    row = conn.execute(
        "SELECT value_json FROM live_node_state WHERE key = %(k)s",
        {"k": key},
    ).fetchone()
    return row["value_json"] if row else None


def upsert_node_state(
    conn: Connection, key: str, value_json: dict[str, Any], updated_at_utc: str
) -> None:
    conn.execute(
        """
        INSERT INTO live_node_state (key, value_json, updated_at_utc)
        VALUES (%(key)s, %(value)s::jsonb, %(ts)s)
        ON CONFLICT (key) DO UPDATE SET
            value_json = EXCLUDED.value_json,
            updated_at_utc = EXCLUDED.updated_at_utc
        """,
        {"key": key, "value": json.dumps(sanitize_json(value_json)), "ts": updated_at_utc},
    )
