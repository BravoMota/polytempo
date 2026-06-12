-- PolyTempo weather collection schema (PostgreSQL v1).
-- Timestamps are ISO-8601 TEXT (UTC instants use trailing Z where applicable).

CREATE TABLE IF NOT EXISTS stations (
    station_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    timezone TEXT NOT NULL,
    lat DOUBLE PRECISION,
    lon DOUBLE PRECISION,
    country TEXT,
    active BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE TABLE IF NOT EXISTS observation_snapshots (
    id BIGSERIAL PRIMARY KEY,
    station_id TEXT NOT NULL REFERENCES stations(station_id),
    source TEXT NOT NULL,
    scraped_at_utc TEXT NOT NULL,
    observed_at_utc TEXT,
    observed_at_local TEXT,
    target_date_local TEXT NOT NULL,
    station_timezone TEXT NOT NULL,
    temp_c DOUBLE PRECISION,
    raw_temp_text TEXT,
    raw_file_path TEXT,
    content_hash TEXT,
    created_at_utc TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS forecast_snapshots (
    id BIGSERIAL PRIMARY KEY,
    station_id TEXT NOT NULL REFERENCES stations(station_id),
    source TEXT NOT NULL,
    model TEXT,
    scraped_at_utc TEXT NOT NULL,
    forecast_generated_at_utc TEXT,
    target_time_utc TEXT,
    target_time_local TEXT,
    target_date_local TEXT NOT NULL,
    station_timezone TEXT NOT NULL,
    lead_hours_to_day_end DOUBLE PRECISION,
    temp_c DOUBLE PRECISION,
    raw_temp_text TEXT,
    requested_lat DOUBLE PRECISION,
    requested_lon DOUBLE PRECISION,
    returned_lat DOUBLE PRECISION,
    returned_lon DOUBLE PRECISION,
    raw_file_path TEXT,
    content_hash TEXT,
    created_at_utc TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS collector_state (
    id BIGSERIAL PRIMARY KEY,
    collector_name TEXT NOT NULL,
    station_id TEXT NOT NULL,
    source TEXT NOT NULL,
    last_started_at_utc TEXT,
    last_success_at_utc TEXT,
    last_error_at_utc TEXT,
    last_error_message TEXT,
    success_count INTEGER NOT NULL DEFAULT 0,
    error_count INTEGER NOT NULL DEFAULT 0,
    updated_at_utc TEXT NOT NULL,
    UNIQUE (collector_name, station_id, source)
);

CREATE INDEX IF NOT EXISTS idx_observation_snapshots_station_source_observed
    ON observation_snapshots(station_id, source, observed_at_utc);

CREATE INDEX IF NOT EXISTS idx_observation_snapshots_station_target_date
    ON observation_snapshots(station_id, target_date_local);

CREATE INDEX IF NOT EXISTS idx_forecast_snapshots_station_source_scraped
    ON forecast_snapshots(station_id, source, scraped_at_utc);

CREATE INDEX IF NOT EXISTS idx_forecast_snapshots_station_source_model_target
    ON forecast_snapshots(station_id, source, model, target_time_utc);

CREATE INDEX IF NOT EXISTS idx_forecast_snapshots_station_target_date
    ON forecast_snapshots(station_id, target_date_local);

CREATE INDEX IF NOT EXISTS idx_collector_state_lookup
    ON collector_state(collector_name, station_id, source);

CREATE TABLE IF NOT EXISTS calibration_observed_tmax (
    station_id TEXT NOT NULL REFERENCES stations(station_id),
    target_date TEXT NOT NULL,
    observed_tmax_f DOUBLE PRECISION NOT NULL,
    observed_tmax_c DOUBLE PRECISION NOT NULL,
    source TEXT NOT NULL,
    fetched_at_utc TEXT NOT NULL,
    PRIMARY KEY (station_id, target_date)
);

CREATE TABLE IF NOT EXISTS calibration_forecast_records (
    station_id TEXT NOT NULL REFERENCES stations(station_id),
    model TEXT NOT NULL,
    run_time_utc TEXT NOT NULL,
    target_date TEXT NOT NULL,
    lead_hours DOUBLE PRECISION NOT NULL,
    predicted_tmax_c DOUBLE PRECISION NOT NULL,
    forecast_lat DOUBLE PRECISION,
    forecast_lon DOUBLE PRECISION,
    raw_file_path TEXT,
    ingested_at_utc TEXT NOT NULL,
    PRIMARY KEY (station_id, model, run_time_utc, target_date)
);

CREATE TABLE IF NOT EXISTS calibration_job_state (
    job_name TEXT PRIMARY KEY,
    last_success_at_utc TEXT,
    last_error_at_utc TEXT,
    last_error_message TEXT,
    last_target_date TEXT,
    updated_at_utc TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_calibration_forecast_station_target
    ON calibration_forecast_records(station_id, target_date);

CREATE INDEX IF NOT EXISTS idx_calibration_observed_station_target
    ON calibration_observed_tmax(station_id, target_date);

CREATE TABLE IF NOT EXISTS open_meteo_fetch_cycles (
    id BIGSERIAL PRIMARY KEY,
    station_id TEXT NOT NULL REFERENCES stations(station_id),
    fetched_at_utc TEXT NOT NULL,
    requested_lat DOUBLE PRECISION,
    requested_lon DOUBLE PRECISION,
    returned_lat DOUBLE PRECISION,
    returned_lon DOUBLE PRECISION,
    collector_name TEXT NOT NULL DEFAULT 'open_meteo',
    meta_staleness_detected BOOLEAN NOT NULL DEFAULT FALSE,
    created_at_utc TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS open_meteo_model_meta_snapshots (
    id BIGSERIAL PRIMARY KEY,
    fetch_cycle_id BIGINT NOT NULL REFERENCES open_meteo_fetch_cycles(id) ON DELETE CASCADE,
    station_id TEXT NOT NULL REFERENCES stations(station_id),
    model TEXT NOT NULL,
    run_init_utc TEXT NOT NULL,
    run_available_utc TEXT NOT NULL,
    run_modified_utc TEXT NOT NULL,
    availability_lag_hours DOUBLE PRECISION NOT NULL,
    update_interval_seconds INTEGER NOT NULL,
    temporal_resolution_seconds INTEGER NOT NULL,
    data_end_utc TEXT,
    fetched_at_utc TEXT NOT NULL,
    UNIQUE (fetch_cycle_id, model)
);

CREATE TABLE IF NOT EXISTS open_meteo_forecast_snapshots (
    id BIGSERIAL PRIMARY KEY,
    fetch_cycle_id BIGINT NOT NULL REFERENCES open_meteo_fetch_cycles(id) ON DELETE CASCADE,
    station_id TEXT NOT NULL REFERENCES stations(station_id),
    model TEXT NOT NULL,
    target_date_local TEXT NOT NULL,
    predicted_tmax_c DOUBLE PRECISION NOT NULL,
    run_init_utc TEXT NOT NULL,
    init_lead_hours DOUBLE PRECISION NOT NULL,
    wall_clock_lead_hours DOUBLE PRECISION NOT NULL,
    fetched_at_utc TEXT NOT NULL,
    UNIQUE (fetch_cycle_id, model, target_date_local)
);

CREATE INDEX IF NOT EXISTS idx_open_meteo_fetch_cycles_station_fetched
    ON open_meteo_fetch_cycles(station_id, fetched_at_utc);

CREATE INDEX IF NOT EXISTS idx_open_meteo_forecast_station_model_target
    ON open_meteo_forecast_snapshots(station_id, model, target_date_local);

CREATE INDEX IF NOT EXISTS idx_open_meteo_meta_fetch_cycle
    ON open_meteo_model_meta_snapshots(fetch_cycle_id);
