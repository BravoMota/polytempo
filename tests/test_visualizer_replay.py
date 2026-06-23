"""Tests for polytempo.visualizer.replay."""

from __future__ import annotations

from datetime import date
from unittest.mock import patch

from polytempo.analysis import AnalysisResult
from polytempo.markets.polymarket import PolymarketBucket, PolymarketEvent
from polytempo.model.distribution import DistributionBuildInfo
from polytempo.visualizer.replay import profile_by_id, replay_event_analysis
from polytempo.profiles.models import EntryGate, TradingProfile
from polytempo.storage.snapshot_reads import ClobSnapshotBundle, OpenMeteoSnapshotBundle
from polytempo.weather.schema import ForecastValues


def _mock_analysis() -> AnalysisResult:
    build = DistributionBuildInfo(
        values_used_c=[24.0],
        default_sigma_c=1.0,
        lead_hours=42.0,
        lead_hours_sigma_floor_c=1.2,
        ensemble_stdev_c=None,
        mean_c=24.0,
        sigma_c=1.2,
        method="lead_time_single_floor",
    )
    return AnalysisResult(
        distribution_mean_c=24.0,
        distribution_sigma_c=1.2,
        distribution_build=build,
        rows=[],
        model_strategy="best_historical",
    )


def _profile() -> TradingProfile:
    return TradingProfile(
        id="bh_argmax_yes_lead42",
        model_strategy="best_historical",
        trade_strategy="argmax_yes",
        entry_gate=EntryGate(target_lead_hours=42.0),
        city="london",
    )


def _event(*, yes_ask: float = 0.12) -> PolymarketEvent:
    return PolymarketEvent(
        event_id="evt-1",
        slug="slug",
        title="title",
        settlement_date=date(2026, 6, 17),
        buckets=[
            PolymarketBucket(
                market_id="m1",
                label="26°C",
                yes_bid=0.1,
                yes_ask=yes_ask,
                liquidity_usd=None,
                spread=None,
                rules=None,
            )
        ],
    )


def test_profile_by_id_finds_known_wallet() -> None:
    profile = profile_by_id("bh_argmax_yes_lead42")
    assert profile is not None
    assert profile.trade_strategy == "argmax_yes"


@patch("polytempo.visualizer.replay.analyze_event")
@patch("polytempo.visualizer.replay.fetch_event")
@patch("polytempo.visualizer.replay.fetch_nearest_clob_snapshot")
@patch("polytempo.visualizer.replay.fetch_nearest_open_meteo_forecast")
@patch("polytempo.visualizer.replay.build_event_settlement_dates")
@patch("polytempo.visualizer.replay.profile_by_id")
def test_replay_event_analysis_returns_result(
    mock_profile,
    mock_dates,
    mock_om,
    mock_clob,
    mock_fetch_event,
    mock_analyze,
) -> None:
    mock_profile.return_value = _profile()
    mock_dates.return_value = {"evt-1": date(2026, 6, 17)}
    mock_om.return_value = OpenMeteoSnapshotBundle(
        fetch_cycle_id=1,
        fetched_at_utc="2026-06-16T18:00:00Z",
        forecast=ForecastValues(
            source="test",
            latitude=51.5,
            longitude=0.05,
            target_date=date(2026, 6, 17),
            values_c=[24.0],
            models=["ukmo_seamless"],
        ),
    )
    mock_clob.return_value = ClobSnapshotBundle(
        poll_slot_utc="2026-06-16T18:00:00Z",
        rows=(
            {
                "bucket_label": "26°C",
                "yes_bid": 0.38,
                "yes_ask": 0.4,
                "spread": 0.02,
                "liquidity_usd": 10.0,
            },
        ),
    )
    mock_fetch_event.return_value = _event()
    mock_analyze.return_value = _mock_analysis()

    result, err = replay_event_analysis(
        profile_id="bh_argmax_yes_lead42",
        polymarket_event_id="evt-1",
        opened_at_utc="2026-06-16T18:00:00+00:00",
        lead_hours=42.0,
        model_strategy="best_historical",
        weather_database_url="postgresql://local/test",
    )
    assert err is None
    assert result is not None
    assert result.analysis.distribution_mean_c == 24.0
    mock_analyze.assert_called_once()


