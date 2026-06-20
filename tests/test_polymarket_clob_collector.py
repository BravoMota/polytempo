"""Tests for Polymarket CLOB snapshot collector DB persistence."""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from polytempo.collectors.config import CollectorConfig, StationConfig
from polytempo.collectors import polymarket_clob as clob_collector
from polytempo.markets.polymarket import PolymarketBucket, PolymarketEvent
from polytempo.storage.postgres import get_connection, insert_station


def _station() -> StationConfig:
    return StationConfig(
        station_id="EGLC",
        station_type="icao",
        name="London City Airport",
        timezone="Europe/London",
        lat=51.5053,
        lon=0.0553,
        country="gb",
        city_slug="london",
        pws_id=None,
    )


def _collector(*, horizon_days: int = 4) -> CollectorConfig:
    return CollectorConfig(
        name="polymarket_clob",
        enabled=True,
        source="polymarket_clob",
        observations_interval_seconds=86400,
        observations_anchor_time_utc="00:00",
        forecast_interval_seconds=300,
        forecast_anchor_time_utc="00:00",
        stations=[_station()],
        target_horizon_days=horizon_days,
    )


def _event(
    event_id: str,
    settlement_date: date,
    *,
    resolved: bool = False,
) -> PolymarketEvent:
    return PolymarketEvent(
        event_id=event_id,
        slug=f"london-{settlement_date.isoformat()}",
        title=f"London high temp {settlement_date.isoformat()}",
        settlement_date=settlement_date,
        buckets=[
            PolymarketBucket(
                market_id=f"m-{event_id}-23",
                label="23°C",
                yes_bid=0.18,
                yes_ask=0.22,
                liquidity_usd=250.0,
                spread=0.04,
                rules=None,
                resolved=resolved,
                outcome="YES" if resolved else None,
                yes_token_id="tok-yes-23",
            ),
            PolymarketBucket(
                market_id=f"m-{event_id}-24",
                label="24°C",
                yes_bid=0.40,
                yes_ask=0.45,
                liquidity_usd=500.0,
                spread=0.05,
                rules=None,
                resolved=False,
                outcome=None,
                yes_token_id="tok-yes-24",
            ),
        ],
    )


def test_polymarket_clob_persists_snapshots_with_slot_timestamps(
    weather_db_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 6, 19, 16, 0, 9, tzinfo=timezone.utc)
    today = date(2026, 6, 19)
    tomorrow = date(2026, 6, 20)
    fetch_calls: list[date | None] = []

    def fake_fetch_weather_events(*, end_on_date: date | None = None, **kwargs: object) -> list[PolymarketEvent]:
        _ = kwargs
        fetch_calls.append(end_on_date)
        if end_on_date == today:
            return [_event("evt-today", today)]
        if end_on_date == tomorrow:
            return [_event("evt-tomorrow", tomorrow)]
        return []

    def fake_first_parseable(
        events: list[PolymarketEvent],
        *,
        city: str | None = None,
        settlement_date: date | None = None,
    ) -> PolymarketEvent | None:
        _ = city
        for event in events:
            if event.settlement_date == settlement_date:
                return event
        return None

    def fake_hydrate(event: PolymarketEvent, **kwargs: object) -> PolymarketEvent:
        _ = kwargs
        return event

    monkeypatch.setattr(clob_collector, "fetch_weather_events", fake_fetch_weather_events)
    monkeypatch.setattr(clob_collector, "first_parseable_weather_event", fake_first_parseable)
    monkeypatch.setattr(clob_collector, "hydrate_prices", fake_hydrate)

    with get_connection(weather_db_url) as conn:
        insert_station(
            conn,
            station_id="EGLC",
            name="London City Airport",
            timezone="Europe/London",
            lat=51.5053,
            lon=0.0553,
            country="gb",
        )
        conn.commit()

        clob_collector.run_station_snapshots(
            conn,
            _collector(),
            _station(),
            now_utc=now,
        )

        count = conn.execute("SELECT COUNT(*) AS n FROM clob_bucket_snapshots").fetchone()
        assert count is not None
        assert int(count["n"]) == 4

        row = conn.execute(
            """
            SELECT poll_slot_utc, fetched_at_utc, lead_hours_to_day_end,
                   wall_clock_lead_hours, yes_bid, yes_ask, settlement_date
            FROM clob_bucket_snapshots
            WHERE polymarket_event_id = 'evt-today' AND bucket_label = '23°C'
            """
        ).fetchone()
        assert row is not None
        assert row["poll_slot_utc"] == "2026-06-19T16:00:00Z"
        assert row["fetched_at_utc"] >= row["poll_slot_utc"]
        assert row["wall_clock_lead_hours"] <= row["lead_hours_to_day_end"]
        assert row["yes_bid"] == pytest.approx(0.18)
        assert row["yes_ask"] == pytest.approx(0.22)
        assert row["settlement_date"] == "2026-06-19"

    assert len(fetch_calls) == 4
    assert fetch_calls[0] == today
    assert fetch_calls[3] == date(2026, 6, 22)


def test_polymarket_clob_skips_missing_and_resolved_events(
    weather_db_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 6, 19, 16, 0, 9, tzinfo=timezone.utc)
    today = date(2026, 6, 19)

    monkeypatch.setattr(
        clob_collector,
        "fetch_weather_events",
        lambda **kwargs: [_event("evt-resolved", today, resolved=True)]
        if kwargs.get("end_on_date") == today
        else [],
    )
    monkeypatch.setattr(
        clob_collector,
        "first_parseable_weather_event",
        lambda events, **kwargs: events[0] if events else None,
    )
    monkeypatch.setattr(clob_collector, "hydrate_prices", lambda event, **kwargs: event)

    with get_connection(weather_db_url) as conn:
        insert_station(
            conn,
            station_id="EGLC",
            name="London City Airport",
            timezone="Europe/London",
            lat=51.5053,
            lon=0.0553,
            country="gb",
        )
        conn.commit()

        clob_collector.run_station_snapshots(
            conn,
            _collector(),
            _station(),
            now_utc=now,
        )

        count = conn.execute("SELECT COUNT(*) AS n FROM clob_bucket_snapshots").fetchone()
        assert count is not None
        assert int(count["n"]) == 0

        state = conn.execute(
            """
            SELECT success_count, error_count
            FROM collector_state
            WHERE collector_name = 'polymarket_clob' AND station_id = 'EGLC'
            """
        ).fetchone()
        assert state is not None
        assert int(state["success_count"]) == 1
        assert int(state["error_count"]) == 0


def test_polymarket_clob_registered_in_collectors() -> None:
    from polytempo.collectors import COLLECTORS

    assert "polymarket_clob" in COLLECTORS


def test_default_config_includes_polymarket_clob() -> None:
    from polytempo.collectors.config import DEFAULT_CONFIG_PATH, load_weather_collectors_config

    if not DEFAULT_CONFIG_PATH.is_file():
        pytest.skip("default config not present")

    config = load_weather_collectors_config(DEFAULT_CONFIG_PATH)
    clob = next(c for c in config.collectors if c.name == "polymarket_clob")
    assert clob.forecast_interval_seconds == 300
    assert clob.target_horizon_days == 4
