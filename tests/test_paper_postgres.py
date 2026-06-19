"""Tests for paper PostgreSQL storage."""

from polytempo.paper.ledger import PostgresLedgerStore
from polytempo.storage.paper_postgres import PaperEventRow, get_paper_connection, insert_paper_event


def test_initialize_and_insert_open(paper_db_url: str) -> None:
    with get_paper_connection(paper_db_url) as conn:
        insert_paper_event(
            conn,
            PaperEventRow(
                profile_id="bh_dist_arb_lead30",
                event_type="OPEN",
                trade_id="t1",
                ts_utc="2026-06-07T12:00:00+00:00",
                polymarket_event_id="evt-1",
                bucket_label="24°C",
                side="YES",
                entry_price=0.30,
                stake_usd=10.0,
                shares=33.3333,
                edge_pp=5.0,
                lead_hours=30.0,
                model_strategy="best_historical",
                trade_action="BUY_YES",
            ),
        )
        conn.commit()

    store = PostgresLedgerStore(database_url=paper_db_url)
    state = store.read_state("bh_dist_arb_lead30")
    assert state.balance_usd == 990.0
    assert len(state.open_trades) == 1
    assert state.open_trades[0].trade_id == "t1"


def test_open_trades_stores_resolved_strategy_and_audit_metadata(paper_db_url: str) -> None:
    from polytempo.analysis import AnalysisResult, AnalysisRow, DistributionBuildInfo

    store = PostgresLedgerStore(database_url=paper_db_url)
    analysis = AnalysisResult(
        distribution_mean_c=24.0,
        distribution_sigma_c=1.0,
        distribution_build=DistributionBuildInfo(
            values_used_c=[24.0],
            default_sigma_c=1.0,
            lead_hours=30.0,
            lead_hours_sigma_floor_c=1.0,
            ensemble_stdev_c=None,
            mean_c=24.0,
            sigma_c=1.0,
            method="test",
        ),
        rows=[
            AnalysisRow(
                label="24°C",
                probability=0.5,
                yes_ask=0.30,
                edge_yes_pp=10.0,
                action="BUY_YES",
                reason="test",
                confidence="medium",
                warnings=[],
                side="YES",
                yes_bid=0.25,
            )
        ],
        model_strategy="best_historical_updated",
        selected_model="ukmo_uk_deterministic_2km",
        calibration_sigma_source="error_std_c",
        fallback_reason=None,
    )
    audit = {
        "requested_model_strategy": "best_historical_updated",
        "resolved_model_strategy": "best_historical_updated",
        "fallback_reason": None,
        "selected_model": "ukmo_uk_deterministic_2km",
        "calibration_sigma_source": "error_std_c",
    }
    store.open_trades_from_analysis(
        "bhu_dist_arb_lead30",
        analysis,
        "evt-1",
        lead_hours=30.0,
        audit_metadata=audit,
    )

    with get_paper_connection(paper_db_url) as conn:
        row = conn.execute(
            """
            SELECT model_strategy, metadata
            FROM paper_events
            WHERE profile_id = %(pid)s AND event_type = 'OPEN'
            """,
            {"pid": "bhu_dist_arb_lead30"},
        ).fetchone()

    assert row is not None
    assert row["model_strategy"] == "best_historical_updated"
    assert row["metadata"]["requested_model_strategy"] == "best_historical_updated"
    assert row["metadata"]["selected_model"] == "ukmo_uk_deterministic_2km"


def test_settle_updates_balance(paper_db_url: str) -> None:
    store = PostgresLedgerStore(database_url=paper_db_url)
    from polytempo.analysis import AnalysisResult, AnalysisRow, DistributionBuildInfo

    analysis = AnalysisResult(
        distribution_mean_c=24.0,
        distribution_sigma_c=1.0,
        distribution_build=DistributionBuildInfo(
            values_used_c=[24.0],
            default_sigma_c=1.0,
            lead_hours=30.0,
            lead_hours_sigma_floor_c=1.0,
            ensemble_stdev_c=None,
            mean_c=24.0,
            sigma_c=1.0,
            method="test",
        ),
        rows=[
            AnalysisRow(
                label="24°C",
                probability=0.5,
                yes_ask=0.30,
                edge_yes_pp=10.0,
                action="BUY_YES",
                reason="test",
                confidence="medium",
                warnings=[],
                side="YES",
                yes_bid=0.25,
            )
        ],
    )
    opened = store.open_trades_from_analysis(
        "bh_dist_arb_lead30",
        analysis,
        "evt-1",
        lead_hours=30.0,
    )
    assert len(opened) == 1
    settled = store.settle_event("bh_dist_arb_lead30", "evt-1", "24°C")
    assert len(settled) == 1
    state = store.read_state("bh_dist_arb_lead30")
    assert state.settled_count == 1
    assert state.balance_usd > 990.0
