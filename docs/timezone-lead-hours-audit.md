# Timezone & `lead_hours` correctness audit

**Date:** 2026-06-23
**Scope:** UTC vs. server-local vs. station-local handling across event discovery, gate
scheduling, forecast fetch, calibration, collectors, ledger, and reports — with a focus on
the two competing `lead_hours` implementations.
**Status:** Analysis only. No code changed (per task constraint + `CLAUDE.md` §3).

---

## 0. TL;DR / Executive summary

**The core hypothesis is correct.** The physically meaningful anchor for `lead_hours` is the
**end of the settlement day in the station's IANA timezone**, *not* UTC midnight on a bare date.

This is provable from contract semantics, not just internal docs. The live London contract
(fetched 2026-06-23) reads:

> *"highest temperature recorded at the **London City Airport Station** in degrees Celsius
> **on 23 Jun '26** … the highest temperature recorded for **all times on this day**"* —
> resolution source **Wunderground**.

So the realized quantity (daily Tmax) is defined over the **station-local calendar day**, and
Open-Meteo is already asked to aggregate on that same basis (`timezone=station.timezone`,
`open_meteo.py:372`). The correct "time remaining until the outcome is locked" is therefore
hours-until-**station-local** end of day.

The codebase has **two** lead helpers and uses them inconsistently:

| Helper | Anchor | Used by |
| --- | --- | --- |
| `model/lead_time.py:lead_hours_to_end_of_target_day` + `calibration_compute.py:compute_lead_hours` | **UTC midnight** of `target+1` | gate scheduling, paper ledger, CLI, calibration CSV/DB, Open-Meteo audit, **CLOB collector** |
| `collectors/util.py:lead_hours_to_day_end` | **station-local midnight** of `target+1` | **Wunderground collector only** |

Because every European station sits at a **non-negative** UTC offset (0 / +1 / +2), the UTC
anchor is **always ≥** the true local end-of-day, so **UTC-anchored leads are systematically
too large by the station's current offset**: **+1 h for London in summer (BST), +2 h for Paris
in summer (CEST), 0 h only in winter (GMT/CET-as-UTC… no, CET is +1; only London-winter is 0).**

> **Today (2026-06-23) the bug is live:** London is on BST (+1) and Paris on CEST (+2), so every
> UTC-anchored lead is currently 1 h (London) / 2 h (Paris) longer than the true local lead.

### Is the UTC convention "intentional and correct"?

It is **intentional and internally self-consistent for calibration**, but **physically wrong**
and **not consistent across subsystems**:

- ✅ **Calibration σ selection is safe.** The calibration CSV/DB leads and the live `best_historical`
  init-lead lookup *both* use the UTC anchor with the same `target_date`, so the ceiling-row
  bucket that gets selected is consistent. The σ/bias attached to a bucket is just *labelled*
  with a lead that is 1–2 h too long. **Not a P0.** (Details §4.)
- ⚠️ **Gate timing is biased and station/DST-dependent.** A "30 h" gate actually fires at
  29 h-to-local-end in London-summer and 28 h in Paris-summer. The *same* global profile enters
  at *different true leads* per city and across DST transitions. **P1.** (§5, §6.)
- 🔴 **Collector columns are conflated.** `observation_snapshots.lead_hours_to_day_end` (WU,
  station-local) and `clob_bucket_snapshots.lead_hours_to_day_end` (CLOB, UTC) **share a column
  name but use different anchors**. Any join/backtest across them is silently off by 1–2 h. **P1.** (§7.)
- ⚠️ **Ensemble σ-floor is boundary-sensitive.** `ensemble_spread` feeds the wall-clock UTC lead
  into hard thresholds at 12/24/48/72 h; the 1–2 h inflation can bump a forecast into the next
  σ-floor bucket. **P2.** (§5.)
- ⚠️ **"today"/"tomorrow" and `date.today()` are mis-anchored.** Profile target-day offsets use
  UTC `now.date()`; the CLI uses **server-local** `date.today()`. Neither is station-local. **P2.** (§8.)

### Recommendation (one line)

Adopt **station-local end-of-day** as the single canonical anchor via one helper
`lead_hours(now_utc, target_date, station_timezone)`, migrate the schedulers/collectors first
(cheap, high value), and stage the calibration migration with a backtest re-baseline (or keep
calibration UTC-anchored but rename it so it can never be confused with scheduling lead). Full
plan in §11.

---

## 1. Glossary (used consistently below)

