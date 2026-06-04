"""Shared helpers for weather collectors."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo


def local_today(timezone_name: str, now_utc: datetime | None = None) -> date:
    """Return today's calendar date in the given IANA timezone."""
    now = now_utc or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("now_utc must be timezone-aware")
    return now.astimezone(ZoneInfo(timezone_name)).date()


def forecast_dates_for_station(
    timezone_name: str,
    now_utc: datetime | None = None,
) -> tuple[date, date]:
    """Return (today_local, tomorrow_local) for hourly forecast fetches."""
    today = local_today(timezone_name, now_utc)
    return today, today + timedelta(days=1)


def lead_hours_to_day_end(
    scraped_at_utc: datetime,
    target_date_local: date,
    timezone_name: str,
) -> float:
    """Hours from scraped_at_utc to end of target_date_local in station timezone."""
    if scraped_at_utc.tzinfo is None:
        raise ValueError("scraped_at_utc must be timezone-aware")
    tz = ZoneInfo(timezone_name)
    end_local = datetime.combine(
        target_date_local + timedelta(days=1),
        time.min,
        tzinfo=tz,
    )
    end_utc = end_local.astimezone(timezone.utc)
    scraped_utc = scraped_at_utc.astimezone(timezone.utc)
    return max(0.0, (end_utc - scraped_utc).total_seconds() / 3600.0)
