# PolyTempo Architecture

## Core idea

PolyTempo is a deterministic pipeline.

```text
market data + weather data
  → normalized inputs
  → calibrated forecasts
  → probability distribution
  → edge calculation
  → deterministic decision
  → report / paper ledger
```

Current status: Phases **0–9** complete. Engine: Open-Meteo (`weather/open_meteo.py`), contract stations (`weather/stations.py`), shared **`weather/schema.py`** (`ForecastValues` + `DailyMaxForecast.to_forecast_values()`), **`analyze_event`** with optional calibration, **`model/calibration.py`**, and the **paper ledger** (`paper/ledger.py`, append-only JSONL OPEN/SETTLE records, $1000 demo balance, 2-5% edge-scaled sizing) wired through `polytempo paper {open,settle,status,list-london}` for London. Next: **Phase 10** reports (`reports/writer.py`) and product wiring (event-specific settlement date + auto-resolution from the Gamma payload).

## Project shape

```text
src/polytempo/
  weather/     # forecast ingestion and normalization
  markets/     # Polymarket fetching and bucket parsing
  model/       # calibration and probability distribution
  strategy/    # edge and deterministic decision rules
  analysis.py   # local analysis use-case layer
  paper/       # simulated paper-trading ledger
  cli/         # command entry points
  reports/     # JSON/Markdown outputs
```

## Module responsibilities

### weather/

Fetch and normalize weather forecast data. **`schema.py`** holds `ForecastValues`
for downstream calibration/analysis; **`open_meteo.py`** returns `DailyMaxForecast`
with `to_forecast_values()` at the boundary. Holds the contract-station registry
(`stations.py`) that maps cities to the named airport observation station
(ICAO, lat/lon, timezone) used by Polymarket settlement. Forecasts target the
station, never a city centroid.

### markets/

Fetch Polymarket market data and parse temperature bucket labels.

### model/

Correct forecasts using calibration and convert them into bucket probabilities.

### strategy/

Compare model probabilities to executable market prices and apply BUY/SKIP rules.

### analysis.py

Local use-case layer that connects buckets, distribution, edge, and decision for analysis.

### paper/

Record simulated trades and outcomes. No real trading. `ledger.py` writes an
append-only JSONL of `OPEN`/`SETTLE` records; balance and open positions are
always derived by replaying the log. Stake sizing is `2%-5%` of the current
balance, scaled linearly by model edge (`edge_pp ≤ 7` → 2%, `edge_pp ≥ 15` →
5%).

### cli/

Expose simple commands for local use.

### reports/

Write human-readable and machine-readable outputs.

## Rule

Any new module must map clearly to one of the boxes above.

## Boundaries

- Low-level modules should stay independent.
- `distribution.py` should not know about market prices.
- `edge.py` should not know how probabilities were created.
- `decision.py` should not fetch data or compute edge.
- The analysis/use-case layer will connect the pieces.
