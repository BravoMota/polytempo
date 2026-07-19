"""Live trading ledger (PostgreSQL-backed, append-only journal).

Everything is derived by replaying ``live_events`` — there is no mutable balance
column, same philosophy as the paper ledger. The journal invariant:

  * an INTENT row is written BEFORE any order is sent (write-ahead),
  * ORDER rows record observed state transitions,
  * exactly one RESULT row per intent records the terminal outcome,
  * SETTLE rows record payouts once the market resolves.
"""

from __future__ import annotations

from datetime import datetime, timezone

from polytempo.live.models import (
    SIDE_YES,
    STATE_PENDING,
    ExecutionResult,
    OrderIntent,
)
from polytempo.storage.live_postgres import (
    LiveEventRow,
    get_live_connection,
    insert_live_event,
)


class LiveLedger:
    """Append-only journal wrapper for the live node."""

    def __init__(self, database_url: str) -> None:
        self._database_url = database_url

    # ── write-ahead intent / order / result ────────────────────────────────
    def record_intent(self, intent: OrderIntent) -> None:
        """Write the write-ahead INTENT row (state PENDING) before sending."""
        with get_live_connection(self._database_url) as conn:
            insert_live_event(conn, _intent_row(intent, "INTENT", STATE_PENDING))
            conn.commit()

    def record_order_state(
        self,
        intent: OrderIntent,
        order_id: str | None,
        state: str,
        filled_shares: float = 0.0,
        avg_fill_price: float | None = None,
        message: str | None = None,
    ) -> None:
        """Append an ORDER row for one observed exchange state transition."""
        with get_live_connection(self._database_url) as conn:
            insert_live_event(
                conn,
                LiveEventRow(
                    event_type="ORDER",
                    ts_utc=_now_iso(),
                    intent_id=intent.intent_id,
                    order_id=order_id,
                    polymarket_event_id=intent.event_id,
                    bucket_label=intent.bucket_label,
                    token_id=intent.token_id,
                    market_side=intent.market_side,
                    limit_price=intent.limit_price,
                    shares=intent.shares,
                    filled_shares=filled_shares,
                    avg_fill_price=avg_fill_price,
                    state=state,
                    knob_id=intent.knob_id,
                    mode=intent.mode,
                    metadata={"message": message} if message is not None else {},
                ),
            )
            conn.commit()

    def record_result(self, result: ExecutionResult) -> None:
        """Write the single terminal RESULT row for one intent."""
        intent = result.intent
        filled = result.filled_shares
        price = result.avg_fill_price
        stake = filled * price if filled > 0 and price is not None else 0.0
        with get_live_connection(self._database_url) as conn:
            insert_live_event(
                conn,
                LiveEventRow(
                    event_type="RESULT",
                    ts_utc=_now_iso(),
                    intent_id=intent.intent_id,
                    order_id=result.order_id,
                    polymarket_event_id=intent.event_id,
                    bucket_label=intent.bucket_label,
                    token_id=intent.token_id,
                    market_side=intent.market_side,
                    limit_price=intent.limit_price,
                    shares=intent.shares,
                    stake_usd=stake,
                    filled_shares=filled,
                    avg_fill_price=price,
                    state=result.state,
                    knob_id=intent.knob_id,
                    mode=intent.mode,
                    edge_pp=intent.edge_pp,
                    lead_hours=intent.lead_hours,
                    metadata={"message": result.message}
                    if result.message is not None
                    else {},
                ),
            )
            conn.commit()

    # ── settlement ─────────────────────────────────────────────────────────
    def record_settlement(
        self,
        polymarket_event_id: str,
        winning_label: str,
        *,
        ts_utc: str | None = None,
    ) -> list[str]:
        """Settle every filled, unsettled intent on the event. Idempotent-safe."""
        ts = ts_utc or _now_iso()
        settled: list[str] = []
        with get_live_connection(self._database_url) as conn:
            rows = conn.execute(
                """
                SELECT r.intent_id, r.filled_shares, r.market_side,
                       r.bucket_label, r.token_id
                FROM live_events r
                WHERE r.event_type = 'RESULT'
                  AND r.polymarket_event_id = %(eid)s
                  AND r.filled_shares > 0
                  AND NOT EXISTS (
                      SELECT 1 FROM live_events s
                      WHERE s.event_type = 'SETTLE' AND s.intent_id = r.intent_id
                  )
                ORDER BY r.id
                """,
                {"eid": polymarket_event_id},
            ).fetchall()

            for r in rows:
                bucket_won = r["bucket_label"] == winning_label
                won = bucket_won if r["market_side"] == SIDE_YES else not bucket_won
                payout = float(r["filled_shares"]) if won else 0.0
                insert_live_event(
                    conn,
                    LiveEventRow(
                        event_type="SETTLE",
                        ts_utc=ts,
                        intent_id=r["intent_id"],
                        polymarket_event_id=polymarket_event_id,
                        bucket_label=r["bucket_label"],
                        token_id=r["token_id"],
                        market_side=r["market_side"],
                        filled_shares=float(r["filled_shares"]),
                        payout_usd=payout,
                        winning_label=winning_label,
                    ),
                )
                settled.append(r["intent_id"])
            conn.commit()
        return settled

    # ── exposure / positions ───────────────────────────────────────────────
    def has_open_on_event(
        self, polymarket_event_id: str, profile_id: str | None = None
    ) -> bool:
        """True if the event has an in-flight intent or a held (unsettled) fill.

        Dedupe from the write-ahead: an intent blocks while it has no RESULT
        (in-flight/crash window) and while its fill is unsettled. A terminal
        zero-fill RESULT (canceled/rejected/failed unfilled) does NOT block —
        a later gate may try the event again. ``profile_id`` scopes the check
        to one gate profile (matched against intent metadata).
        """
        profile_clause = (
            "AND i.metadata->>'profile_id' = %(pid)s" if profile_id else ""
        )
        params: dict[str, str] = {"eid": polymarket_event_id}
        if profile_id:
            params["pid"] = profile_id
        with get_live_connection(self._database_url) as conn:
            row = conn.execute(
                f"""
                SELECT 1 FROM live_events i
                WHERE i.event_type = 'INTENT'
                  AND i.polymarket_event_id = %(eid)s
                  {profile_clause}
                  AND (
                      NOT EXISTS (
                          SELECT 1 FROM live_events r
                          WHERE r.event_type = 'RESULT' AND r.intent_id = i.intent_id
                      )
                      OR EXISTS (
                          SELECT 1 FROM live_events r
                          WHERE r.event_type = 'RESULT'
                            AND r.intent_id = i.intent_id
                            AND r.filled_shares > 0
                            AND NOT EXISTS (
                                SELECT 1 FROM live_events s
                                WHERE s.event_type = 'SETTLE'
                                  AND s.intent_id = r.intent_id
                            )
                      )
                  )
                LIMIT 1
                """,
                params,
            ).fetchone()
        return row is not None

    def journal_modes(self) -> set[str]:
        """Distinct ``mode`` values present in the journal (live-mode purity guard)."""
        with get_live_connection(self._database_url) as conn:
            rows = conn.execute(
                "SELECT DISTINCT mode FROM live_events WHERE mode IS NOT NULL"
            ).fetchall()
        return {str(r["mode"]) for r in rows}

    def open_event_ids(self) -> list[str]:
        """Events with filled RESULT rows not yet settled."""
        with get_live_connection(self._database_url) as conn:
            rows = conn.execute(
                """
                SELECT DISTINCT r.polymarket_event_id AS eid
                FROM live_events r
                WHERE r.event_type = 'RESULT'
                  AND r.filled_shares > 0
                  AND r.polymarket_event_id IS NOT NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM live_events s
                      WHERE s.event_type = 'SETTLE' AND s.intent_id = r.intent_id
                  )
                ORDER BY r.polymarket_event_id
                """
            ).fetchall()
        return [str(r["eid"]) for r in rows]

    def open_exposure_usd(self) -> float:
        """Filled-unsettled RESULT stake plus in-flight INTENT stake (no RESULT yet)."""
        with get_live_connection(self._database_url) as conn:
            return self._exposure(conn, None)

    def event_exposure_usd(self, polymarket_event_id: str) -> float:
        """Same as ``open_exposure_usd`` scoped to one event."""
        with get_live_connection(self._database_url) as conn:
            return self._exposure(conn, polymarket_event_id)

    def open_positions_by_token(self) -> dict[str, float]:
        """Filled, unsettled shares grouped by token_id (for reconciliation)."""
        with get_live_connection(self._database_url) as conn:
            rows = conn.execute(
                """
                SELECT r.token_id AS token_id,
                       COALESCE(SUM(r.filled_shares), 0) AS shares
                FROM live_events r
                WHERE r.event_type = 'RESULT'
                  AND r.filled_shares > 0
                  AND r.token_id IS NOT NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM live_events s
                      WHERE s.event_type = 'SETTLE' AND s.intent_id = r.intent_id
                  )
                GROUP BY r.token_id
                """
            ).fetchall()
        return {str(r["token_id"]): float(r["shares"]) for r in rows}

    def realized_pnl_on_day(self, day_iso: str) -> float:
        """Sum over SETTLE rows dated ``day_iso`` of payout minus the RESULT stake."""
        with get_live_connection(self._database_url) as conn:
            row = conn.execute(
                """
                SELECT COALESCE(
                    SUM(s.payout_usd - COALESCE(r.stake_usd, 0)), 0
                ) AS pnl
                FROM live_events s
                JOIN live_events r
                  ON r.intent_id = s.intent_id AND r.event_type = 'RESULT'
                WHERE s.event_type = 'SETTLE'
                  AND LEFT(s.ts_utc, 10) = %(day)s
                """,
                {"day": day_iso},
            ).fetchone()
        return float(row["pnl"]) if row else 0.0

    # ── crash recovery / reconciliation ────────────────────────────────────
    def unresolved_intents(self) -> list[dict]:
        """INTENT rows with no RESULT row, with latest ORDER order_id/state."""
        with get_live_connection(self._database_url) as conn:
            rows = conn.execute(
                """
                SELECT
                    i.intent_id AS intent_id,
                    i.token_id AS token_id,
                    i.limit_price AS limit_price,
                    i.shares AS shares,
                    (SELECT o.order_id FROM live_events o
                     WHERE o.event_type = 'ORDER' AND o.intent_id = i.intent_id
                     ORDER BY o.id DESC LIMIT 1) AS order_id,
                    (SELECT o.state FROM live_events o
                     WHERE o.event_type = 'ORDER' AND o.intent_id = i.intent_id
                     ORDER BY o.id DESC LIMIT 1) AS state
                FROM live_events i
                WHERE i.event_type = 'INTENT'
                  AND NOT EXISTS (
                      SELECT 1 FROM live_events r
                      WHERE r.event_type = 'RESULT' AND r.intent_id = i.intent_id
                  )
                ORDER BY i.id
                """
            ).fetchall()
        return [
            {
                "intent_id": r["intent_id"],
                "token_id": r["token_id"],
                "limit_price": (
                    float(r["limit_price"]) if r["limit_price"] is not None else None
                ),
                "shares": float(r["shares"]) if r["shares"] is not None else None,
                "order_id": r["order_id"],
                "state": r["state"],
            }
            for r in rows
        ]

    def resolve_stale_intent(
        self, intent_id: str, state: str, *, message: str | None = None
    ) -> None:
        """Write the missing terminal RESULT row (filled_shares=0) for an intent."""
        with get_live_connection(self._database_url) as conn:
            intent = conn.execute(
                """
                SELECT polymarket_event_id, bucket_label, token_id, market_side,
                       limit_price, shares, knob_id, mode, edge_pp, lead_hours
                FROM live_events
                WHERE event_type = 'INTENT' AND intent_id = %(iid)s
                ORDER BY id DESC LIMIT 1
                """,
                {"iid": intent_id},
            ).fetchone()
            order_id = conn.execute(
                """
                SELECT order_id FROM live_events
                WHERE event_type = 'ORDER' AND intent_id = %(iid)s
                ORDER BY id DESC LIMIT 1
                """,
                {"iid": intent_id},
            ).fetchone()
            insert_live_event(
                conn,
                LiveEventRow(
                    event_type="RESULT",
                    ts_utc=_now_iso(),
                    intent_id=intent_id,
                    order_id=order_id["order_id"] if order_id else None,
                    polymarket_event_id=intent["polymarket_event_id"] if intent else None,
                    bucket_label=intent["bucket_label"] if intent else None,
                    token_id=intent["token_id"] if intent else None,
                    market_side=intent["market_side"] if intent else None,
                    limit_price=intent["limit_price"] if intent else None,
                    shares=intent["shares"] if intent else None,
                    stake_usd=0.0,
                    filled_shares=0.0,
                    state=state,
                    knob_id=intent["knob_id"] if intent else None,
                    mode=intent["mode"] if intent else None,
                    metadata={"message": message} if message is not None else {},
                ),
            )
            conn.commit()

    # ── halt / kill state ──────────────────────────────────────────────────
    def halt(self, reason: str) -> None:
        """Append a HALT row that stops new orders until cleared."""
        self._write_halt({"reason": reason})

    def clear_halt(self) -> None:
        """Append a HALT row that clears an active halt."""
        self._write_halt({"cleared": True})

    def is_halted(self) -> bool:
        """True if the most recent HALT row is not a clear."""
        with get_live_connection(self._database_url) as conn:
            row = conn.execute(
                """
                SELECT metadata FROM live_events
                WHERE event_type = 'HALT'
                ORDER BY id DESC LIMIT 1
                """
            ).fetchone()
        if row is None:
            return False
        return (row["metadata"] or {}).get("cleared") is not True

    # ── helpers ────────────────────────────────────────────────────────────
    def _write_halt(self, metadata: dict) -> None:
        with get_live_connection(self._database_url) as conn:
            insert_live_event(
                conn,
                LiveEventRow(event_type="HALT", ts_utc=_now_iso(), metadata=metadata),
            )
            conn.commit()

    @staticmethod
    def _exposure(conn, polymarket_event_id: str | None) -> float:
        scope = "AND r.polymarket_event_id = %(eid)s" if polymarket_event_id else ""
        intent_scope = (
            "AND i.polymarket_event_id = %(eid)s" if polymarket_event_id else ""
        )
        row = conn.execute(
            f"""
            SELECT
                (SELECT COALESCE(SUM(r.stake_usd), 0)
                 FROM live_events r
                 WHERE r.event_type = 'RESULT'
                   AND r.filled_shares > 0
                   {scope}
                   AND NOT EXISTS (
                       SELECT 1 FROM live_events s
                       WHERE s.event_type = 'SETTLE' AND s.intent_id = r.intent_id
                   )) AS filled_stake,
                (SELECT COALESCE(SUM(i.stake_usd), 0)
                 FROM live_events i
                 WHERE i.event_type = 'INTENT'
                   {intent_scope}
                   AND NOT EXISTS (
                       SELECT 1 FROM live_events rr
                       WHERE rr.event_type = 'RESULT' AND rr.intent_id = i.intent_id
                   )) AS inflight_stake
            """,
            {"eid": polymarket_event_id} if polymarket_event_id else {},
        ).fetchone()
        return float(row["filled_stake"]) + float(row["inflight_stake"])


def _intent_row(intent: OrderIntent, event_type: str, state: str) -> LiveEventRow:
    return LiveEventRow(
        event_type=event_type,
        ts_utc=intent.ts_utc,
        intent_id=intent.intent_id,
        polymarket_event_id=intent.event_id,
        bucket_label=intent.bucket_label,
        token_id=intent.token_id,
        market_side=intent.market_side,
        limit_price=intent.limit_price,
        shares=intent.shares,
        stake_usd=intent.stake_usd,
        state=state,
        knob_id=intent.knob_id,
        mode=intent.mode,
        edge_pp=intent.edge_pp,
        lead_hours=intent.lead_hours,
        metadata=dict(intent.metadata),
    )


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
