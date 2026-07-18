"""Hold-to-settlement backtest harness for paper trading profiles.

Walks historical London weather events and simulates paper trading against
point-in-time snapshots, **without writing to the paper database**. It reuses
the live decision path end-to-end:

* :func:`polytempo.paper.run.run_profile` for the entry gate, one-open-per
  event/profile dedupe, and ``best_historical*`` model-strategy fallback skip;
* :func:`polytempo.analysis.analyze_event` (via ``run_profile``) for the model
  + trade strategy — strategy logic is never forked here;
* the ledger bankroll math (``stake_fraction`` ramp, flat ``stake_usd`` ticket,
  ``stake_fraction`` Kelly override) mirrored by :class:`InMemoryLedgerStore`.

Only the persistence layer is swapped: an in-memory :class:`LedgerStore`
replaces :class:`polytempo.paper.ledger.PostgresLedgerStore`, so no rows are
ever written to ``polytempo_paper``. Point-in-time inputs come from the weather
DB snapshot readers (Open-Meteo, CLOB, Wunderground), and the winning bucket
comes from the resolved Gamma event.

Scope v1: hold-to-settlement only (no active ADD/FLATTEN). Active
(edge-following) profiles are skipped.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from polytempo.analysis import AnalysisResult
from polytempo.markets.polymarket import (
    PolymarketEvent,
    fetch_event,
    strip_untradeable_bucket_prices,
    winning_label_from_event,
)
from polytempo.model.lead_time import gate_target_utc, lead_hours_to_end_of_target_day
from polytempo.paper.ledger import (
    LedgerState,
    OpenTrade,
    _entry_price,
)
from polytempo.paper.run import run_profile
from polytempo.paper.sizing import allocate_stakes
from polytempo.profiles.models import SIZING_MODE_LEGACY, TradingProfile
from polytempo.storage.paper_postgres import STARTING_BALANCE_USD
from polytempo.storage.snapshot_reads import (
    fetch_backtest_event_ids,
    fetch_nearest_clob_snapshot,
    fetch_nearest_open_meteo_forecast,
    fetch_nearest_wunderground_adjusted_tmax,
    hydrate_event_from_clob_snapshot,
)
from polytempo.weather.schema import ForecastValues
from polytempo.weather.stations import get_station
from polytempo.weather.wu_live_forecast import append_wunderground_snapshot_forecast

logger = logging.getLogger(__name__)

_ARCHIVE_TS_FORMAT = "%Y%m%dT%H%M%SZ"


# --------------------------------------------------------------------------- #
# In-memory ledger (mirrors PostgresLedgerStore, no DB writes)
# --------------------------------------------------------------------------- #
class InMemoryLedgerStore:
    """In-memory :class:`~polytempo.paper.ledger.LedgerStore` for backtests.

    Replays an append-only event log per profile exactly like
    :class:`~polytempo.paper.ledger.PostgresLedgerStore`, reusing the same
    bankroll math (``stake_fraction`` ramp, flat ticket, Kelly fraction) so the
    simulated ledger matches live paper behaviour. Nothing is persisted.
    """

    def __init__(self) -> None:
        self._events: dict[str, list[dict]] = {}

    def read_state(self, profile_id: str) -> LedgerState:
        rows = self._events.get(profile_id, [])
        balance = STARTING_BALANCE_USD
        open_by_id: dict[str, dict] = {}
        settled = 0
        realized = 0.0
        for record in rows:
            kind = record["event_type"]
            if kind == "OPEN":
                balance -= float(record["stake_usd"])
                open_by_id[record["trade_id"]] = record
            elif kind in ("SETTLE", "CLOSE"):
                trade = open_by_id.pop(record["trade_id"], None)
                payout = float(record.get("payout_usd") or 0.0)
                balance += payout
                if trade is not None:
                    realized += payout - float(trade["stake_usd"])
                settled += 1
        open_trades = [_open_trade_from_record(r) for r in open_by_id.values()]
        return LedgerState(
            balance_usd=round(balance, 4),
            open_trades=open_trades,
            settled_count=settled,
            realized_pnl_usd=round(realized, 4),
        )

    def has_open_on_event(self, profile_id: str, event_id: str) -> bool:
        return any(
            t.event_id == event_id
            for t in self.read_state(profile_id).open_trades
        )

    def open_trades_from_analysis(
        self,
        profile_id: str,
        analysis: AnalysisResult,
        event_id: str,
        *,
        lead_hours: float | None = None,
        model_strategy: str | None = None,
        audit_metadata: dict | None = None,
        sizing_mode: str = SIZING_MODE_LEGACY,
        event_budget_fraction: float | None = None,
    ) -> list[OpenTrade]:
        state = self.read_state(profile_id)
        balance = state.balance_usd
        held = {(t.event_id, t.bucket_label, t.side) for t in state.open_trades}
        opened: list[OpenTrade] = []
        records = self._events.setdefault(profile_id, [])

        eligible = []
        for row in analysis.rows:
            if row.action not in ("BUY_YES", "BUY_NO"):
                continue
            if (event_id, row.label, row.side) in held:
                continue
            if row.edge_yes_pp is None:
                continue
            entry_price = _entry_price(row)
            if entry_price is None or entry_price <= 0:
                continue
            eligible.append(row)

        allocated = allocate_stakes(
            eligible,
            balance,
            sizing_mode=sizing_mode,
            event_budget_fraction=event_budget_fraction,
        )

        for row, stake in allocated:
            entry_price = _entry_price(row)
            assert entry_price is not None and entry_price > 0
            if stake <= 0 or stake > balance:
                continue
            shares = round(stake / entry_price, 4)
            trade = OpenTrade(
                trade_id=uuid.uuid4().hex[:12],
                event_id=event_id,
                bucket_label=row.label,
                yes_ask=row.yes_ask if row.yes_ask is not None else 0.0,
                edge_pp=row.edge_yes_pp if row.edge_yes_pp is not None else 0.0,
                stake_usd=stake,
                shares=shares,
                side=row.side,
                entry_price=entry_price,
            )
            records.append(
                {
                    "event_type": "OPEN",
                    "trade_id": trade.trade_id,
                    "polymarket_event_id": event_id,
                    "bucket_label": trade.bucket_label,
                    "side": trade.side,
                    "entry_price": entry_price,
                    "stake_usd": stake,
                    "shares": shares,
                    "edge_pp": trade.edge_pp,
                    "yes_ask": row.yes_ask,
                }
            )
            opened.append(trade)
            balance -= stake
        return opened

    def settle_event(
        self,
        profile_id: str,
        event_id: str,
        winning_label: str,
    ) -> list[OpenTrade]:
        state = self.read_state(profile_id)
        settled: list[OpenTrade] = []
        records = self._events.setdefault(profile_id, [])
        for trade in state.open_trades:
            if trade.event_id != event_id:
                continue
            bucket_won = trade.bucket_label == winning_label
            won = bucket_won if trade.side == "YES" else not bucket_won
            payout = round(trade.shares, 4) if won else 0.0
            records.append(
                {
                    "event_type": "SETTLE",
                    "trade_id": trade.trade_id,
                    "polymarket_event_id": event_id,
                    "bucket_label": trade.bucket_label,
                    "side": trade.side,
                    "payout_usd": payout,
                    "winning_label": winning_label,
                    "outcome": "YES" if won else "NO",
                }
            )
            settled.append(trade)
        return settled

    def close_position(
        self,
        profile_id: str,
        trade_id: str,
        sell_price: float,
        *,
        reason: str,
    ) -> OpenTrade | None:
        state = self.read_state(profile_id)
        trade = next(
            (t for t in state.open_trades if t.trade_id == trade_id), None
        )
        if trade is None:
            return None
        payout = round(trade.shares * sell_price, 4)
        self._events.setdefault(profile_id, []).append(
            {
                "event_type": "CLOSE",
                "trade_id": trade_id,
                "polymarket_event_id": trade.event_id,
                "payout_usd": payout,
                "outcome": reason,
            }
        )
        return trade


def _open_trade_from_record(record: dict) -> OpenTrade:
    side = record.get("side") or "YES"
    raw_yes_ask = record.get("yes_ask")
    yes_ask = float(raw_yes_ask) if raw_yes_ask is not None else 0.0
    entry_price = record.get("entry_price")
    return OpenTrade(
        trade_id=record["trade_id"],
        event_id=record["polymarket_event_id"],
        bucket_label=record["bucket_label"],
        yes_ask=yes_ask,
        edge_pp=float(record["edge_pp"]),
        stake_usd=float(record["stake_usd"]),
        shares=float(record["shares"]),
        side=side,
        entry_price=float(entry_price) if entry_price is not None else None,
    )


# --------------------------------------------------------------------------- #
# As-of calibration CSV resolution
# --------------------------------------------------------------------------- #
def calibration_path_as_of(base_path: Path, at_utc: datetime) -> Path:
    """Resolve the calibration CSV that was live at ``at_utc``.

    Archives live in ``<base_path.parent>/historic/<stem>_<UTC>.csv`` and are
    written *before* a file is overwritten (see
    ``archive_calibration_stats_csv_before_write``): the archive stamped ``TS``
    holds the contents that were live in the window ending at ``TS``. So the
    version in effect at ``at_utc`` is the earliest archive whose timestamp is
    strictly after ``at_utc``; if there is none (``at_utc`` is newer than every
    archive, or there are no archives) the current ``base_path`` is returned.
    """
    historic = base_path.parent / "historic"
    if not historic.is_dir():
        return base_path
    stem = base_path.stem
    suffix = base_path.suffix
    prefix = f"{stem}_"
    candidates: list[tuple[datetime, Path]] = []
    for candidate in historic.glob(f"{stem}_*{suffix}"):
        ts_text = candidate.name[len(prefix): -len(suffix)] if suffix else candidate.name[len(prefix):]
        try:
            ts = datetime.strptime(ts_text, _ARCHIVE_TS_FORMAT).replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            continue
        candidates.append((ts, candidate))
    future = sorted((ts, p) for ts, p in candidates if ts > at_utc)
    if not future:
        return base_path
    return future[0][1]


# --------------------------------------------------------------------------- #
# Point-in-time gate inputs
# --------------------------------------------------------------------------- #
def _open_view_event(
    event: PolymarketEvent,
    clob_rows: tuple[dict, ...] | list[dict],
) -> PolymarketEvent:
    """Rebuild the pre-settlement view of a (now resolved) Gamma event.

    Clears resolution flags/prices so ``is_event_resolved`` is False (forcing
    ``run_profile`` down the OPEN path), then overlays the historical CLOB
    snapshot prices captured at the gate instant.
    """
    stripped = strip_untradeable_bucket_prices(event)
    unresolved = replace(
        stripped,
        buckets=[
            replace(bucket, resolved=False, outcome=None)
            for bucket in stripped.buckets
        ],
    )
    return hydrate_event_from_clob_snapshot(unresolved, clob_rows)


@dataclass(frozen=True)
class GateInputs:
    """Reconstructed point-in-time inputs for one profile at its lead gate."""

    at_utc: datetime
    lead_hours: float
    forecast: ForecastValues
    open_event: PolymarketEvent
    resolved_event: PolymarketEvent
    winning_label: str | None
    calibration_path: Path


def build_gate_inputs(
    profile: TradingProfile,
    event: PolymarketEvent,
    settlement_date: date,
    at_utc: datetime,
    *,
    weather_database_url: str | None = None,
    use_wunderground: bool = True,
) -> tuple[GateInputs | None, str | None]:
    """Load point-in-time inputs at ``at_utc`` for ``profile`` on ``event``.

    Returns ``(inputs, None)`` on success or ``(None, reason)`` when a required
    snapshot is missing at the gate instant.
    """
    station = get_station(profile.city)
    om_bundle = fetch_nearest_open_meteo_forecast(
        station,
        settlement_date,
        at_utc,
        database_url=weather_database_url,
    )
    if om_bundle is None:
        return None, "no_open_meteo_snapshot"

    clob_bundle = fetch_nearest_clob_snapshot(
        event.event_id,
        at_utc,
        database_url=weather_database_url,
    )
    if clob_bundle is None:
        return None, "no_clob_snapshot"

    forecast = om_bundle.forecast
    if use_wunderground:
        wu_snapshot = fetch_nearest_wunderground_adjusted_tmax(
            station,
            settlement_date,
            at_utc,
            database_url=weather_database_url,
        )
        if wu_snapshot is not None:
            forecast = append_wunderground_snapshot_forecast(
                forecast,
                predicted_tmax_c=wu_snapshot.predicted_tmax_c,
                as_of_utc=at_utc,
                observed_running_max_c=wu_snapshot.observed_running_max_c,
            )

    open_event = _open_view_event(event, clob_bundle.rows)
    inputs = GateInputs(
        at_utc=at_utc,
        lead_hours=lead_hours_to_end_of_target_day(settlement_date, now=at_utc),
        forecast=forecast,
        open_event=open_event,
        resolved_event=event,
        winning_label=winning_label_from_event(event),
        calibration_path=calibration_path_as_of(
            profile.calibration_stats_path, at_utc
        ),
    )
    return inputs, None


# --------------------------------------------------------------------------- #
# Results
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class BacktestTrade:
    """One settled backtest position."""

    profile_id: str
    event_id: str
    settlement_date: date
    bucket_label: str
    side: str
    entry_price: float
    stake_usd: float
    shares: float
    edge_pp: float
    won: bool
    payout_usd: float

    @property
    def pnl_usd(self) -> float:
        return round(self.payout_usd - self.stake_usd, 4)


@dataclass
class ProfileBacktestResult:
    """Aggregate outcome for one profile across the backtest window."""

    profile_id: str
    trades: list[BacktestTrade] = field(default_factory=list)
    skips: dict[str, int] = field(default_factory=dict)
    # (settlement_date, realized balance after that day's settlements)
    equity_curve: list[tuple[date, float]] = field(default_factory=list)
    final_balance: float = STARTING_BALANCE_USD

    @property
    def starting_balance(self) -> float:
        return STARTING_BALANCE_USD

    @property
    def pnl_usd(self) -> float:
        return round(self.final_balance - STARTING_BALANCE_USD, 4)

    @property
    def trade_count(self) -> int:
        return len(self.trades)

    @property
    def win_count(self) -> int:
        return sum(1 for t in self.trades if t.won)

    @property
    def win_rate(self) -> float | None:
        if not self.trades:
            return None
        return self.win_count / len(self.trades)

    @property
    def max_drawdown_usd(self) -> float:
        """Largest peak-to-trough drop of the realized equity curve (>= 0)."""
        peak = STARTING_BALANCE_USD
        max_dd = 0.0
        for _, balance in self.equity_curve:
            peak = max(peak, balance)
            max_dd = max(max_dd, peak - balance)
        return round(max_dd, 4)


@dataclass
class BacktestResult:
    """Full backtest output across all profiles."""

    start: date
    end: date
    profiles: dict[str, ProfileBacktestResult]
    events_processed: int = 0

    def daily_breakdown(self) -> dict[date, dict[str, float]]:
        """Per settlement-date realized PnL and trade count, summed over profiles."""
        out: dict[date, dict[str, float]] = {}
        for result in self.profiles.values():
            for trade in result.trades:
                bucket = out.setdefault(
                    trade.settlement_date, {"pnl_usd": 0.0, "trades": 0.0}
                )
                bucket["pnl_usd"] = round(bucket["pnl_usd"] + trade.pnl_usd, 4)
                bucket["trades"] += 1
        return dict(sorted(out.items()))


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
EventForDate = Callable[[str, date], "PolymarketEvent | None"]
InputBuilder = Callable[
    [TradingProfile, PolymarketEvent, date, datetime],
    "tuple[GateInputs | None, str | None]",
]


def default_event_for_date_factory(
    city: str,
    start: date,
    end: date,
    *,
    weather_database_url: str | None = None,
) -> EventForDate:
    """Build a date→event resolver backed by stored CLOB snapshots.

    Discovers the ``(settlement_date, event_id)`` pairs we have snapshots for in
    ``[start, end]`` (including resolved/closed events), then fetches each event
    from Gamma on demand for its bucket structure and winning outcome.
    """
    event_index = fetch_backtest_event_ids(
        city, start, end, database_url=weather_database_url
    )
    logger.info(
        "backtest discovered %d event(s) from CLOB snapshots for %s in %s..%s",
        len(event_index),
        city,
        start.isoformat(),
        end.isoformat(),
    )

    def _resolve(_city: str, settlement_date: date) -> PolymarketEvent | None:
        event_id = event_index.get(settlement_date)
        if event_id is None:
            return None
        return fetch_event(event_id)

    return _resolve


def _date_range(start: date, end: date) -> list[date]:
    if end < start:
        raise ValueError(f"end {end} is before start {start}")
    span = (end - start).days
    return [start + timedelta(days=offset) for offset in range(span + 1)]


def _bump_skip(result: ProfileBacktestResult, reason: str) -> None:
    result.skips[reason] = result.skips.get(reason, 0) + 1


def run_backtest(
    profiles: list[TradingProfile],
    start: date,
    end: date,
    *,
    city: str = "london",
    weather_database_url: str | None = None,
    use_wunderground: bool = True,
    event_for_date: EventForDate | None = None,
    input_builder: InputBuilder | None = None,
) -> BacktestResult:
    """Simulate hold-to-settlement paper trading over ``[start, end]``.

    For each settlement date in the window the harness discovers the weather
    event, then for each (non-active) profile reconstructs the inputs at that
    profile's lead gate, runs the live ``run_profile`` decision path to OPEN,
    and settles the position against the resolved event's winning bucket. Each
    profile keeps a compounding $1000 bankroll shared across every event, in
    chronological settlement order.

    ``event_for_date`` and ``input_builder`` are injection seams (defaulting to
    Gamma discovery and weather-DB snapshot reads) so tests can drive the loop
    with in-memory fixtures.
    """
    resolved_event_for_date = event_for_date or default_event_for_date_factory(
        city, start, end, weather_database_url=weather_database_url
    )
    resolved_input_builder = input_builder or (
        lambda profile, event, settlement_date, at_utc: build_gate_inputs(
            profile,
            event,
            settlement_date,
            at_utc,
            weather_database_url=weather_database_url,
            use_wunderground=use_wunderground,
        )
    )

    active = [p for p in profiles if p.is_active]
    for profile in active:
        logger.info(
            "backtest skipping active profile %s (v1 is hold-to-settlement only)",
            profile.id,
        )
    hold_profiles = [p for p in profiles if not p.is_active]

    store = InMemoryLedgerStore()
    results = {p.id: ProfileBacktestResult(profile_id=p.id) for p in hold_profiles}
    events_processed = 0

    for settlement_date in _date_range(start, end):
        event = resolved_event_for_date(city, settlement_date)
        if event is None:
            logger.info("no event discovered for %s", settlement_date.isoformat())
            continue
        events_processed += 1

        for profile in hold_profiles:
            result = results[profile.id]
            at_utc = gate_target_utc(
                settlement_date, profile.entry_gate.target_lead_hours
            )
            inputs, skip_reason = resolved_input_builder(
                profile, event, settlement_date, at_utc
            )
            if inputs is None:
                _bump_skip(result, skip_reason or "no_inputs")
                continue

            gate_profile = replace(
                profile, calibration_stats_path=inputs.calibration_path
            )
            open_result = run_profile(
                store,
                gate_profile,
                inputs.forecast,
                inputs.open_event,
                lead_hours=inputs.lead_hours,
                dedupe=True,
                enforce_gate=True,
            )
            if open_result.action != "OPENED":
                _bump_skip(result, open_result.action)
                # Nothing opened -> nothing to settle for this profile/event.
                if not open_result.opened:
                    continue

            if inputs.winning_label is None:
                _bump_skip(result, "unresolved_no_winner")
                logger.warning(
                    "event %s has no winning bucket; leaving %s positions open",
                    event.event_id,
                    profile.id,
                )
                continue

            settle_result = run_profile(
                store,
                gate_profile,
                inputs.forecast,
                inputs.resolved_event,
                lead_hours=inputs.lead_hours,
                dedupe=True,
                enforce_gate=False,
            )
            for trade in settle_result.settled:
                bucket_won = trade.bucket_label == inputs.winning_label
                won = bucket_won if trade.side == "YES" else not bucket_won
                payout = round(trade.shares, 4) if won else 0.0
                result.trades.append(
                    BacktestTrade(
                        profile_id=profile.id,
                        event_id=event.event_id,
                        settlement_date=settlement_date,
                        bucket_label=trade.bucket_label,
                        side=trade.side,
                        entry_price=trade.entry_price or 0.0,
                        stake_usd=trade.stake_usd,
                        shares=trade.shares,
                        edge_pp=trade.edge_pp,
                        won=won,
                        payout_usd=payout,
                    )
                )

            balance = store.read_state(profile.id).balance_usd
            result.final_balance = balance
            if settle_result.settled:
                result.equity_curve.append((settlement_date, balance))

    return BacktestResult(
        start=start,
        end=end,
        profiles=results,
        events_processed=events_processed,
    )
