"""Tests for lead-time helpers."""

from datetime import date, datetime, timezone

import pytest

from polytempo.model.lead_time import (
    gate_close_utc,
    gate_open_utc,
    gate_target_utc,
    lead_hours_at_target,
    lead_hours_before_target,
    lead_hours_missed_target,
    lead_hours_to_end_of_target_day,
)


def test_lead_hours_anchors_at_end_of_same_day_target() -> None:
    target = date(2026, 5, 28)
    now = datetime(2026, 5, 28, 12, 0, tzinfo=timezone.utc)
    assert lead_hours_to_end_of_target_day(target, now=now) == pytest.approx(12.0)


def test_lead_hours_anchors_at_end_of_next_day_target() -> None:
    target = date(2026, 5, 29)
    now = datetime(2026, 5, 28, 12, 0, tzinfo=timezone.utc)
    assert lead_hours_to_end_of_target_day(target, now=now) == pytest.approx(36.0)


def test_lead_hours_clamps_to_zero_past_end_of_target() -> None:
    target = date(2026, 5, 28)
    now = datetime(2026, 5, 29, 0, 1, tzinfo=timezone.utc)
    assert lead_hours_to_end_of_target_day(target, now=now) == 0.0


def test_lead_hours_at_target_within_tolerance() -> None:
    assert lead_hours_at_target(30.0, 30.0) is True
    assert lead_hours_at_target(30.025, 30.0, tolerance_seconds=90.0) is True
    assert lead_hours_at_target(29.9, 30.0, tolerance_seconds=90.0) is False
    assert lead_hours_at_target(30.1, 30.0, tolerance_seconds=90.0) is False


def test_lead_hours_before_and_missed_target() -> None:
    assert lead_hours_before_target(31.0, 30.0) is True
    assert lead_hours_missed_target(29.0, 30.0) is True
    assert lead_hours_before_target(30.0, 30.0) is False
    assert lead_hours_missed_target(30.0, 30.0) is False


def test_gate_target_utc() -> None:
    target = date(2026, 5, 29)
    # end of target = 2026-05-30 00:00 UTC; 30h lead => 2026-05-28 18:00
    target_at = gate_target_utc(target, 30.0)
    assert target_at == datetime(2026, 5, 28, 18, 0, tzinfo=timezone.utc)
    assert gate_open_utc(target, 30.0) == target_at


def test_gate_close_utc() -> None:
    target = date(2026, 5, 29)
    close_at = gate_close_utc(target, 28.0)
    assert close_at == datetime(2026, 5, 28, 20, 0, tzinfo=timezone.utc)
