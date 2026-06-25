"""Orchestration for bootstrap and daily calibration jobs."""

from __future__ import annotations

import logging
import sys
from datetime import date, datetime, time, timedelta
from datetime import timezone as dt_timezone
from pathlib import Path

import httpx

from polytempo.collectors.config import StationConfig
from polytempo.storage.postgres import get_connection, insert_station, utc_now_iso
from polytempo.weather.calibration_compute import (
    archive_calibration_stats_csv_before_write,
    compute_calibration_stats,
    iter_forecast_records_from_payload,
    join_with_observations,
    tag_calibration_stats_anchor,
    write_calibration_stats_csv,
)
from polytempo.weather.calibration_stats_csv import (
    LEAD_HOURS_ANCHOR_RUN_INIT,
    LEAD_HOURS_ANCHOR_SCRAPED_AT,
)
from polytempo.weather.calibration_config import CalibrationConfig
from polytempo.weather.calibration_storage import (
    DEFAULT_JOB_NAME,
    WU_CALIBRATION_JOB_NAME,
    WU_FORECAST_MODEL,
    WuHistoryObservationRow,
    load_forecast_records,
    load_observations_map,
    read_job_state,
    record_job_success,
    upsert_forecast_record,
    upsert_observation,
    upsert_wu_history_observation,
)
from polytempo.weather.calibration_wu_forecasts import (
    ingest_wu_forecasts_from_snapshots,
    min_wu_forecast_snapshot_time,
)
from polytempo.weather.data_dir import WEATHER_DATA_DIR
from polytempo.weather.historical_forecasts import (
    DEFAULT_RAW_FORECASTS_DIR,
    DEFAULT_SINGLE_RUNS_BASE_URL,
    _station_model_from_raw_filename,
    floor_run_time_to_init_grid,
    generate_run_times_utc,
    load_or_fetch_single_run_payload,
    parse_run_time_utc_from_raw_filename,
    read_raw_forecast_response,
    raw_forecast_path,
)
from polytempo.weather.wunderground import (
    fetch_wu_history_daily_observations,
    fetch_wunderground_observations_range,
    to_calibration_observed,
)

logger = logging.getLogger(__name__)


def _utc_yesterday(today_utc: date | None = None) -> date:
    today = today_utc or datetime.now(dt_timezone.utc).date()
    return today - timedelta(days=1)


def _parse_iso_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(dt_timezone.utc)


def _sync_stations(conn, stations: list[StationConfig]) -> None:
    for station in stations:
        insert_station(
            conn,
            station_id=station.station_id,
            name=station.name,
            timezone=station.timezone,
            lat=station.lat,
            lon=station.lon,
            country=station.country,
            active=True,
        )


def ingest_observations(
    conn,
    config: CalibrationConfig,
    *,
    start_date: date,
    end_date: date,
    client: httpx.Client,
) -> int:
    count = 0
    for station in config.stations:
        rows = fetch_wunderground_observations_range(
            station.station_id,
            start_date,
            end_date,
            client=client,
            country=station.country,
            city_slug=station.city_slug,
            lat=station.lat,
            lon=station.lon,
        )
        for row in rows:
            upsert_observation(conn, to_calibration_observed(row))
            count += 1
    return count


def _ingest_payload_file(conn, path: Path) -> int:
    station_id, model = _station_model_from_raw_filename(path.name)
    run_time = parse_run_time_utc_from_raw_filename(path.name)
    payload = read_raw_forecast_response(path)
    count = 0
    for record in iter_forecast_records_from_payload(
        payload,
        station_id=station_id,
        model=model,
        run_time_utc=run_time,
    ):
        upsert_forecast_record(conn, record, raw_file_path=str(path))
        count += 1
    return count


