# Repo Diagnosis — 2026-07-12

Full-repo diagnosis performed 2026-07-12 from olivesurface (Windows clone) against
the live production DBs on mac0. Scope: every moving part — collectors,
calibration, model/strategy pipeline, paper bot, active wallets, CLI, storage,
reports, deploy — plus a rebuilt knowledge graph (`graphify-out/`, 2,074 nodes /
5,149 edges / 116 communities). Analysis only; no code changed.

Companion docs: [MODEL_DIAGNOSTICS_2026-06-13.md](MODEL_DIAGNOSTICS_2026-06-13.md)
(model quality), [timezone-lead-hours-audit.md](timezone-lead-hours-audit.md)
(lead-anchor audit, 2026-06-23).

---

## 0. TL;DR

- **Connectivity:** mac0 reachable over Tailscale at `100.76.158.32`, now via a
  **direct path (~23 ms)** instead of the DERP relay (50–130 ms). Port 5432 open,
  passwordless `jnlow` auth works, `polytempo paper status` returns all ~438 wallets.
- **One confirmed crash bug** (dormant in prod): `run_daily_calibration.py`
  continuous mode dies with a `TypeError` on its first loop.
- **Ledger design gets slower every day**: full per-profile event-log replay +
  one fresh Postgres connection per operation, multiplied over the tailnet.
- **No guard against double SETTLE/CLOSE** per trade — a race silently
  double-credits payout.
- **The 2026-06-23 timezone audit's P1s are still unimplemented** (verified
  against current code).
- **The 42-wallet active experiment is partly redundant** (the three dist_arb
  variants collapse into identical wallets) **and bleeding by construction**
  (taker churn around `exit_edge_pp: 0`).

---

## 1. Connectivity check (mac0)

- `tailscale status`: Mac present at `100.76.158.32`
  (`macbook-pro-de-joo-mota.tail348c53.ts.net`, `BravoMota@`).
- `tailscale ping`: pong via `188.37.182.154:26631` in **23 ms — direct path**,
  no longer DERP-relayed.
- `Test-NetConnection 100.76.158.32 -Port 5432` → open.
- `polytempo paper status` → all profiles returned (378 gated + 16 xsell +
  42 active wallets).

---

## 2. Architecture — every moving part, verified against code

Two Postgres DBs on mac0, one deterministic pipeline, six launchd jobs.

**Data in** — `scripts/run_collector.py` drives three collectors on UTC
wall-clock slots (`collectors/schedule.py`):

- **Wunderground** HTML scrape: observations every 5 min + hourly forecasts,
  parsed from the embedded Angular `app-root-state` JSON (`collectors/wunderground.py`).
- **Open-Meteo** multi-model daily-max with S3 rolling-meta pairing for
  run-init leads (`collectors/open_meteo.py`, `weather/open_meteo.py`).
- **CLOB snapshots**: live order books every 5 min, 3 cities, 4-day horizon
  (`collectors/polymarket_clob.py`).

Nightly (`--once`, launchd 01:00 mac0 local) `run_daily_calibration.py`
recomputes `calibration_stats_updated.csv`; the frozen `calibration_stats.csv`
is the bh experiment control and is never regenerated.

**Decision path** — `paper/market_context.py:fetch_market_context`:
Gamma discovery (endDate window + `title_search`, lowest-temp events excluded)
→ `hydrate_prices` overwrites Gamma's cached quotes with live CLOB books →
Open-Meteo live bundle + WU adjusted-Tmax appended (`weather/wu_live_forecast.py`)
→ `analysis.py:analyze_event` builds a Normal distribution per model strategy
(bh = frozen CSV best-model, bhu = nightly CSV, whu = precision-weighted mixture)
→ per-bucket edges (`strategy/edge.py`) → one of 14 strategy `.decide()`s →
BUY/SKIP rows.

**Execution** — `scripts/run_paper_bot.py` (launchd, KeepAlive) sleeps to exact
gate instants. Each tick (`paper/bot.py:run_tick`): settle sweep (15 min) →
xsell TP/SL exit sweep (3-min fast poll on resolution day) → active-wallet
sweep (deduped per gate instant via `paper_bot_state`) → gated opens.
Ledger = append-only `paper_events` (OPEN/SETTLE/CLOSE/TICK/GATE_SKIP);
balances always replayed from $1,000 (`paper/ledger.py`).

**Out** — live Markdown reports (`reports/`), nightly performance CSV export,
Streamlit viewer on `127.0.0.1:8501`, `polytempo-health.sh` evidence bundles,
`pg_dump` backups with 14-day retention.

---

## 3. Findings

### 3.1 Confirmed bug (new)

**`scripts/run_daily_calibration.py:86-91` — continuous mode crashes instantly.**
It calls:

```python
is_slot_due(now, last_slot, interval_seconds=..., anchor_time_utc=...)
```

but the signature (`collectors/schedule.py:67`) is
`is_slot_due(now_utc, interval_seconds, anchor_time_utc, last_run_slot)` —
`last_slot` lands on `interval_seconds` positionally *and* `interval_seconds`
is passed by keyword → guaranteed `TypeError` on the first loop iteration.
Even if the signature matched, the `(due, slot)` tuple return is used as a bare
truthy bool, so it would fire every iteration. Production dodges this because
launchd runs `--once`, but COMMANDS.md advertises the "optional daemon loop".
Five-line fix (or delete daemon mode and make `--once` the only path).

### 3.2 Known-but-open (2026-06-23 timezone audit — all verified still present)

- **P1** — gate/ledger lead anchored to UTC midnight instead of station-local
  end-of-day: London-summer gates fire 1 h late vs. their nominal lead; stored
  `paper_events.lead_hours` is mislabeled by the DST offset.
