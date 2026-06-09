"""Tests for PostgreSQL test-database URL guards."""

import pytest

from polytempo.storage.postgres import assert_test_database_url, database_name_from_url


def test_database_name_from_url() -> None:
    assert database_name_from_url("postgresql://host/polytempo_test") == "polytempo_test"


def test_assert_test_database_url_rejects_prod() -> None:
    with pytest.raises(RuntimeError, match="refusing non-test database"):
        assert_test_database_url("postgresql://host/polytempo")


def test_assert_test_database_url_accepts_test_db() -> None:
    assert_test_database_url("postgresql://host/polytempo_test")
