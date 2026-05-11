"""Tests for Polymarket/Gamma market ingestion."""

import os

import httpx
import pytest

from polytempo.markets.polymarket import (
    PolymarketEvent,
    fetch_event,
    fetch_weather_events,
    parse_event_payload,
    to_market_prices,
)


def _payload() -> dict:
    return {
        "id": "event-1",
        "slug": "madrid-high-temp",
        "title": "Madrid high temperature",
        "markets": [
            {
                "id": "m1",
                "groupItemTitle": "23°C",
                "question": "Will it be 23°C?",
                "description": "Rules for 23°C",
                "bestBid": "0.31",
                "bestAsk": "0.34",
                "liquidityNum": "1234.50",
                "spread": "0.03",
            },
            {
                "id": "m2",
                "groupItemTitle": "24°C",
                "question": "Will it be 24°C?",
                "description": "Rules for 24°C",
                "bestBid": 0.21,
                "bestAsk": 0.26,
                "liquidityNum": 900,
                "spread": 0.05,
            },
        ],
    }


def test_parse_event_payload_parses_event_fields() -> None:
    event = parse_event_payload(_payload())

    assert event.event_id == "event-1"
    assert event.slug == "madrid-high-temp"
    assert event.title == "Madrid high temperature"


def test_parse_event_payload_parses_multiple_buckets() -> None:
    event = parse_event_payload(_payload())

    assert [bucket.market_id for bucket in event.buckets] == ["m1", "m2"]
    assert [bucket.label for bucket in event.buckets] == ["23°C", "24°C"]


def test_parse_event_payload_prefers_group_item_title() -> None:
    event = parse_event_payload(_payload())

    assert event.buckets[0].label == "23°C"


def test_parse_event_payload_falls_back_to_question() -> None:
    payload = _payload()
    payload["markets"][0].pop("groupItemTitle")

    event = parse_event_payload(payload)

    assert event.buckets[0].label == "Will it be 23°C?"


def test_parse_event_payload_converts_numeric_strings_to_floats() -> None:
    event = parse_event_payload(_payload())
    bucket = event.buckets[0]

    assert bucket.yes_bid == pytest.approx(0.31)
    assert bucket.yes_ask == pytest.approx(0.34)
    assert bucket.liquidity_usd == pytest.approx(1234.50)
    assert bucket.spread == pytest.approx(0.03)


def test_parse_event_payload_missing_optional_bucket_fields_become_none() -> None:
    payload = {
        "id": "event-1",
        "title": "Temperature event",
        "markets": [{"id": "m1", "question": "Will it be 23°C?"}],
    }

    event = parse_event_payload(payload)
    bucket = event.buckets[0]

    assert bucket.yes_bid is None
    assert bucket.yes_ask is None
    assert bucket.liquidity_usd is None
    assert bucket.spread is None
    assert bucket.rules is None


@pytest.mark.parametrize(
    "payload",
    [
        {"title": "Missing id", "markets": []},
        {"id": "event-1", "markets": []},
        {"id": "event-1", "title": "Missing markets"},
        {"id": "event-1", "title": "Bad markets", "markets": None},
    ],
)
def test_parse_event_payload_missing_required_event_fields_raises(payload: dict) -> None:
    with pytest.raises(ValueError):
        parse_event_payload(payload)


def test_to_market_prices_preserves_order_and_maps_fields() -> None:
    event = parse_event_payload(_payload())

    prices = to_market_prices(event)

    assert [price.label for price in prices] == ["23°C", "24°C"]
    assert prices[0].yes_bid == pytest.approx(0.31)
    assert prices[0].yes_ask == pytest.approx(0.34)
    assert prices[0].liquidity_usd == pytest.approx(1234.50)
    assert prices[0].spread == pytest.approx(0.03)


def test_fetch_event_calls_expected_url(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return _payload()

    def fake_get(url: str) -> FakeResponse:
        calls.append(url)
        return FakeResponse()

    monkeypatch.setattr(httpx, "get", fake_get)

    event = fetch_event("event-1", base_url="https://example.test/")

    assert calls == ["https://example.test/events/event-1"]
    assert isinstance(event, PolymarketEvent)
    assert event.event_id == "event-1"


def test_fetch_event_rejects_empty_event_id() -> None:
    with pytest.raises(ValueError):
        fetch_event("  ")


def test_fetch_weather_events_calls_expected_url_and_params(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict]] = []

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> list[dict]:
            return [_payload()]

    def fake_get(url: str, params: dict) -> FakeResponse:
        calls.append((url, params))
        return FakeResponse()

    monkeypatch.setattr(httpx, "get", fake_get)

    events = fetch_weather_events(limit=5, base_url="https://example.test/")

    assert [event.event_id for event in events] == ["event-1"]
    assert calls == [
        (
            "https://example.test/events",
            {
                "tag_slug": "weather",
                "limit": 5,
                "active": "true",
                "closed": "false",
                "order": "volume",
                "ascending": "false",
            },
        )
    ]


def test_fetch_weather_events_rejects_invalid_limit() -> None:
    with pytest.raises(ValueError):
        fetch_weather_events(limit=0)


def test_live_gamma_weather_event_list_and_fetch() -> None:
    if os.environ.get("POLYTEMPO_RUN_LIVE_API_TESTS") != "1":
        pytest.skip("set POLYTEMPO_RUN_LIVE_API_TESTS=1 to run live Gamma API smoke test")

    events = fetch_weather_events(limit=5)
    if not events:
        pytest.skip("Gamma API returned no active weather events")

    first_event = events[0]
    fetched = fetch_event(first_event.event_id)

    assert fetched.event_id == first_event.event_id
    assert fetched.title
    assert isinstance(fetched.buckets, list)
