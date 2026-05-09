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
Phase 4.5 planned: local analysis/use-case layer
```

Current scope:

```text
No APIs yet.
No Polymarket/Gamma API yet.
No forecast API yet.
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

No weather API yet. Use fake distribution.

---

## Phase 7 — Open-Meteo ingestion

**Goal:** fetch real forecast data.

Build:

```text
weather/open_meteo.py
weather/schema.py
```

Output normalized forecast values.

No trading decision inside the weather module.

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
