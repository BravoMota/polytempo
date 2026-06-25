"""PostgreSQL storage for the updated calibration pipeline."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, datetime
from datetime import timezone as dt_timezone

from psycopg import Connection

from polytempo.storage.postgres import (
    get_calibration_job_state,
    upsert_calibration_forecast_record,
    upsert_calibration_job_state,
    upsert_calibration_observed_tmax,
    upsert_wu_history_daily_observation,
    utc_now_iso,
)
from polytempo.weather.calibration_compute import ForecastRecord
from polytempo.weather.observations import CalibrationObservedTmax

logger = logging.getLogger(__name__)

_DUPLICATE_SECONDS_BEFORE_OFFSET = re.compile(
    r"(\d{2}:\d{2}:\d{2}):00([+Z])"
)

DEFAULT_JOB_NAME = "daily_calibration"
WU_CALIBRATION_JOB_NAME = "wu_calibration"
WU_FORECAST_MODEL = "wunderground"


@dataclass(frozen=True)
class WuHistoryObservationRow:
    """One hourly observation from a WU history/daily page (metric °C)."""

    station_id: str
    target_date: date
    observed_at_utc: datetime
    observed_at_local: str | None
    temp_c: float


def _format_run_time_utc(value: datetime) -> str:
    return value.astimezone(dt_timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_run_time_utc_value(value: datetime | str | object) -> datetime | None:
    """Parse ``run_time_utc`` / snapshot timestamps from DB text or datetime."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=dt_timezone.utc)
        return value.astimezone(dt_timezone.utc)

    text = str(value).strip()
    if not text:
        return None

    text = text.replace("Z", "+00:00")
    if " " in text and "T" not in text:
        text = text.replace(" ", "T", 1)

    for candidate in (text, _DUPLICATE_SECONDS_BEFORE_OFFSET.sub(r"\1\2", text)):
        try:
            parsed = datetime.fromisoformat(candidate)
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt_timezone.utc)
        return parsed.astimezone(dt_timezone.utc)
    return None


def upsert_observation(conn: Connection, row: CalibrationObservedTmax) -> None:
    upsert_calibration_observed_tmax(
        conn,
        station_id=row.station_id,
        target_date=row.target_date.isoformat(),
        observed_tmax_f=row.observed_tmax_f,
        observed_tmax_c=row.observed_tmax_c,
        source=row.source,
        fetched_at_utc=utc_now_iso(),
    )


def upsert_forecast_record(
    conn: Connection,
    record: ForecastRecord,
    *,
    raw_file_path: str | None = None,
) -> None:
    upsert_calibration_forecast_record(
        conn,
        station_id=record.station_id,
        model=record.model,
        run_time_utc=_format_run_time_utc(record.run_time_utc),
        target_date=record.target_date.isoformat(),
        lead_hours=record.lead_hours,
        predicted_tmax_c=record.predicted_tmax_c,
        forecast_lat=record.forecast_lat,
        forecast_lon=record.forecast_lon,
        raw_file_path=raw_file_path,
        ingested_at_utc=utc_now_iso(),
    )


def load_observations_map(conn: Connection) -> dict[tuple[str, date], float]:
    rows = conn.execute(
        """
        SELECT station_id, target_date, observed_tmax_c
        FROM calibration_observed_tmax
        ORDER BY station_id, target_date
        """
    ).fetchall()
    out: dict[tuple[str, date], float] = {}
    for row in rows:
        out[(str(row["station_id"]), date.fromisoformat(str(row["target_date"])))] = float(
            row["observed_tmax_c"]
        )
    return out


