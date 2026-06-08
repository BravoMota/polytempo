"""Tests for Open-Meteo probe schedule helpers."""

from datetime import datetime, timezone

import httpx
import pytest

from polytempo.weather.open_meteo_probe import (
    ProbeSlot,
    next_probe_instant,
    probe_slot_key,
    run_probe,
    slot_for_instant,
)


def test_slot_for_instant_on_hour() -> None:
    instant = datetime(2026, 6, 8, 9, 0, tzinfo=timezone.utc)
    slot = slot_for_instant(instant)
    assert slot is not None
    assert slot.slot == "on_hour"
    assert slot.hour_utc == 9


def test_slot_for_instant_plus_5min() -> None:
    instant = datetime(2026, 6, 8, 14, 5, tzinfo=timezone.utc)
    slot = slot_for_instant(instant)
    assert slot is not None
    assert slot.slot == "plus_5min"


def test_slot_for_instant_non_probe_returns_none() -> None:
    instant = datetime(2026, 6, 8, 14, 3, tzinfo=timezone.utc)
    assert slot_for_instant(instant) is None


def test_next_probe_instant_same_hour() -> None:
    now = datetime(2026, 6, 8, 9, 1, tzinfo=timezone.utc)
    nxt = next_probe_instant(now)
    assert nxt == datetime(2026, 6, 8, 9, 5, tzinfo=timezone.utc)


def test_next_probe_instant_after_plus_10() -> None:
    now = datetime(2026, 6, 8, 9, 11, tzinfo=timezone.utc)
    nxt = next_probe_instant(now)
    assert nxt == datetime(2026, 6, 8, 10, 0, tzinfo=timezone.utc)


def test_next_probe_instant_rolls_to_next_day() -> None:
    now = datetime(2026, 6, 8, 23, 11, tzinfo=timezone.utc)
    nxt = next_probe_instant(now)
    assert nxt == datetime(2026, 6, 9, 0, 0, tzinfo=timezone.utc)


def test_probe_slot_key() -> None:
    slot = ProbeSlot(
        instant=datetime(2026, 6, 8, 9, 0, tzinfo=timezone.utc),
        hour_utc=9,
        slot="on_hour",
    )
    assert probe_slot_key(slot).endswith("+on_hour")


def test_run_probe_records_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise_timeout(*args, **kwargs):
        raise httpx.ReadTimeout("timed out")

    monkeypatch.setattr(
        "polytempo.weather.open_meteo_probe.fetch_daily_max",
        _raise_timeout,
    )

    slot = ProbeSlot(
        instant=datetime(2026, 6, 8, 9, 0, tzinfo=timezone.utc),
        hour_utc=9,
        slot="on_hour",
    )
    record = run_probe(
        city="london",
        target_date=datetime(2026, 6, 9, tzinfo=timezone.utc).date(),
        slot=slot,
    )
    assert record["success"] is False
    assert record["error_type"] == "ReadTimeout"
    assert record["slot"] == "on_hour"
    assert record["models_count"] == 8
