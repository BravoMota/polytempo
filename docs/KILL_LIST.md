# PolyTempo Kill List

Things we are intentionally not building yet:

- LLM agent
- Multi-agent workflows
- Live trading
- Automatic order placement
- Complex dashboard
- Browser automation
- Raw GRIB ingestion
- DWD MOSMIX
- ECMWF raw Open Data
- Advanced portfolio sizing
- Arbitrage engine
- Generic prediction-market framework

These may be considered later only after the deterministic core is proven.

Originally on this list, since built after the core was proven:

- Database — PostgreSQL `polytempo_weather` + `polytempo_paper`
- Real-time daemon / background scheduler — `scripts/run_paper_bot.py`, `scripts/run_collector.py`, `scripts/run_daily_calibration.py`
