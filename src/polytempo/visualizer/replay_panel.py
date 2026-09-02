"""Analysis replay panels: chart, bucket table, model/market summary."""

from __future__ import annotations

import streamlit as st

from polytempo.reports.live_report import format_calibration_row
from polytempo.visualizer.chart import (
    bucket_table_dataframe,
    bucket_table_has_replay_mismatch,
    build_analysis_chart,
)
from polytempo.markets.polymarket import winning_label_from_event
from polytempo.visualizer.loaders import load_replay, load_settlement_replay
from polytempo.visualizer.paths import ROW_HEIGHT
from polytempo.visualizer.styling import style_bucket_table, table_height
from polytempo.visualizer.trades import RealizedEventGroup, bucket_probs_from_trades


def _render_model_summary(analysis) -> None:
    build = analysis.distribution_build
    mean_col, sigma_col = st.columns(2)
    mean_col.metric("mean", f"{build.mean_c:.2f} °C")
    sigma_col.metric("sigma", f"{build.sigma_c:.2f} °C")

    meta: list[str] = [
        f"strategy `{analysis.model_strategy}`",
        f"method `{build.method}`",
    ]
    if analysis.selected_model is not None:
        meta.append(f"model `{analysis.selected_model}`")
    if analysis.calibration_sigma_source is not None:
        meta.append(f"sigma `{analysis.calibration_sigma_source}`")
    if analysis.fallback_reason is not None:
        meta.append(f"fallback `{analysis.fallback_reason}`")
    st.caption(" · ".join(meta))

    if analysis.calibration_row is not None:
        with st.expander("Calibration row", expanded=False):
            st.text(format_calibration_row(analysis.calibration_row))


def _render_market_summary(market) -> None:
    if market is not None:
        mean_col, spread_col = st.columns(2)
        mean_col.metric("implied mean", f"{market.mean_c:.2f} °C")
        spread_col.metric("discrete spread", f"{market.discrete_std_c:.2f} °C")
        st.caption(
            "Weighted mean/std of bucket centers from normalized yes_ask "
            "(discrete, not normal)."
        )
    else:
        st.caption("No yes_ask prices available for market-implied stats.")


def _render_time_slot_banner(*, opened_at: str, sources) -> None:
    with st.container(border=True):
        st.markdown("**Decision-time snapshots**")
        open_col, om_col, clob_col = st.columns(3)
        open_col.markdown(f"**OPEN (UTC)**\n\n`{opened_at}`")
        om_col.markdown(
            f"**Open-Meteo cycle**\n\n`{sources.fetch_cycle_fetched_at or '—'}`"
        )
        clob_col.markdown(
            f"**CLOB slot**\n\n`{sources.clob_poll_slot or '—'}`"
        )
        for warning in sources.warnings:
            st.warning(warning)


def render_event_replay(
    *,
    wallet: str,
    group: RealizedEventGroup,
    weather_url: str | None,
) -> None:
    opened_at = min(t.opened_at_utc for t in group.trades)
    lead = group.trades[0].lead_hours
    model_strategy = group.trades[0].model_strategy
    trade_signature = tuple(
        (t.bucket_label, t.yes_bid, t.yes_ask) for t in group.trades
    )
    traded_labels = {t.bucket_label for t in group.trades}

    if weather_url is None:
        st.caption("Set POLYTEMPO_DATABASE_URL for analysis replay chart.")
        return

    try:
        replay, err = load_replay(
            wallet,
            group.polymarket_event_id,
            opened_at,
            lead,
            model_strategy,
            weather_url,
            trade_signature,
        )
    except Exception as exc:
        st.warning(f"Replay failed: {exc}")
        return

    if replay is None:
        st.warning(err or "Replay unavailable for this event.")
        return

    _render_replay_result(
        replay,
        traded_labels=traded_labels,
        resolution_label=group.resolution_label,
        trade_p_by_bucket=bucket_probs_from_trades(group.trades),
        opened_at=opened_at,
    )


def render_settlement_replay(
    *,
    wallet: str,
    settlement_date,
    weather_url: str,
    lead_hours: float | None,
    model_strategy: str | None,
) -> None:
    """Gate-time snapshot replay when the paper ledger has no fills."""
    try:
        replay, err = load_settlement_replay(
            wallet,
            settlement_date,
            weather_url,
            lead_hours,
            model_strategy,
        )
    except Exception as exc:
        st.warning(f"Replay failed: {exc}")
        return
    if replay is None:
        st.warning(err or "Replay unavailable for this settlement date.")
        return
    _render_replay_result(
        replay,
        traded_labels=set(),
        resolution_label=winning_label_from_event(replay.event),
        trade_p_by_bucket=None,
        opened_at=replay.opened_at_utc,
    )


def _render_replay_result(
    replay,
    *,
    traded_labels: set[str],
    resolution_label: str | None,
    trade_p_by_bucket: dict | None,
    opened_at: str,
) -> None:
    sources = replay.snapshot_sources
    _render_time_slot_banner(opened_at=opened_at, sources=sources)

    chart_bundle = build_analysis_chart(
        replay.analysis,
        replay.event,
        traded_bucket_labels=traded_labels,
        resolution_label=resolution_label,
    )
    st.plotly_chart(chart_bundle.figure, width="stretch")

    model_col, market_col = st.columns(2, gap="large")
    with model_col:
        st.subheader("Model")
        _render_model_summary(replay.analysis)
    with market_col:
        st.subheader("Market")
        _render_market_summary(chart_bundle.market)

    bucket_df = bucket_table_dataframe(
        replay.analysis,
        trade_p_by_bucket=trade_p_by_bucket,
    )
    st.dataframe(
        style_bucket_table(bucket_df),
        hide_index=True,
        width="stretch",
        height=table_height(len(bucket_df)),
        row_height=ROW_HEIGHT,
    )
    if trade_p_by_bucket and bucket_table_has_replay_mismatch(bucket_df):
        st.warning(
            "Replay **P** does not match ledger **P (from trades)** for one or more "
            "traded buckets (calibration CSV or snapshots may differ from OPEN time)."
        )

    st.caption(
        "_Replay uses current calibration CSV on disk, "
        "not the file version at trade time._"
    )
