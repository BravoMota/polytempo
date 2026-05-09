"""Tests for edge calculation."""

import ast
from pathlib import Path

import pytest

from polytempo.strategy.edge import (
    BucketEdge,
    MarketPrice,
    ProbabilityQuote,
    calculate_bucket_edges,
    calculate_yes_edge,
)


def test_calculate_yes_edge_positive() -> None:
    assert calculate_yes_edge(0.42, 0.31) == pytest.approx(0.11)


def test_calculate_yes_edge_negative() -> None:
    assert calculate_yes_edge(0.25, 0.40) == pytest.approx(-0.15)


def test_calculate_yes_edge_invalid_model_probability_raises() -> None:
    with pytest.raises(ValueError, match="model_probability"):
        calculate_yes_edge(-0.01, 0.5)
    with pytest.raises(ValueError, match="model_probability"):
        calculate_yes_edge(1.01, 0.5)


def test_calculate_yes_edge_invalid_yes_ask_raises() -> None:
    with pytest.raises(ValueError, match="yes_ask"):
        calculate_yes_edge(0.5, -0.1)
    with pytest.raises(ValueError, match="yes_ask"):
        calculate_yes_edge(0.5, 1.1)


def test_calculate_bucket_edges_matches_by_label() -> None:
    probs = [
        ProbabilityQuote(label="23°C", probability=0.4),
        ProbabilityQuote(label="24°C", probability=0.35),
    ]
    prices = [
        MarketPrice(label="24°C", yes_bid=0.30, yes_ask=0.32),
        MarketPrice(label="23°C", yes_bid=0.38, yes_ask=0.40),
    ]
    edges = calculate_bucket_edges(probs, prices)
    assert len(edges) == 2
    by_label = {e.label: e for e in edges}
    assert by_label["23°C"].yes_ask == 0.40
    assert by_label["24°C"].yes_ask == 0.32


def test_calculate_bucket_edges_preserves_probability_order() -> None:
    probs = [
        ProbabilityQuote(label="A", probability=0.1),
        ProbabilityQuote(label="B", probability=0.2),
        ProbabilityQuote(label="C", probability=0.3),
    ]
    prices = [
        MarketPrice(label="C", yes_bid=0.1, yes_ask=0.12),
        MarketPrice(label="A", yes_bid=0.05, yes_ask=0.06),
        MarketPrice(label="B", yes_bid=0.15, yes_ask=0.18),
    ]
    edges = calculate_bucket_edges(probs, prices)
    assert [e.label for e in edges] == ["A", "B", "C"]


def test_missing_market_price_produces_none_edge() -> None:
    probs = [ProbabilityQuote(label="only-model", probability=0.5)]
    prices: list[MarketPrice] = []
    edges = calculate_bucket_edges(probs, prices)
    assert len(edges) == 1
    e = edges[0]
    assert e.yes_bid is None
    assert e.yes_ask is None
    assert e.edge_yes is None
    assert e.edge_yes_pp is None


def test_missing_yes_ask_produces_none_edge() -> None:
    probs = [ProbabilityQuote(label="x", probability=0.5)]
    prices = [MarketPrice(label="x", yes_bid=0.4, yes_ask=None)]
    edges = calculate_bucket_edges(probs, prices)
    assert edges[0].edge_yes is None
    assert edges[0].edge_yes_pp is None


def test_edge_yes_pp_is_percentage_points() -> None:
    probs = [ProbabilityQuote(label="x", probability=0.42)]
    prices = [MarketPrice(label="x", yes_bid=0.30, yes_ask=0.31)]
    edges = calculate_bucket_edges(probs, prices)
    assert edges[0].edge_yes == pytest.approx(0.11)
    assert edges[0].edge_yes_pp == pytest.approx(11.0)


def test_edge_module_does_not_import_distribution() -> None:
    root = Path(__file__).resolve().parents[1]
    edge_path = root / "src" / "polytempo" / "strategy" / "edge.py"
    source = edge_path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            assert mod != "polytempo.model.distribution"
        elif isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name != "polytempo.model.distribution"