| Term | Definition as used in this report | Where it actually comes from in code |
| --- | --- | --- |
| **`target_date` / settlement day `D`** | The **station-local calendar day** whose daily Tmax the contract resolves on. | Bot/CLI choose it from `now`/`date.today()` offsets + lead scan; matched against Gamma `endDate`'s UTC date. |
| **Gamma `endDate`** | Polymarket event field; for daily temp markets it is **noon UTC on the observation day** (`…T12:00:00Z`). Parsed to a bare **UTC date** in `polymarket.py:_parse_settlement_date` (l.402). | `markets/polymarket.py:88,402-425` |
| **`event.settlement_date`** | `endDate` reduced to a UTC `date`. Because `endDate` is at 12:00Z it equals `D` robustly (tz-insensitive). | `polymarket.py:402` |
| **`target_date_local`** | DB column on snapshot tables. **In the WU + Open-Meteo collectors it is station-local** (`local_today(station.timezone)`). It is *named* `_local` but the CLOB collector derives its date from `local_today` too, so the date is local everywhere — only the **lead anchor** differs. | `wunderground.py:250,495`, `open_meteo.py:562`, `polymarket_clob.py:43` |
| **`lead_hours` (scheduling/wall-clock)** | Hours from `now` to **UTC midnight** of `D+1`. | `model/lead_time.py:13` |
| **`lead_hours_to_day_end` (WU collector)** | Hours from `scraped_at` to **station-local midnight** of `D+1`. | `collectors/util.py:26` |
| **`init_lead_hours`** | Hours from a model **run init** (UTC) to **UTC midnight** of `D+1`. The lead used for `best_historical` calibration-row lookup. | `calibration_compute.py:60`, `open_meteo.py:434` |
| **`wall_clock_lead_hours`** | Hours from `fetched_at` to **UTC midnight** of `D+1`. When the poll actually happened. | `open_meteo.py:422`, `polymarket_clob.py:60` |
| **"today"/"tomorrow"** | Profile `target_day`. Resolved as **UTC** `now.date()` (+0/+1) in the bot; as **server-local** `date.today()` (+0/+1) in the CLI. Neither is `local_today(station.timezone)`. | `market_context.py:64,98`, `cli/main.py:295` |

**Anchor cheat-sheet** (summer 2026, target day `D`):

```
UTC anchor      end(D) = (D+1) 00:00Z
London BST(+1)  end(D) = (D+1) 00:00 local = D 23:00Z   →  lead_UTC = lead_localLondon + 1
Paris  CEST(+2) end(D) = (D+1) 00:00 local = D 22:00Z   →  lead_UTC = lead_localParis  + 2
```

---

## 2. Contract & Open-Meteo semantics (verified, not assumed)

**Polymarket (verified by live Gamma fetch, 2026-06-23):**

- `Highest temperature in London on June 23?` → `endDate = 2026-06-23T12:00:00Z`, resolves on
  **EGLC / London City Airport**, **Wunderground**, "all times on **this day**" (the local
  calendar day).
- `Highest temperature in Paris on June 23?` → `endDate = 2026-06-23T12:00:00Z`, **Paris-Le
  Bourget (LFPB)**.

Two consequences:
1. **The resolution window is the station-local day**, confirming the local anchor is the
   physically correct one.
2. **`endDate` is noon-UTC**, i.e. it is a *nominal label*, **not** a settlement instant (the
   day's max isn't realized until the afternoon, and WU finalizes later). Parsing it to a UTC
   date yields `D` regardless of timezone, which is why discovery/`date_mismatch` currently
   "work" despite the anchor confusion (see §3, §8). **This is luck, not design** — if Polymarket
   ever moved `endDate` to a midnight-adjacent time, `_parse_settlement_date` + the
   `end_date_min/max` window would become tz-fragile.

**Open-Meteo (`open_meteo.py`):** `fetch_forecast_payload` sends `timezone=station.timezone`
(l.372) and `daily=temperature_2m_max`. So `daily.time` entries are **station-local dates** and
each `temperature_2m_max` is the **station-local daily max**. The forecast `target_date` is thus
already station-local — but the lead attached to it (`_build_lead_maps`, l.401-439) is computed
against **UTC** midnight. **This is the central mismatch:** local-day value, UTC-day anchor.

---

## 3. Full data-flow trace