def ingest_forecasts_for_range(
    conn,
    config: CalibrationConfig,
    *,
    run_start: datetime,
    run_end: datetime,
    client: httpx.Client,
    raw_dir: Path = DEFAULT_RAW_FORECASTS_DIR,
) -> tuple[int, int]:
    fetched = 0
    ingested_rows = 0
    run_start_utc = run_start.astimezone(dt_timezone.utc)
    run_end_utc = run_end.astimezone(dt_timezone.utc)
    if run_end_utc < run_start_utc:
        logger.info(
            "forecast ingest skipped: run_end %s before run_start %s (already caught up)",
            run_end_utc.isoformat(),
            run_start_utc.isoformat(),
        )
        return 0, 0

    for station in config.stations:
        if station.lat is None or station.lon is None:
            continue
        for model_cfg in config.models:
            grid_start = floor_run_time_to_init_grid(
                run_start_utc,
                model_cfg.run_init_interval_hours,
            )
            if grid_start > run_end_utc:
                continue
            run_times = generate_run_times_utc(
                grid_start,
                run_end_utc,
                model_cfg.run_init_interval_hours,
            )
            for run_time in run_times:
                cached_path = raw_forecast_path(
                    raw_dir,
                    station.station_id,
                    model_cfg.name,
                    run_time,
                )
                if not cached_path.exists():
                    try:
                        load_or_fetch_single_run_payload(
                            station.lat,
                            station.lon,
                            model_cfg.name,
                            run_time,
                            station_id=station.station_id,
                            timezone=station.timezone,
                            raw_dir=raw_dir,
                            forecast_days=model_cfg.forecast_days,
                            base_url=DEFAULT_SINGLE_RUNS_BASE_URL,
                            client=client,
                        )
                        fetched += 1
                    except Exception as exc:
                        logger.warning(
                            "forecast fetch failed station=%s model=%s run=%s: %s",
                            station.station_id,
                            model_cfg.name,
                            run_time.isoformat(),
                            exc,
                        )
                        continue
                    cached_path = raw_forecast_path(
                        raw_dir,
                        station.station_id,
                        model_cfg.name,
                        run_time,
                    )
                if cached_path.exists():
                    ingested_rows += _ingest_payload_file(conn, cached_path)
    return fetched, ingested_rows


def _join_diagnostics(
    forecasts: list,
    observations: dict[tuple[str, date], float],
    joined: list,
) -> dict[str, object]:
    """Summarize inner-join coverage for logging."""
    obs_dates = sorted({d for _, d in observations})
    forecast_dates = sorted({r.target_date for r in forecasts})
    unjoined = len(forecasts) - len(joined)
    unjoined_dates: dict[str, int] = {}
    for record in forecasts:
        if (record.station_id, record.target_date) not in observations:
            key = record.target_date.isoformat()
            unjoined_dates[key] = unjoined_dates.get(key, 0) + 1
    top_unjoined = sorted(unjoined_dates.items(), key=lambda x: x[1], reverse=True)[:8]
    return {
        "observation_days": len(obs_dates),
        "observation_range": (
            f"{obs_dates[0].isoformat()}..{obs_dates[-1].isoformat()}"
            if obs_dates
            else None
        ),
        "forecast_records": len(forecasts),
        "forecast_target_range": (
            f"{forecast_dates[0].isoformat()}..{forecast_dates[-1].isoformat()}"
            if forecast_dates
            else None
        ),
        "joined_rows": len(joined),
        "unjoined_forecasts": unjoined,
        "top_unjoined_target_dates": top_unjoined,
        "stat_groups": 0,
    }


