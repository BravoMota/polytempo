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

Current status: Phases 0-4 are complete. Phase 4.5 is next: add an analysis/use-case layer that assembles existing local modules using fake/local inputs.

## Project shape

```text
src/polytempo/
  weather/     # forecast ingestion and normalization
  markets/     # Polymarket fetching and bucket parsing
  model/       # calibration and probability distribution
  strategy/    # edge and deterministic decision rules
  analysis.py   # planned analysis/use-case layer
  paper/       # simulated paper-trading ledger
  cli/         # command entry points
  reports/     # JSON/Markdown outputs
```

## Module responsibilities

### weather/

Fetch and normalize weather forecast data.

### markets/

Fetch Polymarket market data and parse temperature bucket labels.

### model/

Correct forecasts using calibration and convert them into bucket probabilities.

### strategy/

Compare model probabilities to executable market prices and apply BUY/SKIP rules.

### analysis.py

Planned use-case layer that connects buckets, distribution, edge, and decision for local analysis.

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