| Stage | Function(s) | Date anchor | Lead anchor | Output / column | Downstream consumer |
| --- | --- | --- | --- | --- | --- |
| **Event discovery (bot)** | `market_context.fetch_market_context` (l.118), `polymarket.fetch_weather_events` (l.318) | `target_date` (chosen upstream); Gamma `end_date_min/max` window (UTC) | — | `PolymarketEvent.settlement_date` (UTC date of `endDate`) | `date_mismatch` check (l.143) |
| **Event discovery (CLOB collector)** | `polymarket_clob._target_dates_for_station` (l.37) | **station-local** `local_today` (l.43) | — | per-day events | snapshot rows |
| **Profile/gate scheduling** | `bot._profile_settlement_dates` → `market_context.settlement_dates_for_profile` (l.84) | **UTC** `now.date()` (l.98) + lead scan | **UTC** `lead_hours_to_end_of_target_day` (l.108) | set of `target_date`s | `work_units_for_profiles`, `next_gate_wake_utc` |
| **Gate fire / wake** | `bot.next_gate_wake_utc` (l.117), `model/lead_time.gate_target_utc` (l.32) | UTC | **UTC** | wake instant; gate open ± tol | runner sleep/fire |
| **Live forecast fetch** | `open_meteo.fetch_open_meteo_live_bundle` (l.442), `_build_lead_maps` (l.401) | **station-local** (daily.time) | `wall_clock`=**UTC** (l.422); `init`=**UTC** (l.434) | `ForecastValues.init_lead_hours`, bundle maps | `analyze_event`, `persist_open_meteo_fetch` |
| **Ledger lead (paper)** | `bot.run_tick` (l.368) → `run.run_profile` (l.70) | UTC `target_date` | **UTC** `ctx.lead_hours` | `paper_events.lead_hours`, gate-skip logs, tick logs | reporting, gate compare |
| **Calibration (offline CSV/DB)** | `calibration_compute.compute_lead_hours` (l.60), `iter_forecast_records_from_payload` (l.81) | **station-local** (Single-Runs daily.time) | **UTC** | `calibration_forecast_records.lead_hours`, `calibration_stats*.csv` rows | `select_ceiling_row` |
| **Calibration (historical fetch planning)** | `historical_forecasts.generate_forecast_run_times` (l.117), `_actual_lead_hours` (l.112) | local `target_date` | **station-local** (`anchor_local`, l.134) | planned `lead_hours` (only used to *pick run inits*, not stored as the bucket) | overwritten by `compute_lead_hours` at ingest |
| **Calibration live lookup** | `analysis._resolve_strategy` (l.154) → `calibration_stats_csv.select_best_model` (l.186) | — | **UTC** init lead (per model) | selected `(model, σ, bias)` | distribution build |
| **Ensemble σ-floor** | `analysis.analyze_event` (l.356) → `distribution.build_distribution` (l.69) → `lead_time_sigma_floor` (l.49) | — | **UTC** wall-clock lead | σ floor (12/24/48/72 thresholds) | edges/decisions |
| **WU collector** | `wunderground.fetch_*` (l.250,499), `collectors/util.lead_hours_to_day_end` (l.26) | **station-local** | **station-local** | `observation_snapshots.lead_hours_to_day_end`, `forecast_snapshots.lead_hours_to_day_end` (+ `station_timezone`) | analysis/backtests |
| **Open-Meteo audit** | `open_meteo.persist_open_meteo_fetch` (l.506) | local `target_date_local` | `init`+`wall_clock`=**UTC** | `open_meteo_forecast_snapshots.init_lead_hours / wall_clock_lead_hours` | delay analysis, Single-Runs join |
| **CLOB collector** | `polymarket_clob._snapshot_rows_for_event` (l.47) | **station-local** date, **UTC** anchor | **UTC** (l.59-60) | `clob_bucket_snapshots.lead_hours_to_day_end` (UTC!), `wall_clock_lead_hours` | price backtests |
| **Reports / visualizer** | `paper/settlement_reporting.py`, `storage/snapshot_reads.py` | reads stored columns; fallback `ts.date()` (UTC, l.41) | reads stored leads (no recompute) | — | dashboards |

**Key observation:** every box that *schedules or trades* (gate, ledger, CLOB, Open-Meteo audit,
calibration) uses the **UTC** anchor; only the **WU collector** uses **station-local**. The
forecast *values* are station-local everywhere.

---

## 4. Why calibration is *self-consistent* (hypothesis partially refuted here)

The worry "wrong calibration bucket" is **not** realized, for the `best_historical` path:

- CSV/DB rows: `lead_hours = compute_lead_hours(run_init_utc, target_date)` — UTC anchor,
  `target_date` from Single-Runs `daily.time` (station-local date). (`calibration_compute.py:60,115`)
- Live lookup: `ForecastValues.init_lead_hours[model] = compute_lead_hours(meta.run_init_utc, target_date)`
  — UTC anchor, `target_date` from live Forecast `daily.time` (station-local date). (`open_meteo.py:434`)
