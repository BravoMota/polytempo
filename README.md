# PolyTempo

PolyTempo is a deterministic weather-market analysis tool.

The goal is to compare weather forecast distributions against Polymarket temperature bucket prices, estimate edge, and produce strict BUY/SKIP recommendations.

Current status: **Phases 0–9 complete** on the plan: Polymarket/Gamma ingestion (`markets/polymarket.py`), Open-Meteo daily-max ensemble (`weather/open_meteo.py` + `weather/stations.py`), shared **`ForecastValues`** in `weather/schema.py` (with **`DailyMaxForecast.to_forecast_values()`** for the pipeline), **manual bias calibration** (`model/calibration.py`, used by **`analyze_event`**), and a **paper trading ledger** (`paper/ledger.py`, $1000 demo balance, 2-5% edge-scaled stake, append-only JSONL) wired through `polytempo paper {open,settle,status,list-london}` for London. **Phase 10** (reports) is next. Live API smoke: `POLYTEMPO_RUN_LIVE_API_TESTS=1 pytest tests/test_pipeline.py` (opt-in; may still skip if no parseable event).

## Quickstart

Create and activate the virtual environment (from the repo root):

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

Run the local end-to-end demo on hardcoded fake inputs:

```bash
polytempo demo
```

Run against **live** Polymarket (Gamma) + Open-Meteo. By default **`--city london`** and **`--days-ahead 1`** (tomorrow): Gamma is queried for weather events whose **end date** falls on that UTC day (`end_date_min` / `end_date_max`), then the first **London** title/slug match with **parseable** Celsius buckets is used. Open-Meteo requests the **same calendar day** max temperature at the **London contract station** (EGLC) from `weather/stations.py`.

```bash
polytempo live
```

Other registry cities (same station table): `polytempo live --city madrid`, etc.

Use a specific Gamma event (forecast still uses `--days-ahead` for the Open-Meteo day; you get a warning if the event’s parsed `endDate` differs):

```bash
polytempo live --event-id YOUR_EVENT_ID --city london
```

Shift the shared target day (default `1` = tomorrow): `polytempo live --days-ahead 0` for **today**.

Same via Typer programmatically (e.g. Windows `py -3`):

```bash
py -3 -c "from polytempo.cli.main import app; app(['demo'], standalone_mode=False)"
```

Run the test suite:

```bash
python3 -m pytest
```

## Paper trading (London, demo only)

```bash
# 1. find an active London weather event
polytempo paper list-london

# 2. lock paper trades against it for a given settlement date
polytempo paper open --event-id <id> --date 2026-05-19

# 3. after the market resolves, settle against the winning bucket label
polytempo paper settle --event-id <id> --winner "23°C"

# 4. inspect the account
polytempo paper status
```

The ledger lives at `paper_ledger.jsonl` (append-only). Starting balance is
$1000; each BUY_YES gets 2-5% of the live balance based on edge.

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
