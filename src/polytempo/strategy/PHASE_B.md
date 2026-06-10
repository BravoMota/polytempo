# Trade strategies — multi-strategy paper trading

Originally the Phase B locked-decisions doc (three strategies, JSONL ledgers).
Updated to the current lineup: **fourteen trade strategies**, run per profile
against a shared model distribution. Every run fetches the event + forecast
once, builds the distribution once per model strategy, then each profile
applies its trade strategy to the same edges. Names are registered in
`profiles/registry.py`; each profile keeps its own bankroll in Postgres.

## Strategies

### 1. `argmax_yes` (baseline, Phase A)

BUY_YES only on the single bucket the model considers most likely (highest
`p_model`), if it clears the gate; SKIP every other bucket. Sizing via the
ledger's edge ramp: 2% of balance at `edge ≤ 7pp`, 5% at `edge ≥ 15pp`.

Config (`DecisionConfig` defaults):
- `min_edge_pp = 0.0`
- `high_confidence_edge_pp = 15.0`
- `min_liquidity_usd = 100.0`

### 2. `argmax_no`

NO-side mirror of `argmax_yes`, but selects by **largest NO edge**
(`edge_no_pp = yes_bid - p_model` — the bucket the market most overprices
relative to the model), not the literal lowest-probability bucket. BUY_NO it
if it passes the gate (`decide_bucket_no`); SKIP the rest.

Config: `DecisionConfig` defaults (same as `argmax_yes`).

### 3. `dist_arb` (fridius2 clone)

For each bucket compute both sides:

```
edge_yes_pp = (p_model - yes_ask) * 100
edge_no_pp  = (yes_bid - p_model) * 100        # NO entry at (1 - yes_bid)
best_pp     = max(edge_yes_pp, edge_no_pp)
```

If `best_pp > 0`, buy the winning side. Otherwise SKIP. No cap on NO entry
price. Sizing via the same 2-5% edge ramp on `best_pp`.

Config (`DistArbConfig`):
- `min_edge_pp = 0.0`
- `high_confidence_edge_pp = 15.0`
- `min_liquidity_usd = 25.0` (stale-quote filter, not a sizing constraint)

### 4. `mid_band` (NimbusCapital clone)

Filter buckets to `0.20 ≤ yes_ask ≤ 0.60`, then BUY_YES on positive edge
with a flat $5 ticket. YES-only; active-sell behavior deferred.

Config (`MidBandConfig`):
- `price_min = 0.20`, `price_max = 0.60`
- `min_edge_pp = 0.0`
- `min_liquidity_usd = 50.0`
- `flat_ticket_usd = 5.0`

### 5. `topk_yes` / 6. `topk_no`

`TopKStrategy` generalizes argmax: bet the `k` (default 3) buckets carrying
the largest edge on the chosen side that also clear the gate; SKIP the rest
(`not in top-k`). `topk_no` is the same class configured with `side="NO"`
(registered via a factory lambda in `profiles/registry.py`).

Config: `DecisionConfig` defaults, `k = 3`.

### 7. `max_edge`

Scans every bucket on **both sides** and trades only the single richest edge
in the whole event, whichever side it falls on; SKIPs everything else
(`not max_edge bucket`). Maximum concentration: one ticket per event.

Config: `DecisionConfig` defaults.

### 8. `edge_band`

Like `dist_arb` picks the better of YES/NO per bucket, but only trades when
the edge sits in a believable band: edges `≤ min` are too thin, edges
`> max` are treated as a red flag (stale quote or model error) and SKIPped
(`edge above band`) rather than chased.

Config (`EdgeBandConfig`):
- `min_edge_pp = 5.0`, `max_edge_pp = 25.0`
- `high_confidence_edge_pp = 15.0`
- `min_liquidity_usd = 25.0`

### 9. `book_arb`

Model-free whole-event sum check. Exactly one bucket resolves YES, so if the
asks across every bucket sum below $1, buying the full YES book locks in
profit regardless of outcome; if the bids sum above $1, the full NO book
does. Buys equal shares of every leg (stake per leg proportional to entry
price, `total_ticket_usd` across the book) and trades nothing unless every
bucket is quoted and liquid. Doubles as a control: it needs no model input.

Config (`BookArbConfig`):
- `min_profit_pp = 2.0` (noise buffer on the $1 gap)
- `high_confidence_profit_pp = 5.0`
- `min_liquidity_usd = 25.0` (every bucket; else `thin book`)
- `total_ticket_usd = 20.0`

### 10. `coverage_band`

Buys the model's credible interval as a basket: the smallest contiguous
bucket window around the model peak holding ≥ `target_mass` probability,
bought YES-only with a flat ticket per leg when the **basket** edge
(`sum(p) - sum(ask)` over the window, in pp) clears the gate. Individual leg
edges may be negative; the basket is the unit. Robust to the model being
right about the distribution but one bucket off on the peak.

