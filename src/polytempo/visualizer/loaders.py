"""Cached data loaders for the performance viewer."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import streamlit as st

from polytempo.visualizer.csv_data import read_csv_raw
from polytempo.visualizer.distribution_data import (
    build_distribution_view,
    list_cities,
    list_resolution_dates,
)
from polytempo.visualizer.replay import replay_at_settlement, replay_event_analysis
from polytempo.visualizer.trades import fetch_realized_trades


@st.cache_data(show_spinner=False)
def load_csv(path_str: str, mtime: float):
    del mtime  # cache key only
    return read_csv_raw(Path(path_str))


@st.cache_data(show_spinner=False, ttl=300)
def load_realized_trades(
    profile_id: str,
    settlement_date: date,
    database_url: str,
):
    return fetch_realized_trades(
        profile_id,
        settlement_date,
        database_url=database_url,
    )


@st.cache_data(show_spinner=False, ttl=300)
def load_replay(
    profile_id: str,
    polymarket_event_id: str,
    opened_at_utc: str,
    lead_hours: float | None,
    model_strategy: str | None,
    weather_database_url: str,
    trade_signature: tuple[tuple[str, float | None, float | None], ...],
):
    open_bucket_prices = {b: (bid, ask) for b, bid, ask in trade_signature}
    return replay_event_analysis(
        profile_id=profile_id,
        polymarket_event_id=polymarket_event_id,
        opened_at_utc=opened_at_utc,
        lead_hours=lead_hours,
        model_strategy=model_strategy,
        open_bucket_prices=open_bucket_prices or None,
        weather_database_url=weather_database_url,
    )


@st.cache_data(show_spinner=False, ttl=300)
def load_settlement_replay(
    profile_id: str,
    settlement_date: date,
    weather_database_url: str,
    lead_hours: float | None,
    model_strategy: str | None,
):
    return replay_at_settlement(
        profile_id=profile_id,
        settlement_date=settlement_date,
        weather_database_url=weather_database_url,
        lead_hours=lead_hours,
        model_strategy=model_strategy,
    )


@st.cache_data(show_spinner=False, ttl=300)
def load_cities(weather_url: str):
    return list_cities(weather_url)


@st.cache_data(show_spinner=False, ttl=300)
def load_resolution_dates(city: str, weather_url: str):
    return list_resolution_dates(city, weather_url)


@st.cache_data(show_spinner=False, ttl=300)
def load_distribution_view(
    city: str,
    settlement_date: date,
    at_utc_iso: str,
    weather_url: str,
    calibration_source: str,
    strategies: tuple[str, ...] = (),
):
    return build_distribution_view(
        city=city,
        settlement_date=settlement_date,
        at_utc=datetime.fromisoformat(at_utc_iso),
        weather_url=weather_url,
        calibration_source=Path(calibration_source),
        strategies=strategies,
    )
