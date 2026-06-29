"""Distribution Explorer page: scrub time, overlay model/strategy/market distributions."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import streamlit as st

from polytempo.analysis import MODEL_STRATEGIES
from polytempo.model.lead_time import (
    end_of_target_day_utc,
    lead_hours_to_end_of_target_day,
)
from polytempo.storage.postgres import resolve_database_url
from polytempo.visualizer.distribution_chart import build_distribution_chart
from polytempo.visualizer.loaders import (
    load_cities,
    load_distribution_view,
    load_resolution_dates,
)
from polytempo.visualizer.styling import inject_no_inner_scroll_css
from polytempo.weather.calibration_stats_csv import (
    DEFAULT_CALIBRATION_STATS_CSV_PATH,
    DEFAULT_UPDATED_CALIBRATION_STATS_CSV_PATH,
)

_CALIBRATION_SOURCES = {
    "static (calibration_stats.csv)": DEFAULT_CALIBRATION_STATS_CSV_PATH,
    "updated (calibration_stats_updated.csv)": DEFAULT_UPDATED_CALIBRATION_STATS_CSV_PATH,
}

_SLIDER_STEP = timedelta(minutes=30)


def _floor_to_step(dt: datetime) -> datetime:
    """Floor a UTC instant to the 30-min slider grid (stable across reruns)."""
    return dt - timedelta(
        minutes=dt.minute % 30, seconds=dt.second, microseconds=dt.microsecond
    )


def _weather_url() -> str | None:
    try:
        return resolve_database_url()
    except RuntimeError:
        return None


def render_distribution_page() -> None:
    inject_no_inner_scroll_css()
    st.title("Distribution explorer")

    weather_url = _weather_url()
    if weather_url is None:
        st.info("Set POLYTEMPO_DATABASE_URL to load forecasts and market snapshots.")
        st.stop()

    cities = load_cities(weather_url)
    if not cities:
        st.warning("No CLOB snapshots in the weather DB yet.")
        st.stop()

    st.sidebar.header("Selection")
    default_city = "london" if "london" in cities else cities[0]
    city = st.sidebar.selectbox("city", cities, index=cities.index(default_city))

    dates = load_resolution_dates(city, weather_url)
    if not dates:
        st.warning(f"No resolution dates for {city}.")
        st.stop()
    settlement_date = st.sidebar.selectbox(
        "resolution date", dates, format_func=lambda d: d.isoformat()
    )

    source_label = st.sidebar.selectbox(
        "model metadata CSV", list(_CALIBRATION_SOURCES.keys())
    )
    calibration_source = _CALIBRATION_SOURCES[source_label]

    st.sidebar.header("Overlays")
    show_models = st.sidebar.toggle("Metadata models (PDF + mean)", value=True)
    show_strats = st.sidebar.toggle("Distribution strategies (PDF + mean + bars)", value=True)
    strat_filter = (
        st.sidebar.multiselect("strategies", list(MODEL_STRATEGIES), default=list(MODEL_STRATEGIES))
        if show_strats
        else []
    )
    show_market = st.sidebar.toggle("Market (yes_ask bars + mean)", value=True)
    show_resolved = st.sidebar.toggle("Resolved bucket", value=True)

    # Time slider anchored to resolution: right edge = lead 0 (clamped to now),
    # left edge = lead 72h. at_utc decreases lead as it moves right. Bounds are
    # floored to the slider grid so an unresolved day's right edge (= now) does
    # not drift every rerun — otherwise Streamlit treats the slider as a new
    # widget and snaps it back to the right (the "slider won't move" bug).
    end_of_day = end_of_target_day_utc(settlement_date)
    now = datetime.now(timezone.utc)
    t_right = _floor_to_step(min(now, end_of_day))
    t_left = end_of_day - timedelta(hours=72)
    if t_left >= t_right:  # resolution >72h away: fall back to a now-anchored window
        t_left = t_right - timedelta(hours=72)

    at_utc = st.slider(
        "snapshot time (UTC) — right = resolution / lead 0",
        min_value=t_left,
        max_value=t_right,
        value=t_right,
        step=_SLIDER_STEP,
        format="MMM DD, HH:mm",
        key=f"dist_slider_{city}_{settlement_date.isoformat()}",
    )
    lead = lead_hours_to_end_of_target_day(settlement_date, now=at_utc)

    view = load_distribution_view(
        city, settlement_date, at_utc.isoformat(), weather_url, str(calibration_source)
    )

    prov = [f"lead **{lead:.1f} h**"]
    if view.om_fetched_at_utc:
        prov.append(f"OM cycle `{view.om_fetched_at_utc}`")
    if view.clob_poll_slot_utc:
        prov.append(f"CLOB slot `{view.clob_poll_slot_utc}`")
    if view.resolved_label:
        prov.append(f"resolved → **{view.resolved_label}**")
    st.caption(" · ".join(prov))

    fig = build_distribution_chart(
        view,
        show_models=show_models,
        show_strats=show_strats,
        strat_filter=set(strat_filter),
        show_market=show_market,
        show_resolved=show_resolved,
    )
    st.plotly_chart(fig, width="stretch")

    for warning in view.warnings:
        st.caption(f"⚠️ {warning}")
