# PolyTempo project audit — 2026-07-06

Audit performed after pulling `f718a5a..950dc3a` (86 files, +7.6k lines: distribution
explorer/visualizer, active edge-following wallets, WU-forecast calibration, CLOB collector
fixes). Covers build/test health, linting, and a realized-P/L analysis of the live paper store
(`polytempo_paper`, window Jun 8 → Jul 6 2026, 559 profiles).

## Summary

- **Test suite is red on a fresh checkout**: 5 collection errors abort the run; 7 failures behind them.
- **One real production bug**: `NaN` in audit metadata crashes the Postgres `jsonb` insert.
- **17 trivial ruff errors** (11 auto-fixable).
- **Realized paper P/L is −4.1% ROI**; `ensemble_spread` (+24%) is the only profitable model,
  `weighted_historical_updated` (−43%) the worst. Long lead gates win, short leads and active
  wallets lose.

---

## 1. Test suite — 5 collection errors + 7 failures

`pytest` does not collect cleanly. 595 tests pass once collection is unblocked. Root causes:

### A. Broken by the `scripts/Calibrator_V1/` move (real regression, `12a217e`)

Three tests hardcode script paths that no longer exist (the scripts moved into
`scripts/Calibrator_V1/`). A collection error in any file aborts the whole run, masking everything
else.

| Test | Hardcoded path (broken) |
| --- | --- |
| `tests/test_calibration_errors_csv.py:14` | `scripts/6_compute_calibration_errors.py` |
| `tests/test_forecast_records_csv.py:14` | `scripts/4_build_forecast_records_csv.py` |
| `tests/test_observed_tmax_csv.py:13` | `scripts/5_build_observed_tmax_csv.py` |

Fix: repoint to `scripts/Calibrator_V1/…`.

### B. Stale assertions — `test_http_open_meteo.py` (2 failures)

`build_single_run_request_params` has emitted `forecast_days` since 2026-05-27 (`fbbe868`), but
`test_build_single_run_request_params_includes_run_and_dates` and
`test_open_meteo_single_runs_http_scratch_matches_request_builder` still assert the dict *without*
it — silently failing for ~6 weeks. `COMMANDS.md` line 322 and `http/open_meteo_single_runs.http`
also still document the old param set (doc drift).

### C. Missing `pandas` — visualizer tests (2 collection errors)

`test_visualizer_chart.py` / `test_visualizer_csv_data.py` need the `[view]` extra. Environment,
not a bug — but they should `pytest.importorskip("pandas")` so a base install collects cleanly.

### D. Test-fixture gaps against Postgres (4 failures)

Only surface on a DB that actually carries the schema's FKs:

- `test_fetch_nearest_clob_snapshot`, `…_ignores_future_only_slots`, `…_rejects_stale_slot` —
  raw `INSERT INTO clob_bucket_snapshots` **without seeding `stations` first** →
  `ForeignKeyViolation` on `clob_bucket_snapshots_station_id_fkey`. The open-meteo tests in the same
  file already call `insert_station`. These pass on the author's DB only because
  `CREATE TABLE IF NOT EXISTS` never re-added the FK there — latent for a clean DB.
- `test_fetch_nearest_open_meteo_forecast_marks_no_meta_models_ineligible` asserts a specific model
  order (`["", init]`), but the query has no `ORDER BY model`, so Postgres returns `[init, ""]`.
  Over-specified test / non-deterministic query ordering.

---

## 2. Real production bug — `NaN` in audit metadata crashes the insert 🔴

`src/polytempo/storage/paper_postgres.py:139` → `json.dumps(row.metadata or {})`.

Python's `json.dumps` serializes `float('nan')` as the bare token `NaN`, which Postgres `jsonb`
rejects (`invalid input syntax for type json … Token "NaN" is invalid`). When a
`weighted_historical_updated` / `best_historical_updated` profile can't build a distribution,
`distribution_mean_c` / `distribution_sigma_c` become `NaN`, and `log_gate_skip → insert_paper_event`
**throws instead of recording the skip**. `upsert_bot_state` (line 153) has the identical exposure.
Reproduced by `tests/test_paper_run.py::test_whu_model_strategy_skip_on_failure`.