- `select_best_model` (`calibration_stats_csv.py:186`) → `select_ceiling_row` picks the smallest
  `lead_hours ≥ current` **using the same per-model init lead**, *not* the wall-clock lead
  (`analysis.py:201,216`). When no model has rolling meta, it falls back to `ensemble_spread`
  (`no_models_with_init_meta`), so the wall-clock lead is **never** used for the ceiling lookup
  in practice.

Both sides share the identical (UTC-anchored, local-date) formula ⇒ the bucket selected matches
the bucket the stats were aggregated into. The σ/bias are *labelled* with a lead 1–2 h longer
than physical truth, but selection is correct.

**Residual P2:** because the UTC anchor mixes runs across DST (a "36 h" UTC bucket contains
35 h-to-local runs in summer and 36 h-to-local in winter), each bucket blends slightly different
*true* leads — mild smearing of bias/σ across the DST boundary. Small, but real if you trust the
buckets to ~1 h resolution.

---

## 5. Critical scenarios (worked numbers)

Let `D` = settlement day. London summer = BST (+1); Paris summer = CEST (+2).
`lead_UTC = lead_local + offset`.

| Instant `now` (UTC) | `lead_UTC` | London `lead_local` (Δ) | Paris `lead_local` (Δ) | Notes |
| --- | --- | --- | --- | --- |
| `D-1 12:00Z` (midday, day before) | **36.0** | 35.0 (−1) | 34.0 (−2) | both still "day before" |
| `D-1 22:00Z` (settle eve) | **26.0** | 25.0 (−1) | 24.0 (−2) | Paris lands exactly on the 24 h σ-floor boundary |
| `D-1 22:30Z` | **25.5** | 24.5 (−1) | **23.5** (−2) | **ensemble σ-floor bug:** Paris true lead 23.5 ⇒ floor **0.8**; code uses 25.5 ⇒ floor **1.2** (+50%) |
| `D 23:30Z` (near end) | **0.5** | **0** (clamped; −0.5) | **0** (clamped; −1.5) | local day already over; UTC says 0.5 h left. `now.date()` = `D` but local date = `D+1` |
| 54 h gate | fires `D-2 18:00Z` | local-anchored would fire `D-2 17:00Z` (London) | `D-2 16:00Z` (Paris) | UTC fires `offset` hours **later** |

**Delta when they disagree:** exactly the station's current UTC offset — **1 h London-summer,
2 h Paris-summer, 0 h London-winter, 1 h Paris-winter (CET).**

**54 h scan sanity:** `settlement_date_span_days(54) = int((54+23)//24)+1 = 4`
(`market_context.py:79`), and the lead-scan loop `range(-1, span+2)` covers `D-1 … D+5`
(`market_context.py:106`). So long-lead discovery is **robust** (the right date is included); only
the *fire instant* is shifted by the offset.

---

## 6. Success-criteria answer

> *"When the London bot fires a 30 h gate at instant T, what calendar day is being settled, what
> local/UTC midnight anchors that day, and does the lead_hours used for (a) gate open/close,
> (b) calibration lookup, and (c) collector snapshots all refer to the same anchor?"*

- **Instant:** `gate_target_utc(D, 30) = (D+1) 00:00Z − 30 h = D-1 18:00Z` (London local `D-1 19:00 BST`).
- **Day settled:** the **station-local** day `D` (e.g. June 23), whose Tmax is realized over
  `D 00:00–24:00 Europe/London` (`= D 23:00Z … wraps`).
- **Anchors:** the code anchors `D` at **UTC midnight `D+1 00:00Z`**; the *true* contract anchor
  is **local midnight `D 23:00Z`** (summer). The code anchor is **1 h late**.
- **Do (a)/(b)/(c) agree?**
  - (a) gate → **UTC** anchor.
  - (b) calibration → **UTC** anchor (init lead). ✅ **agrees with (a).**
  - (c) collectors → **split**: CLOB snapshot lead = **UTC** (agrees with a/b); **WU snapshot
    lead = station-local (disagrees by 1 h).**

So: **(a) = (b) = CLOB(c) ≠ WU(c).** The trading/calibration triangle is internally consistent
(all UTC), but **none of them matches the physical contract anchor**, and the **WU collector is
the odd one out**. A backtest that takes prices/forecasts from the WU collector and compares
their `lead_hours_to_day_end` to the ledger's gate lead is **off by the station offset**.

---

## 7. Inventory — every date/lead function

