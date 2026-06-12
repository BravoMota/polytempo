"""Weather data collectors."""

from __future__ import annotations

from polytempo.collectors import open_meteo, wunderground

COLLECTORS = {
    "wunderground": wunderground.run_cycle,
    "open_meteo": open_meteo.run_cycle,
}
