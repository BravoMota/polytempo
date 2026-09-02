"""Tests for distribution-explorer data assembly."""

from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock

import pytest

from polytempo.analysis import MODEL_STRATEGIES
from polytempo.markets.polymarket import PolymarketBucket, PolymarketEvent
from polytempo.visualizer.distribution_data import strat_overlays
from polytempo.weather.schema import ForecastValues


def _forecast() -> ForecastValues:
    return ForecastValues(
        source="test",
        latitude=51.5,
        longitude=-0.1,
        target_date=date(2026, 8, 1),
        values_c=[24.0],
        models=["ecmwf_ifs"],
    )


def _event() -> PolymarketEvent:
    return PolymarketEvent(
        event_id="evt-1",
        slug="slug",
        title="title",
        settlement_date=date(2026, 8, 1),
        buckets=[
            PolymarketBucket(
                market_id="m1",
                label="24°C",
                yes_bid=0.1,
                yes_ask=0.12,
                liquidity_usd=None,
                spread=None,
                rules=None,
            )
        ],
    )


def _analysis(strategy: str) -> MagicMock:
    result = MagicMock()
    result.distribution_mean_c = 20.0
    result.distribution_sigma_c = 1.2
    result.rows = []
    result.selected_model = None
    result.fallback_reason = None
    result.model_strategy = strategy
    return result


def test_strat_overlays_computes_only_requested(monkeypatch) -> None:
    called: list[str] = []

    def fake_analyze(_forecast, _event, **kwargs):
        strategy = kwargs["model_strategy"]
        called.append(strategy)
        return _analysis(strategy)

    monkeypatch.setattr(
        "polytempo.visualizer.distribution_data.analyze_event", fake_analyze
    )
    overlays, skipped = strat_overlays(
        _forecast(),
        _event(),
        station_id="EGLC",
        lead_hours=24.0,
        strategies=(
            "weighted_historical_market_sigma",
            "weighted_historical_updated_sharp",
        ),
    )
    assert skipped == []
    assert called == [
        "weighted_historical_market_sigma",
        "weighted_historical_updated_sharp",
    ]
    assert [o.strategy for o in overlays] == called


def test_distribution_chart_colors_cover_all_model_strategies() -> None:
    pytest.importorskip("plotly")
    from polytempo.visualizer.distribution_chart import STRAT_COLORS

    missing = set(MODEL_STRATEGIES) - set(STRAT_COLORS)
    assert missing == set()


def test_strat_overlays_empty_strategies_skips_analysis(monkeypatch) -> None:
    def fail_analyze(*_args, **_kwargs):
        raise AssertionError("analyze_event should not run")

    monkeypatch.setattr(
        "polytempo.visualizer.distribution_data.analyze_event", fail_analyze
    )
    overlays, skipped = strat_overlays(
        _forecast(),
        _event(),
        station_id="EGLC",
        lead_hours=24.0,
        strategies=(),
    )
    assert overlays == []
    assert skipped == []
