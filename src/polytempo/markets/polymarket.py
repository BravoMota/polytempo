"""Polymarket market ingestion.

Fetches and normalizes weather market events, bucket prices, bid/ask, liquidity,
and rules. Should not make trading decisions.
"""

from __future__ import annotations

from dataclasses import dataclass

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

    return PolymarketEvent(
        event_id=str(event_id),
        slug=str(payload.get("slug") or ""),
        title=str(title),
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
) -> list[PolymarketEvent]:
    """Fetch active weather-tagged events, ordered as popular by volume."""
    if limit <= 0:
        raise ValueError("limit must be positive")

    url = f"{base_url.rstrip('/')}/events"
    response = httpx.get(
        url,
        params={
            "tag_slug": "weather",
            "limit": limit,
            "active": "true",
            "closed": "false",
            "order": "volume",
            "ascending": "false",
        },
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list):
        raise ValueError("events response must be a list")
    return [parse_event_payload(event_payload) for event_payload in payload]


def _to_optional_float(value: object, field_name: str) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be numeric") from exc
