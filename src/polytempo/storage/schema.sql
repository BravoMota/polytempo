-- PolyTempo weather collection schema (v1).
-- Timestamps are ISO-8601 TEXT (UTC instants use trailing Z where applicable).

CREATE TABLE IF NOT EXISTS stations (
    station_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    timezone TEXT NOT NULL,
    lat REAL,
    lon REAL,
    country TEXT,
    active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS observation_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    station_id TEXT NOT NULL,
    source TEXT NOT NULL,
    scraped_at_utc TEXT NOT NULL,
    observed_at_utc TEXT,
    observed_at_local TEXT,
    target_date_local TEXT NOT NULL,
    station_timezone TEXT NOT NULL,
    temp_c REAL,
    raw_temp_text TEXT,
    raw_file_path TEXT,
    content_hash TEXT,
    created_at_utc TEXT NOT NULL,
    FOREIGN KEY (station_id) REFERENCES stations(station_id)
);

CREATE TABLE IF NOT EXISTS forecast_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    station_id TEXT NOT NULL,
    source TEXT NOT NULL,
    model TEXT,
    scraped_at_utc TEXT NOT NULL,
    forecast_generated_at_utc TEXT,
    target_time_utc TEXT,
    target_time_local TEXT,
    target_date_local TEXT NOT NULL,
    station_timezone TEXT NOT NULL,
    lead_hours_to_day_end REAL,
    temp_c REAL,
    requested_lat REAL,
    requested_lon REAL,
    returned_lat REAL,
    returned_lon REAL,
    raw_file_path TEXT,
    content_hash TEXT,
    created_at_utc TEXT NOT NULL,
    FOREIGN KEY (station_id) REFERENCES stations(station_id)
);

CREATE TABLE IF NOT EXISTS collector_state (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
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
    UNIQUE(collector_name, station_id, source)
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
