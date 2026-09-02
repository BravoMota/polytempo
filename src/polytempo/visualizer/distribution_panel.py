"""Overlay controls and summary cards for the Distribution Explorer page."""

from __future__ import annotations

from dataclasses import dataclass

import streamlit as st

from polytempo.analysis import MODEL_STRATEGIES
from polytempo.visualizer.distribution_data import DistributionView
from polytempo.visualizer.distribution_chart import FORECAST_COLORS, MODEL_PALETTE, STRAT_COLORS
from polytempo.visualizer.prefs import (
    OverlayPrefs,
    enabled_strats_from_prefs,
    load_prefs,
    save_overlays,
)
from polytempo.weather.calibration_storage import WU_FORECAST_MODEL

_GREY = "#888888"
_DISABLED_CLASS = "dist-overlay-disabled"


@dataclass(frozen=True)
class OverlayState:
    show_forecasts: bool
    enabled_strats: frozenset[str]
    show_market: bool
    show_resolved: bool


def _persist_overlays() -> None:
    save_overlays(
        OverlayPrefs(
            show_forecasts=bool(st.session_state.dist_show_forecasts),
            show_market=bool(st.session_state.dist_show_market),
            show_resolved=bool(st.session_state.dist_show_resolved),
            enabled_strats=[
                strat
                for strat in MODEL_STRATEGIES
                if st.session_state.get(f"dist_strat_{strat}")
            ],
        )
    )


def _init_overlay_state() -> None:
    overlays = load_prefs().overlays
    if "dist_show_forecasts" not in st.session_state:
        st.session_state.dist_show_forecasts = (
            True if overlays is None else overlays.show_forecasts
        )
    if "dist_show_market" not in st.session_state:
        st.session_state.dist_show_market = (
            True if overlays is None else overlays.show_market
        )
    if "dist_show_resolved" not in st.session_state:
        st.session_state.dist_show_resolved = (
            True if overlays is None else overlays.show_resolved
        )
    enabled = enabled_strats_from_prefs(overlays)
    for strat in MODEL_STRATEGIES:
        key = f"dist_strat_{strat}"
        if key not in st.session_state:
            st.session_state[key] = strat in enabled


def read_overlay_state() -> OverlayState:
    _init_overlay_state()
    enabled = frozenset(
        strat for strat in MODEL_STRATEGIES if st.session_state[f"dist_strat_{strat}"]
    )
    return OverlayState(
        show_forecasts=st.session_state.dist_show_forecasts,
        enabled_strats=enabled,
        show_market=st.session_state.dist_show_market,
        show_resolved=st.session_state.dist_show_resolved,
    )


def render_overlay_controls() -> None:
    """Overlay toggles in the sidebar."""
    _init_overlay_state()
    st.sidebar.header("Overlays")
    st.sidebar.toggle(
        "Forecasts (Open-Meteo + WU)",
        key="dist_show_forecasts",
        on_change=_persist_overlays,
    )
    st.sidebar.toggle(
        "Market (yes_ask + implied mean)",
        key="dist_show_market",
        on_change=_persist_overlays,
    )
    st.sidebar.toggle(
        "Resolved bucket",
        key="dist_show_resolved",
        on_change=_persist_overlays,
    )

    st.sidebar.markdown("**Distribution strategies**")
    for strat in MODEL_STRATEGIES:
        st.sidebar.toggle(strat, key=f"dist_strat_{strat}", on_change=_persist_overlays)


def _wrap(html: str, *, disabled: bool) -> str:
    cls = f' class="{_DISABLED_CLASS}"' if disabled else ""
    return f'<div{cls}>{html}</div>'


def _model_color(model: str, idx: int, disabled: bool) -> str:
    if disabled:
        return _GREY
    return FORECAST_COLORS.get(model, MODEL_PALETTE[idx % len(MODEL_PALETTE)])


def _strat_color(strategy: str, disabled: bool) -> str:
    if disabled:
        return _GREY
    return STRAT_COLORS.get(strategy, _GREY)