def recompute_updated_stats(
    conn,
    output_path: Path,
    *,
    include_wu: bool = True,
) -> tuple[int, int, int, dict[str, object]]:
    """Join Open-Meteo + WU forecasts with observations and write one stats CSV.

    Returns ``(om_joined, wu_joined, stat_groups, diagnostics)``.
    """
    observations = load_observations_map(conn)

    om_forecasts = load_forecast_records(conn, exclude_models=(WU_FORECAST_MODEL,))
    om_joined = join_with_observations(om_forecasts, observations)

    wu_forecasts: list = []
    wu_joined: list = []
    if include_wu:
        wu_forecasts = load_forecast_records(conn, model=WU_FORECAST_MODEL)
        wu_joined = join_with_observations(wu_forecasts, observations)

    all_forecasts = om_forecasts + wu_forecasts
    all_joined = om_joined + wu_joined
    diag = _join_diagnostics(all_forecasts, observations, all_joined)
    diag["om_joined"] = len(om_joined)
    diag["wu_joined"] = len(wu_joined)

    if not all_joined:
        diag["stat_groups"] = 0
        return len(om_joined), len(wu_joined), 0, diag

    om_stats = (
        tag_calibration_stats_anchor(
            compute_calibration_stats(om_joined),
            LEAD_HOURS_ANCHOR_RUN_INIT,
        )
        if om_joined
        else []
    )
    wu_stats = (
        tag_calibration_stats_anchor(
            compute_calibration_stats(wu_joined),
            LEAD_HOURS_ANCHOR_SCRAPED_AT,
        )
        if wu_joined
        else []
    )
    tagged = om_stats + wu_stats
    archived = archive_calibration_stats_csv_before_write(output_path)
    if archived is not None:
        logger.info("archived previous calibration stats to %s", archived)
    write_calibration_stats_csv(tagged, output_path)
    diag["stat_groups"] = len(tagged)
    return len(om_joined), len(wu_joined), len(tagged), diag


def recompute_stats(conn, output_path: Path) -> tuple[int, int, dict[str, object]]:
    """Backward-compatible wrapper around :func:`recompute_updated_stats`."""
    om_joined, wu_joined, stat_groups, diag = recompute_updated_stats(
        conn,
        output_path,
        include_wu=True,
    )
    return om_joined + wu_joined, stat_groups, diag


def _log_recompute_summary(prefix: str, diag: dict[str, object]) -> None:
    """Explain join/aggregate counts (joined_rows are calibration samples, not failures)."""
    logger.info(
        "%s recompute: observation_days=%s obs_range=%s forecast_records=%s "
        "forecast_target_range=%s joined_rows=%s unjoined_forecasts=%s stat_groups=%s",
        prefix,
        diag.get("observation_days"),
        diag.get("observation_range"),
        diag.get("forecast_records"),
        diag.get("forecast_target_range"),
        diag.get("joined_rows"),
        diag.get("unjoined_forecasts"),
        diag.get("stat_groups"),
    )
    top_unjoined = diag.get("top_unjoined_target_dates")
    if isinstance(top_unjoined, list) and top_unjoined:
        logger.info("%s top unjoined target_dates (no observation): %s", prefix, top_unjoined)
    print(
        f"{prefix}: joined_rows={diag.get('joined_rows')} "
        f"(forecast+observation pairs used for bias/MAE/RMSE; not API failures), "
        f"unjoined_forecasts={diag.get('unjoined_forecasts')} "
        f"(forecast target_date has no observation), "
        f"stat_groups={diag.get('stat_groups')}",
        file=sys.stderr,
    )
    print(
        f"{prefix}: observations={diag.get('observation_days')} days "
        f"({diag.get('observation_range')}), "
        f"forecast_records={diag.get('forecast_records')} "
        f"(target_range={diag.get('forecast_target_range')})",
        file=sys.stderr,
    )


