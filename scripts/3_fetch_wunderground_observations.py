#!/usr/bin/env python3
"""Fetch Wunderground daily Tmax observations for offline calibration.

Standalone, configured via the global vars below. Offline tooling only —
not used by live analysis. HTTP/parse logic lives in
``polytempo.weather.wunderground``.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from polytempo.weather.observations import (  # noqa: E402
    DEFAULT_OBSERVATIONS_PATH,
    write_observations_jsonl,
)
from polytempo.weather.wunderground import (  # noqa: E402
    fetch_station_country_code,
    fetch_wunderground_observations_range,
)

# --- magic vars (edit here) ---

STATION_ID = "EGLC"
START_DATE = date(2026, 2, 25)
END_DATE = date(2026, 5, 25)

OUT_PATH = DEFAULT_OBSERVATIONS_PATH


def main() -> int:
    with httpx.Client() as client:
        country_code = fetch_station_country_code(STATION_ID, client=client)
        observations = fetch_wunderground_observations_range(
            STATION_ID,
            START_DATE,
            END_DATE,
            client=client,
            country_code=country_code,
        )

    if not observations:
        print("no observations fetched; not writing output", file=sys.stderr)
        return 2

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text("", encoding="utf-8")
    write_observations_jsonl(observations, OUT_PATH)

    print(
        f"wrote {len(observations)} observations to {OUT_PATH} "
        f"(station={STATION_ID}, country={country_code}, "
        f"{START_DATE.isoformat()}..{END_DATE.isoformat()})",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
