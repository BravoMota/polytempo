-- PolyTempo paper trading schema (PostgreSQL v1).
-- Append-only event log; balances derived from views.
-- Separate database from weather collection (polytempo).

CREATE TABLE IF NOT EXISTS paper_events (
    id BIGSERIAL PRIMARY KEY,
    profile_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    trade_id TEXT,
    ts_utc TEXT NOT NULL,
    polymarket_event_id TEXT,
    bucket_label TEXT,
    side TEXT,
    entry_price DOUBLE PRECISION,
    stake_usd DOUBLE PRECISION,
    shares DOUBLE PRECISION,
    edge_pp DOUBLE PRECISION,
    yes_bid DOUBLE PRECISION,
    yes_ask DOUBLE PRECISION,
    winning_label TEXT,
    payout_usd DOUBLE PRECISION,
    outcome TEXT,
    lead_hours DOUBLE PRECISION,
    model_strategy TEXT,
    trade_action TEXT,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_paper_events_profile_ts
    ON paper_events(profile_id, ts_utc);

CREATE INDEX IF NOT EXISTS idx_paper_events_trade_id
    ON paper_events(trade_id)
    WHERE trade_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_paper_events_profile_event
    ON paper_events(profile_id, polymarket_event_id)
    WHERE event_type = 'OPEN';

CREATE TABLE IF NOT EXISTS paper_bot_state (
    key TEXT PRIMARY KEY,
    value_json JSONB NOT NULL,
    updated_at_utc TEXT NOT NULL
);

-- Open positions: OPEN without SETTLE or CLOSE for same trade_id.
CREATE OR REPLACE VIEW paper_open_positions AS
SELECT o.*
FROM paper_events o
WHERE o.event_type = 'OPEN'
  AND NOT EXISTS (
      SELECT 1
      FROM paper_events c
      WHERE c.trade_id = o.trade_id
        AND c.event_type IN ('SETTLE', 'CLOSE')
  );

-- Per-profile balance from $1000 starting bankroll.
CREATE OR REPLACE VIEW paper_profile_balances AS
WITH opens AS (
    SELECT profile_id, COALESCE(SUM(stake_usd), 0) AS open_stake
    FROM paper_events
    WHERE event_type = 'OPEN'
    GROUP BY profile_id
),
settles AS (
    SELECT profile_id, COALESCE(SUM(payout_usd), 0) AS payouts
    FROM paper_events
    WHERE event_type IN ('SETTLE', 'CLOSE')
    GROUP BY profile_id
),
profiles AS (
    SELECT profile_id FROM paper_events
    UNION
    SELECT profile_id FROM opens
    UNION
    SELECT profile_id FROM settles
)
SELECT
    p.profile_id,
    1000.0 - COALESCE(o.open_stake, 0) + COALESCE(s.payouts, 0) AS balance_usd,
    COALESCE(o.open_stake, 0) AS total_open_stake_usd,
    COALESCE(s.payouts, 0) AS total_payout_usd
FROM profiles p
LEFT JOIN opens o ON o.profile_id = p.profile_id
LEFT JOIN settles s ON s.profile_id = p.profile_id;

-- Recent bot tick / gate decisions.
CREATE OR REPLACE VIEW paper_recent_ticks AS
SELECT *
FROM paper_events
WHERE event_type IN ('TICK', 'GATE_SKIP')
ORDER BY ts_utc DESC;
