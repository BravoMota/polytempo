"""WU forecast calibration: o'clock scrapes, adjusted Tmax, 1h lead buckets."""

from __future__ import annotations

import math
import sys
from dataclasses import dataclass
from datetime import date, datetime
from datetime import timezone as dt_timezone
from pathlib import Path

from psycopg import Connection

from polytempo.model.lead_time import lead_hours_to_end_of_target_day
from polytempo.weather.calibration_compute import ForecastRecord
from polytempo.weather.calibration_storage import (
    WU_FORECAST_MODEL,
    WuHistoryObservationRow,
    load_wu_history_observations_for_days,
    parse_run_time_utc_value,
    upsert_forecast_record,
)


@dataclass(frozen=True)
class ForecastHourRow:
    """One hourly forecast temperature from a collector snapshot group."""

    target_time_utc: datetime
    temp_c: float


def is_oclock_scrape(scraped_at_utc: datetime) -> bool:
    """True when scrape time is on the hour (minute == 0)."""
    return scraped_at_utc.minute == 0


def normalize_scrape_time(scraped_at_utc: datetime) -> datetime:
    """Zero sub-minute components for stable run_time keys."""
    scraped = scraped_at_utc.astimezone(dt_timezone.utc)
    return scraped.replace(second=0, microsecond=0)


def bucket_wall_clock_lead_hours(lead: float, *, max_hours: int = 60) -> int | None:
    """Floor wall-clock lead to an integer hour bucket in ``[0, max_hours]``."""
    bucket = int(math.floor(lead))
    if bucket < 0 or bucket > max_hours:
        return None
    return bucket


def _parse_snapshot_time(value: str) -> datetime:
    parsed = parse_run_time_utc_value(value)
    if parsed is None:
        raise ValueError(f"invalid snapshot timestamp: {value!r}")
    return parsed


def observed_running_max_c(
    history_observations: list[WuHistoryObservationRow],
    *,
    target_date: date,
    as_of_utc: datetime,
) -> float | None:
    """Max observed temp for ``target_date`` at or before ``as_of_utc``."""
    as_of = as_of_utc.astimezone(dt_timezone.utc)
    obs_temps = [
        row.temp_c
        for row in history_observations
        if row.target_date == target_date and row.observed_at_utc <= as_of
    ]
    return max(obs_temps) if obs_temps else None


def adjusted_predicted_tmax_c(
    *,
    target_date: date,
    as_of_utc: datetime,
    hourly_forecast_rows: list[ForecastHourRow],
    history_observations: list[WuHistoryObservationRow],
) -> float | None:
    """Combine observed-so-far with remaining hourly forecast for daily Tmax."""
    as_of = as_of_utc.astimezone(dt_timezone.utc)

    max_obs = observed_running_max_c(
        history_observations,
        target_date=target_date,
        as_of_utc=as_of,
    )
    fc_temps = [
        row.temp_c
        for row in hourly_forecast_rows
        if row.target_time_utc > as_of
    ]
    max_fc = max(fc_temps) if fc_temps else None

    if max_obs is not None and max_fc is not None:
        return max(max_obs, max_fc)
    if max_obs is not None:
        return max_obs
    if max_fc is not None:
        return max_fc
    return None


@dataclass(frozen=True)
class ForecastSnapshotGroup:
    """Hourly forecast rows for one o'clock scrape and target day."""

    station_id: str
    scraped_at_utc: datetime
    target_date: date
    hourly_rows: list[ForecastHourRow]
    forecast_lat: float | None = None
    forecast_lon: float | None = None


def _group_forecast_snapshots(
    rows: list[dict[str, object]],
) -> list[ForecastSnapshotGroup]:
    hourly: dict[tuple[str, datetime, date], list[ForecastHourRow]] = {}
    meta: dict[tuple[str, datetime, date], tuple[float | None, float | None]] = {}
    for row in rows:
        scraped = _parse_snapshot_time(str(row["scraped_at_utc"]))
        if not is_oclock_scrape(scraped):
            continue
        target_date = date.fromisoformat(str(row["target_date_local"]))
        target_time_raw = row.get("target_time_utc")
        temp_c = row.get("temp_c")
        if target_time_raw is None or temp_c is None:
            continue
        scraped_norm = normalize_scrape_time(scraped)
        key = (str(row["station_id"]), scraped_norm, target_date)
        hourly.setdefault(key, []).append(
            ForecastHourRow(
                target_time_utc=_parse_snapshot_time(str(target_time_raw)),
                temp_c=float(temp_c),
            )
        )
        if key not in meta:
            lat_raw = row.get("requested_lat")
            lon_raw = row.get("requested_lon")
            meta[key] = (
                float(lat_raw) if lat_raw is not None else None,
                float(lon_raw) if lon_raw is not None else None,
            )
    return [
        ForecastSnapshotGroup(
            station_id=station_id,
            scraped_at_utc=scraped_at,
            target_date=target_date,
            hourly_rows=hours,
            forecast_lat=meta[(station_id, scraped_at, target_date)][0],
            forecast_lon=meta[(station_id, scraped_at, target_date)][1],
        )
        for (station_id, scraped_at, target_date), hours in hourly.items()
    ]