def render_overlay_info(view: DistributionView, state: OverlayState) -> None:
    """Model, strategy, and market summaries — grey when hidden on the chart."""
    st.subheader("Snapshot details")

    prov_bits: list[str] = [f"lead **{view.lead_hours:.1f} h**"]
    if view.om_fetched_at_utc:
        prov_bits.append(f"OM `{view.om_fetched_at_utc}`")
    if view.wu_scraped_at_utc:
        prov_bits.append(f"WU scrape `{view.wu_scraped_at_utc}`")
    if view.clob_poll_slot_utc:
        prov_bits.append(f"CLOB `{view.clob_poll_slot_utc}`")
    if view.resolved_label:
        prov_bits.append(f"resolved **{view.resolved_label}**")
    st.caption(" · ".join(prov_bits))

    col_f, col_s, col_m = st.columns([1.3, 1.3, 0.9], gap="large")

    with col_f:
        st.markdown("#### Forecasts")
        disabled = not state.show_forecasts
        if not view.model_overlays:
            st.caption("No forecast overlays at this lead.")
        for idx, o in enumerate(view.model_overlays):
            color = _model_color(o.model, idx, disabled)
            source = "Wunderground" if o.model == WU_FORECAST_MODEL else "Open-Meteo"
            html = (
                f'<p style="margin:0 0 0.15rem;font-size:1.05rem;font-weight:600;'
                f'color:{color}">{o.model}</p>'
                f'<p style="margin:0;font-size:0.82rem;color:#aaa">{source}</p>'
                f'<p style="margin:0.15rem 0 0;font-size:0.92rem;color:#ccc">'
                f"μ {o.mean_c:.2f} °C · σ {o.sigma_c:.2f} °C "
                f"({o.sigma_source})</p>"
                f'<p style="margin:0.2rem 0 0;font-size:0.82rem;color:#999">'
                f"raw {o.predicted_c:.2f} °C · bias {o.bias_c:+.2f} °C · "
                f"lookup lead {o.lookup_lead_hours:.0f} h</p>"
            )
            if o.detail:
                html += (
                    f'<p style="margin:0.15rem 0 0.85rem;font-size:0.78rem;color:#999">'
                    f"{o.detail}</p>"
                )
            else:
                html += '<p style="margin:0 0 0.85rem"></p>'
            st.markdown(_wrap(html, disabled=disabled), unsafe_allow_html=True)

    with col_s:
        st.markdown("#### Distributions")
        strat_by_name = {o.strategy: o for o in view.strat_overlays}
        for strat in MODEL_STRATEGIES:
            o = strat_by_name.get(strat)
            disabled = strat not in state.enabled_strats
            color = _strat_color(strat, disabled)
            if o is None:
                note = "off" if disabled else "unavailable"
                st.markdown(
                    _wrap(
                        f'<p style="margin:0 0 0.85rem;font-size:0.88rem;color:#666">'
                        f"{strat} — {note}</p>",
                        disabled=True,
                    ),
                    unsafe_allow_html=True,
                )
                continue
            top_prob = max(o.bucket_probs, key=lambda p: p[1], default=(None, 0.0))
            top_lbl, top_p = top_prob
            top_line = (
                f" · top bucket {top_lbl} ({top_p:.1%})"
                if top_lbl is not None
                else ""
            )
            model_line = (
                f'<p style="margin:0.15rem 0 0;font-size:0.82rem;color:#aaa">'
                f"selected model <strong>{o.selected_model}</strong></p>"
                if o.selected_model
                else ""
            )
            html = (
                f'<p style="margin:0 0 0.15rem;font-size:1.0rem;font-weight:600;'
                f'color:{color}">{strat}</p>'
                f'{model_line}'
                f'<p style="margin:0 0 0.85rem;font-size:0.92rem;color:#ccc">'
                f"μ {o.mean_c:.2f} °C · σ {o.sigma_c:.2f} °C{top_line}</p>"
            )
            st.markdown(_wrap(html, disabled=disabled), unsafe_allow_html=True)

    with col_m:
        st.markdown("#### Market")
        disabled = not state.show_market
        market = view.market
        if market is None:
            st.caption("No market prices for this event.")
        else:
            mean_txt = (
                f"{market.implied_mean_c:.2f} °C"
                if market.implied_mean_c is not None
                else "—"
            )
            std_txt = (
                f"{market.discrete_std_c:.2f} °C"
                if market.discrete_std_c is not None
                else "—"
            )
            color = _GREY if disabled else "#4da6ff"
            html = (
                f'<p style="margin:0 0 0.35rem;font-size:1.35rem;font-weight:700;'
                f'color:{color}">{mean_txt}</p>'
                f'<p style="margin:0;font-size:0.82rem;color:#aaa">implied mean</p>'
                f'<p style="margin:0.75rem 0 0.15rem;font-size:1.05rem;font-weight:600;'
                f'color:{color}">{std_txt}</p>'
                f'<p style="margin:0;font-size:0.82rem;color:#aaa">discrete spread</p>'
                f'<p style="margin:0.85rem 0 0;font-size:0.78rem;color:#777">'
                f"Per-bucket yes_ask masses (not a normal fit).</p>"
            )
            st.markdown(_wrap(html, disabled=disabled), unsafe_allow_html=True)

        if view.resolved_label:
            resolved_disabled = not state.show_resolved
            res_color = _GREY if resolved_disabled else "#66ff99"
            res_html = (
                f'<p style="margin:1rem 0 0;font-size:0.88rem;color:#aaa">Resolved</p>'
                f'<p style="margin:0.1rem 0 0;font-size:1.1rem;font-weight:600;'
                f'color:{res_color}">{view.resolved_label}</p>'
            )
            st.markdown(
                _wrap(res_html, disabled=resolved_disabled),
                unsafe_allow_html=True,
            )