Fix: recursively replace non-finite floats with `None` before `json.dumps` (or handle via
`default=`); add a regression test with a `NaN` in metadata. Notable because this sits on the
**worst-performing model's** failure path (§4).

---

## 3. Lint — 17 ruff errors, all trivial

11 auto-fixable (`ruff check --fix`): unused imports in `cli/main.py`, `paper/bot.py`,
`calibration_compute.py`, `calibration_wu_forecasts.py`, `calibration_config.py`,
`calibration_runner.py`, `visualizer/replay.py`; unused locals `latitude`/`longitude` in
`weather/open_meteo.py:413-414`; empty f-string in `bot_log.py:106`; a few `E402` in
`scripts/Calibrator_V1/`.

---

## 4. Results analysis — paper trading, Jun 8 → Jul 6 (559 profiles)

**Measurement note.** `paper_profile_balances.balance_usd` is **cash-only** — it deducts open stake
but does not mark open positions to value. ~$650k is locked across 2,352 open positions, so raw
balance shows a misleading −$68k. The honest metric is **realized P/L on closed trades** (terminal
`SETTLE`/`CLOSE` payout − `OPEN` stake, joined on `trade_id`):

> **Overall realized: −$24,994 on $607k staked = −4.12% ROI.**

### By model family — the calibration thesis is not holding up

| Model | Realized ROI | Staked |
| --- | --- | --- |
| `ensemble_spread` (es) | **+24.1%** | $105k |
| `best_historical` (bh) | −1.5% | $241k |
| `best_historical_updated` (bhu) | −10.5% | $201k |
| `weighted_historical_updated` (whu) | **−42.8%** 🔴 | $60k |

The naive **live ensemble spread is the only profitable model**. The most sophisticated one —
`weighted_historical_updated`, the newest — is the *worst*. Consistent with the prior finding that
the loss is **μ/bias from stale calibration**, not σ: WHU precision-weights the models, so a stale
bias is amplified rather than averaged out. It is also the model whose skip path hits the NaN crash
in §2.

### By lead gate — enter early, not late

| Gate | ROI | | Gate | ROI |
| --- | --- | --- | --- | --- |
| lead42 | **+17.2%** | | lead18 | −7.2% |
| lead30 | **+15.7%** | | lead15 | −23.0% |
| lead36 | +0.9% | | **active** | **−33.7%** |
| lead24 | −5.0% | | lead12 | **−36.5%** |

Entering close to settlement is systematically destructive. The **active edge-following wallets**
(newest feature, `afe8eda`) are the 2nd-worst cohort at −33.7%. Individual `*_max_roi_active` and
`*_topk_yes_lead12` wallets sit near **−95% ROI** (all but wiped out).

### By trade strategy

`max_edge`, `edge_band`, `argmax_yes` are robust winners. `dist_arb` / `dist_arb_tight` lose
−17 to −19% *on calibration models* but are ~breakeven-to-positive on ES (`es_dist_arb_kelly` +21%),
confirming the losses come from **bad calibration distributions**, not the strategy logic.

The winning envelope is clearly **`ensemble_spread` × lead30/42 × max_edge/edge_band**.

---

## Recommendations (prioritized)

1. **Fix the test suite** (unblocks CI): update 3 Calibrator_V1 paths, refresh the 2 `forecast_days`
   assertions + COMMANDS/.http, seed `stations` in the 3 CLOB tests, add `ORDER BY model`,
   `importorskip` pandas. All low-risk.
2. **Fix the NaN→jsonb crash** (`paper_postgres.py`) — small sanitizer + regression test.
3. **Investigate/disable `weighted_historical_updated`** — −42.8% ROI and it owns the crash path.
   The data says the calibration-μ approach is losing to naive ensemble; WHU is the acute case.
4. **Reconsider the active wallets and short-lead gates** — active (−34%) and lead12/15 (−23 to
   −36%) are consistent capital sinks.
5. **Doc drift** — README still says "378 profiles / 3 model strategies", but there are now 4
   families (es added) and 559 profiles; run `ruff --fix` for the cleanups.

---

*Method: realized P/L computed by joining `OPEN.stake_usd` to summed `SETTLE`+`CLOSE.payout_usd` on
`trade_id` over `paper_events`; not from `balance_usd`. Test/lint results from `pytest` and
`ruff check` on the Windows dev checkout with `.venv` (Python 3.14).*