def run_bootstrap(
    config: CalibrationConfig,
    database_url: str,
    *,
    end_date: date | None = None,
    skip_observations: bool = False,
) -> int:
    """Populate calibration store from start_date through yesterday."""
    end = end_date or _utc_yesterday()
    if end < config.start_date:
        print("end_date is before start_date; nothing to do", file=sys.stderr)
        return 0

    with httpx.Client() as client, get_connection(database_url) as conn:
        _sync_stations(conn, config.stations)
        if skip_observations:
            obs_count = 0
            print("bootstrap: skipping observation ingest (--no-obs)", file=sys.stderr)
        else:
            obs_count = ingest_observations(
                conn,
                config,
                start_date=config.start_date,
                end_date=end,
                client=client,
            )
        run_start = datetime.combine(
            config.start_date,
            time.min,
            tzinfo=dt_timezone.utc,
        )
        run_end = datetime.combine(end, time.max.replace(microsecond=0), tzinfo=dt_timezone.utc)
        fetched, forecast_rows = ingest_forecasts_for_range(
            conn,
            config,
            run_start=run_start,
            run_end=run_end,
            client=client,
        )
        om_joined, wu_joined, stat_groups, diag = recompute_updated_stats(
            conn,
            config.updated_stats_csv,
        )
        if om_joined == 0:
            conn.rollback()
            print("no joined forecast+observation rows after bootstrap; not updating job state", file=sys.stderr)
            return 2
        record_job_success(conn, last_target_date=end)
        conn.commit()

    print(
        f"bootstrap: observations_ingested={obs_count} fetched_raw={fetched} "
        f"forecast_rows_ingested={forecast_rows}",
        file=sys.stderr,
    )
    _log_recompute_summary("bootstrap", diag)
    print(f"wrote {config.updated_stats_csv}", file=sys.stderr)
    return 0


def run_daily(
    config: CalibrationConfig,
    database_url: str,
    *,
    end_date: date | None = None,
) -> int:
    """Incremental nightly calibration update."""
    end = end_date or _utc_yesterday()

    with httpx.Client() as client, get_connection(database_url) as conn:
        _sync_stations(conn, config.stations)
        state = read_job_state(conn, DEFAULT_JOB_NAME)
        if state is None or state.get("last_target_date") is None:
            print(
                "bootstrap has not run yet; run scripts/bootstrap_calibration_store.py first",
                file=sys.stderr,
            )
            return 2

        last_target = date.fromisoformat(str(state["last_target_date"]))
        obs_start = min(end, last_target + timedelta(days=1))
        if obs_start > end:
            obs_start = end

        obs_count = ingest_observations(
            conn,
            config,
            start_date=obs_start,
            end_date=end,
            client=client,
        )

        last_success_text = state.get("last_success_at_utc")
        if isinstance(last_success_text, str) and last_success_text:
            # Hint only: ingest_forecasts_for_range snaps per-model to init grid.
            run_start = _parse_iso_utc(last_success_text)
        else:
            run_start = datetime.combine(
                config.start_date,
                time.min,
                tzinfo=dt_timezone.utc,
            )
        run_end = datetime.combine(
            end,
            time.max.replace(microsecond=0),
            tzinfo=dt_timezone.utc,
        )
        fetched, forecast_rows = ingest_forecasts_for_range(
            conn,
            config,
            run_start=run_start,
            run_end=run_end,
            client=client,
        )

        om_joined, wu_joined, stat_groups, diag = recompute_updated_stats(
            conn,
            config.updated_stats_csv,
        )
        if om_joined == 0:
            conn.rollback()
            print("no joined forecast+observation rows; not updating job state", file=sys.stderr)
            return 2

        record_job_success(conn, last_target_date=end)
        conn.commit()

    print(
        f"daily: observations_ingested={obs_count} fetched_raw={fetched} "
        f"forecast_rows_ingested={forecast_rows}",
        file=sys.stderr,
    )
    _log_recompute_summary("daily", diag)
    print(f"wrote {config.updated_stats_csv}", file=sys.stderr)
    return 0


def _wu_raw_history_dir() -> Path:
    return WEATHER_DATA_DIR / "raw" / "wunderground" / "history_daily"


