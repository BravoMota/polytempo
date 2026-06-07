"""Lead-time helpers shared by live trading, paper bot, and calibration."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone


def end_of_target_day_utc(target: date) -> datetime:
    """UTC instant at the end of ``target`` (midnight UTC on the following day)."""
    return datetime.combine(target + timedelta(days=1), time.min, tzinfo=timezone.utc)


def lead_hours_to_end_of_target_day(
    target: date,
    now: datetime | None = None,
) -> float:
    """Hours from ``now`` (UTC) until UTC midnight at the END of ``target``.

    Anchor: ``datetime.combine(target + 1 day, 00:00 UTC)``. Matches the offline
    calibration pipeline ``compute_lead_hours`` and ``calibration_stats.csv``
    row indexing. Clamps to ``0.0`` when ``now`` is past end-of-target.
    """
    end_of_target = datetime.combine(
        target + timedelta(days=1), time.min, tzinfo=timezone.utc
    )
    now = now if now is not None else datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    return max(0.0, (end_of_target - now.astimezone(timezone.utc)).total_seconds() / 3600.0)


def gate_target_utc(target: date, target_lead_hours: float) -> datetime:
    """UTC instant when lead hours equals ``target_lead_hours`` before end of ``target``."""
    end_of_target = datetime.combine(
        target + timedelta(days=1), time.min, tzinfo=timezone.utc
    )
    return end_of_target - timedelta(hours=target_lead_hours)


def gate_open_utc(target: date, target_lead_hours: float) -> datetime:
    """Alias for :func:`gate_target_utc` (legacy name)."""
    return gate_target_utc(target, target_lead_hours)


def gate_close_utc(target: date, min_lead_hours: float) -> datetime:
    """UTC instant when lead hours drops to ``min_lead_hours`` (gate closes)."""
    end_of_target = datetime.combine(
        target + timedelta(days=1), time.min, tzinfo=timezone.utc
    )
    return end_of_target - timedelta(hours=min_lead_hours)


def lead_hours_at_target(
    lead_hours: float,
    target_lead_hours: float,
    tolerance_seconds: float = 90.0,
) -> bool:
    """True when ``lead_hours`` is within ``tolerance_seconds`` of ``target_lead_hours``."""
    tolerance_hours = tolerance_seconds / 3600.0
    return abs(lead_hours - target_lead_hours) <= tolerance_hours


def lead_hours_before_target(
    lead_hours: float,
    target_lead_hours: float,
    tolerance_seconds: float = 90.0,
) -> bool:
    """True when still early — target open time has not passed (beyond tolerance)."""
    tolerance_hours = tolerance_seconds / 3600.0
    return lead_hours > target_lead_hours + tolerance_hours


def lead_hours_missed_target(
    lead_hours: float,
    target_lead_hours: float,
    tolerance_seconds: float = 90.0,
) -> bool:
    """True when the target instant has passed and this cycle was missed."""
    tolerance_hours = tolerance_seconds / 3600.0
    return lead_hours < target_lead_hours - tolerance_hours
