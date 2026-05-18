# PolyTempo Development Plan

This plan is intentionally small and deterministic. The goal is to avoid uncontrolled vibe coding and rebuild the project one clean phase at a time.

## Core principle

```text
Deterministic engine decides.
LLM explains or audits later.
```

Do not add LLM agents, live trading, dashboards, databases, or background schedulers until the deterministic core is proven.

---

## Current progress

```text
Phase 0 complete: skeleton, docs, Cursor rules
Phase 1 complete: temperature bucket parsing
Phase 2 complete: distribution math from forecast values
Phase 3 complete: YES-side edge calculation
Phase 4 complete: deterministic BUY_YES/SKIP decision rules
Phase 4.5 complete: local analysis/use-case layer
Phase 5 complete: first CLI demo (polytempo demo)
Phase 6 complete: Polymarket/Gamma market ingestion (markets/polymarket.py)
Phase 7 complete: Open-Meteo daily-max forecast ingestion (weather/open_meteo.py)
Phase 8 complete: manual forecast calibration (model/calibration.py) + weather/schema.py
Phase 9 complete: paper trading ledger (paper/ledger.py) + `polytempo paper {open,settle,status,list-london}` CLI
```

Current scope:

```text
Polymarket/Gamma ingestion: implemented (HTTP client + payload parsing).
Open-Meteo ingestion: implemented (multi-model daily max temperature).
weather/schema.py: ForecastValues (calibration + analyze_event). DailyMaxForecast.to_forecast_values() bridges fetch → schema.
analyze_event: Polymarket event + ForecastValues + optional CalibrationRule → AnalysisResult.
paper/ledger.py: append-only JSONL of OPEN/SETTLE records. $1000 demo balance. Stake 2-5% of balance, linear ramp on edge_pp (2% at 7pp → 5% at >=15pp). London-only entry-point for now.
Opt-in live smoke: tests/test_pipeline.py (POLYTEMPO_RUN_LIVE_API_TESTS=1).
Next plan phase: Phase 10 reports (reports/writer.py).
No LLM agent.
No dashboard.
No live trading.
BUY_YES only for now.
NO-side edge is intentionally deferred.
```

---

## Phase 0 — Skeleton and rules

**Goal:** create the repo safely.

Do:

```text
1. Create project skeleton
2. Add README + docs
3. Add .cursor/rules/project.mdc
4. Add empty module files
5. Add pyproject.toml
6. Run pytest once
7. Commit
```

Commit:

```bash
git add .
git commit -m "chore: create PolyTempo skeleton"
```

---

## Phase 1 — Bucket parsing

**Goal:** understand Polymarket temperature bucket labels.

Build:

```text
markets/buckets.py
```

Support:

```text
"23°C"
"21°C - 22°C"
"11°C or below"
"25°C or higher"
```

Output:

```text
numeric lower/upper temperature interval
```

Tests:

```text
tests/test_buckets.py
```

---

## Phase 2 — Distribution math

**Goal:** convert forecast temperature distribution into bucket probabilities.

Build:

```text
model/distribution.py
```

Input:

```text
mu_c
sigma_c
bucket intervals
```

Output:

```text
P(each bucket)
```

Tests:

```text
tests/test_distribution.py
```

---

## Phase 3 — Edge calculation

**Goal:** compare our probability vs market price.

Build:

```text
strategy/edge.py
```

Input:

```text
bucket probability
YES ask price
YES bid price
spread
liquidity
```

Output:

```text
edge_pp
```

Important:

```text
Use ask price for buying YES, not chart percentage.
```

Tests:

```text
tests/test_edge.py
```

---

## Phase 4 — Deterministic decision rules

**Goal:** strict BUY/SKIP.

Build:

```text
strategy/decision.py
```

Initial rules:

```text
BUY_YES only if:
- edge >= 7 percentage points
- liquidity >= $100
otherwise SKIP
```

Spread is a warning only, not a hard blocker, because the product currently assumes BUY_YES and hold until settlement.
Liquidity is a crude temporary proxy; later prefer ask-side depth for the intended stake size.

Tests:

```text
tests/test_decision.py
```

## Phase 4.5 — Local analysis/use-case layer

**Goal:** assemble existing modules into one end-to-end local analysis using fake/local inputs.

