"""Observed daily Tmax records for offline calibration joins."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from polytempo.weather.data_dir import WEATHER_DATA_DIR

DEFAULT_OBSERVATIONS_PATH = WEATHER_DATA_DIR / "observed_tmax.jsonl"


@dataclass(frozen=True)
class ObservedTmax:
    """Observed daily maximum temperature for one station and calendar day."""

    station_id: str
    target_date: date
    observed_tmax_c: float
    source: str


def parse_observation_records(records: list[dict[str, Any]]) -> list[ObservedTmax]:
    """Validate and parse raw observation dicts."""
    out: list[ObservedTmax] = []
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            raise ValueError(f"record {index} must be a dict")

        for key in ("station_id", "target_date", "observed_tmax_c", "source"):
            if key not in record:
                raise ValueError(f"record {index} missing required key {key!r}")

        observed_tmax_c = float(record["observed_tmax_c"])
        if not math.isfinite(observed_tmax_c):
            raise ValueError(f"record {index} observed_tmax_c must be finite")

        out.append(
            ObservedTmax(
                station_id=str(record["station_id"]),
                target_date=date.fromisoformat(str(record["target_date"])),
                observed_tmax_c=observed_tmax_c,
                source=str(record["source"]),
            )
        )
    return out


def _observation_to_dict(observation: ObservedTmax) -> dict[str, Any]:
    return {
        "station_id": observation.station_id,
        "target_date": observation.target_date.isoformat(),
        "observed_tmax_c": observation.observed_tmax_c,
        "source": observation.source,
    }


def _observation_from_dict(data: dict[str, Any]) -> ObservedTmax:
    return ObservedTmax(
        station_id=str(data["station_id"]),
        target_date=date.fromisoformat(str(data["target_date"])),
        observed_tmax_c=float(data["observed_tmax_c"]),
        source=str(data["source"]),
    )


def write_observations_jsonl(observations: list[ObservedTmax], path: Path) -> None:
    """Append observation records as JSONL."""
    if not observations:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for observation in observations:
            handle.write(json.dumps(_observation_to_dict(observation), sort_keys=True))
            handle.write("\n")


def read_observations_jsonl(path: Path) -> list[ObservedTmax]:
    """Read all observations; missing file returns empty list."""
    if not path.exists():
        return []

    observations: list[ObservedTmax] = []
    with path.open(encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                data = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSON on line {line_no} of {path}") from exc
            if not isinstance(data, dict):
                raise ValueError(f"expected object on line {line_no} of {path}")
            observations.append(_observation_from_dict(data))
    return observations