def ingest_wu_history_observations(
    conn,
    config: CalibrationConfig,
    *,
    start_date: date,
    end_date: date,
    client: httpx.Client,
) -> int:
    """Fetch Daily Observations (metric v1 API) and upsert hourly rows."""
    if config.wunderground_forecast is None or not config.wunderground_forecast.enabled:
        return 0

    raw_dir = _wu_raw_history_dir()
    count = 0
    failures: list[str] = []
    total_days = (end_date - start_date).days + 1
    day_num = 0
    day = start_date
    print(
        f"wu history: fetching hourly obs {start_date.isoformat()} .. {end_date.isoformat()} "
        f"({total_days} days x {len(config.stations)} station(s))",
        file=sys.stderr,
        flush=True,
    )
    while day <= end_date:
        day_num += 1
        for station in config.stations:
            country_code = (station.country or "GB").upper()
            try:
                parsed, path = fetch_wu_history_daily_observations(
                    station.station_id,
                    day,
                    country_code=country_code,
                    raw_dir=raw_dir,
                    client=client,
                )
                for obs in parsed:
                    upsert_wu_history_observation(
                        conn,
                        WuHistoryObservationRow(
                            station_id=station.station_id,
                            target_date=day,
                            observed_at_utc=obs.observed_at_utc,
                            observed_at_local=obs.observed_at_local,
                            temp_c=obs.temp_c,
                        ),
                        raw_file_path=str(path),
                    )
                    count += 1
                print(
                    f"wu history: {station.station_id} {day.isoformat()} "
                    f"+{len(parsed)} rows ({day_num}/{total_days})",
                    file=sys.stderr,
                    flush=True,
                )
            except Exception as exc:
                failures.append(f"{station.station_id} {day.isoformat()}: {exc}")
                print(
                    f"wu history: {station.station_id} {day.isoformat()} FAILED ({day_num}/{total_days})",
                    file=sys.stderr,
                    flush=True,
                )
        day += timedelta(days=1)

    if failures:
        print(
            "wu history observation failures (skipped):\n- " + "\n- ".join(failures),
            file=sys.stderr,
        )
    return count


def _resolve_wu_snapshot_min(
    conn,
    config: CalibrationConfig,
) -> datetime:
    wu = config.wunderground_forecast
    assert wu is not None
    if wu.forecast_snapshot_min_utc is not None:
        return wu.forecast_snapshot_min_utc
    station_ids = config.station_ids
    db_min = min_wu_forecast_snapshot_time(conn, station_ids)
    if db_min is not None:
        return db_min
    raise ValueError(
        "no WU forecast_snapshots found and forecast_snapshot_min_utc is unset in config"
    )


def run_wu_bootstrap(
    config: CalibrationConfig,
    database_url: str,
    *,
    end_date: date | None = None,
    skip_observations: bool = False,
) -> int:
    """Bootstrap WU history observations and forecast calibration records."""
    wu = config.wunderground_forecast
    if wu is None or not wu.enabled:
        print("wunderground_forecast disabled in config; nothing to do", file=sys.stderr)
        return 0

    end = end_date or _utc_yesterday()
    if end < config.start_date:
        print("end_date is before start_date; nothing to do", file=sys.stderr)
        return 0

    with httpx.Client() as client, get_connection(database_url) as conn:
        _sync_stations(conn, config.stations)
        if not skip_observations:
            print(
                f"wu bootstrap: fetching daily Tmax {config.start_date.isoformat()} .. {end.isoformat()}",
                file=sys.stderr,
                flush=True,
            )
            ingest_observations(
                conn,
                config,
                start_date=config.start_date,
                end_date=end,
                client=client,
            )
        history_count = ingest_wu_history_observations(
            conn,
            config,
            start_date=config.start_date,
            end_date=end,
            client=client,
        )
        if history_count == 0:
            conn.rollback()
            print(
                "no WU history observations ingested; check v1 API access and station config",
                file=sys.stderr,
            )
            return 2
        snapshot_min = _resolve_wu_snapshot_min(conn, config)
        print(
            f"wu bootstrap: ingesting forecast snapshots since {snapshot_min.isoformat()}",
            file=sys.stderr,
            flush=True,
        )
        forecast_count = ingest_wu_forecasts_from_snapshots(
            conn,
            station_ids=config.station_ids,
            scraped_since_utc=snapshot_min,
            max_lead_hours=wu.max_lead_hours,
        )
        print("wu bootstrap: recomputing calibration stats", file=sys.stderr, flush=True)
        om_joined, wu_joined, stat_groups, diag = recompute_updated_stats(
            conn,
            config.updated_stats_csv,
        )
        if wu_joined == 0:
            conn.rollback()
            print(
                "no joined WU forecast+observation rows after bootstrap; not updating job state",
                file=sys.stderr,
            )
            return 2
        record_job_success(conn, job_name=WU_CALIBRATION_JOB_NAME, last_target_date=end)
        conn.commit()

    print(
        f"wu bootstrap: history_obs_rows={history_count} "
        f"forecast_records_ingested={forecast_count}",
        file=sys.stderr,
    )
    _log_recompute_summary("wu bootstrap", diag)
    print(f"wrote {config.updated_stats_csv}", file=sys.stderr)
    return 0


