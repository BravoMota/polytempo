# PolyTempo

PolyTempo is a deterministic weather-market analysis tool.

The goal is to compare weather forecast distributions against Polymarket temperature bucket prices, estimate edge, and produce strict BUY/SKIP recommendations.

Current status: **skeleton only**.

## Principles

- Deterministic core first
- No LLM agent in the critical path
- No live trading
- Paper trading only
- Small modules
- Tests before expansion

## Planned flow

```text
weather forecasts
  → calibration
  → probability distribution
  → bucket probabilities
  → market price comparison
  → deterministic decision
  → report / paper ledger
```
