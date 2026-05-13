# PolyTempo

PolyTempo is a deterministic weather-market analysis tool.

The goal is to compare weather forecast distributions against Polymarket temperature bucket prices, estimate edge, and produce strict BUY/SKIP recommendations.

Current status: **deterministic core complete through Phase 6** (Polymarket/Gamma market fetch and normalization in `markets/polymarket.py`). **Forecast ingestion (Open-Meteo, Phase 7) not started**; end-to-end analysis can still use local/fake forecast values until then.

## Quickstart

Run the local end-to-end demo on hardcoded fake inputs:

```bash
py -3 -c "from polytempo.cli.main import app; app(['demo'], standalone_mode=False)"
```

Run the test suite:

```bash
py -3 -m pytest
```

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
