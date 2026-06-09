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
| PostgreSQL `observation_snapshots`, `forecast_snapshots` | Live collector HTML scrape (`scripts/run_collector.py`) |
| `raw/wunderground/` | Raw collector HTML sidecars |

Collector data is **not** used for calibration joins.

## Lead-hours convention

Canonical for forecast records, error joins, `calibration_stats*.csv`, and live `polytempo live` lookup:

- `lead_hours = (UTC midnight at the END of target_date) - run_time_utc` in hours.
- Live `polytempo live` uses the same anchor with `run_time_utc = now`.

Tmax is aggregated over the **station-local** calendar day by Open-Meteo; the lead anchor is UTC midnight on both sides.

## Example observation record (legacy jsonl)

```json
{"station_id": "EGLC", "target_date": "2026-05-14", "observed_tmax_c": 22.4, "source": "wunderground"}
```

Updated-store DB rows also carry `observed_tmax_f` (reported °F from WU page).
