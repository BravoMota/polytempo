"""Weather data collectors."""

from __future__ import annotations

from polytempo.collectors import open_meteo, polymarket_clob, wunderground

COLLECTORS = {
    "wunderground": wunderground.run_cycle,
    "open_meteo": open_meteo.run_cycle,
    "polymarket_clob": polymarket_clob.run_cycle,
}
