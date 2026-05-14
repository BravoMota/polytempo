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

Current status: Phases **0–8** are complete through Open-Meteo (`weather/open_meteo.py`), contract stations (`weather/stations.py`), shared **`weather/schema.py`** (`ForecastValues` + `DailyMaxForecast.to_forecast_values()`), **`analyze_event`** with optional calibration, and **`model/calibration.py`**. Next: **Phase 9** paper ledger (`paper/ledger.py` is still a stub) and product wiring (e.g. event-specific location/date for meaningful live runs).

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

Record simulated trades and outcomes. No real trading.

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
