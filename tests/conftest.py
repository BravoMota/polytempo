"""Shared fixtures for PostgreSQL-backed tests."""

from __future__ import annotations

import os

import pytest

from polytempo.storage.postgres import get_connection, initialize_database


@pytest.fixture
def weather_db_url() -> str:
    """Provide a clean Postgres database URL for storage/collector tests."""
    url = os.environ.get("POLYTEMPO_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not url:
        pytest.skip("Set POLYTEMPO_DATABASE_URL or DATABASE_URL for Postgres tests")

    initialize_database(url)
    with get_connection(url) as conn:
        conn.execute(
            "TRUNCATE TABLE forecast_snapshots, observation_snapshots, "
            "collector_state, stations RESTART IDENTITY CASCADE"
        )
        conn.commit()
    return url