| File:line | Function | Anchor TZ | Date source | Used by |
| --- | --- | --- | --- | --- |
| `model/lead_time.py:13` | `lead_hours_to_end_of_target_day` | **UTC** | param `target` | bot gate (`bot.py:126,382`), CLI (`main.py:426`), `settlement_dates_for_profile` (l.108), `market_context.fetch_market_context` (l.167), CLOB (l.59-60), Open-Meteo wall-clock (`open_meteo.py:422`) |
| `model/lead_time.py:32/40/45` | `gate_target_utc`/`gate_open_utc`/`gate_close_utc` | **UTC** | param | `bot.next_gate_wake_utc` (l.131), tests |
| `model/lead_time.py:53-80` | `lead_hours_at_target`/`before`/`missed` | n/a (compares numbers) | — | gate logic (`bot.py`, `run.py`) |
| `calibration_compute.py:60` | `compute_lead_hours` | **UTC** | param `target_date` | forecast-record ingest (l.115), Open-Meteo init lead (`open_meteo.py:434`) |
| `collectors/util.py:9` | `local_today` | **station-local** | `now_utc` → tz | WU (l.250,459), CLOB (l.43), Open-Meteo collector (l.31) |
| `collectors/util.py:17` | `forecast_dates_for_station` | **station-local** | `local_today` | WU forecast pages (l.459) |
| `collectors/util.py:26` | `lead_hours_to_day_end` | **station-local** | param | **WU only** (`wunderground.py:499`) |
| `historical_forecasts.py:112` | `_actual_lead_hours` | **station-local** (`anchor_local`) | param | run-time planning (l.155,617); lead **not** persisted as bucket (overwritten by `compute_lead_hours` at ingest) |
| `historical_forecasts.py:134` | `generate_forecast_run_times` | **station-local** | param `target_date` | calibration fetch planning |
| `market_context.py:47` | `resolve_target_dates` | **server-local** `date.today()` | `date.today()` | **DEAD CODE — no callers** |
| `market_context.py:60` | `preview_settlement_dates` | **UTC** `now.date()` | `now` (UTC) | `bot.run_preview` (l.279) |
| `market_context.py:79` | `settlement_date_span_days` | n/a | — | `settlement_dates_for_profile` |
| `market_context.py:84` | `settlement_dates_for_profile` | **UTC** `now.date()` + lead scan | `now` (UTC) | `bot._profile_settlement_dates` (l.94) |
| `cli/main.py:295,357` | live target-date selection | **server-local** `date.today()` | `date.today()` | CLI `live` |
| `markets/polymarket.py:402` | `_parse_settlement_date` | **UTC** date of `endDate` | Gamma | discovery, `date_mismatch`, reporting |
| `paper/settlement_reporting.py:30` | `realization_day` | Gamma settlement date, else **UTC** `ts.date()` | event / ts | performance report bucketing |
| `distribution.py:49` | `lead_time_sigma_floor` | n/a (thresholds) | wall-clock UTC lead | `ensemble_spread` σ |
| `weather/open_meteo.py:401` | `_build_lead_maps` | **UTC** leads on **local** dates | local daily.time | bundle, persistence |

---

## 8. `date.today()` / `now.date()` audit

| Site | Expression | Effective TZ | Risk |
| --- | --- | --- | --- |
| `cli/main.py:295` | `today = date.today()` | **server-local (machine TZ)** | **P2.** Neither UTC nor station-local. On `mac0` (TZ unknown) this can be a different civil day than London. Picks the wrong "today/tomorrow" target for the CLI. Inconsistent with the bot (which uses UTC). |
| `cli/main.py:357` | `days_ahead = (target_date - date.today()).days` | server-local | Cosmetic (display only). |
| `market_context.py:49` | `resolve_target_dates(... today=date.today())` | server-local | **Dead code** — flag, don't fix. |
| `market_context.py:64` | `preview_settlement_dates: today = now.date()` (now UTC) | **UTC** | P2. Preview can label day boundaries off near local/UTC midnight; preview-only. |
| `market_context.py:98` | `settlement_dates_for_profile: today = now.date()` (now UTC) | **UTC** | P2. "today/tomorrow" base offsets are UTC-relative, not station-local. Near local/UTC midnight (e.g. London-summer 23:00–24:00Z, Paris-summer 22:00–24:00Z) UTC "today" = `D` while station-local "today" = `D+1`. **Largely mitigated** by the lead-scan second loop (l.106-114), which adds any date with an active/upcoming gate, so the *gate still fires*; the residual risk is a `target_day="today"` profile around the midnight divergence and conceptual mislabeling. |
| `bot.py:204` | `_in_active_exit_window: local = now.astimezone(LONDON_TZ)` | **station-local (hardcoded London!)** | **P2.** Correct *idea* (local day), but `LONDON_TZ` is hardcoded (`bot.py:49`). For a Paris/Madrid profile the active-exit window uses London's clock, not the station's. |
| `settlement_reporting.py:41` | `return ts.date()` | UTC | Cosmetic fallback (only when Gamma date missing). |