Build:

```text
src/polytempo/analysis.py
tests/test_analysis.py
```

Do:

```text
parse bucket labels
build distribution
compute bucket probabilities
convert to ProbabilityQuote
calculate edges
apply decision rules
return AnalysisResult
```

This layer connects buckets + distribution + edge + decision. Do not call it an agent or orchestrator.

## Phase 5 — First CLI demo

**Goal:** prove end-to-end without APIs.

Build:

```text
cli/main.py
```

Command:

```bash
polytempo demo
```

It uses fake data and prints:

```text
bucket probabilities
market prices
edge table
recommendation
```

---

## Phase 6 — Polymarket ingestion

**Goal:** fetch real market buckets and prices.

Build:

```text
markets/polymarket.py
```

Fetch:

```text
event title
bucket labels
YES ask/bid
spread
liquidity
rules
```

**Status:** complete (`markets/polymarket.py`). Forecast and calibration layers build on this (Phases 7–8).

---

## Phase 7 — Open-Meteo ingestion

**Goal:** fetch real forecast data.

Build:

```text
weather/open_meteo.py
```

Output starts as `DailyMaxForecast` (multi-model `values_c` for the target date).
`weather/schema.py` defines `ForecastValues` for calibration/analysis; use
`DailyMaxForecast.to_forecast_values()` at the boundary.

Also includes `weather/stations.py`: a versioned contract-station registry
(London/EGLC, Madrid/LEMD, Amsterdam/EHAM, Warsaw/EPWA, Paris/LFPB, Milan/LIMC)
with ICAO, lat/lon, and IANA timezone. `fetch_for_station(station, date)` uses
the registry directly. Forecast requests pass explicit `temperature_unit=celsius`
and `timezone`, and parsed values are rejected if outside [-40, 60] °C.

No trading decision inside the weather module.

**Status:** complete. Next: Phase 9 paper ledger (and product wiring such as event-specific location/date for live pipeline).

---

## Phase 8 — Calibration

**Goal:** correct forecast bias.

Build:

```text
model/calibration.py
```

Start simple:

```text
corrected_temperature = raw_temperature - known_bias
```

Later:

```text
learn bias from forecast vs settlement history
```

Calibration happens before distribution building:

```text
raw forecasts
  -> calibration
  -> corrected forecasts
  -> distribution
  -> bucket probabilities
```

**Status:** complete (`model/calibration.py`). Wired through `analyze_event` on `ForecastValues`. Next: Phase 9 paper ledger.

---

## Phase 9 — Paper ledger

**Goal:** track simulated trades.

Build:

```text
paper/ledger.py
```

Store:

```text
OPEN
CHECK
SETTLE
CANCEL
```

Use an append-only JSONL file.

**Status:** complete (`paper/ledger.py`). $1000 starting balance, stake 2-5% of
live balance per BUY_YES bucket, scaled linearly by edge (floor 7pp → 2%,
ceiling 15pp → 5%). Records are append-only JSONL with `OPEN` and `SETTLE`
events; balance is always derived by replaying the log. Sizing reads the
current balance before each batch and decrements it sequentially so multiple
buys in the same event do not over-stake. YES shares pay $1 each when the
bucket label matches the winner, $0 otherwise. CLI: `polytempo paper open`,
`polytempo paper settle --winner "<label>"`, `polytempo paper status`,
`polytempo paper list-london`. London-only for now (the open command pulls
from `weather/stations.py` `london`). NO-side and auto-resolution from the
Gamma payload are deferred.

---

## Phase 10 — Reports

**Goal:** inspect outputs easily.

Build:

```text
reports/writer.py
```

Write:

```text
reports/latest_analysis.json
reports/latest_analysis.md
```

---

## Phase 11 — Optional LLM audit

Only consider this after the deterministic system works.

The LLM should not be the decision maker.

Allowed role:

```text
auditor / explainer / report summarizer
```

Forbidden role:

```text
autonomous trader / probability inventor / web-browsing decision maker
```

---

## Development rules

- One phase at a time.
- One task, one diff, one test.
- Commit after each phase.
- Do not implement future phases early.
- Do not add dependencies without asking.
- Do not add architecture layers unless the current phase needs them.
- Keep the project boring, local, and testable.
