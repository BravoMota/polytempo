# Phase B — Multi-strategy paper trading

Locked decisions for the three strategies we run in parallel on every paper
run. Every `polytempo live` (and `paper open`) fetches the event + forecast
once, builds the model distribution once, then runs all three strategies
against the same model output. Each keeps its own ledger and bankroll.

## Strategies

### 1. `argmax_yes` (baseline, Phase A)

Buy YES on any bucket whose model edge is strictly positive. Sizing via the
ledger's edge ramp: 2% of balance at `edge ≤ 7pp`, 5% at `edge ≥ 15pp`.

Config (`DecisionConfig` defaults):
- `min_edge_pp = 0.0`
- `high_confidence_edge_pp = 15.0`
- `min_liquidity_usd = 100.0`

### 2. `dist_arb` (fridius2 clone)

For each bucket compute both sides:

```
edge_yes_pp = (p_model - yes_ask) * 100
edge_no_pp  = (yes_bid - p_model) * 100        # NO entry at (1 - yes_bid)
best_pp     = max(edge_yes_pp, edge_no_pp)
```

If `best_pp > 0`, buy the winning side. Otherwise SKIP. No cap on NO entry
price. Sizing via the same 2-5% edge ramp on `best_pp`.

Config:
- `min_edge_pp = 0.0`
- `min_liquidity_usd = 100.0`
- `high_confidence_edge_pp = 15.0`
- `no_max_price = None` (no ceiling)

### 3. `mid_band` (NimbusCapital clone)

Filter buckets to `0.20 ≤ yes_ask ≤ 0.60`, then BUY_YES on positive edge
with a flat $5 ticket. YES-only in Phase B; active-sell behavior deferred.

Config:
- `price_min = 0.20`, `price_max = 0.60`
- `min_edge_pp = 0.0`
- `min_liquidity_usd = 50.0`
- `flat_ticket_usd = 5.0`

## Bankroll & ledgers

Each strategy starts with $1000 and keeps an independent JSONL ledger:

```
paper_ledger/
  argmax_yes.jsonl
  dist_arb.jsonl
  mid_band.jsonl
```

Balance derived by replaying the file. One line per OPEN or SETTLE record.

## OPEN record schema

```json
{
  "type": "OPEN",
  "trade_id": "...",
  "ts": "2026-05-20T12:00:00Z",
  "strategy": "dist_arb",
  "event_id": "...",
  "event_slug": "...",
  "settlement_date": "2026-05-21",
  "bucket_label": "20°C",
  "side": "YES" | "NO",
  "entry_price": 0.77,
  "yes_bid": 0.23,
  "yes_ask": 0.25,
  "p_model": 0.18,
  "edge_pp": 5.0,
  "stake_usd": 20.0,
  "shares": 25.97,
  "confidence": "medium",
  "warnings": []
}
```

`entry_price = yes_ask` for YES legs, `1 - yes_bid` for NO legs.
`shares = stake_usd / entry_price`.

## SETTLE record schema

```json
{
  "type": "SETTLE",
  "trade_id": "...",
  "ts": "...",
  "strategy": "dist_arb",
  "event_id": "...",
  "bucket_label": "20°C",
  "side": "NO",
  "winning_label": "21°C",
  "outcome": "YES" | "NO",
  "payout_usd": 25.97
}
```

YES leg pays `shares × $1` if `winning_label == bucket_label`, else $0.
NO leg pays `shares × $1` if `winning_label != bucket_label`, else $0.

## Surface changes

- `TradeDecision.action`: add `"BUY_NO"` as a valid value.
- `AnalysisRow`: add `stake_usd: float | None = None`, `side: str | None = None`.
- `paper/ledger.py`: BUY_NO support, honor `row.stake_usd` override, richer
  OPEN record fields, per-strategy file paths.
- `cli/main.py`: `live` and `paper open` loop over the three strategies and
  write each to its own ledger; report renders all three side-by-side.
- `paper status`: shows balances for all three ledgers in one table.

## Out of scope for Phase B

- Kelly sizing (revisit in Phase C if backtest motivates it).
- Active-sell logic for `mid_band` (Phase D, needs websocket).
- Event-level caps (`max_legs_per_event`, `max_pct_per_event`).
- Backtest harness (Phase C).
- Wallet tracker for the three observed wallets (Phase D, parallel track).
