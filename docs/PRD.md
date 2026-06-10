# PolyTempo PRD

## Goal

Build a deterministic system that analyzes Polymarket max-temperature markets and identifies possible edge.

The system should answer:

> Given weather forecasts and market prices, is any bucket mispriced enough to justify a trade?

Current progress: all plan phases (0-10) are complete — Polymarket ingestion (Gamma discovery + live CLOB order-book prices), Open-Meteo forecast ingestion, calibration (frozen + nightly-updated), edge/decision with 14 trade strategies (YES and NO sides), Markdown run reports, and profile-based paper trading: 378 profiles (3 model × 14 trade × 9 lead gates) with per-profile $1000 bankrolls in PostgreSQL, driven by an always-on bot.

## Non-goals

- No LLM agent
- No autonomous browsing
- No live trading
- No complex dashboard
- No over-engineered plugin system

## First supported market type

Polymarket highest-temperature markets with bucket outcomes such as:

- `23°C`
- `21°C - 22°C`
- `11°C or below`
- `25°C or higher`

## First product interface

A CLI command.

Example future command:

```bash
polytempo analyze --city madrid --date 2026-04-29
```

## Expected output

The system should eventually output:

- Market metadata
- Settlement station
- Forecast distribution
- Bucket probabilities
- Market prices
- Edge table
- Deterministic recommendation
- Skip reasons

## Current decision scope

- BUY_YES and BUY_NO (NO-side via `dist_arb`, `argmax_no`, `topk_no`, `max_edge`, `edge_band`, `book_arb`, `max_roi`, `dist_arb_tight`, `dist_arb_kelly`, `tail_fade`).
- Assume buy and hold until settlement (no active sell / take-profit yet).
- Spread is a warning only, not a hard blocker (except `dist_arb_tight`, which gates on it).
- Liquidity is a crude stale-quote filter; later prefer ask-side depth for the intended stake size.
