"""Distribution Explorer page: scrub time, overlay model/strategy/market distributions."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import streamlit as st

from polytempo.model.lead_time import (
    end_of_target_day_utc,
    lead_hours_to_end_of_target_day,
)
from polytempo.storage.postgres import resolve_database_url
from polytempo.visualizer.distribution_chart import build_distribution_chart
from polytempo.visualizer.distribution_panel import (
    read_overlay_state,
    render_overlay_controls,
    render_overlay_info,
)
from polytempo.visualizer.loaders import (
    load_cities,
    load_distribution_view,
    load_resolution_dates,
)
from polytempo.visualizer.styling import (
    inject_distribution_explorer_css,
    inject_no_inner_scroll_css,
)
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
    inject_distribution_explorer_css()
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

    render_overlay_controls()
    overlay_state = read_overlay_state()

    # Time slider: right edge = 24:00 UTC on settlement date (lead 0). Left = 72h before.
    # Bounds are floored to the slider grid so widget keys stay stable across reruns.
    end_of_day = end_of_target_day_utc(settlement_date)
    now = datetime.now(timezone.utc)
    t_right = _floor_to_step(end_of_day)
    t_left = _floor_to_step(end_of_day - timedelta(hours=72))
    if t_left >= t_right:
        t_left = t_right - timedelta(hours=72)

    default_at = _floor_to_step(min(now, t_right))
    if default_at < t_left:
        default_at = t_left

    slider_key = f"dist_slider_{city}_{settlement_date.isoformat()}"
    if slider_key not in st.session_state:
        st.session_state[slider_key] = default_at

    at_utc = st.slider(
        "snapshot time (UTC)",
        min_value=t_left,
        max_value=t_right,
        step=_SLIDER_STEP,
        format="MMM DD, HH:mm",
        key=slider_key,
        help="Right edge = resolution at 24:00 UTC on the settlement date (lead 0).",
    )
    lead = lead_hours_to_end_of_target_day(settlement_date, now=at_utc)

    lead_col, time_col = st.columns([1, 3])
    lead_col.metric("Lead to resolution", f"{lead:.1f} h")
    time_col.caption(
        f"Scrubbing **{at_utc.strftime('%Y-%m-%d %H:%M')} UTC** · "
        f"window **{t_left.strftime('%b %d %H:%M')}** → "
        f"**{t_right.strftime('%b %d %H:%M')}** (lead 0 at right edge)"
    )

    view = load_distribution_view(
        city,
        settlement_date,
        at_utc.isoformat(),
        weather_url,
        str(calibration_source),
        tuple(sorted(overlay_state.enabled_strats)),
    )

    fig = build_distribution_chart(
        view,
        show_models=overlay_state.show_forecasts,
        show_strats=bool(overlay_state.enabled_strats),
        strat_filter=set(overlay_state.enabled_strats),
        show_market=overlay_state.show_market,
        show_resolved=overlay_state.show_resolved,
    )
    st.plotly_chart(fig, width="stretch")

    render_overlay_info(view, overlay_state)

    for warning in view.warnings:
        st.caption(f"⚠️ {warning}")
