"""PostgreSQL storage for the updated calibration pipeline."""

from __future__ import annotations

from datetime import date, datetime
from datetime import timezone as dt_timezone

from psycopg import Connection

from polytempo.storage.postgres import (
    get_calibration_job_state,
    upsert_calibration_forecast_record,
    upsert_calibration_job_state,
    upsert_calibration_observed_tmax,
    utc_now_iso,
)
from polytempo.weather.calibration_compute import ForecastRecord
from polytempo.weather.observations import CalibrationObservedTmax

DEFAULT_JOB_NAME = "daily_calibration"


def _format_run_time_utc(value: datetime) -> str:
    return value.astimezone(dt_timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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


def load_forecast_records(conn: Connection) -> list[ForecastRecord]:
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
        run_text = str(row["run_time_utc"]).replace("Z", "+00:00")
        run_time = datetime.fromisoformat(run_text).astimezone(dt_timezone.utc)
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