**Should "today/tomorrow" be station-local?** **Yes.** The contract day is station-local;
`local_today(station.timezone, now)` already exists and is the right primitive. The bot should
resolve target-day offsets relative to `local_today`, the CLI should stop using `date.today()`.

---

## 9. Bug list (severity-ranked)

**P1 — `lead_hours_to_day_end` column conflation across tables.**
`observation_snapshots`/`forecast_snapshots.lead_hours_to_day_end` are **station-local**
(`wunderground.py:499` → `util.py:26`); `clob_bucket_snapshots.lead_hours_to_day_end` is **UTC**
(`polymarket_clob.py:80` ← `lead_hours_to_end_of_target_day`). Same name, different meaning.
*Failure mode:* any analysis joining CLOB prices to WU forecasts/observations on lead, or
comparing either to the ledger gate lead, is silently off by the station offset (1–2 h in
summer). Also internally odd: CLOB *chooses* its date with `local_today` (l.43) but *measures*
lead to UTC midnight.

**P1 — Gate/ledger lead anchored at UTC midnight, not station-local end of day.**
`bot.next_gate_wake_utc` (l.117-137), `gate_target_utc`, `run.run_profile` ledger lead, all use
`lead_hours_to_end_of_target_day` (UTC). *Failure mode:* (1) every gate fires `offset` hours
later than its nominal lead implies; (2) a single global profile's `target_lead_hours` maps to
**different true leads per city and across DST** (30 h ⇒ 30 h London-winter, 29 h London-summer,
28 h Paris-summer) — so cross-city/cross-season behavior is not comparable, and DST transitions
shift entry timing by 1 h overnight; (3) stored `paper_events.lead_hours` is physically
mislabeled, polluting any lead-conditioned performance analysis.

**P2 — Ensemble σ-floor boundary sensitivity.**
`distribution.lead_time_sigma_floor` (l.49) uses the UTC wall-clock lead against hard thresholds
(12/24/48/72). *Failure mode:* near a boundary the +1/+2 h inflation bumps the floor (e.g. Paris
`D-1 22:30Z`: true 23.5 h → 0.8, code 25.5 h → 1.2), widening σ ~50 %, flattening the
distribution and shifting edges/decisions. Affects `ensemble_spread` profiles only.

**P2 — "today/tomorrow" resolved against UTC, not station-local.**
`settlement_dates_for_profile`/`preview_settlement_dates` (`market_context.py:64,98`). Mitigated
by the lead scan for gate firing, but conceptually wrong and fragile at the local/UTC midnight
divergence; would become acute if `endDate` semantics changed.

**P2 — CLI `date.today()` is server-local.** `cli/main.py:295,357`. Off-by-a-day if `mac0`/dev
machine isn't UTC; inconsistent with the bot.

**P2 — Active-exit window hardcodes `Europe/London`.** `bot.py:49,199-204`. Wrong local clock for
non-London stations.

**P3 / cosmetic / latent:**
- Dead code `resolve_target_dates` (`market_context.py:47`) carrying a server-local `date.today()`.
- Calibration DST smearing within UTC buckets (§4 residual).
- `realization_day` UTC `ts.date()` fallback (`settlement_reporting.py:41`).
- **Latent:** `_parse_settlement_date` + `end_date_min/max` discovery rely on `endDate` being at
  noon UTC; would break if Polymarket moved it near a UTC midnight (`polymarket.py:340-343,402`).

**Refuted worry:** "wrong calibration bucket for `best_historical`" — **not a bug** (§4); init
lead and CSV share the anchor and date basis.

---

## 10. Test evidence of the split

- `tests/test_lead_time.py:18-27` asserts the **UTC** anchor (target `2026-05-28`, now noon UTC
  ⇒ 12.0 h; next-day ⇒ 36.0 h).
