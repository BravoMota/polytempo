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

Current status: all plan phases (**0–10**) complete. Engine: Open-Meteo (`weather/open_meteo.py`), contract stations (`weather/stations.py`), shared **`weather/schema.py`**, **`analyze_event`** with three model strategies (`best_historical`, `best_historical_updated`, `ensemble_spread`), fourteen trade strategies in `strategy/`, and **profile-based paper trading**: 378 profiles (3 model × 14 trade × 9 lead gates) from `config/paper_profiles.yaml`, each with its own $1000 bankroll in PostgreSQL (`polytempo_paper`). The always-on bot (`scripts/run_paper_bot.py`) opens at exact lead-hour gates and settles resolved events; nightly calibration (`scripts/run_daily_calibration.py`) refreshes the updated stats CSV; reports (`reports/writer.py`) capture every live run.

## Project shape

```text
src/polytempo/
  weather/     # forecast ingestion, normalization, calibration pipelines
  markets/     # Polymarket fetching and bucket parsing
  model/       # calibration and probability distribution
  strategy/    # edge and deterministic decision rules (14 trade strategies)
  analysis.py   # local analysis use-case layer
  profiles/    # trading profiles (model × trade × lead gate) from YAML
  paper/       # simulated paper-trading ledger + bot pipeline
  collectors/  # Wunderground HTML collectors → Postgres snapshots
  storage/     # PostgreSQL access + schemas (weather + paper databases)
  cli/         # command entry points
  reports/     # Markdown run reports
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

Fetch Polymarket data and parse temperature bucket labels. **Gamma**
(`gamma-api.polymarket.com`) is used for *discovery* — event/bucket metadata,
settlement date, clob token ids, and resolution. Executable prices
(`yes_bid`/`yes_ask`/`spread`/`liquidity_usd`) come from the live **CLOB** order
book (`clob.polymarket.com` `POST /books`) via `hydrate_prices`, not Gamma's cached
snapshot fields, which lag the book and can quote a phantom price on an empty side.
Hydration is applied on decision paths only (`live`, `paper open`); pure
discovery/listing paths keep raw Gamma data.

### model/

Correct forecasts using calibration and convert them into bucket probabilities.

### strategy/

Compare model probabilities to executable market prices and apply BUY/SKIP rules.

### analysis.py

Local use-case layer that connects buckets, distribution, edge, and decision for analysis.

### profiles/

Trading profiles: every combination of model strategy × trade strategy ×
lead-time entry gate, generated from `config/paper_profiles.yaml`
(`load.py`). `registry.py` maps trade-strategy names to zero-arg factories;
YAML names are validated against it at load time.

### paper/

Record simulated trades and outcomes. No real trading. `ledger.py` stores
`OPEN`/`SETTLE` records per profile in PostgreSQL (`polytempo_paper`,
`PostgresLedgerStore`); balance and open positions are always derived by
replaying the per-profile event log from its $1000 start. Stake sizing is
`2%-5%` of the current balance, scaled linearly by model edge (`edge_pp ≤ 7`
→ 2%, `edge_pp ≥ 15` → 5%), unless the strategy supplies a flat
`stake_usd` (e.g. `mid_band`). `run.py` orchestrates settle → gate check →
dedupe → open per profile; `bot.py` is the always-on scheduler loop.

### collectors/

Continuous Wunderground HTML scraping (observations + hourly forecasts) into
the weather Postgres database on UTC wall-clock schedules.

### storage/

PostgreSQL connection helpers and schemas for the two databases:
`polytempo_weather` (collector snapshots + calibration store) and
`polytempo_paper` (profile ledgers, ticks, views).

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
