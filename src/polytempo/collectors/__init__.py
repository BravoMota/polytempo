"""Weather data collectors."""

from __future__ import annotations

from polytempo.collectors import wunderground

COLLECTORS = {
    "wunderground": wunderground.run_cycle,
}
