#!/usr/bin/env python3
"""Run the live trading node (lead-gate scheduled; dry_run by default).

Startup order: config -> ledger -> execution client -> reconcile -> loop.
In ``live`` mode a failed reconciliation refuses to start; in ``dry_run`` it
warns and continues.
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from polytempo.live.config import (  # noqa: E402
    DEFAULT_LIVE_CONFIG_PATH,
    load_live_node_config,
    resolve_live_credentials,
)
from polytempo.live.execution import (  # noqa: E402
    DryRunExecutionClient,
    PolymarketExecutionClient,
)
from polytempo.live.ledger import LiveLedger  # noqa: E402
from polytempo.live.marketdata import fetch_book_depth  # noqa: E402
from polytempo.live.models import MODE_LIVE  # noqa: E402
from polytempo.live.node import run_node_tick  # noqa: E402
from polytempo.live.reconcile import reconcile  # noqa: E402
from polytempo.live.risk import RiskEngine  # noqa: E402
from polytempo.storage.live_postgres import resolve_live_database_url  # noqa: E402

logger = logging.getLogger(__name__)

SETTLE_SWEEP_INTERVAL = timedelta(minutes=15)
RECONCILE_INTERVAL = timedelta(hours=6)

_stop = False


def _handle_signal(signum: int, _frame: object) -> None:
    global _stop
    logger.info("received signal %s, stopping...", signum)
    _stop = True


def _sleep_until(deadline: datetime) -> None:
    while not _stop:
        now = datetime.now(timezone.utc)
        if now >= deadline:
            return
        time.sleep(min((deadline - now).total_seconds(), 1.0))


def _single_token_book(token_id: str):
    books = fetch_book_depth([token_id])
    return books.get(token_id)


def _run_reconcile(ledger: LiveLedger, client) -> bool:
    report = reconcile(
        ledger,
        client,
        now_iso=datetime.now(timezone.utc).isoformat(),
    )
    for line in report.discrepancies:
        logger.warning("reconcile: %s", line)
    for intent_id in report.resolved_intents:
        logger.info("reconcile resolved stale intent %s", intent_id)
    return report.ok


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the live trading node")
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_LIVE_CONFIG_PATH,
        help="Path to live_node.yaml",
    )
    parser.add_argument(
        "--database-url",
        default=None,
        help="Override POLYTEMPO_LIVE_DATABASE_URL",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single tick and exit",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    config = load_live_node_config(args.config)
    ledger = LiveLedger(resolve_live_database_url(override=args.database_url))

    if config.mode == MODE_LIVE:
        # A live journal must not be polluted by dry_run rows: exposure, dedupe,
        # and reconciliation all read the same tables. Use a fresh database.
        other_modes = ledger.journal_modes() - {MODE_LIVE}
        if other_modes:
            logger.error(
                "journal contains non-live rows (%s); point "
                "POLYTEMPO_LIVE_DATABASE_URL at a fresh database for live mode",
                ", ".join(sorted(other_modes)),
            )
            return 1
        client = PolymarketExecutionClient(resolve_live_credentials())
    else:
        client = DryRunExecutionClient(
            book_provider=_single_token_book,
            starting_balance_usd=config.dry_run_balance_usd,
        )
    risk = RiskEngine(config.risk)

    logger.info(
        "live node starting: mode=%s knob=%s (%s x %s x gates %s)",
        config.mode,
        config.knob.id,
        config.knob.model_strategy,
        config.knob.trade_strategy,
        list(config.knob.lead_gates),
    )

    if not _run_reconcile(ledger, client):
        if config.mode == MODE_LIVE:
            logger.error("reconciliation failed; refusing to start in live mode")
            return 1
        logger.warning("reconciliation reported discrepancies (dry_run: continuing)")

    last_reconcile = datetime.now(timezone.utc)
    while not _stop:
        try:
            tick = run_node_tick(config, ledger, client, risk)
            for line in tick.lines:
                print(line)
            if tick.settled_events:
                print(f"settled: {', '.join(tick.settled_events)}")
            if tick.halted:
                logger.error("tick stopped by risk hard-stop; check kill switch/limits")
        except Exception:
            logger.exception("tick failed")
            tick = None

        if args.once:
            return 0

        now = datetime.now(timezone.utc)
        if now - last_reconcile >= RECONCILE_INTERVAL:
            _run_reconcile(ledger, client)
            last_reconcile = now

        wake_at = now + SETTLE_SWEEP_INTERVAL
        if tick is not None and tick.next_gate_wake is not None and tick.next_gate_wake > now:
            wake_at = min(wake_at, tick.next_gate_wake)
        logger.info("sleeping until %s", wake_at.strftime("%Y-%m-%dT%H:%M:%SZ"))
        _sleep_until(wake_at)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
