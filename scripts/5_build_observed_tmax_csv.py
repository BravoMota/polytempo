#!/usr/bin/env python3
"""Convert observed Tmax JSONL to CSV for offline calibration joins.

Reads ``data/weather/observed_tmax.jsonl`` and writes
``data/weather/observed_tmax.csv``. Offline tooling only.
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from polytempo.weather.observations import (  # noqa: E402
    DEFAULT_OBSERVATIONS_PATH,
    ObservedTmax,
    read_observations_jsonl,
)

# --- magic vars (edit here) ---

IN_PATH = DEFAULT_OBSERVATIONS_PATH
OUT_PATH = DEFAULT_OBSERVATIONS_PATH.with_suffix(".csv")

OBSERVED_TMAX_COLUMNS = (
    "station_id",
    "target_date",
    "observed_tmax_c",
    "source",
)


def write_observed_tmax_csv(observations: list[ObservedTmax], path: Path) -> None:
    """Write observation rows to CSV with a stable column order."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=OBSERVED_TMAX_COLUMNS)
        writer.writeheader()
        for row in observations:
            writer.writerow(
                {
                    "station_id": row.station_id,
                    "target_date": row.target_date.isoformat(),
                    "observed_tmax_c": row.observed_tmax_c,
                    "source": row.source,
                }
            )


def load_observed_tmax_rows(in_path: Path) -> list[ObservedTmax]:
    """Read and sort observation rows from JSONL."""
    observations = read_observations_jsonl(in_path)
    observations.sort(key=lambda row: (row.station_id, row.target_date))
    return observations


def build_observed_tmax_csv(in_path: Path, out_path: Path) -> int:
    """Read JSONL and write CSV. Returns number of rows written."""
    observations = load_observed_tmax_rows(in_path)
    write_observed_tmax_csv(observations, out_path)
    return len(observations)


def main() -> int:
    observations = load_observed_tmax_rows(IN_PATH)
    if not observations:
        print("no observations found; not writing output", file=sys.stderr)
        return 2

    write_observed_tmax_csv(observations, OUT_PATH)
    print(f"wrote {len(observations)} rows to {OUT_PATH}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
