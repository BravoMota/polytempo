# Active Position Management (Exit Policies / TP-SL)

## Context

Today every paper position is held to settlement — `append_close()` in `ledger.py:264` raises `NotImplementedError` and the `CLOSE` event type sits reserved in the schema. Oliver wants active management: take-profit / stop-loss style exits, run as a measurable experiment against the hold-to-settlement baseline.

Decisions made:
- **Exit policy becomes a 4th profile dimension**: `hold` / `edge_exit` / `trail30` → 3 × 14 × 9 × 3 = **1,134 profiles**. Hold keeps the existing unsuffixed IDs (`bh_dist_arb_lead30`) so live history continues; clones get `_edgex` / `_trail` suffixes and start fresh at $1,000.
- **edge_exit** (replaces fixed %-TP/SL): at each sweep, recompute the model edge with a **fresh forecast** and live book, measured on the exit side (YES sells at bid, NO at 1−ask). Close when edge ≤ 0pp — covers both TP (price converged past model) and SL (forecast flipped, e.g. tail shots). Plus a hard **ROI floor backstop at −50% of stake** (edge-only can never stop a loser when the model doesn't update — price drop makes edge *bigger*). `book_arb` and `coverage_band` are **exempt** (model-free / basket entries; their edge_exit clones behave as hold) — one frozenset constant to change if the data says otherwise.
- **trail30**: trailing stop only, no TP — track per-trade high-water mark of exit value; close when value drops ≥30% from peak. Applies to all strategies. Lets winners ride toward 1.0 (binary payoff: fixed TP would amputate the fat right tail the strategies' edge lives in).
- **Cadence**: piggyback the existing 15-min settle sweep in `run_tick` (it also fires at gate wakes — harmless, documented).
- **Stale-book guardrail**: skip a trade's evaluation (no trigger, no peak update) when exit-side quote is missing/zero, spread > 0.05, or liquidity < $100 — reuse `DistArbTightConfig` thresholds.

**No SQL migration needed** (verified): `paper_open_positions` already excludes CLOSE rows, `paper_profile_balances` already credits CLOSE `payout_usd`, `metadata JSONB` exists on `paper_events`.

## Implementation Steps

### 1. Profile model — `src/polytempo/profiles/models.py`
- `EXIT_POLICIES = ("hold", "edge_exit", "trail30")` constant; `TradingProfile.exit_policy: str = "hold"` (after `enabled`), validated.
- New frozen dataclass `ExitPolicyParams(edge_threshold_pp=0.0, roi_floor=-0.5, trail_drop=0.30)`.

### 2. Profile generation — `src/polytempo/profiles/load.py`
- `_EXIT_POLICY_SUFFIX = {"hold": "", "edge_exit": "_edgex", "trail30": "_trail"}`; ID = `{abbrev}_{trade}_{leadN}{suffix}`.
- `generate_all_twelve_profiles(...)`: new kwarg `exit_policies` defaulting to `["hold"]` (old yaml → exactly 378, back-compat), innermost loop.
- `load_paper_profiles()`: read `exit_policies` block keys from yaml when present.
- New `load_exit_policy_params(path) -> ExitPolicyParams`: merge yaml values over dataclass defaults.

### 3. Config — `config/paper_profiles.yaml`
```yaml
exit_policies:
  hold: {}
  edge_exit: {edge_threshold_pp: 0.0, roi_floor: -0.5}
  trail30: {trail_drop: 0.30}
```
Update header comment (378 → 1134) and ID-scheme comment.

### 4. Storage read helpers — `src/polytempo/storage/paper_postgres.py`
- `fetch_open_positions(conn)`: one `SELECT * FROM paper_open_positions` for ALL profiles (the monitor must NOT replicate `settle_resolved_open_events`' per-profile connection loop).
- `fetch_bot_state(conn, key)`: read one KV row.

### 5. Ledger CLOSE path — `src/polytempo/paper/ledger.py`
Replace reserved `append_close` (Protocol :82 and impl :264) with `close_trade(profile_id, *, trade_id, event_id, bucket_label, side, shares, exit_price, exit_reason, metadata)`. Insert `event_type="CLOSE"` row mirroring `settle_event`: `payout_usd = round(shares * exit_price, 4)`, `trade_action=exit_reason` (`EDGE_EXIT|SL_FLOOR|TRAIL`), metadata `{exit_reason, exit_price, current_edge_pp, peak_value, yes_bid, yes_ask}`. Update module docstring. `read_state` (:131) already handles CLOSE.

### 6. Edge recomputation — `src/polytempo/analysis.py`
New `compute_event_edges(forecast, event, *, lead_hours, model_strategy, station_id, calibration_stats_path, ...) -> list[BucketEdge]` — standalone ~25 lines reusing the existing pipeline (`to_market_prices` → `calibrate_forecast` → distribution build → `probabilities_for_buckets` → `calculate_bucket_edges`), same path as `analyze_event` (:377–410). Leave `analyze_event` untouched (surgical rule). Returns `BucketEdge` (has `model_probability`, bid/ask, `spread`, `liquidity_usd` — `AnalysisRow` lacks the last two).

### 7. Monitor core — NEW `src/polytempo/paper/monitor.py`
Constants: `EDGE_EXIT_EXEMPT_TRADE_STRATEGIES = frozenset({"book_arb", "coverage_band"})`, `TRAIL_PEAKS_STATE_KEY = "trail_peaks"`, `MONITOR_STATE_KEY = "last_monitor"`, guard thresholds from `DistArbTightConfig` (max_spread 0.05, min_liquidity 100).

`monitor_open_positions(store, profiles, params, *, now=None, dry_run=False) -> MonitorResult`:
1. **One DB read**: `fetch_open_positions` + `fetch_bot_state(trail_peaks)`.
2. Filter in memory: drop hold profiles, unknown profile_ids, and edge_exit trades whose `profile.trade_strategy` is exempt. (First live sweeps evaluate 0 trades — all existing positions are hold.)
3. Group by event. Per event (try/except, log, continue): `hydrate_prices(fetch_event(eid))` (1 Gamma + 1 CLOB call); skip if resolved (settle sweep owns it), `settlement_date is None`, or `lead_hours_to_end_of_target_day(...) <= 0`; one Open-Meteo forecast fetch; `compute_event_edges` **once per (event, model_strategy)** actually needed.
4. Per trade, in memory:
   - Exit value/share: YES → `yes_bid`; NO → `1 − yes_ask`.
   - Stale guardrail → skip (count it), no peak update.
   - edge_exit: exit edge = `(p − yes_bid)·100` for YES, `(yes_ask − p)·100` for NO (signs verified against `calculate_no_edge` in `strategy/edge.py:53`). Trigger `EDGE_EXIT` if ≤ `edge_threshold_pp`; else `SL_FLOOR` if `shares·exit_value − stake ≤ roi_floor·stake`.
   - trail30: peak = max(stored peak, current value), seeded from the OPEN row's stored `yes_bid`/`yes_ask` entry quotes; trigger `TRAIL` if `value ≤ (1−trail_drop)·peak`.
5. Writes (none when `dry_run`): `store.close_trade(...)` per decision; trail peaks upserted as **one** `paper_bot_state` JSON blob per sweep (pruned of closed trades — no migration, single write, restart-safe with deterministic re-seed from entry quotes); sweep summary to `last_monitor` KV. **No `paper_events` TICK row** — a synthetic profile_id would surface as a phantom $1000 account in the balances view.
6. Return summary lines for the tick box (1 summary line + 1 per close).

### 8. Bot integration — `src/polytempo/paper/bot.py` + `scripts/run_paper_bot.py`
- `run_tick(...)`: new `exit_params` kwarg; call `monitor_open_positions` right after `settle_resolved_open_events` (:237), wrapped in try/except; append lines to the tick box.
- `run_paper_bot.py`: `load_exit_policy_params(config_path)` at startup AND in the mtime-reload branch (:103–106); pass through.
- Dedupe note (verified): `profile_has_open_on_event` queries the view, so a CLOSE unblocks re-entry — but each profile's gate fires once (±90s) and monitor runs before opens, so the bot can never re-buy. Only manual `enforce_gate=False` CLI runs can; document as operator caveat in the monitor docstring.

### 9. Dry-run CLI — `src/polytempo/cli/main.py`
`@paper_app.command("monitor")` — **always dry-run** (live closes only via the bot): loads store/profiles/params, runs `monitor_open_positions(dry_run=True)`, prints evaluated/closes/skips + table of would-be closes.

### 10. Docs & test files (edit only — NEVER run pytest here, prod DB URLs in env)
- `tests/test_profiles.py:57`: 378 → 1134 + assert unsuffixed hold IDs unchanged.
- `tests/test_ledger.py` `test_append_close_not_implemented` → `close_trade` test.
- Count/ID mentions: `COMMANDS.md:84` (+ document `paper monitor`), `README.md:12,65`, `docs/PLAN.md:38`, `docs/PRD.md:11`, `docs/ARCHITECTURE.md:17`, `src/polytempo/strategy/PHASE_B.md:180`.

## Verification (no pytest)

1. `python -m compileall src scripts` + import smoke of `polytempo.paper.monitor`.
2. Offline profile check: `python -c` snippet → `load_paper_profiles(...)` count == 1134, legacy 378 IDs byte-identical.
3. `polytempo paper monitor` against live DB → must print `evaluated=0 closes=0` (only hold positions exist yet); re-run after first suffixed opens for real dry-run output.
4. `python scripts/run_paper_bot.py --once` preview path loads 1,134 profiles cleanly.
5. First live sweep: tick box shows monitor line; read-only SQL spot-checks on `paper_events` CLOSE rows, `trail_peaks` / `last_monitor` KV, suffixed-profile balances, closed trade gone from `paper_open_positions`.

## Known risks (accepted, documented)

- `settle_resolved_open_events` per-profile connection loop triples (378→1134 connections per resolved event over Tailscale) — pre-existing hotspot, out of scope; the new `fetch_open_positions` pattern is the future fix.
- `read_state.settled_count` counts CLOSEs (cosmetic inflation in `paper status`).
- Suffixed IDs (≤31 chars) overflow the `:<22`/`:<24` log padding — ragged output, nothing breaks; defer.
- 15-min snapshots: price can gap through a stop; paper fill is at observed snapshot price, so realized SL losses can exceed nominal −50%.
- Param defaults exist in yaml AND `ExitPolicyParams` — keep identical.
