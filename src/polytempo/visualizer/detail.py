"""Trade drill-down panel for the performance viewer."""

from __future__ import annotations

from datetime import date

import pandas as pd
import streamlit as st

from polytempo.visualizer.paths import ROW_HEIGHT
from polytempo.visualizer.styling import (
    TRADE_DETAIL_ANCHOR,
    scroll_to_anchor,
    table_height,
)
from polytempo.storage.paper_postgres import resolve_paper_database_url
from polytempo.storage.postgres import resolve_database_url
from polytempo.visualizer.csv_data import detail_context_from_filtered, wallet_settlement_dates
from polytempo.visualizer.loaders import load_realized_trades
from polytempo.visualizer.replay_panel import render_event_replay, render_settlement_replay
from polytempo.visualizer.trades import RealizedTrade, group_by_event


def paper_db_url() -> str | None:
    try:
        return resolve_paper_database_url()
    except RuntimeError:
        return None


def weather_db_url() -> str | None:
    try:
        return resolve_database_url()
    except RuntimeError:
        return None


def trade_row_dict(trade: RealizedTrade) -> dict:
    return {
        "bucket": trade.bucket_label,
        "side": trade.side,
        "entry": trade.entry_price,
        "stake_usd": trade.stake_usd,
        "shares": trade.shares,
        "yes_bid": trade.yes_bid,
        "yes_ask": trade.yes_ask,
        "edge_pp": trade.edge_pp,
        "realization_type": trade.realization_type,
        "exit": trade.outcome,
        "payout_usd": trade.payout_usd,
        "pnl_usd": trade.pnl_usd,
        "opened_at": trade.opened_at_utc,
        "realized_at": trade.realized_at_utc,
    }


def _csv_replay_knobs(
    filtered: pd.DataFrame,
    wallet: str,
    settlement_date: date,
) -> tuple[float | None, str | None]:
    """Lead hours and model strategy from the visible CSV row."""
    sub = filtered[
        (filtered["profile_id"] == wallet)
        & (filtered["settlement_date"] == settlement_date)
    ]
    if sub.empty:
        return None, None
    row = sub.iloc[0]
    lead_raw = row.get("lead_hours")
    lead_hours: float | None
    try:
        text = "" if lead_raw is None else str(lead_raw).strip()
        lead_hours = float(text) if text else None
    except (TypeError, ValueError):
        lead_hours = None
    model_raw = row.get("model")
    model_strategy = None if model_raw is None else str(model_raw).strip() or None
    return lead_hours, model_strategy


def trades_dataframe(trade_dicts: list[dict]) -> pd.DataFrame:
    cols = [
        "bucket",
        "side",
        "entry",
        "stake_usd",
        "shares",
        "yes_bid",
        "yes_ask",
        "edge_pp",
        "realization_type",
        "exit",
        "payout_usd",
        "pnl_usd",
        "opened_at",
        "realized_at",
    ]
    if not trade_dicts:
        return pd.DataFrame(columns=cols)
    return pd.DataFrame(trade_dicts)[cols]


def render_trade_detail(
    filtered: pd.DataFrame,
    visible_dates: list[date],
) -> None:
    st.markdown(f'<div id="{TRADE_DETAIL_ANCHOR}"></div>', unsafe_allow_html=True)
    if st.session_state.pop("scroll_to_detail", False):
        scroll_to_anchor(TRADE_DETAIL_ANCHOR)

    wallets = sorted(filtered["profile_id"].unique())
    expanded = bool(st.session_state.get("detail_expanded", False))
    with st.expander("Trade detail", expanded=expanded):
        pref_wallet = st.session_state.get("detail_wallet")
        wallet_idx = wallets.index(pref_wallet) if pref_wallet in wallets else 0
        wallet = st.selectbox("wallet", wallets, index=wallet_idx)
        st.session_state["detail_wallet"] = wallet

        date_options = wallet_settlement_dates(filtered, wallet, visible_dates)
        if not date_options:
            st.caption("No realized trades for this wallet in the visible window.")
            return

        pref_date = st.session_state.get("detail_date")
        date_idx = date_options.index(pref_date) if pref_date in date_options else 0
        settlement_date = st.selectbox(
            "settlement date",
            date_options,
            index=date_idx,
            format_func=lambda d: d.isoformat(),
        )
        st.session_state["detail_date"] = settlement_date

        context = detail_context_from_filtered(filtered, wallet, settlement_date)
        if context:
            st.caption(context)

        paper_url = paper_db_url()
        trades: list[RealizedTrade] = []
        if paper_url is not None:
            try:
                trades = load_realized_trades(wallet, settlement_date, paper_url)
            except Exception as exc:
                st.error(f"Could not load trades: {exc}")
                return

        weather_url = weather_db_url()
        if not trades:
            if weather_url is None:
                st.warning("No matching trades in the paper ledger for this selection.")
                st.info(
                    "Set POLYTEMPO_DATABASE_URL to replay the gate-time distribution "
                    "(needed for backtest / research knobs)."
                )
                return
            st.caption(
                "No paper-ledger fills for this wallet — replaying the model at "
                "the profile gate from weather snapshots."
            )
            lead_hours, model_strategy = _csv_replay_knobs(
                filtered, wallet, settlement_date
            )
            render_settlement_replay(
                wallet=wallet,
                settlement_date=settlement_date,
                weather_url=weather_url,
                lead_hours=lead_hours,
                model_strategy=model_strategy,
            )
            return

        for group in group_by_event(trades):
            st.markdown(
                f"**Event** `{group.polymarket_event_id}` — "
                f"resolution: **{group.resolution_label}**"
            )
            render_event_replay(wallet=wallet, group=group, weather_url=weather_url)
            st.divider()
            st.markdown("**Trades**")
            trades_df = trades_dataframe([trade_row_dict(t) for t in group.trades])
            st.dataframe(
                trades_df,
                hide_index=True,
                width="stretch",
                height=table_height(len(trades_df)),
                row_height=ROW_HEIGHT,
            )
            with st.expander("Audit metadata", expanded=False):
                for trade in group.trades:
                    st.caption(f"trade `{trade.trade_id}`")
                    if trade.open_metadata:
                        st.json(trade.open_metadata)
                    if trade.exit_metadata:
                        st.json(trade.exit_metadata)
