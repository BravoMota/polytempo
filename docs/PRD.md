# PolyTempo PRD

## Goal

Build a deterministic system that analyzes Polymarket max-temperature markets and identifies possible edge.

The system should answer:

> Given weather forecasts and market prices, is any bucket mispriced enough to justify a trade?

## Non-goals

- No LLM agent
- No autonomous browsing
- No live trading
- No complex dashboard
- No database at first
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
