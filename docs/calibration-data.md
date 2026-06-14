# Calibration data

Weather calibration artifacts live under **`data/weather/`** at the repository root. There are two paths: the **frozen V1 manual pipeline** (`best_historical`) and the **automated updated store** (`best_historical_updated`).

## Updated path (automated, `best_historical_updated`)

Canonical store is **PostgreSQL** (`calibration_observed_tmax`, `calibration_forecast_records`, `calibration_job_state`). Runtime output is `statistical/calibration_stats_updated.csv` (gitignored, regenerated nightly).

| Layer | Role |
| --- | --- |
| Postgres `calibration_*` tables | Incremental observations + forecast records + job state |
| `raw/single-runs/` | Cached Open-Meteo Single Runs JSON (reused from V1 fetch) |
| `statistical/calibration_stats_updated.csv` | Nightly CSV for `polytempo live --model-strategy best_historical_updated` |

**Observations:** WU history daily page reported `temperatureMax` in °F, stored as both `observed_tmax_f` and `observed_tmax_c` (F→C, 2 dp). Not `max()` of hourly obs, not metric API.

**Scripts:**

| Script | Purpose |
| --- | --- |
| `scripts/bootstrap_calibration_store.py` | One-time heavy load from `2026-02-01` (see `config/calibration.yaml`) |
| `scripts/run_daily_calibration.py --once` | Incremental nightly update (cron 02:00 UTC) |

Config: `config/calibration.yaml` — stations resolved from `config/weather_collectors.yaml`; models and cadence baked in (no `capabilities_csv` at runtime).

**Not used by the updated path:** `observed_tmax.jsonl`, `observed_tmax.csv`, `processed/forecast_records.csv`, `statistical/forecast_errors.csv`, `statistical/calibration_stats.csv`.

## Frozen V1 path (`best_historical`)

Numbered scripts under `scripts/Calibrator_V1/` (or legacy numbered `scripts/1_` … `scripts/6_`) produce the static baseline in git:

| Path | Produced by | Purpose |
| --- | --- | --- |
| `raw/single-runs/` | script 2 | Cached Single Runs JSON |
| `observed_tmax.jsonl` | script 3 | Observed daily Tmax (legacy WU API) |
| `processed/forecast_records.csv` | script 4 | Parsed forecast rows |
| `observed_tmax.csv` | script 5 | CSV view of observations |
| `statistical/forecast_errors.csv` | script 6 | Joined errors |
| `statistical/calibration_stats.csv` | script 6 | Grouped stats for `best_historical` |

## Collector snapshots (separate)

| Path / store | Purpose |
| --- | --- |
| PostgreSQL `observation_snapshots`, `forecast_snapshots` | Wunderground HTML scrape (`scripts/run_collector.py`) |
| PostgreSQL `open_meteo_fetch_cycles`, `open_meteo_model_meta_snapshots`, `open_meteo_forecast_snapshots` | Open-Meteo rolling meta + Forecast API audit (`run_collector.py` `open_meteo` block) |
| `raw/wunderground/` | Raw WU HTML sidecars |

WU collector data is **not** used for calibration joins. Open-Meteo collector rows are for **delay / run-correspondence analysis** against `calibration_forecast_records` (Single Runs).

### Open-Meteo live audit (init vs wall-clock lead)

Each `open_meteo` poll stores:

| Column | Meaning |
| --- | --- |
| `init_lead_hours` | `compute_lead_hours(run_init_utc, target_date)` — matches calibration / Single Runs `run=` semantics |
| `wall_clock_lead_hours` | Hours from `fetched_at_utc` to end of target day — when the collector actually polled |
| `run_init_utc` | From rolling `data/{model}/static/meta.json` (`last_run_initialisation_time`) |
| `availability_lag_hours` | Meta: hours from init to `last_run_availability_time` |

Join live forecasts to Single Runs calibration rows:

```sql
SELECT live.fetched_at_utc, live.model, live.run_init_utc, live.target_date_local,
       live.predicted_tmax_c AS live_c, cal.predicted_tmax_c AS single_runs_c,
       live.predicted_tmax_c - cal.predicted_tmax_c AS delta_c
FROM open_meteo_forecast_snapshots live
JOIN calibration_forecast_records cal
  ON cal.station_id = live.station_id
 AND cal.model = live.model
 AND cal.run_time_utc = live.run_init_utc
 AND cal.target_date = live.target_date_local;
```

## Lead-hours convention

Canonical for forecast records, error joins, and `calibration_stats*.csv`:

- `lead_hours = (UTC midnight at the END of target_date) - run_time_utc` in hours.

**Paper bot (`fetch_market_context`):**

- **Entry gates / ledger:** wall-clock lead from `now` to end of target day (`lead_hours_to_end_of_target_day`).
- **`best_historical` CSV lookup:** per-model init lead from rolling `meta.json` (`compute_lead_hours(run_init_utc, target_date)`), carried on `ForecastValues.init_lead_hours`.

### `best_historical` and models without rolling meta

Open-Meteo publishes rolling `data/{model}/static/meta.json` for some models only (e.g. UKMO deterministic, ECMWF). Others (`icon_eu`, `gfs_seamless`, seamless blends) return **404** — there is no auditable `run_init_utc`.

**Decision (Option 1):** models without verified rolling meta are **excluded from `best_historical` competition**. They are not assigned wall-clock lead for calibration lookup (that would understate lead vs init-based peers and bias selection toward artificially low σ buckets). Forecast values from those models may still appear in the live bundle and in `ensemble_spread`; only the calibrated single-model picker skips them.

Eligibility signal: non-empty `ForecastValues.model_run_init_utc` for that model. If no live model has meta, `best_historical` falls back to `ensemble_spread` with reason `no_models_with_init_meta`.

Tmax is aggregated over the **station-local** calendar day by Open-Meteo; the lead anchor is UTC midnight on both sides.

`polytempo live` still uses wall-clock lead only (not wired to init metadata).

## Example observation record (legacy jsonl)

```json
{"station_id": "EGLC", "target_date": "2026-05-14", "observed_tmax_c": 22.4, "source": "wunderground"}
```

Updated-store DB rows also carry `observed_tmax_f` (reported °F from WU page).
