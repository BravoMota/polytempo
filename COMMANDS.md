# Commands (copy-paste)

Developer cheat sheet for PolyTempo CLI entrypoints. For product overview see [README.md](README.md).

## New terminal

```bash
cd /path/to/PolyTempo
source .venv/bin/activate
set -a && source .env && set +a
```

## One-time setup

```bash
cd /path/to/PolyTempo
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
set -a && source .env && set +a

```

## demo

Run the analysis against hardcoded fake inputs (no APIs, no files). Prints the per-bucket table to stdout. Good for smoke-testing the analysis layer in isolation.

```bash
polytempo demo
```

## live

Unified entrypoint: fetch a Polymarket event + Open-Meteo forecast, run **all active profiles** from `config/paper_profiles.yaml` (Postgres paper store), optionally open paper trades, and write one Markdown report per target day.

### Step-by-step (what happens on `polytempo live`)

1. **Resolve station** from `--city` (default `london`) → `Station` (EGLC for London) via `weather/stations.py`.
2. **Resolve `--mode`** — `preview` (model only) or `trade` (also open paper trades). If TTY and flag omitted, prompts `Open paper trades? [y/N]`. Non-TTY default: `preview`.
3. **Resolve `--day`** — `today` (T+0), `tomorrow` (T+1), or `both`. If TTY and trade mode chosen, prompts `1=today  2=tomorrow  3=both`. Non-TTY default: `tomorrow`. `--days-ahead N` overrides this: it targets a single day = `today + N` and skips the prompt.
4. **For each target day** (loop runs once or twice):
  1. **Event lookup.** Explicit `--event-id` calls `fetch_event(id)` (warns if `settlement_date != target_day`). Otherwise scans Polymarket weather events filtered by `--city` + `end_on_date=target_day` and picks the first parseable Celsius-bucket event. Aborts the day if nothing matches. The event is then run through `hydrate_prices`, which replaces each bucket's `yes_bid`/`yes_ask`/`spread`/`liquidity_usd` with the live **CLOB** order book (`POST /books`); Gamma's cached price fields are discovery-only and are never used for edge.
  2. **Forecast fetch.** `fetch_for_station(station, target_day)` calls Open-Meteo across the live model set and normalizes to `ForecastValues`.
  3. **Lead-hours check.** `lead_hours = hours from now to UTC midnight at the END of the target day` (i.e. UTC midnight of the day after `target_date`). This matches the offline anchor in step 6 / `compute_lead_hours`, so live lead values index `calibration_stats.csv` row-for-row. If `< 6`, prints a stderr warning (forecast value drops, edges sharpen near settle). No hard block.
  4. **Distribution build** per `--model-strategy`:

    | Strategy                    | mean                                                                             | sigma                                                                      |
    | --------------------------- | -------------------------------------------------------------------------------- | -------------------------------------------------------------------------- |
    | `best_historical` (default) | selected model's prediction `- bias_c`                                           | selected model's `error_std_c`, falling back to `rmse_c`                   |
    | `best_historical_updated`   | same as `best_historical`, but reads the nightly `calibration_stats_updated.csv` | same                                                                       |
    | `weighted_historical_updated` | precision-weighted blend of eligible models: `w_i ∝ (1/error_std_c²)^2`; `μ = Σ w_i(pred_i − bias_i)` | damped mixture variance: `σ² = within + 0.2 × between`, where `within = Σ w_i σ_i²` and `between = Σ w_i(μ_i − μ)²`; `error_std_c` only (no `rmse_c` fallback); no `ensemble_spread` fallback on failure |
    | `weighted_historical_updated_sharp` | same as WHU but sharper: `w_i ∝ (1/error_std_c²)^2.8` | `σ² = within` only (`disagreement_weight=0`); profile abbrev `whus` |
    | `ensemble_spread`           | mean across live models                                                          | spread across live models, combined in quadrature with the lead-time floor |

     `best_historical` reads `data/weather/statistical/calibration_stats.csv` (produced by step 6 below) and, **per available live model**, picks the row whose `lead_hours` is the smallest value `>=` the current live lead time. Live and calibration `lead_hours` share the same end-of-target-date UTC anchor, so the ceiling lookup is exact. It then chooses the model with the lowest valid `error_std_c` (falling back to `rmse_c` when std is missing/zero/non-finite) and `n_samples > 0`. If the CSV is missing, no model has a qualifying ceiling row, the live forecast lost model identity, or `station_id`/`lead_hours` are unknown, the command silently falls back to `ensemble_spread` and reports the reason via `fallback_reason` (`selected_model`, `sigma_source`, `calibration_row`, `fallback_reason` appear in the report).
  5. **Per-bucket probabilities** — each bucket label → `TemperatureBucket` via `parse_temperature_bucket`; `probabilities_for_buckets` integrates `Normal(mean, sigma)` over each half-open interval.
  6. **Per-profile decisions** — `run_profiles` produces one `AnalysisResult` per active profile from `config/paper_profiles.yaml`. Each profile combines its **own** model strategy × trade strategy (the `--model-strategy` flag only controls the report's Distribution section, not the profiles). Lead-time entry gates are **not** enforced by `live` (`enforce_gate=False`) — only the always-on bot enforces them.
  7. **Mode branch:**
    - `**preview`**: analysis only — no trades opened. (Resolved events are still settled against the Postgres store.)
    - `**trade**`: per-profile **dedupe** — a profile that already has an open trade on `event_id` returns `DEDUPED_OPEN_TRADES_EXIST`; otherwise OPEN rows are written to the paper Postgres database (`POLYTEMPO_PAPER_DATABASE_URL`).
  8. **Markdown report** written to `reports/live/live_<UTC>.md` with sections: Inputs, Event, Forecast, Distribution, Run outcome (per-profile result tables + opened/settled trades).
  9. **Stdout summary** — per-profile actions; in trade mode, per-profile balances after writes.

### Flags


| Flag                          | Meaning                                                                                                                                                              |
| ----------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `--mode {preview,trade}`      | Preview = model only. Trade = also opens paper trades. Prompted on TTY when omitted.                                                                                 |
| `--day {today,tomorrow,both}` | Target day(s). Prompted on TTY when `--mode trade`. Default `tomorrow`.                                                                                              |
| `--days-ahead N`              | Target a single day = today + N (e.g. `0`=today, `3`=T+3). Overrides `--day` and its prompt.                                                                         |
| `--event-id`                  | Pin a specific Gamma event id (one day only; not compatible with `--day both`).                                                                                      |
| `--city`                      | Contract station registry key (default `london`).                                                                                                                    |
| `--limit`                     | Max events to scan when `--event-id` is not set (default `20`).                                                                                                      |
| `--model-strategy`            | `best_historical` (default), `best_historical_updated`, `weighted_historical_updated`, or `ensemble_spread`. Affects the report's Distribution section only; profiles use their own model strategy. |


### Examples

```bash
# Interactive — prompts mode and day
polytempo live

# Cron / non-interactive — preview both days
polytempo live --mode preview --day both --city london

# Trade tomorrow with ensemble distribution (override default)
polytempo live --mode trade --day tomorrow --model-strategy ensemble_spread

# Target a specific horizon (T+3) for a single day
polytempo live --mode preview --days-ahead 3 --city london
```

`live` is the canonical entrypoint. `paper open` below remains as a thin per-event wrapper for scripts that already use it.

## paper (PostgreSQL profiles)

Paper trading uses **756 hold trading profiles** (3 model strategies × 14 trade strategies × 9 lead-time gates × {legacy, budget_normalize_wallet_percent}) defined in `config/paper_profiles.yaml`, plus xsell/active experiment wallets. State lives in a **separate Postgres database** (`polytempo_paper`), not JSONL files. Profile ids are `{bh|bhu|whu}_{trade}_{leadN}` for legacy bankroll sizing, or the same id with a `_bnwp` suffix for **budget_normalize_wallet_percent** wallets that renormalize implied stakes onto `event_budget_fraction` of current balance (default 10%) per event — legs under $0.50 are skipped and legs in [$0.50, $1) are floored to $1 (e.g. `bh_dist_arb_lead30`, `whu_mid_band_lead24_bnwp`). Each profile keeps an independent $1000 bankroll. Trade-strategy names in the YAML are validated against `profiles/registry.py` at load time. Overnight / research backtests use a separate file — `config/backtest_profiles.yaml` — so expanding the research grid does not create new live wallets.

### Database setup (one-time)

```sql
CREATE DATABASE polytempo_paper;
CREATE DATABASE polytempo_paper_test;  -- pytest only
```

```bash
export POLYTEMPO_PAPER_DATABASE_URL='postgresql://jnlow@100.74.116.100:5432/polytempo_paper'
export POLYTEMPO_PAPER_TEST_DATABASE_URL='postgresql://jnlow@HOST:5432/polytempo_paper_test'

python scripts/init_paper_db.py
python scripts/init_paper_db.py --database-url "$POLYTEMPO_PAPER_TEST_DATABASE_URL"
```

External programs can query views: `paper_open_positions`, `paper_profile_balances`, `paper_recent_ticks`.

### run_paper_bot (always-on)

Schedule-driven bot: wakes at the exact UTC instant when lead hours equals each profile's target (±90s), settles resolved events every 15 minutes.

`--once` is a **dry-run preview** only: shows model decisions for **today, tomorrow, and day after** in per-date tables. No Postgres writes, no gate enforcement — use the continuous bot for real paper trades.

```bash
python scripts/run_paper_bot.py
python scripts/run_paper_bot.py --once   # preview snapshot (no DB)
python scripts/run_paper_bot.py --config config/paper_profiles.yaml
```

Continuous mode requires `POLYTEMPO_PAPER_DATABASE_URL`. `--once` does not need the paper DB.

**BHU/WHU calibration guard:** profiles using `best_historical_updated` or `weighted_historical_updated` are disabled when `data/weather/statistical/calibration_stats_updated.csv` is missing, empty, or older than 48 hours; BH profiles keep running. If a `best_historical` or `best_historical_updated` profile would fall back to `ensemble_spread` at entry time (e.g. no qualifying calibration row), the bot logs a warning, records a `GATE_SKIP` with reason `model_strategy_fallback`, and does **not** open a trade. `weighted_historical_updated` does **not** fall back to `ensemble_spread`; on failure it skips with the requested strategy preserved. OPEN/TICK ledger rows store the **resolved** `model_strategy` plus audit metadata (`requested_model_strategy`, `fallback_reason`, `selected_model`, `distribution_params`, `weighted_contributions`, `calibration_selection` with the chosen CSV row + Open-Meteo `predicted_tmax_c`, `distribution_mean_c` / `distribution_sigma_c`, …).

**Init-lead (Phase 2):** the bot fetches rolling Open-Meteo metadata + forecast via `fetch_open_meteo_live_bundle`. Entry gates and ledger `lead_hours` stay **wall-clock**; `best_historical` uses **per-model init lead** only for models with rolling `meta.json` (others are excluded from selection — see `[docs/calibration-data.md](docs/calibration-data.md)`). **Restart `run_paper_bot.py` after deploying this change.**

No live orders. The bot also runs an **active sweep at each lead-gate instant** (the same
clock as the hold wallets) for any `active_wallets` profiles — see `paper active-monitor` below.

### paper active-monitor (edge-following wallets, dry-run)

The `active_wallets` block in `config/paper_profiles.yaml` defines 42 `{model}_{trade}_active`
wallets (model × strat, **no lead gate**). At each lead-gate tick (lead hours
12/15/18/24/30/36/42/48/54) the bot re-prices each open leg against a
fresh forecast + live book and **scales in** while the leg edge stays `>= add_edge_pp`
(one ramp ticket per tick, capped at `max_position_fraction` of balance per leg) or **flattens the
leg** when its edge drops below `exit_edge_pp` (taking profit or cutting losses). Entry-side selection
is the base trade strategy's job; once held, management follows that leg's own edge. Re-entry is
allowed if the edge returns. Fills are taker; wide/illiquid quotes are skipped that tick.

`active-monitor` runs **one sweep in dry-run** (no DB writes) and prints the adds/flattens/opens the
controller would make right now:

```bash
polytempo paper active-monitor
polytempo paper active-monitor --config config/paper_profiles.yaml
```

### probe_open_meteo_schedule (API demand study)

Long-running probe: hits the same 8-model Open-Meteo forecast request the paper bot uses, at **UTC :00, :05, and :10** every hour. Logs success/errors to JSONL (`max_retries=1` per probe — raw API behavior, no retry masking).

Run for 24h+ to cover all hours. Compare failure rates at `on_hour` vs `plus_5min` / `plus_10min`.

```bash
python scripts/probe_open_meteo_schedule.py
python scripts/probe_open_meteo_schedule.py --output data/weather/open_meteo_probe.jsonl
python scripts/probe_open_meteo_schedule.py --city london
```

### paper open

Fetch event + forecast for the settlement date, run all active profiles, open trades. Auto-settles if resolved. Per-profile dedupe (not global across profiles).


| Flag         | Meaning                             |
| ------------ | ----------------------------------- |
| `--event-id` | Polymarket Gamma event id           |
| `--date`     | Settlement date `YYYY-MM-DD`        |
| `--city`     | Contract station (default `london`) |
| `--config`   | Path to `paper_profiles.yaml`       |


```bash
polytempo paper open --event-id 509200 --date 2026-05-23 --city london
```

### paper status

Balances for all active profiles from Postgres.

```bash
polytempo paper status
polytempo paper status --config config/paper_profiles.yaml
```

### paper scenarios

Per-event scenario PnL table for every event with open trades. For each possible winning bucket, prints net PnL per strategy plus total. Fetches the live event from Polymarket to read current `yes_ask` (market prob).

Unlikely tails are rolled into one row each (`X°C or lower` / `Y°C or higher`); the rolled row's PnL is the **worst case** across the collapsed buckets. Any bucket with a YES position is always kept individual so jackpot/conviction scenarios stay visible.


| Flag         | Meaning                                                                                                                 |
| ------------ | ----------------------------------------------------------------------------------------------------------------------- |
| `--event-id` | Restrict to one event id. Default: every event with open trades.                                                        |
| `--min-prob` | Rollup threshold on current `yes_ask` (default `0.05`). Buckets below this and with no YES position fold into the tail. |


```bash
polytempo paper scenarios
polytempo paper scenarios --event-id 529428 --min-prob 0.10
```

### paper settle

Settle every open trade on an event against the winning bucket. Use when you want to force-resolve before Polymarket's own resolution flows through.


| Flag         | Meaning                             |
| ------------ | ----------------------------------- |
| `--event-id` | Polymarket event id                 |
| `--winner`   | Winning bucket label, e.g. `"23°C"` |


```bash
polytempo paper settle --event-id 509200 --winner "29°C"
```

### paper list-london

List London weather events; pass `--date` to filter Gamma by settlement day.

```bash
polytempo paper list-london --limit 20 --date 2026-05-23
```

### paper performance report

Daily realized P/L matrix (markdown). Rows = wallets; columns = market settlement dates. Cell = that day's realized P/L as **% of start-of-day balance**. Includes `since` (first OPEN) so new wallets aren't judged on stale cumulative totals from `paper status`.

```bash
deploy/bin/run-with-env.sh scripts/report_performance.py
deploy/bin/run-with-env.sh scripts/report_performance.py --days 14 --top 0          # all profiles
deploy/bin/run-with-env.sh scripts/report_performance.py --group model_trade --top 20
deploy/bin/run-with-env.sh scripts/report_performance.py --min-settled-days 3 --sort balance
deploy/bin/run-with-env.sh scripts/report_performance.py --out reports/performance/latest.md
```

**Full-history CSV** (long format, one row per wallet × settlement date; strategy knobs as columns):

```bash
deploy/bin/run-with-env.sh scripts/report_performance.py --all --csv reports/performance/daily.csv
deploy/bin/run-with-env.sh scripts/report_performance.py --days 14 --csv reports/performance/daily.csv
```

**Viewer** (summary reads CSV; install once: `pip install -e ".[view]"`):

```bash
streamlit run scripts/view_performance.py -- reports/performance/daily.csv
```

The Daily P/L table is driven by `daily.csv` (offline). **Trade detail** (wallet + settlement date) queries `paper_events` on demand when `POLYTEMPO_PAPER_DATABASE_URL` is set — trades, prices, and resolution. **Analysis replay charts** also need `POLYTEMPO_DATABASE_URL` (Open-Meteo + CLOB snapshots). Install viewer deps: `pip install -e ".[view]"` (includes `plotly`). Click a cell on the heatmap to pre-fill drill-down.

On mac0 the viewer runs as LaunchDaemon on **127.0.0.1:8501** (SSH tunnel: `ssh -L 8501:127.0.0.1:8501 jnlow@mac0`). Sidebar **Refresh from DB** re-runs the performance export only; nightly job at **03:30** writes `reports/performance/daily.csv`.

`reports/` is gitignored — CSV and markdown snapshots are local/generated.

| Flag | Meaning |
| ---- | ------- |
| `--days` | Trailing settlement dates (default `7`) |
| `--end` | Last settlement date inclusive `YYYY-MM-DD` (default: today UTC) |
| `--group` | `profile` (default), `trade`, or `model_trade` rollup (markdown only) |
| `--top` | Max rows sorted by `--sort` (default `40`; `0` = all; markdown only) |
| `--min-settled-days` | Drop rows with fewer than N settlement days in the window |
| `--sort` | `7d` (default), `balance`, or `name` (markdown only) |
| `--out` | Write markdown file (default: stdout) |
| `--csv` | Write long-format CSV |
| `--all` | CSV: every settlement date since first trade (ignores `--days` window) |

Active-sell A/B vs hold twins: `deploy/bin/run-with-env.sh scripts/report_xsell.py`.

## backtest (hold-to-settlement simulation)

Replays historical London weather events between `--start` and `--end` and simulates paper trading for each profile at its lead gate — **without writing to `polytempo` or `polytempo_paper`**. It reuses the live decision path end-to-end (`run_profile` for entry gate + one-open-per-event/profile dedupe + `best_historical*` model-strategy fallback skip, `analyze_event` for the model × trade strategy) and the ledger bankroll math (`stake_fraction` 2–5% edge ramp, flat `stake_usd` ticket, `stake_fraction` Kelly override). Only persistence is swapped: an in-memory ledger replaces `PostgresLedgerStore`.

### What happens per event × profile

1. **Discover events** from stored CLOB snapshots (`clob_bucket_snapshots` — `city_slug` + `settlement_date`), so resolved/closed historical events are included. Each event id is then fetched from Gamma for its bucket structure + winning outcome. (Only dates with CLOB snapshots produce trades — London coverage currently starts **2026-06-20**.)
2. **Gate instant** = UTC time when `lead_hours == profile.entry_gate.target_lead_hours` (`gate_target_utc`).
3. **Point-in-time inputs** at that instant (weather DB snapshot reads):
   - Open-Meteo forecast — `fetch_nearest_open_meteo_forecast`
   - CLOB prices — `fetch_nearest_clob_snapshot`
   - Wunderground adjusted Tmax if available — `fetch_nearest_wunderground_adjusted_tmax`
   - **Calibration CSV as-of that date** — the archived `data/weather/statistical/historic/calibration_stats_updated_<UTC>.csv` that was live at the gate instant (not today's file); falls back to the current file when the instant is newer than every archive.
4. **OPEN** via `run_profile` on the pre-settlement (unresolved) event view hydrated with the gate-instant CLOB snapshot — same guards as paper.
5. **SETTLE** hold-to-resolution against the resolved Gamma winning bucket. Bankroll compounds chronologically across events; each profile keeps an independent $1000 start.

Scope v1: **hold-to-settlement only** (no active ADD/FLATTEN). Active (`active_wallets`) profiles are skipped.

### Flags

| Flag                | Meaning                                                                            |
| ------------------- | ---------------------------------------------------------------------------------- |
| `--start`           | First settlement date `YYYY-MM-DD` (required)                                      |
| `--end`             | Last settlement date `YYYY-MM-DD`, inclusive (required)                            |
| `--config`          | Profile YAML (default `config/backtest_profiles.yaml` — research grid, not live paper) |
| `--profiles`        | Restrict to these profile ids (space-separated)                                    |
| `--trade-strategy`  | Restrict to profiles with this trade strategy (e.g. `dist_arb`)                    |
| `--model-strategy`  | Restrict to profiles with this distribution model (`best_historical`, `best_historical_updated`, `weighted_historical_updated`, `weighted_historical_updated_sharp`, `ensemble_spread`, …). Composes with `--trade-strategy` (AND). |
| `--event-budget`    | Lock event_budget strategy: `legacy` or `budget_normalize_wallet_percent` (alias `bnwp`). Omit = config `event_budgets`. |
| `--sizing-mode`     | Deprecated alias for `--event-budget` |
| `--city`            | Contract station registry key (default `london`)                                   |
| `--database-url`    | Weather DB URL override (read-only; defaults to `POLYTEMPO_DATABASE_URL`)          |
| `--no-wunderground` | Skip the Wunderground snapshot forecast in the input reconstruction               |
| `--csv`             | Write per-profile summary CSV                                                       |
| `--daily-csv`       | Write visualizer-compatible daily CSV (one row per profile × settlement date)     |
| `--daily`           | Also print the per-day PnL breakdown                                               |

`--daily-csv` matches `report_performance.py --csv` / the Streamlit viewer schema (`profile_id`, knobs, `settlement_date`, `pnl_usd`, `pnl_pct`, `sod_balance_usd`, `n_trades`). Point the viewer sidebar at that file. Keep it under `reports/backtest/` so **Refresh from DB** does not overwrite `reports/performance/daily.csv`. Summary heatmap works offline; trade-detail / replay still need the paper + weather DBs and will not show backtest fills.

**Config split:** live paper bot reads [`config/paper_profiles.yaml`](config/paper_profiles.yaml) (creates wallets in `polytempo_paper`). Backtest defaults to [`config/backtest_profiles.yaml`](config/backtest_profiles.yaml) (research models/trades welcome; in-memory only — never writes the paper DB). Pass `--config config/paper_profiles.yaml` only when you want to replay the exact live hold grid.

Reads the weather DB (CLOB / Open-Meteo / WU snapshots) and Gamma (winning bucket). Output: per-profile PnL, trade count, win rate, final balance, max drawdown, plus a summed "no-open reasons" list so empty runs are diagnosable. Running all ~330 profiles over a wide window is slow (many per-gate DB reads); filter with `--profiles` / `--trade-strategy` for quick iterations.

```bash
export POLYTEMPO_DATABASE_URL='postgresql://…/polytempo'   # weather DB (read-only)

# one profile, quick sanity window (fast)
python scripts/backtest.py --start 2026-06-25 --end 2026-06-30 \
  --profiles bh_dist_arb_lead24

# one trade strategy across the settled window + per-day breakdown, dump CSV
python scripts/backtest.py --start 2026-06-20 --end 2026-07-10 \
  --trade-strategy dist_arb --daily --csv reports/backtest/dist_arb.csv

# summary + visualizer daily CSV (open daily file in Streamlit sidebar)
python scripts/backtest.py --start 2026-06-20 --end 2026-07-10 \
  --trade-strategy dist_arb \
  --csv reports/backtest/dist_arb_summary.csv \
  --daily-csv reports/backtest/dist_arb_daily.csv

# lock ONLY the distribution model (every trade strategy on best_historical_updated)
python scripts/backtest.py --start 2026-06-20 --end 2026-07-10 \
  --model-strategy best_historical_updated

# lock BOTH the model and the trade strategy (one exact cell of the matrix)
python scripts/backtest.py --start 2026-06-20 --end 2026-07-10 \
  --model-strategy weighted_historical_updated --trade-strategy dist_arb

# budget_normalize_wallet_percent only for one trade strategy
python scripts/backtest.py --start 2026-06-20 --end 2026-07-10 \
  --trade-strategy dist_arb --event-budget budget_normalize_wallet_percent

# legacy bankroll sizing only
python scripts/backtest.py --start 2026-06-20 --end 2026-07-10 \
  --trade-strategy dist_arb --event-budget legacy

# all hold profiles (slow — full profile matrix)
python scripts/backtest.py --start 2026-06-20 --end 2026-07-10
```

Tests (in-memory, no DB / no network):

```bash
pytest tests/test_backtest.py
```

## fetch-historical-forecasts

Fetch Open-Meteo Single Runs and cache **full API JSON** under `data/weather/raw/single-runs/`. In date-range mode, also append parsed Tmax rows to JSONL. Offline only — not used by `polytempo live`.

**Raw filename convention** (run time is not in the API body):

`{station_id}_{model}_{run_time_utc}.json`

Example: `EGLC_ukmo_uk_deterministic_2km_2026-05-01T120000Z.json`

- UTC init encoded as `YYYY-MM-DDTHHMMSSZ` (no colons or spaces)
- Station and model tokens are sanitized for macOS / Linux / Windows

**Modes:**

1. **Explicit runs** — repeat `--run-time` (raw JSON only; no `--start-date` / `--end-date`)
2. **Date range** — `--start-date` + `--end-date` (raw JSON per unique run + parsed JSONL at `--out`)


| Flag                   | Meaning                                                                                    |
| ---------------------- | ------------------------------------------------------------------------------------------ |
| `--station-id`         | Contract station id (e.g. `EGLC`)                                                          |
| `--latitude`           | Station latitude                                                                           |
| `--longitude`          | Station longitude                                                                          |
| `--model`              | Open-Meteo model id (e.g. `ukmo_seamless`)                                                 |
| `--run-time`           | UTC model init (repeatable). Raw-only mode when set.                                       |
| `--run-time-start`     | First UTC init for a run range (with `--run-interval-hours`)                               |
| `--run-time-end`       | Last UTC init for a run range (defaults to `--run-time-start`)                             |
| `--run-interval-hours` | Hours between inits in a run range (e.g. `6` for 00/06/12/18Z)                             |
| `--forecast-days`      | Single-Runs `forecast_days` horizon (default `7`)                                          |
| `--start-date`         | First target calendar day (`YYYY-MM-DD`; required without raw run mode)                    |
| `--end-date`           | Last target calendar day (`YYYY-MM-DD`, inclusive)                                         |
| `--max-lead-hours`     | Maximum lead time before target day (default `72`)                                         |
| `--lead-step-hours`    | Lead-time step in hours (default `6`)                                                      |
| `--timezone`           | IANA timezone for target-day midnight anchor (default `UTC`; use `Europe/London` for EGLC) |
| `--raw-dir`            | Raw Single-Runs JSON directory (default `data/weather/raw/single-runs/`)                   |
| `--out`                | Parsed JSONL path (default `data/weather/historical_forecasts.jsonl`)                      |
| `--base-url`           | Single-Runs API base (default `https://single-runs-api.open-meteo.com/v1/forecast`)        |
| `--dry-run`            | Print planned request count; do not call API                                               |


Each API call is `GET {base-url}` with query params built by `build_single_run_request_params()`:
`latitude`, `longitude`, `daily=temperature_2m_max`, `temperature_unit=celsius`, `models`, `timezone`, `run` (UTC init, e.g. `2026-03-29T00:00`). No `start_date`/`end_date`. Client timeout **120s** (archive is slow). See `http/open_meteo_single_runs.http`.

Dry-run (30 days × 12 leads ≈ 360 requests):

```bash
polytempo fetch-historical-forecasts \
  --station-id EGLC \
  --latitude 51.5053 \
  --longitude 0.0553 \
  --model ukmo_seamless \
  --start-date 2026-04-01 \
  --end-date 2026-04-30 \
  --timezone Europe/London \
  --dry-run
```

One explicit run (single raw JSON):

```bash
polytempo fetch-historical-forecasts \
  --station-id EGLC \
  --latitude 51.5053 \
  --longitude 0.0553 \
  --model ukmo_uk_deterministic_2km \
  --timezone Europe/London \
  --run-time 2026-05-01T12:00:00Z \
  --out data/weather/historical_forecasts.jsonl
```

## compute-calibration-stats

Join forecast JSONL with observation JSONL and write RMSE / MAE / bias by station, model, and lead bucket.


| Flag             | Meaning                         |
| ---------------- | ------------------------------- |
| `--forecasts`    | Historical forecast JSONL input |
| `--observations` | Observed Tmax JSONL input       |
| `--out`          | Output calibration stats JSON   |


```bash
polytempo compute-calibration-stats \
  --forecasts data/weather/historical_forecasts.jsonl \
  --observations data/weather/observed_tmax.jsonl \
  --out data/weather/calibration_stats.json
```

## Tests

All tests:

```bash
pytest
```

Calibration-related tests only:

```bash
pytest tests/test_historical_forecasts.py tests/test_http_open_meteo.py \
  tests/test_observations.py tests/test_calibration_dataset.py
```

## Weather collection (PostgreSQL)

Continuous local scraping of Wunderground HTML pages into PostgreSQL + raw files. Config: `[config/weather_collectors.yaml](config/weather_collectors.yaml)`. Raw HTML: `data/weather/raw/wunderground/`.

**Contract stations collected:** London (EGLC + ILONDO288 PWS), Madrid (LEMD), Milan (LIMC) — WU obs/forecast, Open-Meteo, and Polymarket CLOB per station. Nightly calibration (`config/calibration.yaml`) remains **EGLC-only** until expanded.

Set the database URL before init, migrate, or run:

```bash
export POLYTEMPO_DATABASE_URL='postgresql://USER:PASSWORD@HOST:5432/polytempo_weather'
```

See `[.env.example](.env.example)` for the expected format.

Each poll per station fetches:

- live observation page (ICAO or PWS dashboard URL)
- hourly forecast page for **today** and **tomorrow** (station local dates)

Parsed rows land in `observation_snapshots` / `forecast_snapshots`; raw HTML + `.meta.json` sidecars are saved under `raw/wunderground/`. `collector_state` tracks success/error per station. Each snapshot stores native API temperatures: `temp_f` (°F from embedded HTML, `units=e`) and `temp_c` (°C from Weather.com API, `units=m`). `raw_temp_text` duplicates `temp_f` as a string (deprecated).

### Open-Meteo collector (`open_meteo` block in YAML)

Forecast-only collector: pairs rolling S3 `meta.json` (per model run init) with the live Forecast API, stores **parsed** rows (no raw JSON):


| Table                             | Contents                                                                      |
| --------------------------------- | ----------------------------------------------------------------------------- |
| `open_meteo_fetch_cycles`         | One row per station poll (`fetched_at_utc`, staleness flag)                   |
| `open_meteo_model_meta_snapshots` | Per-model init / availability / lag                                           |
| `open_meteo_forecast_snapshots`   | Per `(model, target_date)` Tmax + optional `run_init_utc` / `init_lead_hours` + `wall_clock_lead_hours` |


Models list lives in `weather_collectors.yaml` under the `open_meteo` collector (`models:`, `target_horizon_days:`). Collector-level `models:` is the default (London/EGLC); Madrid and Milan override with per-station `models:` (e.g. `meteofrance_arpege_europe` for LEMD, `italia_meteo_arpae_icon_2i` for LIMC). See `[docs/calibration-data.md](docs/calibration-data.md)` for SQL joins vs `calibration_forecast_records`.

Scheduling uses **UTC wall-clock slots** (not sleep-after-work). Per collector in YAML:

```yaml
observations_interval_seconds: 300
observations_anchor_time_utc: "00:00"   # obs at …:00, …:05, …:10 UTC
forecast_interval_seconds: 3600
forecast_anchor_time_utc: "00:00"       # forecast at 00:00, 01:00, … UTC
```

Observations and forecasts run on independent schedules. Legacy `interval_seconds` / `anchor_time_local` still load but log a deprecation warning.

Initialize schema (idempotent):

```bash
python scripts/init_weather_db.py
```

Migrate existing SQLite data (optional, one-time):

```bash
python scripts/migrate_sqlite_to_postgres.py \
  --sqlite data/weather/polytempo_weather.db \
  --init-target
```

Dry-run source counts only:

```bash
python scripts/migrate_sqlite_to_postgres.py --dry-run
```

Run collectors (continuous; reloads config when YAML mtime changes):

```bash
python scripts/run_collector.py
```

One-shot cycle (no sleep loop):

```bash
python scripts/run_collector.py --once
```

Tests (require `POLYTEMPO_TEST_DATABASE_URL` pointing at a database whose name contains `test`; storage/collector DB tests skip otherwise):

```bash
POLYTEMPO_TEST_DATABASE_URL='postgresql://USER:PASS@host:5432/polytempo_test' \
  python scripts/init_weather_db.py --database-url "$POLYTEMPO_TEST_DATABASE_URL"

POLYTEMPO_TEST_DATABASE_URL='postgresql://USER:PASS@host:5432/polytempo_test' \
  pytest tests/test_storage_postgres.py tests/test_calibration_storage.py \
  tests/test_collector_config.py tests/test_collector_schedule.py \
  tests/test_collectors_wunderground.py tests/test_open_meteo_meta.py \
  tests/test_open_meteo_collector.py tests/test_postgres_safety.py
```

### Open-Meteo collector — post-deploy runbook

1. `export POLYTEMPO_DATABASE_URL='postgresql://…/polytempo'` (weather DB, not paper).
2. `python scripts/init_weather_db.py` — creates `open_meteo_*` tables (idempotent).
3. Enable `open_meteo` in `config/weather_collectors.yaml` (`enabled: true`, `models:`, `stations:`).
4. Smoke: `python scripts/run_collector.py --once` (runs all enabled collectors).
5. Verify:

```bash
psql "$POLYTEMPO_DATABASE_URL" -c "\dt open_meteo*"
psql "$POLYTEMPO_DATABASE_URL" -c "SELECT COUNT(*) FROM open_meteo_fetch_cycles;"
```

1. Production loop: `python scripts/run_collector.py`

To debug open_meteo only, temporarily set `wunderground.enabled: false` in YAML.

## Production supervision (mac0)

Six jobs on mac0 run as user `jnlow` from `/Users/jnlow/projects/PolyTempo`, supervised by LaunchDaemons. Full runbook: [docs/mac0-setup.md](docs/mac0-setup.md).


| Label                       | Schedule                        |
| --------------------------- | ------------------------------- |
| `com.polytempo.collector`   | long-lived (internal UTC slots) |
| `com.polytempo.paper-bot`   | long-lived                      |
| `com.polytempo.calibration` | **01:00 mac0 local** (`--once`) |
| `com.polytempo.db-backup`   | **02:00 mac0 local** (`--once`) |
| `com.polytempo.performance-export` | **03:30 mac0 local** (`--all --csv`) |
| `com.polytempo.performance-viewer` | long-lived **127.0.0.1:8501** |


```bash
# one-time install (on mac0, as admin):
sudo deploy/bin/install-launchd.sh

# day-to-day (as jnlow, optional NOPASSWD sudo — see deploy/sudoers.d/polytempo.example):
sudo deploy/bin/polytempo-service status all
sudo deploy/bin/polytempo-service restart collector
sudo deploy/bin/polytempo-service restart performance-viewer
sudo deploy/bin/polytempo-service run calibration
sudo deploy/bin/polytempo-service run db-backup
sudo deploy/bin/polytempo-service run performance-export
```

### polytempo-health (production bundle)

Collect launchd status, log tails, and DB freshness into a raw evidence dump for LLM review:

```bash
deploy/bin/polytempo-health.sh
deploy/bin/polytempo-health.sh /tmp/health.md   # custom output path
deploy/bin/polytempo-health.sh --no-db            # skip SQL when DB unreachable
```

Default output: `reports/health/health_<UTC>.md`. Each file embeds an **LLM review prompt** at the top — attach the file to an agent and say *“follow the LLM review prompt in the attached file.”*

Optional env tunables (see `.env.example`): `POLYTEMPO_HEALTH_ERR_LINES` (default 80), `POLYTEMPO_HEALTH_OUT_LINES` (default 30).

One-time migration if old live reports sit at `reports/` root:

```bash
mkdir -p reports/live reports/health
mv reports/live_*.md reports/live/ 2>/dev/null || true
```

Smoke before install uses `deploy/bin/run-with-env.sh` (sources `.env`, prepends Homebrew to PATH for `pg_dump`).

## Automated calibration (updated store)

Nightly automation for `best_historical_updated`. Does **not** modify frozen `scripts/Calibrator_V1/` or `data/weather/statistical/calibration_stats.csv`.


| Script                                   | When                                        | Purpose                                                                                                                                                                     |
| ---------------------------------------- | ------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `scripts/bootstrap_calibration_store.py` | Once on prod                                | Fetch WU daily highs (metric hourly max °C) from `2026-02-01` … yesterday; bulk-fetch Single Runs; upsert `calibration_`* Postgres tables; write `calibration_stats_updated.csv` |
| `scripts/run_daily_calibration.py`       | mac0 **01:00 local** via launchd (`--once`) | Incremental observations + new run inits since last success; archives prior `calibration_stats_updated.csv` to `statistical/historic/`; full recompute from DB → `calibration_stats_updated.csv` |
| `scripts/backup_databases.py`            | mac0 **02:00 local** via launchd (`--once`) | `pg_dump -Fc` all four Postgres DBs → `backups/`; 14-day retention ([docs/database-backups.md](docs/database-backups.md))                                                   |


Config: `config/calibration.yaml` (stations from `config/weather_collectors.yaml`, models + cadence baked in — no `capabilities_csv` at runtime).

```bash
# one-time bootstrap (uses POLYTEMPO_DATABASE_URL on prod):
python scripts/bootstrap_calibration_store.py

# re-run forecasts + stats only (observations already in DB):
python scripts/bootstrap_calibration_store.py --no-obs

# nightly cron:
python scripts/run_daily_calibration.py --once

# optional daemon loop (same 02:00 UTC anchor):
python scripts/run_daily_calibration.py

# database backups (see docs/database-backups.md):
python scripts/backup_databases.py
python scripts/backup_databases.py --once
```

Model strategies:


| Strategy                  | Calibration CSV                                                    |
| ------------------------- | ------------------------------------------------------------------ |
| `best_historical`         | `data/weather/statistical/calibration_stats.csv` (frozen, in git)  |
| `best_historical_updated` | `data/weather/statistical/calibration_stats_updated.csv` (nightly) |
| `weighted_historical_updated` | `data/weather/statistical/calibration_stats_updated.csv` (nightly) |
| `weighted_historical_updated_sharp` | `data/weather/statistical/calibration_stats_updated.csv` (nightly) |
| `ensemble_spread`         | *(none)*                                                           |


```bash
polytempo live --model-strategy best_historical_updated ...
```

Bootstrap stderr summary fields:


| Field                    | Meaning                                                                                    |
| ------------------------ | ------------------------------------------------------------------------------------------ |
| `observations_ingested`  | Days upserted into `calibration_observed_tmax` this run                                    |
| `fetched_raw`            | New Single-Runs JSON files downloaded (0 if cache complete)                                |
| `forecast_rows_ingested` | Predicted Tmax rows upserted into `calibration_forecast_records`                           |
| `joined_rows`            | Inner-join pairs `(forecast, observation)` with `error_c` — **not** API failures           |
| `unjoined_forecasts`     | Forecast rows whose `target_date` has no observation (often future dates beyond yesterday) |
| `stat_groups`            | Aggregated CSV rows: one per `(station_id, model, lead_hours)`                             |


## Offline calibration pipeline (standalone scripts)

Numbered scripts under `scripts/` run in order. All use global vars at the top (no CLI flags). Data lives under `data/weather/` (see [docs/calibration-data.md](docs/calibration-data.md)).


| Step | Script                                             | Purpose                                                                                                 |
| ---- | -------------------------------------------------- | ------------------------------------------------------------------------------------------------------- |
| 1    | `scripts/1_analyze_single_runs_models.py`          | Probe model capabilities → `single_runs_model_capabilities.csv` + `raw_capabilities/`                   |
| 2    | `scripts/2_fetch_historical_forecasts_by_model.py` | Bulk-fetch raw Single Runs JSON → `raw/single-runs/`                                                    |
| 3    | `scripts/3_fetch_wunderground_observations.py`     | Fetch observed Tmax → `observed_tmax.jsonl`                                                             |
| 4    | `scripts/4_build_forecast_records_csv.py`          | Raw JSON → `processed/forecast_records.csv`                                                             |
| 5    | `scripts/5_build_observed_tmax_csv.py`             | `observed_tmax.jsonl` → `observed_tmax.csv`                                                             |
| 6    | `scripts/6_compute_calibration_errors.py`          | Join forecasts + observations → `statistical/forecast_errors.csv` + `statistical/calibration_stats.csv` |


### 1 — Single Runs model capability probe

Probes each model in its `MODELS` list against the Single Runs API and writes one CSV row per model to `data/weather/single_runs_model_capabilities.csv`. Every **successful** API payload is written under `data/weather/raw_capabilities/`.

Per model (3–6 API calls):

- **Baseline 00Z** (`forecast_days=16`): response grid lat/lon, distance from configured coordinates, `daily_non_null_at_00z`.
- **12:00 / 18:00 UTC init**: `daily_non_null_at_12z` and `daily_non_null_at_18z`. Model is unavailable only if **both are 0**.
- **Run-init cadence ladder** (`01 → 03 → 06 → 12 UTC`): first hour with non-null daily Tmax → `run_init_interval_hours`.

```bash
python scripts/1_analyze_single_runs_models.py
```

Expect a few minutes; archive calls can take 30–120s each. Requires network.

### 2 — Bulk Single Runs raw fetch by model

Reads `data/weather/single_runs_model_capabilities.csv` and, for each model with `status=ok`, steps run inits from `RUN_TIME_START_UTC` to `RUN_TIME_END_UTC` using that model's `run_init_interval_hours`. `forecast_days` per request is `max(daily_non_null_at_12z, daily_non_null_at_18z)`.

Raw JSON responses land in `data/weather/raw/single-runs/` using `{station}_{model}_{run_time_utc}.json`. Existing files are skipped (resumable).

```bash
python scripts/2_fetch_historical_forecasts_by_model.py
```

### 3 — Wunderground observations fetch

Fetches observed daily Tmax from Wunderground for one station/date range and writes `data/weather/observed_tmax.jsonl`.

- Calls `api.weather.com/v1/location/{ICAO}:9:{COUNTRY}/observations/historical.json` (`units=m`); daily high = `max(observations[*].temp)`.
- Country code auto-resolved via `v3/location/point?icaoCode={ICAO}`, fallback `GB`.

```bash
python scripts/3_fetch_wunderground_observations.py
```

### 4 — Processed forecast records CSV

Reads `data/weather/raw/single-runs/*.json` and writes one CSV row per non-null daily Tmax to `data/weather/processed/forecast_records.csv`.

- Columns: `station_id`, `model`, `run_time_utc`, `target_date`, `lead_hours`, `predicted_tmax_c`, `forecast_lat`, `forecast_lon`.
- `lead_hours` = hours from `run_time_utc` to end of `target_date` in UTC. Example: run `2026-04-01T12:00Z`, target `2026-04-02` → `36`.

```bash
python scripts/4_build_forecast_records_csv.py
```

### 5 — Observed Tmax CSV

Converts `data/weather/observed_tmax.jsonl` → `data/weather/observed_tmax.csv`.

- Columns: `station_id`, `target_date`, `observed_tmax_c`, `source`

```bash
python scripts/5_build_observed_tmax_csv.py
```

### 6 — Calibration errors + stats

Inner-joins `data/weather/processed/forecast_records.csv` with `data/weather/observed_tmax.csv` on `(station_id, target_date)` and writes:

- `data/weather/statistical/forecast_errors.csv` — one row per joined prediction with `error_c`, `abs_error_c`, `squared_error_c`.
- `data/weather/statistical/calibration_stats.csv` — grouped by `(station_id, model, lead_hours)` with `n_samples`, `bias_c`, `mae_c`, `rmse_c`, `error_std_c`.

Notes:

- Groups use **exact `lead_hours`** (no lead buckets) to preserve native model cadence (e.g. 3h for `ukmo_uk_deterministic_2km`).
- Forecasts with no matching observation are silently skipped; rows with non-finite numerics are dropped on load.
- `error_std_c` is the **sample standard deviation** of `error_c` (not the "standard error" `sd / sqrt(n)`). One-sample groups report `error_std_c = 0.0`.

```bash
python scripts/6_compute_calibration_errors.py
```

## HTTP scratch files

Manual [REST Client `.http` requests](http/) under `http/`:

- `http/open_meteo_single_runs.http` — minimal Single Runs example (current fetch CLI shape)
- `http/open_meteo_calibration_dataset_exploration.http` — **API capability probes** from `docs/research/single-run-api_study.md` (Single Runs, Previous Runs, metadata, anti-patterns)
- `http/open_meteo_forecast.http` — current/live forecast smoke test (`api.open-meteo.com`)
- `http/polymarket_gamma.http` — Gamma event payload inspection + CLOB `POST /books` live order-book inspection

These files are **not** part of the Python package. Use them to inspect API payload shapes before coding parsers. Do **not** use them in live analysis.



:)