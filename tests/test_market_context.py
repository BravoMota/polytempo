"""Tests for market context helpers (no network)."""

from datetime import date, datetime, timezone

import pytest

from polytempo.paper.market_context import (
    resolve_target_dates,
    settlement_date_span_days,
    settlement_dates_for_profile,
)


def test_resolve_target_dates_today() -> None:
    today = date(2026, 6, 7)
    assert resolve_target_dates("today", today=today) == [today]


def test_resolve_target_dates_tomorrow() -> None:
    today = date(2026, 6, 7)
    assert resolve_target_dates("tomorrow", today=today) == [date(2026, 6, 8)]


def test_resolve_target_dates_both() -> None:
    today = date(2026, 6, 7)
    assert resolve_target_dates("both", today=today) == [today, date(2026, 6, 8)]


def test_resolve_target_dates_rejects_unknown() -> None:
    with pytest.raises(ValueError, match="target_day"):
        resolve_target_dates("next_week")


def test_settlement_date_span_days() -> None:
    assert settlement_date_span_days(20) == 2
    assert settlement_date_span_days(30) == 3
    assert settlement_date_span_days(54) == 4


def test_preview_settlement_dates_three_days() -> None:
    now = datetime(2026, 6, 7, 12, 0, tzinfo=timezone.utc)
    from polytempo.paper.market_context import preview_settlement_dates

    assert preview_settlement_dates(now=now, days=3) == [
        date(2026, 6, 7),
        date(2026, 6, 8),
        date(2026, 6, 9),
    ]


def test_settlement_dates_includes_gate_day_for_20h_lead() -> None:
    """20h gate fires on settlement day at 04:00 UTC — must still fetch that date."""
    now = datetime(2026, 6, 9, 4, 0, tzinfo=timezone.utc)
    dates = settlement_dates_for_profile("tomorrow", 20.0, now=now)
    assert date(2026, 6, 9) in dates


def test_settlement_dates_includes_today_plus_two_for_54h_lead() -> None:
    """54h gate for tomorrow's market fires two days ahead of settlement."""
    now = datetime(2026, 6, 7, 12, 0, tzinfo=timezone.utc)
    dates = settlement_dates_for_profile("tomorrow", 54.0, now=now)
    assert date(2026, 6, 9) in dates


def test_settlement_dates_30h_before_midnight_on_prior_day() -> None:
    now = datetime(2026, 6, 8, 12, 0, tzinfo=timezone.utc)
    dates = settlement_dates_for_profile("tomorrow", 30.0, now=now)
    assert date(2026, 6, 9) in dates
