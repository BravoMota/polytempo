"""Shared fixtures for PostgreSQL-backed tests."""

from __future__ import annotations

import os

import pytest

from polytempo.storage.postgres import (
    assert_test_database_url,
    get_connection,
    initialize_database,
)


@pytest.fixture
def paper_db_url() -> str:
    """Provide a clean Postgres database URL for paper trading tests."""
    url = os.environ.get("POLYTEMPO_PAPER_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set POLYTEMPO_PAPER_TEST_DATABASE_URL for paper Postgres tests")
    assert_test_database_url(url)

    from polytempo.storage.paper_postgres import initialize_paper_database, truncate_paper_tables
    from polytempo.storage.paper_postgres import get_paper_connection

    initialize_paper_database(url)
    with get_paper_connection(url) as conn:
        truncate_paper_tables(conn)
        conn.commit()
    return url


@pytest.fixture
def weather_db_url() -> str:
    """Provide a clean Postgres database URL for storage/collector tests."""
    url = os.environ.get("POLYTEMPO_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set POLYTEMPO_TEST_DATABASE_URL for Postgres tests")
    assert_test_database_url(url)

    initialize_database(url)
    with get_connection(url) as conn:
        conn.execute(
            "TRUNCATE TABLE open_meteo_forecast_snapshots, open_meteo_model_meta_snapshots, "
            "open_meteo_fetch_cycles, calibration_forecast_records, calibration_observed_tmax, "
            "calibration_job_state, forecast_snapshots, observation_snapshots, "
            "collector_state, stations RESTART IDENTITY CASCADE"
        )
        conn.commit()
    return url