def load_forecast_records(
    conn: Connection,
    *,
    model: str | None = None,
    exclude_models: tuple[str, ...] = (),
) -> list[ForecastRecord]:
    if model is not None:
        rows = conn.execute(
            """
            SELECT station_id, model, run_time_utc, target_date, lead_hours,
                   predicted_tmax_c, forecast_lat, forecast_lon
            FROM calibration_forecast_records
            WHERE model = %s
            ORDER BY station_id, model, run_time_utc, target_date
            """,
            (model,),
        ).fetchall()
    elif exclude_models:
        rows = conn.execute(
            """
            SELECT station_id, model, run_time_utc, target_date, lead_hours,
                   predicted_tmax_c, forecast_lat, forecast_lon
            FROM calibration_forecast_records
            WHERE model <> ALL(%s)
            ORDER BY station_id, model, run_time_utc, target_date
            """,
            (list(exclude_models),),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT station_id, model, run_time_utc, target_date, lead_hours,
                   predicted_tmax_c, forecast_lat, forecast_lon
            FROM calibration_forecast_records
            ORDER BY station_id, model, run_time_utc, target_date
            """
        ).fetchall()
    records: list[ForecastRecord] = []
    for row in rows:
        run_time = parse_run_time_utc_value(row["run_time_utc"])
        if run_time is None:
            logger.warning(
                "skipping calibration_forecast_records row with invalid run_time_utc=%r "
                "(station=%s model=%s target_date=%s)",
                row["run_time_utc"],
                row["station_id"],
                row["model"],
                row["target_date"],
            )
            continue
        records.append(
            ForecastRecord(
                station_id=str(row["station_id"]),
                model=str(row["model"]),
                run_time_utc=run_time,
                target_date=date.fromisoformat(str(row["target_date"])),
                lead_hours=float(row["lead_hours"]),
                predicted_tmax_c=float(row["predicted_tmax_c"]),
                forecast_lat=float(row["forecast_lat"])
                if row["forecast_lat"] is not None
                else None,
                forecast_lon=float(row["forecast_lon"])
                if row["forecast_lon"] is not None
                else None,
            )
        )
    return records


def upsert_wu_history_observation(
    conn: Connection,
    row: WuHistoryObservationRow,
    *,
    raw_file_path: str | None = None,
) -> None:
    upsert_wu_history_daily_observation(
        conn,
        station_id=row.station_id,
        target_date=row.target_date.isoformat(),
        observed_at_utc=_format_run_time_utc(row.observed_at_utc),
        observed_at_local=row.observed_at_local,
        temp_c=row.temp_c,
        fetched_at_utc=utc_now_iso(),
        raw_file_path=raw_file_path,
    )


def load_wu_history_observations_for_day(
    conn: Connection,
    *,
    station_id: str,
    target_date: date,
) -> list[WuHistoryObservationRow]:
    rows = conn.execute(
        """
        SELECT station_id, target_date, observed_at_utc, observed_at_local, temp_c
        FROM wu_history_daily_observations
        WHERE station_id = %s AND target_date = %s
        ORDER BY observed_at_utc
        """,
        (station_id, target_date.isoformat()),
    ).fetchall()
    out: list[WuHistoryObservationRow] = []
    for row in rows:
        observed_at = parse_run_time_utc_value(row["observed_at_utc"])
        if observed_at is None:
            logger.warning(
                "skipping wu_history_daily_observations row with invalid observed_at_utc=%r",
                row["observed_at_utc"],
            )
            continue
        out.append(
            WuHistoryObservationRow(
                station_id=str(row["station_id"]),
                target_date=date.fromisoformat(str(row["target_date"])),
                observed_at_utc=observed_at,
                observed_at_local=str(row["observed_at_local"])
                if row["observed_at_local"] is not None
                else None,
                temp_c=float(row["temp_c"]),
            )
        )
    return out


def load_wu_history_observations_for_days(
    conn: Connection,
    *,
    station_ids: list[str],
    target_dates: list[date],
) -> dict[tuple[str, date], list[WuHistoryObservationRow]]:
    if not station_ids or not target_dates:
        return {}
    rows = conn.execute(
        """
        SELECT station_id, target_date, observed_at_utc, observed_at_local, temp_c
        FROM wu_history_daily_observations
        WHERE station_id = ANY(%s) AND target_date = ANY(%s)
        ORDER BY station_id, target_date, observed_at_utc
        """,
        (station_ids, [d.isoformat() for d in target_dates]),
    ).fetchall()
    out: dict[tuple[str, date], list[WuHistoryObservationRow]] = {}
    for row in rows:
        observed_at = parse_run_time_utc_value(row["observed_at_utc"])
        if observed_at is None:
            logger.warning(
                "skipping wu_history_daily_observations row with invalid observed_at_utc=%r",
                row["observed_at_utc"],
            )
            continue
        station_id = str(row["station_id"])
        target = date.fromisoformat(str(row["target_date"]))
        key = (station_id, target)
        out.setdefault(key, []).append(
            WuHistoryObservationRow(
                station_id=station_id,
                target_date=target,
                observed_at_utc=observed_at,
                observed_at_local=str(row["observed_at_local"])
                if row["observed_at_local"] is not None
                else None,
                temp_c=float(row["temp_c"]),
            )
        )
    return out


def read_job_state(conn: Connection, job_name: str = DEFAULT_JOB_NAME) -> dict[str, object] | None:
    return get_calibration_job_state(conn, job_name)


def record_job_success(
    conn: Connection,
    *,
    job_name: str = DEFAULT_JOB_NAME,
    last_target_date: date,
) -> None:
    now = utc_now_iso()
    upsert_calibration_job_state(
        conn,
        job_name=job_name,
        updated_at_utc=now,
        last_success_at_utc=now,
        last_target_date=last_target_date.isoformat(),
        last_error_at_utc=None,
        last_error_message=None,
    )


def record_job_error(
    conn: Connection,
    *,
    job_name: str = DEFAULT_JOB_NAME,
    error_message: str,
) -> None:
    upsert_calibration_job_state(
        conn,
        job_name=job_name,
        updated_at_utc=utc_now_iso(),
        last_error_at_utc=utc_now_iso(),
        last_error_message=error_message,
    )
