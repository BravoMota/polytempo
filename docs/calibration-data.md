# Offline calibration data

All weather calibration artifacts live under **`data/weather/`** at the repository root.

## Layout

| Path | Produced by | Purpose |
| --- | --- | --- |
| `raw/` | `scripts/2_fetch_historical_forecasts_by_model.py`, `polytempo fetch-historical-forecasts` | Cached Open-Meteo Single Runs JSON (one file per run init) |
| `raw_capabilities/` | `scripts/1_analyze_single_runs_models.py` | Probe API payloads for model capability analysis |
| `single_runs_model_capabilities.csv` | `scripts/1_analyze_single_runs_models.py` | Per-model cadence, grid offset, daily Tmax availability |
| `historical_forecasts.jsonl` | `polytempo fetch-historical-forecasts` | Parsed historical model-run Tmax predictions |
| `observed_tmax.jsonl` | `scripts/3_fetch_wunderground_observations.py` | Observed daily Tmax for join targets |
| `observed_tmax.csv` | `scripts/5_build_observed_tmax_csv.py` | CSV view of observations |
| `processed/forecast_records.csv` | `scripts/4_build_forecast_records_csv.py` | One row per predicted Tmax from raw JSON |
| `statistical/forecast_errors.csv` | `scripts/6_compute_calibration_errors.py` | Joined forecast + observation rows with signed/abs/squared errors |
| `statistical/calibration_stats.csv` | `scripts/6_compute_calibration_errors.py` | `n_samples`, bias, MAE, RMSE, `error_std_c` by `(station, model, lead_hours)` |
| `calibration_stats.json` | `polytempo compute-calibration-stats` | RMSE / MAE / bias by station, model, lead bucket |

## Rules

- Generated files are machine-written. Do not hand-edit.
- **Live analysis reads prepared artifacts only.** Historical or Single-Runs APIs must not be called during live runs.
- Re-running fetch commands skips duplicates when output already exists. Partial failures may leave gaps; inspect stderr and re-run.
- Run times are snapped to synoptic model inits (00/06/12/18 UTC) in CLI date-range mode.

## Example observation record (`observed_tmax.jsonl`)

One JSON object per line:

```json
{"station_id": "EGLC", "target_date": "2026-05-14", "observed_tmax_c": 22.4, "source": "wunderground"}
```

Required keys: `station_id`, `target_date` (YYYY-MM-DD), `observed_tmax_c`, `source`.