Config (`CoverageBandConfig`):
- `target_mass = 0.80`
- `min_basket_edge_pp = 5.0`
- `high_confidence_edge_pp = 15.0` (on the basket edge)
- `min_liquidity_usd = 25.0`
- `flat_ticket_usd = 5.0` (per leg)

### 11. `max_roi`

Like `max_edge` scans both sides of every bucket, but ranks by expected
return on stake (`win_probability / entry_price - 1`) instead of edge pp:
5pp of edge at a 5-cent ask doubles the stake in expectation, while 5pp at
50 cents returns 10%. One ticket per event. `min_win_probability` floors the
chosen side's model win probability so the ranking cannot chase longshots
whose ROI explodes as the entry price approaches zero.

Config (`MaxRoiConfig`):
- `min_roi = 0.05`, `high_confidence_roi = 0.25`
- `min_win_probability = 0.05`
- `min_liquidity_usd = 25.0`

### 12. `dist_arb_tight`

`dist_arb` selection behind hard quote-quality gates: the spread must be
present and ≤ `max_spread` (a missing spread rejects the bucket — not a
warning), and the liquidity floor is raised to 100. Tests whether dist_arb's
long-lead "edge" is real mispricing or stale/wide quotes; compare the two
head-to-head per lead.

Config (`DistArbTightConfig`):
- `min_edge_pp = 0.0`
- `high_confidence_edge_pp = 15.0`
- `min_liquidity_usd = 100.0`
- `max_spread = 0.05`

### 13. `dist_arb_kelly`

Identical selection to `dist_arb`; only sizing differs. Stakes a fractional
Kelly bet via `TradeDecision.stake_fraction` (fraction of current balance,
honored by the ledger ahead of the edge ramp): full Kelly for a binary
payout at entry price `c` with win probability `p` is `(p - c) / (1 - c)`,
scaled by `kelly_multiplier` and capped at `max_stake_fraction` — the ramp
ceiling, so the two sizing schemes stay comparable.

Config (`DistArbKellyConfig`):
- selection gates as `DistArbConfig` (`min_edge_pp = 0.0`, `min_liquidity_usd = 25.0`)
- `kelly_multiplier = 0.25`, `max_stake_fraction = 0.05`

### 14. `tail_fade`

BUY_NO on the open-ended tail buckets (`or_below` / `or_higher` / `above`
label kinds) when the market still bids ≥ `min_yes_bid` for an outcome the
model puts at ≤ `max_model_probability` — harvesting favorite-longshot bias.
Selection is structural (tail position), not edge-ranked, isolating whether
NO trades win because of the model or because longshots are systematically
overpriced. Requires the NO edge to be positive (`tail not overpriced`
otherwise).

Config (`TailFadeConfig`):
- `max_model_probability = 0.05`
- `min_yes_bid = 0.03`
- `high_confidence_edge_pp = 15.0`
- `min_liquidity_usd = 25.0`

## Bankroll & ledgers

Each **profile** (model strategy × trade strategy × lead gate — 378 total
from `config/paper_profiles.yaml`) starts with $1000 and keeps an independent
ledger in the paper Postgres database (`paper_events` table, append-only;
see `storage/schema_paper_postgres.sql`). Balance is always derived by
replaying the profile's OPEN/SETTLE rows (`paper_profile_balances` view).

The original Phase B JSONL files under `paper_ledger/` are legacy artifacts
and no longer written.

## OPEN record fields

One `paper_events` row with `event_type='OPEN'`:

`profile_id`, `trade_id`, `ts_utc`, `polymarket_event_id`, `bucket_label`,
`side` (`YES`/`NO`), `entry_price`, `stake_usd`, `shares`, `edge_pp`,
`yes_bid`, `yes_ask`, `lead_hours`, `model_strategy`, `metadata`.

`entry_price = yes_ask` for YES legs, `1 - yes_bid` for NO legs.
`shares = stake_usd / entry_price`.

## SETTLE record fields

One `paper_events` row with `event_type='SETTLE'`: `profile_id`, `trade_id`,
`ts_utc`, `bucket_label`, `side`, `winning_label`, `outcome`, `payout_usd`.

YES leg pays `shares × $1` if `winning_label == bucket_label`, else $0.
NO leg pays `shares × $1` if `winning_label != bucket_label`, else $0.

## Out of scope

- Active-sell logic for `mid_band` (needs websocket).
- **Take-profit / early-exit when a held bucket re-prices favorably against the
  model.** Hold-to-settlement only; see also the per-profile de-dup guard on
  re-runs that prevents same-day exposure stacking.
- Event-level caps (`max_legs_per_event`, `max_pct_per_event`).
- Backtest harness.
- Wallet tracker for the observed wallets.