@patch("polytempo.visualizer.replay.analyze_event")
@patch("polytempo.visualizer.replay.fetch_event")
@patch("polytempo.visualizer.replay.fetch_nearest_clob_snapshot")
@patch("polytempo.visualizer.replay.fetch_nearest_open_meteo_forecast")
@patch("polytempo.visualizer.replay.build_event_settlement_dates")
@patch("polytempo.visualizer.replay.profile_by_id")
def test_replay_falls_back_to_open_prices_when_no_clob(
    mock_profile,
    mock_dates,
    mock_om,
    mock_clob,
    mock_fetch_event,
    mock_analyze,
) -> None:
    mock_profile.return_value = _profile()
    mock_dates.return_value = {"evt-1": date(2026, 6, 17)}
    mock_om.return_value = OpenMeteoSnapshotBundle(
        fetch_cycle_id=1,
        fetched_at_utc="2026-06-16T18:00:00Z",
        forecast=ForecastValues(
            source="test",
            latitude=51.5,
            longitude=0.05,
            target_date=date(2026, 6, 17),
            values_c=[24.0],
            models=["ukmo_seamless"],
        ),
    )
    mock_clob.return_value = None
    mock_fetch_event.return_value = _event(yes_ask=0.49)
    mock_analyze.return_value = _mock_analysis()

    result, err = replay_event_analysis(
        profile_id="bh_argmax_yes_lead42",
        polymarket_event_id="evt-1",
        opened_at_utc="2026-06-16T18:00:00+00:00",
        lead_hours=42.0,
        model_strategy="best_historical",
        open_bucket_prices={"26°C": (0.16, 0.18)},
        weather_database_url="postgresql://local/test",
    )
    assert err is None
    assert result is not None
    assert "partial market prices" in result.snapshot_sources.warnings[0]
    event_passed = mock_analyze.call_args.args[1]
    assert event_passed.buckets[0].yes_ask == 0.18


@patch("polytempo.visualizer.replay.analyze_event")
@patch("polytempo.visualizer.replay.fetch_event")
@patch("polytempo.visualizer.replay.fetch_nearest_clob_snapshot")
@patch("polytempo.visualizer.replay.fetch_nearest_open_meteo_forecast")
@patch("polytempo.visualizer.replay.build_event_settlement_dates")
@patch("polytempo.visualizer.replay.profile_by_id")
def test_replay_strips_resolved_gamma_prices(
    mock_profile,
    mock_dates,
    mock_om,
    mock_clob,
    mock_fetch_event,
    mock_analyze,
) -> None:
    mock_profile.return_value = _profile()
    mock_dates.return_value = {"evt-1": date(2026, 6, 17)}
    mock_om.return_value = OpenMeteoSnapshotBundle(
        fetch_cycle_id=1,
        fetched_at_utc="2026-06-16T18:00:00Z",
        forecast=ForecastValues(
            source="test",
            latitude=51.5,
            longitude=0.05,
            target_date=date(2026, 6, 17),
            values_c=[24.0],
            models=["ukmo_seamless"],
        ),
    )
    mock_clob.return_value = None
    mock_fetch_event.return_value = PolymarketEvent(
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
                liquidity_usd=None,
                spread=None,
                rules=None,
                resolved=True,
                outcome="YES",
            )
        ],
    )
    mock_analyze.return_value = _mock_analysis()

    result, err = replay_event_analysis(
        profile_id="bh_argmax_yes_lead42",
        polymarket_event_id="evt-1",
        opened_at_utc="2026-06-16T18:00:00+00:00",
        lead_hours=42.0,
        model_strategy="best_historical",
        open_bucket_prices={"26°C": (0.16, 0.18)},
        weather_database_url="postgresql://local/test",
    )
    assert err is None
    assert result is not None
    event_passed = mock_analyze.call_args.args[1]
    assert event_passed.buckets[0].yes_ask == 0.18
