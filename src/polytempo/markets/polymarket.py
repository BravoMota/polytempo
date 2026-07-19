"""Polymarket market ingestion.

Fetches and normalizes weather market events, bucket prices, bid/ask, liquidity,
and rules. Should not make trading decisions.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, timezone

import httpx

from polytempo.strategy.edge import MarketPrice


@dataclass(frozen=True)
class PolymarketBucket:
    """Normalized market row for one temperature bucket.

    ``resolved`` is True once the underlying market is closed and Gamma has
    posted outcome prices. ``outcome`` is "YES" if the YES leg paid (this is
    the winning bucket), "NO" otherwise. Both are None on unresolved markets.
    """

    market_id: str
    label: str
    yes_bid: float | None
    yes_ask: float | None
    liquidity_usd: float | None
    spread: float | None
    rules: str | None
    resolved: bool = False
    outcome: str | None = None
    yes_token_id: str | None = None
    no_token_id: str | None = None


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
        resolved, outcome = _parse_resolution(market)
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
                resolved=resolved,
                outcome=outcome,
                yes_token_id=_parse_clob_token_id(market.get("clobTokenIds"), 0),
                no_token_id=_parse_clob_token_id(market.get("clobTokenIds"), 1),
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


def winning_label_from_event(event: PolymarketEvent) -> str | None:
    """Return the bucket label whose YES leg resolved, or None if undetermined.

    Polymarket weather events are a set of mutually-exclusive bucket markets;
    exactly one bucket should resolve YES. Returns None if zero or more than
    one bucket reports a YES outcome (event is unresolved or in a weird state).
    """
    winners = [b.label for b in event.buckets if b.resolved and b.outcome == "YES"]
    if len(winners) == 1:
        return winners[0]
    return None


def is_event_resolved(event: PolymarketEvent) -> bool:
    """True once at least one bucket has a YES/NO outcome from Gamma."""
    return any(bucket.resolved for bucket in event.buckets)


UNTRADEABLE_YES_ASK = 0.999


def is_untradeable_market_price(
    yes_bid: float | None,
    yes_ask: float | None,
) -> bool:
    """True for post-settlement or otherwise non-executable book quotes."""
    _ = yes_bid
    return yes_ask is not None and yes_ask >= UNTRADEABLE_YES_ASK


def strip_untradeable_bucket_prices(event: PolymarketEvent) -> PolymarketEvent:
    """Clear bid/ask on resolved buckets and other untradeable quotes."""
    buckets: list[PolymarketBucket] = []
    for bucket in event.buckets:
        if bucket.resolved or is_untradeable_market_price(bucket.yes_bid, bucket.yes_ask):
            buckets.append(replace(bucket, yes_bid=None, yes_ask=None, spread=None))
        else:
            buckets.append(bucket)
    return replace(event, buckets=buckets)


def _parse_resolution(market: dict) -> tuple[bool, str | None]:
    """Map Gamma ``closed`` + ``outcomePrices`` to (resolved, "YES"|"NO"|None).

    ``outcomePrices`` is sometimes a JSON-encoded string like '["1","0"]' and
    sometimes a list. The first element is the YES payout (1 means YES won).
    """
    closed = bool(market.get("closed"))
    raw = market.get("outcomePrices")
    if isinstance(raw, str):
        text = raw.strip()
        if text.startswith("["):
            import json

            try:
                raw = json.loads(text)
            except ValueError:
                raw = None
    if not isinstance(raw, list) or not raw:
        return closed, None
    yes_price = raw[0]
    try:
        yes_value = float(yes_price)
    except (TypeError, ValueError):
        return closed, None
    if yes_value == 1.0:
        return True, "YES"
    if yes_value == 0.0:
        return True, "NO"
    return closed, None


@dataclass(frozen=True)
class ClobQuote:
    """Live best bid/ask, spread, and executable liquidity from the CLOB book."""

    yes_bid: float | None
    yes_ask: float | None
    spread: float | None
    liquidity_usd: float | None


def fetch_clob_book(
    token_ids: list[str],
    base_url: str = "https://clob.polymarket.com",
) -> dict[str, ClobQuote]:
    """Fetch live order books for ``token_ids`` via the CLOB ``POST /books`` endpoint.

    Returns a map from token id to its ``ClobQuote``. Best bid is the highest resting
    bid price, best ask the lowest resting ask price; an empty side stays ``None``
    (never synthesized to 0/1). ``liquidity_usd`` is the total notional resting on
    both sides (sum of ``size * price``).
    """
    if not token_ids:
        return {}

    url = f"{base_url.rstrip('/')}/books"
    response = httpx.post(url, json=[{"token_id": token_id} for token_id in token_ids])
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list):
        raise ValueError("books response must be a list")

    quotes: dict[str, ClobQuote] = {}
    for book in payload:
        if not isinstance(book, dict):
            continue
        asset_id = book.get("asset_id")
        if asset_id is None:
            continue
        quotes[str(asset_id)] = _quote_from_book(book)
    return quotes


def hydrate_prices(
    event: PolymarketEvent,
    base_url: str = "https://clob.polymarket.com",
) -> PolymarketEvent:
    """Return ``event`` with bucket prices/liquidity replaced by live CLOB book data.

    Gamma's ``bestBid``/``bestAsk``/``liquidityNum`` are cached snapshots that lag the
    live book and can quote phantom prices on an empty side. This overwrites
    ``yes_bid``/``yes_ask``/``spread``/``liquidity_usd`` from the live CLOB book for
    every bucket that carries a YES token id. Buckets without a token id (or with an
    empty book) are left as-is. Call only on decision paths (strategy/edge); discovery
    and listing paths can keep raw Gamma data.
    """
    token_ids = [b.yes_token_id for b in event.buckets if b.yes_token_id]
    if not token_ids:
        return event

    quotes = fetch_clob_book(token_ids, base_url=base_url)

    hydrated: list[PolymarketBucket] = []
    for bucket in event.buckets:
        quote = quotes.get(bucket.yes_token_id) if bucket.yes_token_id else None
        if quote is None:
            hydrated.append(bucket)
            continue
        hydrated.append(
            replace(
                bucket,
                yes_bid=quote.yes_bid,
                yes_ask=quote.yes_ask,
                spread=quote.spread,
                liquidity_usd=quote.liquidity_usd,
            )
        )
    return replace(event, buckets=hydrated)


def _quote_from_book(book: dict) -> ClobQuote:
    """Reduce one CLOB ``/books`` order book into best bid/ask, spread, liquidity."""
    bids = book.get("bids") or []
    asks = book.get("asks") or []
    best_bid = max((float(b["price"]) for b in bids), default=None)
    best_ask = min((float(a["price"]) for a in asks), default=None)
    spread = best_ask - best_bid if best_bid is not None and best_ask is not None else None
    notional = sum(float(o["size"]) * float(o["price"]) for o in (*bids, *asks))
    liquidity_usd = notional if notional > 0 else None
    return ClobQuote(
        yes_bid=best_bid,
        yes_ask=best_ask,
        spread=spread,
        liquidity_usd=liquidity_usd,
    )


def _parse_clob_token_id(value: object, index: int) -> str | None:
    """Extract one clob token id from Gamma ``clobTokenIds`` (``[0]`` YES, ``[1]`` NO).

    Like ``outcomePrices``, the field is sometimes a JSON-encoded string and
    sometimes a list. The first element is the YES outcome token, the second NO.
    """
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("["):
            import json

            try:
                value = json.loads(text)
            except ValueError:
                return None
    if not isinstance(value, list) or len(value) <= index:
        return None
    token = value[index]
    return str(token) if token not in (None, "") else None


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
    city: str | None = None,
) -> list[PolymarketEvent]:
    """Fetch active weather-tagged events, ordered as popular by volume.

    When ``end_on_date`` is set, asks Gamma for events whose ``endDate`` falls on
    that UTC calendar day (``end_date_min`` / ``end_date_max`` query params).

    When ``city`` is set, passes ``title_search`` so Gamma narrows results to that
    city instead of only the global top ``limit`` weather events by volume.
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
    if city is not None:
        needle = city.strip().lower()
        if needle:
            params["title_search"] = needle

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


def is_lowest_temperature_event(event: PolymarketEvent) -> bool:
    """True for Polymarket *lowest* daily-temperature markets.

    These settle on the daily minimum temperature; this project resolves daily
    highest (Tmax) markets only, so they must never be selected.
    """
    return "lowest" in f"{event.title} {event.slug}".lower()


def first_parseable_weather_event(
    events: list[PolymarketEvent],
    *,
    city: str | None = None,
    settlement_date: date | None = None,
) -> PolymarketEvent | None:
    """Return the first event whose bucket labels parse as Celsius temperature buckets.

    This project resolves daily *highest* temperature markets only; lowest-temperature
    events (e.g. "Lowest temperature in London on July 2?") are always ignored, even
    when Gamma ranks them above the matching highest market by volume.

    If ``city`` is set, only events whose title or slug contains that substring
    (case-insensitive) are considered.

    If ``settlement_date`` is set, only events whose parsed Gamma ``endDate``
    matches that calendar day are considered (events without a parsed date are
    excluded).
    """
    from polytempo.markets.buckets import parse_temperature_bucket

    events = [event for event in events if not is_lowest_temperature_event(event)]

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