- `tests/test_cli.py:58-83` re-asserts the same UTC convention (comment explicitly: "anchor must
  match the offline `compute_lead_hours` convention (UTC midnight)").
- `tests/test_collectors_wunderground.py:186-190` asserts the **station-local** anchor — but
  **deliberately picks a January (GMT) date** ("Europe/London is on GMT (no DST offset)") so
  local == UTC and the **divergence is never exercised**. A summer date would make
  `lead_hours_to_day_end` return 35.0 while `lead_hours_to_end_of_target_day` returns 36.0.
- `tests/test_polymarket_clob_collector.py:155` asserts `wall_clock ≤ lead_hours_to_day_end`
  within the CLOB (UTC) table only — it cannot catch the cross-table conflation.

The test suite thus *documents* the two conventions and *avoids* the case where they disagree.

---

## 11. Proposed fix plan (analysis only — not implemented)

### Canonical convention

Make **station-local end of day** canonical. Introduce one helper (parameterized, no default
that hides the tz):

```python
# model/lead_time.py
def lead_hours(now_utc: datetime, target_date: date, station_timezone: str) -> float:
    """Hours from now_utc to station-local midnight at the END of target_date."""
```

`collectors/util.py:lead_hours_to_day_end` is already exactly this (modulo arg order); promote it
to the canonical home and have everyone call it. Keep `gate_target_utc`/`gate_close_utc`
re-expressed in terms of the local anchor.

### Sequenced migration (lowest risk → highest)

1. **Collectors first (cheap, high value, no trading impact).**
   - Point `polymarket_clob` at the station-local anchor so its `lead_hours_to_day_end` matches
     the WU collector's column meaning. (Or, if you prefer to keep CLOB aligned to the gate,
     **rename** the CLOB column to `lead_hours_to_day_end_utc` to end the conflation.) Decide
     *one* anchor for both collector columns and make the name state it.
   - Migration impact: new rows change anchor; old rows stay. Add a `lead_anchor` note or version
     marker, or backfill a recomputed column. No trading behavior changes.

2. **Scheduling/gate + ledger.**
   - Switch `settlement_dates_for_profile`, `next_gate_wake_utc`, `gate_target_utc`,
     `fetch_market_context` lead, and the `ctx.lead_hours` stored in the ledger to the local
     anchor + `station.timezone`.
   - Resolve `target_day` offsets via `local_today(station.timezone, now)` (replaces
     `now.date()`); fix CLI to use `local_today` (replaces `date.today()`).
   - **Impact on gate timing vs. historical backtests:** every gate moves earlier by the offset
     (1 h London-summer, 2 h Paris-summer). Paper-bot entry instants and stored `lead_hours`
     change; any backtest baselined on the old UTC leads must be **re-baselined**. Recommend a
     one-time annotation in `paper_events` (e.g. `lead_anchor='local'` from cutover) so pre/post
     rows are distinguishable.
   - Parameterize the active-exit window by `station.timezone` (drop hardcoded `LONDON_TZ`).

3. **Ensemble σ-floor.** Once scheduling lead is local, `lead_time_sigma_floor` automatically
   gets the correct lead — no further change, but add boundary tests (§12).

4. **Calibration — choose one:**
   - **Option A (full unification, preferred long-term):** recompute `lead_hours` for
     `calibration_forecast_records` and regenerate `calibration_stats_updated.csv` with the local
     anchor (`scripts/run_daily_calibration.py`). The live init lead must switch to the local
     anchor in lockstep (`open_meteo.py:434`). **Do NOT touch the frozen V1
     `calibration_stats.csv`** (per `memory/wu-fahrenheit-artifact` — the frozen bh CSV must not
     be regenerated); instead either deprecate `best_historical` in favor of
     `best_historical_updated`, or accept that V1 buckets stay UTC-labelled and document it.
   - **Option B (minimal, keep calibration UTC):** leave `compute_lead_hours` UTC-anchored but
     **rename** it / its outputs to `init_lead_hours_utc` and document that calibration lead is a
     *UTC-anchored model-age*, intentionally distinct from scheduling lead. Lowest risk; preserves
     all existing buckets and the §4 self-consistency. The only cost is the permanent ~offset-hour
     label gap between calibration lead and scheduling lead (acceptable since they are never
     compared numerically in code).
   - **Recommendation:** Option B now (zero data migration, removes the *conflation* by naming),
     Option A when the updated store next gets a planned rebuild.

### DB column impact summary