def ingest_wu_forecasts_from_snapshots(
    conn: Connection,
    *,
    station_ids: list[str],
    scraped_since_utc: datetime,
    max_lead_hours: int = 60,
    history_obs_by_day: dict[tuple[str, date], list[WuHistoryObservationRow]] | None = None,
) -> int:
    """Build WU calibration forecast records from o'clock ``forecast_snapshots``."""
    if not station_ids:
        return 0

    since_text = scraped_since_utc.astimezone(dt_timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = conn.execute(
        """
        SELECT station_id, scraped_at_utc, target_date_local, target_time_utc, temp_c,
               requested_lat, requested_lon
        FROM forecast_snapshots
        WHERE source = 'wunderground'
          AND station_id = ANY(%s)
          AND scraped_at_utc >= %s
        ORDER BY station_id, scraped_at_utc, target_date_local, target_time_utc
        """,
        (station_ids, since_text),
    ).fetchall()

    print(
        f"wu forecasts: loaded {len(rows)} snapshot rows from DB",
        file=sys.stderr,
        flush=True,
    )
    groups = _group_forecast_snapshots([dict(row) for row in rows])
    if not groups:
        return 0

    print(
        f"wu forecasts: processing {len(groups)} o'clock scrape groups",
        file=sys.stderr,
        flush=True,
    )

    if history_obs_by_day is None:
        target_dates = sorted({group.target_date for group in groups})
        history_obs_by_day = load_wu_history_observations_for_days(
            conn,
            station_ids=station_ids,
            target_dates=target_dates,
        )

    count = 0
    total_groups = len(groups)
    for group in sorted(groups, key=lambda g: (g.station_id, g.scraped_at_utc, g.target_date)):
        lead_raw = lead_hours_to_end_of_target_day(group.target_date, now=group.scraped_at_utc)
        lead_bucket = bucket_wall_clock_lead_hours(lead_raw, max_hours=max_lead_hours)
        if lead_bucket is None:
            continue

        history_obs = history_obs_by_day.get((group.station_id, group.target_date), [])
        predicted = adjusted_predicted_tmax_c(
            target_date=group.target_date,
            as_of_utc=group.scraped_at_utc,
            hourly_forecast_rows=group.hourly_rows,
            history_observations=history_obs,
        )
        if predicted is None:
            continue

        record = ForecastRecord(
            station_id=group.station_id,
            model=WU_FORECAST_MODEL,
            run_time_utc=group.scraped_at_utc,
            target_date=group.target_date,
            lead_hours=float(lead_bucket),
            predicted_tmax_c=predicted,
            forecast_lat=group.forecast_lat,
            forecast_lon=group.forecast_lon,
        )
        upsert_forecast_record(conn, record)
        count += 1
        if count % 100 == 0:
            print(
                f"wu forecasts: {count}/{total_groups} groups ingested",
                file=sys.stderr,
                flush=True,
            )
    if count:
        print(
            f"wu forecasts: done ({count}/{total_groups} groups ingested)",
            file=sys.stderr,
            flush=True,
        )
    return count


def min_wu_forecast_snapshot_time(conn: Connection, station_ids: list[str]) -> datetime | None:
    """Return earliest ``forecast_snapshots.scraped_at_utc`` for WU, if any."""
    if not station_ids:
        return None
    row = conn.execute(
        """
        SELECT MIN(scraped_at_utc) AS min_scraped
        FROM forecast_snapshots
        WHERE source = 'wunderground' AND station_id = ANY(%s)
        """,
        (station_ids,),
    ).fetchone()
    if row is None or row["min_scraped"] is None:
        return None
    return _parse_snapshot_time(str(row["min_scraped"]))
