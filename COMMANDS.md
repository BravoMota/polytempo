# Commands (copy-paste)

Developer cheat sheet for PolyTempo CLI entrypoints. For product overview see [README.md](README.md).

## One-time setup

```bash
cd /path/to/PolyTempo
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

## live

Fetch a Polymarket event + Open-Meteo forecast for the contract station, run the analysis, and write a markdown report. The forecast distribution is built per `--model-strategy`:

| Strategy | mean | sigma |
| --- | --- | --- |
| `ensemble_spread` (default) | mean across live models | spread across live models, combined in quadrature with the lead-time floor |
| `best_historical` | selected model's prediction `- bias_c` | selected model's `error_std_c`, falling back to `rmse_c` |

`best_historical` reads `data/weather/statistical/calibration_stats.csv` (produced by step 6 below) and, **per available live model**, picks the row whose `lead_hours` is the smallest value `>=` the current live lead time. It then chooses the model with the lowest valid `error_std_c` (falling back to `rmse_c` when std is missing/zero/non-finite) and `n_samples > 0`. If the CSV is missing, no model has a qualifying ceiling row, the live forecast lost model identity, or `station_id`/`lead_hours` are unknown, the command silently falls back to `ensemble_spread` and reports the reason via `fallback_reason` in the report and CLI output (`selected_model`, `sigma_source`, `calibration_row`, `fallback_reason`).

```bash
polytempo live --city london --days-ahead 1 --model-strategy best_historical
```

## fetch-historical-forecasts

Fetch Open-Meteo Single Runs and cache **full API JSON** under `data/weather/raw/`. In date-range mode, also append parsed Tmax rows to JSONL. Offline only — not used by `polytempo live`.

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
| `--raw-dir`            | Raw Single-Runs JSON directory (default `data/weather/raw/`)                               |
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

## Offline calibration pipeline (standalone scripts)

Numbered scripts under `scripts/` run in order. All use global vars at the top (no CLI flags). Data lives under `data/weather/` (see [docs/calibration-data.md](docs/calibration-data.md)).

| Step | Script | Purpose |
| --- | --- | --- |
| 1 | `scripts/1_analyze_single_runs_models.py` | Probe model capabilities → `single_runs_model_capabilities.csv` + `raw_capabilities/` |
| 2 | `scripts/2_fetch_historical_forecasts_by_model.py` | Bulk-fetch raw Single Runs JSON → `raw/` |
| 3 | `scripts/3_fetch_wunderground_observations.py` | Fetch observed Tmax → `observed_tmax.jsonl` |
| 4 | `scripts/4_build_forecast_records_csv.py` | Raw JSON → `processed/forecast_records.csv` |
| 5 | `scripts/5_build_observed_tmax_csv.py` | `observed_tmax.jsonl` → `observed_tmax.csv` |
| 6 | `scripts/6_compute_calibration_errors.py` | Join forecasts + observations → `statistical/forecast_errors.csv` + `statistical/calibration_stats.csv` |

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

Raw JSON responses land in `data/weather/raw/` using `{station}_{model}_{run_time_utc}.json`. Existing files are skipped (resumable).

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

Reads `data/weather/raw/*.json` and writes one CSV row per non-null daily Tmax to `data/weather/processed/forecast_records.csv`.

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
- `http/polymarket_gamma.http` — Gamma event payload inspection

These files are **not** part of the Python package. Use them to inspect API payload shapes before coding parsers. Do **not** use them in live analysis.
