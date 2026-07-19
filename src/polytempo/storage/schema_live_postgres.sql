-- PolyTempo live trading schema (PostgreSQL v1).
-- Append-only journal; positions/exposure/pnl derived by replaying the journal.
-- Separate database from paper trading (polytempo_paper) and weather collection.

CREATE TABLE IF NOT EXISTS live_events (
    id BIGSERIAL PRIMARY KEY,
    event_type TEXT NOT NULL,            -- INTENT | ORDER | RESULT | SETTLE | HALT | RECONCILE
    intent_id TEXT,
    order_id TEXT,
    ts_utc TEXT NOT NULL,
    polymarket_event_id TEXT,
    bucket_label TEXT,
    token_id TEXT,
    market_side TEXT,                    -- YES | NO
    limit_price DOUBLE PRECISION,
    shares DOUBLE PRECISION,
    stake_usd DOUBLE PRECISION,
    filled_shares DOUBLE PRECISION,
    avg_fill_price DOUBLE PRECISION,
    state TEXT,
    knob_id TEXT,
    mode TEXT,                           -- dry_run | live
    edge_pp DOUBLE PRECISION,
    lead_hours DOUBLE PRECISION,
    payout_usd DOUBLE PRECISION,
    winning_label TEXT,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_live_events_intent_id
    ON live_events(intent_id)
    WHERE intent_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_live_events_order_id
    ON live_events(order_id)
    WHERE order_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_live_events_polymarket_event
    ON live_events(polymarket_event_id)
    WHERE polymarket_event_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_live_events_type_ts
    ON live_events(event_type, ts_utc);

CREATE TABLE IF NOT EXISTS live_node_state (
    key TEXT PRIMARY KEY,
    value_json JSONB NOT NULL,
    updated_at_utc TEXT NOT NULL
);