def run_wu_daily(
    config: CalibrationConfig,
    database_url: str,
    *,
    end_date: date | None = None,
) -> int:
    """Incremental nightly WU calibration update."""
    wu = config.wunderground_forecast
    if wu is None or not wu.enabled:
        return 0

    end = end_date or _utc_yesterday()

    with httpx.Client() as client, get_connection(database_url) as conn:
        _sync_stations(conn, config.stations)
        state = read_job_state(conn, WU_CALIBRATION_JOB_NAME)
        if state is None or state.get("last_target_date") is None:
            print(
                "WU bootstrap has not run yet; run scripts/bootstrap_wu_calibration_store.py first",
                file=sys.stderr,
            )
            return 2

        last_target = date.fromisoformat(str(state["last_target_date"]))
        history_start = min(end, last_target + timedelta(days=1))
        print(
            f"wu daily: fetching history obs {history_start.isoformat()} .. {end.isoformat()}",
            file=sys.stderr,
            flush=True,
        )
        history_count = ingest_wu_history_observations(
            conn,
            config,
            start_date=history_start,
            end_date=end,
            client=client,
        )

        ingest_observations(
            conn,
            config,
            start_date=history_start,
            end_date=end,
            client=client,
        )

        last_success_text = state.get("last_success_at_utc")
        if isinstance(last_success_text, str) and last_success_text:
            scraped_since = _parse_iso_utc(last_success_text)
        else:
            scraped_since = _resolve_wu_snapshot_min(conn, config)

        print(
            f"wu daily: ingesting forecast snapshots since {scraped_since.isoformat()}",
            file=sys.stderr,
            flush=True,
        )
        forecast_count = ingest_wu_forecasts_from_snapshots(
            conn,
            station_ids=config.station_ids,
            scraped_since_utc=scraped_since,
            max_lead_hours=wu.max_lead_hours,
        )

        print("wu daily: recomputing calibration stats", file=sys.stderr, flush=True)
        om_joined, wu_joined, stat_groups, diag = recompute_updated_stats(
            conn,
            config.updated_stats_csv,
        )
        if wu_joined == 0:
            conn.rollback()
            print("no joined WU forecast+observation rows; not updating job state", file=sys.stderr)
            return 2

        record_job_success(conn, job_name=WU_CALIBRATION_JOB_NAME, last_target_date=end)
        conn.commit()

    print(
        f"wu daily: history_obs_rows={history_count} "
        f"forecast_records_ingested={forecast_count}",
        file=sys.stderr,
    )
    _log_recompute_summary("wu daily", diag)
    print(f"wrote {config.updated_stats_csv}", file=sys.stderr)
    return 0