- **P1** — `lead_hours_to_day_end` column conflation: WU snapshot tables are
  station-local, `clob_bucket_snapshots` is UTC, same column name. Any
  lead-joined backtest between them is silently off by 1–2 h. This is the one
  quietly poisoning future backtest data as CLOB snapshots accumulate.
- **P2** — `LONDON_TZ` hardcoded in `paper/bot.py:50` for the active-exit
  window; CLI still uses server-local `date.today()` (`cli/main.py:302`).

The audit's §11 migration plan is good; none of it has shipped.

### 3.3 Design risks (new)

1. **Ledger performance degrades linearly with history, multiplied over the
   network.** `PostgresLedgerStore.read_state` does `SELECT *` of a profile's
   entire event history — including TICK/GATE_SKIP rows with fat JSONB audit
   metadata it then ignores — and every ledger operation opens a **fresh
   psycopg connection** (no pooling). Compounding: `flatten_leg` calls
   `close_position` per ticket, each re-reading full state (a 19-ticket
   dist_arb leg = 19 full replays + ~38 connections);
   `settle_resolved_open_events` does a `has_open_on_event` roundtrip for all
   ~438 profiles × each open event, every 15 minutes. Tolerable at 23 ms
   direct; approaching minutes per sweep if the path falls back to DERP.
   Fixes: filter `event_type IN ('OPEN','SETTLE','CLOSE')` + needed columns in
   `read_state`; reuse one connection per tick; replace the settle scan with
   one query against the existing `paper_open_positions` view.
2. **Nothing prevents a double SETTLE/CLOSE per trade.** `read_state` credits
   payout even when the matching OPEN was already popped, so a manual
   `paper settle` racing the bot double-credits silently. Fix:
   `CREATE UNIQUE INDEX ... ON paper_events(trade_id) WHERE event_type IN ('SETTLE','CLOSE')`.
3. **Three of the 42 active wallets are the same wallet.**
   `{bh,bhu,whu}_dist_arb_active`, `_dist_arb_tight_active`, and
   `_dist_arb_kelly_active` show byte-identical balances ($91.10 / $48.73 /
   $52.73 per model, same open/settled counts). Cause: the active controller
   substitutes its own sizing (`stake_fraction` ramp) and its own add/exit
   thresholds (5pp/0pp) for the strategy's — kelly sizing and tight thresholds
   are discarded, so the three variants collapse into one experiment at 3× the
   trade volume.
4. **The active experiment bleeds by construction.** Most active wallets are
   down 80–90%; the low-churn `mid_band_active` family is the only green one.
   Mechanism: taker fills both directions (add at ask, flatten at bid) with
   `exit_edge_pp: 0` means every noise-flip of edge around zero pays the full
   spread. Consider hysteresis (N consecutive ticks below exit, or a negative
   exit floor like −3pp) and charging the spread inside the add/exit decision.
5. **Local vs. mac0 behavioral drift.** A stale local
   `calibration_stats_updated.csv` silently disables all bhu/whu profiles via
   the 48 h freshness gate (`profiles/calibration_ready.py`), so local previews
   don't match the bot on mac0.

### 3.4 Hygiene (small)

- 17 ruff findings (unused imports/variables, e.g. `bot.py`'s
  `lead_hours_before_target`); no `[tool.ruff]`/`[tool.mypy]` config in
  `pyproject.toml`; **no CI** despite 68 test files.
- Dead weight: `paper_ledger/*.jsonl` (pre-Postgres legacy, still tracked),
  `market_context.resolve_target_dates` (dead code, flagged in the audit),
  `analyze_event`/`analyze_event_multi` share ~80 duplicated lines.
- `ts_utc` is TEXT in both schemas (ISO strings sort, but forecloses
  time-window SQL); `_execute_sql_script`'s line-based splitting breaks the day
  a schema grows a function body.
- The trust-auth paper DB is writable by anyone on the tailnet — fine with
  3 devices today, worth remembering before the tailnet grows.

---

## 4. Proposed improvements (priority order)

1. **Fix `run_daily_calibration.py`'s `is_slot_due` call** (or delete daemon
   mode; `--once` is what prod uses).
2. **Add the SETTLE/CLOSE partial unique index** — one migration line,
   permanent correctness.
3. **Ledger efficiency pass**: filtered `read_state`, one connection per tick,
   view-based settle sweep. No behavior change.
4. **Ship the timezone audit's step 1** (rename/re-anchor the CLOB lead column)
   before more backtest data accumulates on the conflated column; then step 2
   (gates/ledger to station-local) with the audit's `lead_anchor` cutover marker.
5. **Rethink the active experiment**: pass strategy sizing through (or drop the
   collapsed dist_arb variants), add exit hysteresis, account for spread cost.
6. **Prune the grid**: es is gone and whu is a real improvement, but
   `dist_arb`/`topk_yes`/`edge_band` remain deeply red across all models a
   month after MODEL_DIAGNOSTICS, while `book_arb`/`tail_fade` barely trade.
   Retire consistently-negative families (stop opening, keep settling).
7. **CI**: a GitHub Action running ruff + the non-DB test subset costs nothing
   and would have caught §3.1 instantly.

---

## 5. Method notes

- No pytest run (standing rule: prod DB URLs in env; `tests/conftest.py` does
  guard via `assert_test_database_url`, but the rule stands).
- DB access was read-only (`paper status`); one Tailscale ping + port probe.
- Knowledge graph rebuilt with graphify: `graphify-out/graph.json` /
  `graph.html` / `GRAPH_REPORT.md`; query with `graphify query "<question>"`.
