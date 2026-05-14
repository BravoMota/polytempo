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

Current status: Phases 0-7 are complete (through Open-Meteo forecast ingestion in `weather/open_meteo.py` and the contract-station registry in `weather/stations.py`). Next: wire real forecasts into `analysis.py` and Phase 8 calibration.

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

Fetch and normalize weather forecast data. Holds the contract-station registry
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
