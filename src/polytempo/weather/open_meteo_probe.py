"""Open-Meteo API probe schedule and request helpers (demand-spike study)."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx

from polytempo.weather.open_meteo import DEFAULT_MODELS, fetch_daily_max
from polytempo.weather.stations import get_station

PROBE_SLOTS: tuple[tuple[int, str], ...] = (
    (0, "on_hour"),
    (5, "plus_5min"),
    (10, "plus_10min"),
)


@dataclass(frozen=True)
class ProbeSlot:
    instant: datetime
    hour_utc: int
    slot: str


def slot_for_instant(instant: datetime) -> ProbeSlot | None:
    """Return probe slot metadata when ``instant`` is a scheduled probe time (UTC)."""
    if instant.tzinfo is None:
        raise ValueError("instant must be timezone-aware")
    instant = instant.astimezone(timezone.utc).replace(second=0, microsecond=0)
    for minute, slot_name in PROBE_SLOTS:
        if instant.minute == minute and instant.second == 0:
            return ProbeSlot(instant=instant, hour_utc=instant.hour, slot=slot_name)
    return None


def next_probe_instant(now: datetime) -> datetime:
    """Next UTC probe instant (:00, :05, or :10) strictly after ``now``."""
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    now_utc = now.astimezone(timezone.utc)
    day_start = datetime.combine(now_utc.date(), datetime.min.time(), tzinfo=timezone.utc)

    candidates: list[datetime] = []
    for hour in range(24):
        for minute, _slot in PROBE_SLOTS:
            candidate = day_start + timedelta(hours=hour, minutes=minute)
            if candidate > now_utc:
                candidates.append(candidate)

    if candidates:
        return min(candidates)

    tomorrow = day_start + timedelta(days=1)
    return tomorrow


def probe_slot_key(slot: ProbeSlot) -> str:
    return f"{slot.instant.isoformat()}+{slot.slot}"


def _ts_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def run_probe(
    *,
    city: str,
    target_date: date,
    slot: ProbeSlot,
    models: tuple[str, ...] = DEFAULT_MODELS,
) -> dict:
    """Execute one probe request (no HTTP retries) and return a JSONL record."""
    station = get_station(city)
    started = time.perf_counter()
    record: dict = {
        "ts_utc": _ts_z(datetime.now(timezone.utc)),
        "hour_utc": slot.hour_utc,
        "slot": slot.slot,
        "target_date": target_date.isoformat(),
        "success": False,
        "status_code": None,
        "error_type": None,
        "error_message": None,
        "latency_ms": None,
        "models_count": len(models),
        "values_count": None,
    }

    try:
        forecast = fetch_daily_max(
            latitude=station.latitude,
            longitude=station.longitude,
            target_date=target_date,
            timezone=station.timezone,
            models=models,
            max_retries=1,
        )
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        record.update(
            {
                "success": True,
                "status_code": 200,
                "latency_ms": round(elapsed_ms, 1),
                "values_count": len(forecast.values_c),
            }
        )
    except httpx.HTTPStatusError as exc:
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        record.update(
            {
                "status_code": exc.response.status_code,
                "error_type": type(exc).__name__,
                "error_message": str(exc),
                "latency_ms": round(elapsed_ms, 1),
            }
        )
    except httpx.HTTPError as exc:
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        record.update(
            {
                "error_type": type(exc).__name__,
                "error_message": str(exc),
                "latency_ms": round(elapsed_ms, 1),
            }
        )
    except Exception as exc:
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        record.update(
            {
                "error_type": type(exc).__name__,
                "error_message": str(exc),
                "latency_ms": round(elapsed_ms, 1),
            }
        )

    return record


def append_jsonl(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, separators=(",", ":")) + "\n")
        fh.flush()
