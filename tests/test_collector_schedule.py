"""Tests for UTC anchor scheduling."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from polytempo.collectors.schedule import (
    is_slot_due,
    next_scheduled_instant_utc,
    parse_anchor_time_utc,
    slot_for_instant,
)


def test_parse_anchor_time_utc() -> None:
    assert parse_anchor_time_utc("00:00").hour == 0
    assert parse_anchor_time_utc("06:30").minute == 30
    assert parse_anchor_time_utc("12:15:45").second == 45
    with pytest.raises(ValueError):
        parse_anchor_time_utc("25:00")


def test_slot_for_instant_five_minute_grid() -> None:
    anchor = "00:00"
    interval = 300
    assert slot_for_instant(
        datetime(2026, 6, 6, 0, 5, 3, tzinfo=timezone.utc),
        interval,
        anchor,
    ) == datetime(2026, 6, 6, 0, 5, 0, tzinfo=timezone.utc)
    assert slot_for_instant(
        datetime(2026, 6, 6, 0, 4, 59, tzinfo=timezone.utc),
        interval,
        anchor,
    ) == datetime(2026, 6, 6, 0, 0, 0, tzinfo=timezone.utc)


def test_next_scheduled_instant_after_late_start() -> None:
    now = datetime(2026, 6, 6, 0, 5, 3, tzinfo=timezone.utc)
    nxt = next_scheduled_instant_utc(now, 300, "00:00")
    assert nxt == datetime(2026, 6, 6, 0, 10, 0, tzinfo=timezone.utc)


def test_next_scheduled_instant_hourly() -> None:
    now = datetime(2026, 6, 6, 1, 0, 1, tzinfo=timezone.utc)
    nxt = next_scheduled_instant_utc(now, 3600, "00:00")
    assert nxt == datetime(2026, 6, 6, 2, 0, 0, tzinfo=timezone.utc)


def test_is_slot_due_skips_already_run_slot() -> None:
    now = datetime(2026, 6, 6, 0, 5, 3, tzinfo=timezone.utc)
    slot = datetime(2026, 6, 6, 0, 5, 0, tzinfo=timezone.utc)
    due, current = is_slot_due(now, 300, "00:00", slot)
    assert due is False
    assert current == slot


def test_is_slot_due_when_never_run() -> None:
    now = datetime(2026, 6, 6, 0, 5, 3, tzinfo=timezone.utc)
    due, current = is_slot_due(now, 300, "00:00", None)
    assert due is True
    assert current == datetime(2026, 6, 6, 0, 5, 0, tzinfo=timezone.utc)


def test_slot_for_instant_non_zero_anchor() -> None:
    instant = datetime(2026, 6, 6, 7, 10, 0, tzinfo=timezone.utc)
    slot = slot_for_instant(instant, 3600, "06:30")
    assert slot == datetime(2026, 6, 6, 6, 30, 0, tzinfo=timezone.utc)


def test_slot_for_instant_day_rollover() -> None:
    instant = datetime(2026, 6, 6, 0, 2, 0, tzinfo=timezone.utc)
    slot = slot_for_instant(instant, 3600, "06:00")
    assert slot == datetime(2026, 6, 6, 0, 0, 0, tzinfo=timezone.utc)
