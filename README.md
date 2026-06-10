# PolyTempo

PolyTempo is a deterministic weather-market analysis tool.

The goal is to compare weather forecast distributions against Polymarket temperature bucket prices, estimate edge, and produce strict BUY/SKIP recommendations.

Current status: **all plan phases (0–10) complete**, and the project has grown past the original plan into a profile-based paper-trading system:

- **Ingestion** — Polymarket Gamma discovery + live CLOB order-book prices (`markets/polymarket.py`), Open-Meteo daily-max ensemble (`weather/open_meteo.py` + `weather/stations.py`), shared `ForecastValues` in `weather/schema.py`.
- **Model** — three model strategies: `best_historical` (frozen calibration CSV), `best_historical_updated` (nightly-recomputed CSV), `ensemble_spread` (live model spread).
- **Trade strategies** — eight: `argmax_yes`, `argmax_no`, `dist_arb`, `mid_band`, `topk_yes`, `topk_no`, `max_edge`, `edge_band` (`strategy/`, registered in `profiles/registry.py`).
- **Paper trading** — **216 profiles** (3 model strategies × 8 trade strategies × 9 lead-time gates) from `config/paper_profiles.yaml`; each profile has its own $1000 bankroll in **PostgreSQL** (`polytempo_paper`). The always-on bot (`scripts/run_paper_bot.py`) opens trades at each profile's exact lead-hour gate and settles resolved events.
- **Data collection** — Wunderground collectors into Postgres (`scripts/run_collector.py`) and nightly calibration updates (`scripts/run_daily_calibration.py`).
- **Reports** — every `polytempo live` run writes a Markdown report under `reports/`.

Live API smoke: `POLYTEMPO_RUN_LIVE_API_TESTS=1 pytest tests/test_pipeline.py` (opt-in; may still skip if no parseable event).

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

Run against **live** Polymarket + Open-Meteo. By default **`--city london`** and **`--days-ahead 1`** (tomorrow): Gamma is queried for weather events whose **end date** falls on that UTC day (`end_date_min` / `end_date_max`), then the first **London** title/slug match with **parseable** Celsius buckets is used. Executable prices (bid/ask/spread/liquidity) are then pulled from the live **CLOB** order book (`POST /books`) — Gamma supplies discovery and resolution only, never the prices that drive edge. Open-Meteo requests the **same calendar day** max temperature at the **London contract station** (EGLC) from `weather/stations.py`.

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

Paper state lives in PostgreSQL (`polytempo_paper`, set `POLYTEMPO_PAPER_DATABASE_URL`);
each of the 216 profiles keeps its own $1000 bankroll. See
[COMMANDS.md](COMMANDS.md) for setup and the always-on bot.

```bash
# 1. find an active London weather event
polytempo paper list-london

# 2. lock paper trades against it for a given settlement date (all active profiles)
polytempo paper open --event-id <id> --date 2026-05-19

# 3. after the market resolves, settle against the winning bucket label
polytempo paper settle --event-id <id> --winner "23°C"

# 4. inspect per-profile balances
polytempo paper status

# 5. per-event scenario PnL for open trades
polytempo paper scenarios
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
