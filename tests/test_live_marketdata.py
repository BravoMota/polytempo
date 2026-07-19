"""Tests for full-depth CLOB book fetching (polytempo.live.marketdata).

No network: ``httpx.post`` is monkeypatched with a small recording fake that
exposes ``.raise_for_status()`` and ``.json()``.
"""

from __future__ import annotations

from datetime import datetime

import httpx
import pytest

from polytempo.live import marketdata
from polytempo.live.marketdata import fetch_book_depth


class FakeResponse:
    def __init__(self, payload: object, *, error: Exception | None = None) -> None:
        self._payload = payload
        self._error = error

    def raise_for_status(self) -> None:
        if self._error is not None:
            raise self._error

    def json(self) -> object:
        return self._payload


class FakePost:
    """Records calls and returns a canned response."""

    def __init__(self, response: FakeResponse) -> None:
        self.response = response
        self.calls: list[tuple[str, object]] = []

    def __call__(self, url: str, *, json: object) -> FakeResponse:
        self.calls.append((url, json))
        return self.response


def _install(monkeypatch, payload: object, *, error: Exception | None = None) -> FakePost:
    fake = FakePost(FakeResponse(payload, error=error))
    monkeypatch.setattr(marketdata.httpx, "post", fake)
    return fake


def _book(asset_id: str, bids: list, asks: list) -> dict:
    return {
        "asset_id": asset_id,
        "bids": [{"price": p, "size": s} for p, s in bids],
        "asks": [{"price": p, "size": s} for p, s in asks],
    }


def test_empty_token_ids_returns_empty_no_network(monkeypatch) -> None:
    fake = _install(monkeypatch, [])

    assert fetch_book_depth([]) == {}
    assert fake.calls == []  # never hit the network


def test_happy_path_two_books_sorted_regardless_of_input_order(monkeypatch) -> None:
    payload = [
        # bids given ascending, asks given descending -> must come out reversed.
        _book("t1", bids=[(0.40, 10), (0.45, 20)], asks=[(0.60, 5), (0.55, 8)]),
        _book("t2", bids=[(0.30, 1)], asks=[(0.70, 2)]),
    ]
    _install(monkeypatch, payload)

    books = fetch_book_depth(["t1", "t2"])

    assert set(books) == {"t1", "t2"}
    b1 = books["t1"]
    assert [lvl.price for lvl in b1.bids] == [0.45, 0.40]  # descending
    assert [lvl.price for lvl in b1.asks] == [0.55, 0.60]  # ascending
    assert b1.best_bid == 0.45
    assert b1.best_ask == 0.55
    # ts_utc is a shared, parseable ISO timestamp.
    assert b1.ts_utc == books["t2"].ts_utc
    assert datetime.fromisoformat(b1.ts_utc)


def test_string_prices_and_sizes_parsed_to_floats(monkeypatch) -> None:
    payload = [_book("t1", bids=[("0.45", "20")], asks=[("0.55", "8.5")])]
    _install(monkeypatch, payload)

    book = fetch_book_depth(["t1"])["t1"]

    assert book.bids[0].price == 0.45
    assert book.bids[0].size == 20.0
    assert book.asks[0].price == 0.55
    assert book.asks[0].size == 8.5
    assert isinstance(book.bids[0].price, float)


def test_non_dict_book_entry_is_skipped(monkeypatch) -> None:
    payload = ["not a dict", _book("t1", bids=[(0.45, 1)], asks=[(0.55, 1)])]
    _install(monkeypatch, payload)

    books = fetch_book_depth(["t1"])

    assert set(books) == {"t1"}


def test_book_missing_asset_id_is_skipped(monkeypatch) -> None:
    payload = [
        {"bids": [{"price": 0.4, "size": 1}], "asks": []},  # no asset_id
        _book("t1", bids=[(0.45, 1)], asks=[(0.55, 1)]),
    ]
    _install(monkeypatch, payload)

    books = fetch_book_depth(["t1"])

    assert set(books) == {"t1"}


def test_unparseable_or_incomplete_level_skips_that_book_only(monkeypatch) -> None:
    payload = [
        {"asset_id": "bad_missing", "bids": [{"size": 1}], "asks": []},  # no "price"
        {"asset_id": "bad_nan", "bids": [{"price": "abc", "size": 1}], "asks": []},
        _book("good", bids=[(0.45, 1)], asks=[(0.55, 1)]),
    ]
    _install(monkeypatch, payload)

    books = fetch_book_depth(["bad_missing", "bad_nan", "good"])

    assert set(books) == {"good"}


def test_non_list_payload_raises_value_error(monkeypatch) -> None:
    _install(monkeypatch, {"error": "nope"})

    with pytest.raises(ValueError):
        fetch_book_depth(["t1"])


def test_empty_sides_give_empty_tuples_and_none_tops(monkeypatch) -> None:
    payload = [{"asset_id": "t1", "bids": [], "asks": []}]
    _install(monkeypatch, payload)

    book = fetch_book_depth(["t1"])["t1"]

    assert book.bids == ()
    assert book.asks == ()
    assert book.best_bid is None
    assert book.best_ask is None


def test_request_posts_books_url_with_token_id_body(monkeypatch) -> None:
    fake = _install(monkeypatch, [])

    fetch_book_depth(["t1", "t2"], base_url="https://clob.example.com/")

    assert len(fake.calls) == 1
    url, body = fake.calls[0]
    assert url == "https://clob.example.com/books"  # trailing slash stripped
    assert body == [{"token_id": "t1"}, {"token_id": "t2"}]


def test_raise_for_status_error_propagates(monkeypatch) -> None:
    request = httpx.Request("POST", "https://clob.polymarket.com/books")
    error = httpx.HTTPStatusError(
        "500", request=request, response=httpx.Response(500, request=request)
    )
    _install(monkeypatch, [], error=error)

    with pytest.raises(httpx.HTTPStatusError):
        fetch_book_depth(["t1"])
