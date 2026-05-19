"""Polymarket market ingestion.

Fetches and normalizes weather market events, bucket prices, bid/ask, liquidity,
and rules. Should not make trading decisions.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone

import httpx

from polytempo.strategy.edge import MarketPrice


@dataclass(frozen=True)
class PolymarketBucket:
    """Normalized market row for one temperature bucket."""

    market_id: str
    label: str
    yes_bid: float | None
    yes_ask: float | None
    liquidity_usd: float | None
    spread: float | None
    rules: str | None


@dataclass(frozen=True)
class PolymarketEvent:
    """Normalized Gamma event containing temperature bucket markets."""

    event_id: str
    slug: str
    title: str
    settlement_date: date | None
    buckets: list[PolymarketBucket]


def parse_event_payload(payload: dict) -> PolymarketEvent:
    """Parse a Gamma event payload into a normalized PolymarketEvent."""
    event_id = payload.get("id")
    title = payload.get("title")
    markets = payload.get("markets")

    if event_id in (None, ""):
        raise ValueError("event id is required")
    if title in (None, ""):
        raise ValueError("event title is required")
    if not isinstance(markets, list):
        raise ValueError("event markets are required")

    buckets: list[PolymarketBucket] = []
    for market in markets:
        if not isinstance(market, dict):
            raise ValueError("event markets must contain objects")

        market_id = market.get("id")
        label = market.get("groupItemTitle") or market.get("question") or ""
        buckets.append(
            PolymarketBucket(
                market_id=str(market_id) if market_id is not None else "",
                label=str(label),
                yes_bid=_to_optional_float(market.get("bestBid"), "bestBid"),
                yes_ask=_to_optional_float(market.get("bestAsk"), "bestAsk"),
                liquidity_usd=_to_optional_float(
                    market.get("liquidityNum"),
                    "liquidityNum",
                ),
                spread=_to_optional_float(market.get("spread"), "spread"),
                rules=market.get("description"),
            )
        )

    settlement_date = _parse_settlement_date(
        payload.get("endDate") or payload.get("endDateIso"),
    )

    return PolymarketEvent(
        event_id=str(event_id),
        slug=str(payload.get("slug") or ""),
        title=str(title),
        settlement_date=settlement_date,
        buckets=buckets,
    )


def to_market_prices(event: PolymarketEvent) -> list[MarketPrice]:
    """Convert normalized Polymarket buckets into internal MarketPrice rows."""
    return [
        MarketPrice(
            label=bucket.label,
            yes_bid=bucket.yes_bid,
            yes_ask=bucket.yes_ask,
            liquidity_usd=bucket.liquidity_usd,
            spread=bucket.spread,
        )
        for bucket in event.buckets
    ]


def fetch_event(
    event_id: str,
    base_url: str = "https://gamma-api.polymarket.com",
) -> PolymarketEvent:
    """Fetch a Gamma event by id and parse it into normalized market data."""
    if not event_id.strip():
        raise ValueError("event_id must not be empty")

    url = f"{base_url.rstrip('/')}/events/{event_id}"
    response = httpx.get(url)
    response.raise_for_status()
    return parse_event_payload(response.json())


def fetch_weather_events(
    limit: int = 20,
    base_url: str = "https://gamma-api.polymarket.com",
    *,
    end_on_date: date | None = None,
) -> list[PolymarketEvent]:
    """Fetch active weather-tagged events, ordered as popular by volume.

    When ``end_on_date`` is set, asks Gamma for events whose ``endDate`` falls on
    that UTC calendar day (``end_date_min`` / ``end_date_max`` query params).
    """
    if limit <= 0:
        raise ValueError("limit must be positive")

    params: dict[str, str | int | bool] = {
        "tag_slug": "weather",
        "limit": limit,
        "active": "true",
        "closed": "false",
        "order": "volume",
        "ascending": "false",
    }
    if end_on_date is not None:
        day = end_on_date.isoformat()
        params["end_date_min"] = f"{day}T00:00:00Z"
        params["end_date_max"] = f"{day}T23:59:59Z"

    url = f"{base_url.rstrip('/')}/events"
    response = httpx.get(
        url,
        params=params,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list):
        raise ValueError("events response must be a list")
    return [parse_event_payload(event_payload) for event_payload in payload]


def first_parseable_weather_event(
    events: list[PolymarketEvent],
    *,
    city: str | None = None,
    settlement_date: date | None = None,
) -> PolymarketEvent | None:
    """Return the first event whose bucket labels parse as Celsius temperature buckets.

    If ``city`` is set, only events whose title or slug contains that substring
    (case-insensitive) are considered.

    If ``settlement_date`` is set, only events whose parsed Gamma ``endDate``
    matches that calendar day are considered (events without a parsed date are
    excluded).
    """
    from polytempo.markets.buckets import parse_temperature_bucket

    if city is not None:
        needle = city.strip().lower()
        if needle:
            events = [
                event
                for event in events
                if needle in event.title.lower() or needle in event.slug.lower()
            ]

    if settlement_date is not None:
        events = [
            event
            for event in events
            if event.settlement_date == settlement_date
        ]

    for event in events:
        if not event.buckets:
            continue
        try:
            for bucket in event.buckets:
                parse_temperature_bucket(bucket.label)
        except ValueError:
            continue
        return event
    return None


def _parse_settlement_date(value: object) -> date | None:
    """Parse Gamma ``endDate`` / ``endDateIso`` into a calendar date (UTC)."""
    if value in (None, "", False):
        return None
    if isinstance(value, (int, float)):
        ts = float(value)
        if ts > 1e12:
            ts /= 1000.0
        return datetime.fromtimestamp(ts, tz=timezone.utc).date()
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        try:
            return date.fromisoformat(text[:10])
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).date()


def _to_optional_float(value: object, field_name: str) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be numeric") from exc
