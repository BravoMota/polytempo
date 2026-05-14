# PolyTempo

PolyTempo is a deterministic weather-market analysis tool.

The goal is to compare weather forecast distributions against Polymarket temperature bucket prices, estimate edge, and produce strict BUY/SKIP recommendations.

Current status: **Phases 0–8 complete** on the plan: Polymarket/Gamma ingestion (`markets/polymarket.py`), Open-Meteo daily-max ensemble (`weather/open_meteo.py` + `weather/stations.py`), shared **`ForecastValues`** in `weather/schema.py` (with **`DailyMaxForecast.to_forecast_values()`** for the pipeline), and **manual bias calibration** (`model/calibration.py`, used by **`analyze_event`**). **Phase 9** (paper ledger) is next. Live API smoke: `POLYTEMPO_RUN_LIVE_API_TESTS=1 pytest tests/test_pipeline.py` (opt-in; may still skip if no parseable event).

## Quickstart

Run the local end-to-end demo on hardcoded fake inputs (after `pip install -e .` from the repo root):

```bash
polytempo demo
```

Same via Typer programmatically (e.g. Windows `py -3`):

```bash
py -3 -c "from polytempo.cli.main import app; app(['demo'], standalone_mode=False)"
```

Run the test suite:

```bash
python3 -m pytest
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
