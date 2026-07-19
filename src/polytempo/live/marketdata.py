"""Full-depth CLOB order-book fetching for the live execution stack.

Mirrors ``markets/polymarket.fetch_clob_book`` (same ``POST /books`` request)
but keeps every resting level so the sizer can walk depth, not just the top of
book. Read-only: makes no trading decisions.
"""

from __future__ import annotations

from datetime import datetime, timezone

import httpx

from polytempo.live.models import BookDepth, BookLevel


def fetch_book_depth(
    token_ids: list[str],
    base_url: str = "https://clob.polymarket.com",
) -> dict[str, BookDepth]:
    """Fetch full-depth order books for ``token_ids`` via CLOB ``POST /books``.

    Returns a map from token id to its ``BookDepth`` with ``bids`` best-first
    (descending price) and ``asks`` best-first (ascending price). Malformed
    books (missing ``asset_id`` or unparseable levels) are skipped.
    """
    if not token_ids:
        return {}

    url = f"{base_url.rstrip('/')}/books"
    response = httpx.post(url, json=[{"token_id": token_id} for token_id in token_ids])
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list):
        raise ValueError("books response must be a list")

    ts_utc = datetime.now(timezone.utc).isoformat()
    books: dict[str, BookDepth] = {}
    for book in payload:
        depth = _depth_from_book(book, ts_utc)
        if depth is not None:
            books[depth.token_id] = depth
    return books


def _depth_from_book(book: object, ts_utc: str) -> BookDepth | None:
    """Build a ``BookDepth`` from one raw CLOB book, or ``None`` if malformed."""
    if not isinstance(book, dict):
        return None
    asset_id = book.get("asset_id")
    if asset_id is None:
        return None
    try:
        bids = _levels(book.get("bids"), descending=True)
        asks = _levels(book.get("asks"), descending=False)
    except (TypeError, ValueError, KeyError):
        return None
    return BookDepth(token_id=str(asset_id), bids=bids, asks=asks, ts_utc=ts_utc)


def _levels(raw: object, *, descending: bool) -> tuple[BookLevel, ...]:
    """Parse and sort one book side into ``BookLevel`` tuples."""
    levels = [
        BookLevel(price=float(level["price"]), size=float(level["size"]))
        for level in (raw or [])
    ]
    levels.sort(key=lambda level: level.price, reverse=descending)
    return tuple(levels)