| Column | Today | After (recommended) |
| --- | --- | --- |
| `observation_snapshots.lead_hours_to_day_end` | station-local | unchanged (canonical) |
| `forecast_snapshots.lead_hours_to_day_end` | station-local | unchanged |
| `clob_bucket_snapshots.lead_hours_to_day_end` | **UTC** | → station-local **or** rename `…_utc` |
| `clob_bucket_snapshots.wall_clock_lead_hours` | UTC | rename or re-anchor to match |
| `open_meteo_forecast_snapshots.init_lead_hours` | UTC | rename `init_lead_hours_utc` (Option B) or re-anchor (Option A) |
| `open_meteo_forecast_snapshots.wall_clock_lead_hours` | UTC | re-anchor to local (audit honesty) |
| `paper_events.lead_hours` | UTC | re-anchor to local + `lead_anchor` marker from cutover |
| `calibration_forecast_records.lead_hours` | UTC | Option B keep (rename concept) / Option A recompute |

---

## 12. Test plan

New/updated tests (run only against `POLYTEMPO_TEST_DATABASE_URL` / `POLYTEMPO_PAPER_TEST_DATABASE_URL`
per `.cursor/rules/postgres-safety.mdc`; **never** against `polytempo` / `polytempo_paper`):

1. **DST divergence (the gap the current suite avoids).**
   - `lead_hours(now=D-1 12:00Z, D, "Europe/London")` in **summer** ⇒ **35.0** (vs UTC 36.0).
   - Same in **winter** ⇒ **36.0** (anchors coincide).
   - Paris summer ⇒ **34.0**; Paris winter ⇒ **35.0**.
   - Rewrite `test_collectors_wunderground.py:test_lead_hours_to_day_end` to use a **June** date
     and assert the local value (currently it dodges this with January).
2. **Gate firing instant by station/season.** `gate_target_utc`-equivalent for 30 h on London-summer
   fires `D-2 17:00Z` (local anchor), London-winter `D-2 18:00Z`, Paris-summer `D-2 16:00Z`.
3. **σ-floor boundary.** Paris `now=D-1 22:30Z`: assert chosen floor is **0.8** (local 23.5 h),
   regression-guard against the 1.2 (UTC 25.5 h) it currently picks.
4. **`local_today` boundary.** London-summer `now=D 23:30Z` ⇒ `local_today = D+1` while
   `now.date() = D`; assert target-day resolution uses the local value.
5. **Cross-collector consistency.** Insert a WU forecast snapshot and a CLOB snapshot for the same
   station/instant/`target_date`; assert their `lead_hours_to_day_end` agree (post-fix) — or, if
   keeping separate anchors, assert they differ by exactly the offset and have distinct column
   names.
6. **Calibration anchor lockstep.** Property test: live `init_lead_hours` and the CSV row lead use
   the *same* anchor function (guards against drifting only one side in Option A).
7. **Active-exit window tz.** A Paris profile's active-exit window keys off `Europe/Paris`, not
   `Europe/London`.

---

## 13. Doc updates required

**`docs/calibration-data.md`:**
- §"Lead-hours convention" (l.76-97) states UTC midnight as canonical and calls it "Tmax …
  aggregated over the **station-local** calendar day; the lead anchor is UTC midnight on both
  sides." **This is accurate as a description but presents the UTC anchor as correct without
  noting it is ~offset hours longer than the true local end-of-day**, and that the WU collector
  uses a *different* (local) anchor. Add: (a) the local-vs-UTC offset table; (b) an explicit note
  that calibration lead is a *UTC-anchored model-age*, deliberately distinct from scheduling lead;
  (c) a pointer to this audit.
- The "Paper bot (`fetch_market_context`)" sub-bullets (l.82-86) say entry-gate lead is
  `lead_hours_to_end_of_target_day` (UTC). After the §11 fix this becomes station-local — update.

**Code docstrings to correct/clarify (post-fix):**
- `model/lead_time.py:17-22` ("Matches the offline calibration pipeline") — if Option B, state the
  anchor difference explicitly; if Option A, update the cross-reference.
- `collectors/util.py:26-42` — note it is the canonical lead.
- `markets/polymarket.py:328-343,402` — document that discovery relies on `endDate` being noon-UTC
  and is tz-fragile if that changes.

**Inline:** mark `market_context.resolve_target_dates` (l.47) as dead/deprecated.

---

## 14. Methodology / what I did not do

- Read `CLAUDE.md`, `.cursor/rules/postgres-safety.mdc`, `docs/calibration-data.md` first (per
  constraints). **No pytest was run** (prod DB URLs are in this environment; see
  `memory/db-access-tailscale`, `memory/no-local-python`).
- One **read-only** Gamma HTTP GET was used to verify contract semantics (§2). No writes, no DB
  access, no trades.
- `graphify query` was not available as a tool in this session; used grep/read instead.
- No fixes were implemented (task constraint). §11 is a proposal.
</content>
</invoke>
