"""UTC wall-clock scheduling for weather collectors."""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone

DEFAULT_ANCHOR_TIME_UTC = "00:00"


def parse_anchor_time_utc(value: str) -> time:
    """Parse HH:MM or HH:MM:SS anchor time (UTC wall clock)."""
    text = str(value).strip()
    if text.count(":") == 2:
        hour, minute, second = (int(part) for part in text.split(":"))
        if not (0 <= hour <= 23 and 0 <= minute <= 59 and 0 <= second <= 59):
            raise ValueError(f"anchor time out of range: {value!r}")
        return time(hour=hour, minute=minute, second=second)
    if text.count(":") == 1:
        hour, minute = (int(part) for part in text.split(":"))
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError(f"anchor time out of range: {value!r}")
        return time(hour=hour, minute=minute)
    raise ValueError(f"anchor time must be HH:MM or HH:MM:SS, got {value!r}")


def _anchor_offset_seconds(anchor_time_utc: str) -> int:
    anchor = parse_anchor_time_utc(anchor_time_utc)
    return anchor.hour * 3600 + anchor.minute * 60 + anchor.second


def _utc_midnight(dt: datetime) -> datetime:
    dt = dt.astimezone(timezone.utc)
    return dt.replace(hour=0, minute=0, second=0, microsecond=0)


def slot_for_instant(
    instant_utc: datetime,
    interval_seconds: int,
    anchor_time_utc: str,
) -> datetime:
    """Return the aligned UTC slot start at or before instant_utc."""
    if instant_utc.tzinfo is None:
        raise ValueError("instant_utc must be timezone-aware")
    instant = instant_utc.astimezone(timezone.utc)
    anchor_offset = _anchor_offset_seconds(anchor_time_utc)
    day_start = _utc_midnight(instant)
    elapsed = int((instant - day_start).total_seconds())
    if elapsed < anchor_offset:
        day_start -= timedelta(days=1)
        elapsed += 86400
    slot_index = (elapsed - anchor_offset) // interval_seconds
    return day_start + timedelta(seconds=anchor_offset + slot_index * interval_seconds)


def next_scheduled_instant_utc(
    now_utc: datetime,
    interval_seconds: int,
    anchor_time_utc: str,
) -> datetime:
    """Return the earliest aligned UTC slot strictly after now_utc."""
    current = slot_for_instant(now_utc, interval_seconds, anchor_time_utc)
    if current <= now_utc.astimezone(timezone.utc):
        return current + timedelta(seconds=interval_seconds)
    return current


def is_slot_due(
    now_utc: datetime,
    interval_seconds: int,
    anchor_time_utc: str,
    last_run_slot: datetime | None,
) -> tuple[bool, datetime]:
    """Return (due, current_slot_utc) for the active aligned slot."""
    slot = slot_for_instant(now_utc, interval_seconds, anchor_time_utc)
    if last_run_slot is not None and slot <= last_run_slot.astimezone(timezone.utc):
        return False, slot
    return True, slot
