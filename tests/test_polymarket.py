"""Tests for Polymarket/Gamma market ingestion."""

import os
from datetime import date

import httpx
import pytest

from polytempo.markets.polymarket import (
    PolymarketBucket,
    PolymarketEvent,
    fetch_clob_book,
    fetch_event,
    fetch_weather_events,
    first_parseable_weather_event,
    hydrate_prices,
    parse_event_payload,
    strip_untradeable_bucket_prices,
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
    assert event.settlement_date is None


def test_parse_event_payload_parses_multiple_buckets() -> None:
    event = parse_event_payload(_payload())

    assert [bucket.market_id for bucket in event.buckets] == ["m1", "m2"]
    assert [bucket.label for bucket in event.buckets] == ["23°C", "24°C"]
    assert event.settlement_date is None


def test_parse_event_payload_reads_end_date() -> None:
    payload = _payload()
    payload["endDate"] = "2026-05-14T16:00:00Z"
    event = parse_event_payload(payload)

    assert event.settlement_date == date(2026, 5, 14)


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


def test_strip_untradeable_bucket_prices_clears_resolved_quotes() -> None:
    event = PolymarketEvent(
        event_id="evt-1",
        slug="slug",
        title="title",
        settlement_date=date(2026, 6, 17),
        buckets=[
            PolymarketBucket(
                market_id="m1",
                label="26°C",
                yes_bid=0.99,
                yes_ask=1.0,
                liquidity_usd=10.0,
                spread=0.01,
                rules=None,
                resolved=True,
                outcome="YES",
            )
        ],
    )
    stripped = strip_untradeable_bucket_prices(event)
    assert stripped.buckets[0].yes_ask is None
    assert stripped.buckets[0].yes_bid is None


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


def test_first_parseable_weather_event_returns_none_for_empty_list() -> None:
    assert first_parseable_weather_event([]) is None


def test_first_parseable_weather_event_skips_unparseable_buckets() -> None:
    bad = parse_event_payload(
        {
            "id": "bad",
            "title": "Bad",
            "markets": [{"id": "x", "groupItemTitle": "Yes"}],
        }
    )
    good = parse_event_payload(_payload())

    assert first_parseable_weather_event([bad, good]) is good


def test_first_parseable_weather_event_returns_none_when_no_match() -> None:
    bad = parse_event_payload(
        {
            "id": "bad",
            "title": "Bad",
            "markets": [{"id": "x", "groupItemTitle": "Yes"}],
        }
    )

    assert first_parseable_weather_event([bad]) is None


def test_first_parseable_weather_event_filters_by_city_in_title_or_slug() -> None:
    hk = parse_event_payload(
        {
            "id": "hk",
            "title": "Hong Kong max temp",
            "slug": "hong-kong-max",
            "markets": [{"id": "m", "groupItemTitle": "25°C"}],
        }
    )
    lon = parse_event_payload(
        {
            "id": "lon",
            "title": "Highest temperature in London on …",
            "slug": "highest-temperature-in-london-on",
            "markets": [{"id": "m", "groupItemTitle": "24°C"}],
        }
    )

    assert first_parseable_weather_event([hk, lon], city="london") is lon
    assert first_parseable_weather_event([hk, lon], city="hong kong") is hk


def test_first_parseable_weather_event_filters_by_settlement_date() -> None:
    d1 = date(2026, 5, 10)
    d2 = date(2026, 5, 14)
    wrong_day = parse_event_payload(
        {
            "id": "a",
            "title": "London A",
            "slug": "london-a",
            "endDate": f"{d1.isoformat()}T12:00:00Z",
            "markets": [{"id": "m", "groupItemTitle": "20°C"}],
        }
    )
    right_day = parse_event_payload(
        {
            "id": "b",
            "title": "London B",
            "slug": "london-b",
            "endDate": f"{d2.isoformat()}T12:00:00Z",
            "markets": [{"id": "m", "groupItemTitle": "21°C"}],
        }
    )

    picked = first_parseable_weather_event(
        [wrong_day, right_day],
        city="london",
        settlement_date=d2,
    )
    assert picked is right_day


def test_first_parseable_weather_event_skips_lowest_temperature_event() -> None:
    settlement = date(2026, 7, 2)
    # Lowest market is listed first (Gamma ranks it above by volume) but must be skipped.
    lowest = parse_event_payload(
        {
            "id": "650120",
            "title": "Lowest temperature in London on July 2?",
            "slug": "lowest-temperature-in-london-on-july-2-2026",
            "endDate": f"{settlement.isoformat()}T12:00:00Z",
            "markets": [{"id": "m", "groupItemTitle": "12°C or below"}],
        }
    )
    highest = parse_event_payload(
        {
            "id": "650208",
            "title": "Highest temperature in London on July 2?",
            "slug": "highest-temperature-in-london-on-july-2-2026",
            "endDate": f"{settlement.isoformat()}T12:00:00Z",
            "markets": [{"id": "m", "groupItemTitle": "25°C"}],
        }
    )

    picked = first_parseable_weather_event(
        [lowest, highest],
        city="london",
        settlement_date=settlement,
    )
    assert picked is highest


def test_first_parseable_weather_event_returns_none_when_only_lowest() -> None:
    settlement = date(2026, 7, 2)
    lowest = parse_event_payload(
        {
            "id": "650120",
            "title": "Lowest temperature in London on July 2?",
            "slug": "lowest-temperature-in-london-on-july-2-2026",
            "endDate": f"{settlement.isoformat()}T12:00:00Z",
            "markets": [{"id": "m", "groupItemTitle": "12°C or below"}],
        }
    )

    picked = first_parseable_weather_event(
        [lowest],
        city="london",
        settlement_date=settlement,
    )
    assert picked is None


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


def test_fetch_weather_events_end_on_date_adds_end_date_range_params(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict] = []

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> list[dict]:
            return [_payload()]

    def fake_get(url: str, params: dict) -> FakeResponse:
        calls.append(params)
        return FakeResponse()

    monkeypatch.setattr(httpx, "get", fake_get)

    fetch_weather_events(limit=3, base_url="https://example.test/", end_on_date=date(2026, 5, 14))

    assert calls[0]["end_date_min"] == "2026-05-14T00:00:00Z"
    assert calls[0]["end_date_max"] == "2026-05-14T23:59:59Z"


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


def test_resolved_bucket_parses_outcome_yes() -> None:
    from polytempo.markets.polymarket import is_event_resolved, winning_label_from_event

    payload = {
        "id": "e1", "title": "t", "slug": "s",
        "markets": [
            {"id": "m1", "groupItemTitle": "23°C", "closed": True, "outcomePrices": ["1", "0"]},
            {"id": "m2", "groupItemTitle": "24°C", "closed": True, "outcomePrices": ["0", "1"]},
        ],
    }
    event = parse_event_payload(payload)
    assert event.buckets[0].resolved is True
    assert event.buckets[0].outcome == "YES"
    assert event.buckets[1].outcome == "NO"
    assert is_event_resolved(event) is True
    assert winning_label_from_event(event) == "23°C"


def test_outcome_prices_accepts_json_string() -> None:
    payload = {
        "id": "e1", "title": "t", "slug": "s",
        "markets": [
            {"id": "m1", "groupItemTitle": "23°C", "closed": True, "outcomePrices": "[\"1\", \"0\"]"},
        ],
    }
    event = parse_event_payload(payload)
    assert event.buckets[0].resolved is True
    assert event.buckets[0].outcome == "YES"


def test_parse_event_payload_reads_clob_token_id_from_list() -> None:
    payload = _payload()
    payload["markets"][0]["clobTokenIds"] = ["yes-token-1", "no-token-1"]
    event = parse_event_payload(payload)

    assert event.buckets[0].yes_token_id == "yes-token-1"


def test_parse_event_payload_reads_clob_token_id_from_json_string() -> None:
    payload = _payload()
    payload["markets"][0]["clobTokenIds"] = '["yes-token-1", "no-token-1"]'
    event = parse_event_payload(payload)

    assert event.buckets[0].yes_token_id == "yes-token-1"


def test_parse_event_payload_missing_clob_token_id_is_none() -> None:
    event = parse_event_payload(_payload())

    assert event.buckets[0].yes_token_id is None


def _books_response() -> list[dict]:
    return [
        {
            "asset_id": "tok-bid-ask",
            "bids": [{"price": "0.40", "size": "100"}, {"price": "0.42", "size": "50"}],
            "asks": [{"price": "0.48", "size": "30"}, {"price": "0.45", "size": "20"}],
        },
        {
            "asset_id": "tok-empty-ask",
            "bids": [{"price": "0.99", "size": "10"}],
            "asks": [],
        },
    ]


def _patch_books(monkeypatch: pytest.MonkeyPatch, response: list[dict]) -> list[dict]:
    calls: list[dict] = []

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> list[dict]:
            return response

    def fake_post(url: str, json: list[dict]) -> FakeResponse:
        calls.append({"url": url, "body": json})
        return FakeResponse()

    monkeypatch.setattr(httpx, "post", fake_post)
    return calls


def test_fetch_clob_book_reduces_book_to_best_quotes(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_books(monkeypatch, _books_response())

    quotes = fetch_clob_book(["tok-bid-ask", "tok-empty-ask"], base_url="https://clob.test/")

    assert calls[0]["url"] == "https://clob.test/books"
    assert calls[0]["body"] == [{"token_id": "tok-bid-ask"}, {"token_id": "tok-empty-ask"}]

    q = quotes["tok-bid-ask"]
    assert q.yes_bid == pytest.approx(0.42)  # highest bid
    assert q.yes_ask == pytest.approx(0.45)  # lowest ask
    assert q.spread == pytest.approx(0.03)
    # notional = 100*0.40 + 50*0.42 + 30*0.48 + 20*0.45 = 84.4
    assert q.liquidity_usd == pytest.approx(84.4)


def test_fetch_clob_book_empty_side_stays_none(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_books(monkeypatch, _books_response())

    q = fetch_clob_book(["tok-empty-ask"])["tok-empty-ask"]

    assert q.yes_bid == pytest.approx(0.99)
    assert q.yes_ask is None
    assert q.spread is None


def test_fetch_clob_book_empty_token_list_skips_request(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_post(*args: object, **kwargs: object) -> None:
        raise AssertionError("should not call /books with no tokens")

    monkeypatch.setattr(httpx, "post", fail_post)

    assert fetch_clob_book([]) == {}


def test_hydrate_prices_overwrites_gamma_with_live_book(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = _payload()
    payload["markets"][0]["clobTokenIds"] = ["tok-bid-ask", "no"]
    # phantom Gamma ask with an empty live ask side -> becomes None after hydration
    payload["markets"][1]["bestAsk"] = 1.0
    payload["markets"][1]["clobTokenIds"] = ["tok-empty-ask", "no"]
    event = parse_event_payload(payload)
    _patch_books(monkeypatch, _books_response())

    hydrated = hydrate_prices(event, base_url="https://clob.test/")

    live = hydrated.buckets[0]
    assert live.yes_bid == pytest.approx(0.42)
    assert live.yes_ask == pytest.approx(0.45)
    assert live.spread == pytest.approx(0.03)
    assert live.liquidity_usd == pytest.approx(84.4)

    phantom = hydrated.buckets[1]
    assert phantom.yes_ask is None  # Gamma's bestAsk=1.0 replaced by empty live book


def test_hydrate_prices_no_token_ids_returns_same_event(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_post(*args: object, **kwargs: object) -> None:
        raise AssertionError("should not call /books when no buckets have token ids")

    monkeypatch.setattr(httpx, "post", fail_post)
    event = parse_event_payload(_payload())

    assert hydrate_prices(event) is event


def test_unresolved_event_has_no_winner() -> None:
    from polytempo.markets.polymarket import is_event_resolved, winning_label_from_event

    payload = {
        "id": "e1", "title": "t", "slug": "s",
        "markets": [
            {"id": "m1", "groupItemTitle": "23°C"},
            {"id": "m2", "groupItemTitle": "24°C"},
        ],
    }
    event = parse_event_payload(payload)
    assert is_event_resolved(event) is False
    assert winning_label_from_event(event) is None
