"""CLI entry point.

Exposes the `polytempo` command:

* `demo`            - run the analysis on hardcoded fake inputs (no APIs).
* `live`            - fetch Polymarket + Open-Meteo, run analysis, write a markdown report.
* `paper open`      - fetch a London event + forecast, run analysis, lock paper trades.
* `paper settle`    - settle open paper trades for an event against a winning bucket.
* `paper status`    - print the paper account balance and open positions.
* `paper list-london` - list active London weather events from Polymarket.
* `fetch-historical-forecasts` - offline Single-Runs fetch for calibration JSONL.
* `compute-calibration-stats` - join forecasts + observations into stats JSON.
"""

from __future__ import annotations

import math
import sys
from datetime import date, datetime, time, timedelta, timezone
from enum import Enum
from pathlib import Path

import typer

try:
    import truststore

    truststore.inject_into_ssl()
except ImportError:
    pass

from polytempo.analysis import (
    MODEL_STRATEGY_BEST_HISTORICAL,
    MODEL_STRATEGY_BEST_HISTORICAL_UPDATED,
    MODEL_STRATEGY_ENSEMBLE_SPREAD,
    MODEL_STRATEGY_WEIGHTED_HISTORICAL_UPDATED,
    AnalysisInput,
    AnalysisResult,
    analyze,
    analyze_event,
)
from polytempo.weather.calibration_stats_csv import (
    DEFAULT_CALIBRATION_STATS_CSV_PATH,
    DEFAULT_UPDATED_CALIBRATION_STATS_CSV_PATH,
)
from polytempo.model.lead_time import lead_hours_to_end_of_target_day
from polytempo.markets.buckets import bucket_label_for_value, parse_temperature_bucket
from polytempo.markets.polymarket import (
    PolymarketEvent,
    fetch_event,
    fetch_weather_events,
    hydrate_prices,
    first_parseable_weather_event,
    is_event_resolved,
    winning_label_from_event,
)
from polytempo.weather.wunderground import fetch_wunderground_observed_tmax
from polytempo.reports.writer import RunReporter
from polytempo.reports.live_report import (
    format_calibration_per_model_compact,
    format_calibration_per_model_md,
    format_calibration_selection_md,
    format_distribution_md,
    format_lead_hours_md,
    format_open_meteo_md,
    format_strategy_analysis_md,
    format_weighted_calibration_md,
    resolve_calibration_selection,
    resolve_weighted_calibration_selection,
)
from polytempo.paper.ledger import PostgresLedgerStore, default_ledger_store
from polytempo.paper.bot_log import format_run_summary
from polytempo.paper.run import ProfileRunResult, RunSummary, open_event_ids, run_profile, run_profiles
from polytempo.profiles.registry import known_trade_strategies, trade_strategy_for_name
from polytempo.paper.market_context import fetch_market_context
from polytempo.profiles import DEFAULT_PROFILES_PATH, load_paper_profiles
from polytempo.strategy.edge import MarketPrice
from polytempo.weather.calibration_dataset import (
    DEFAULT_CALIBRATION_STATS_PATH,
    compute_calibration_stats,
    join_forecasts_with_observations,
    write_calibration_stats_json,
)
from polytempo.weather.historical_forecasts import (
    DEFAULT_FORECAST_DAYS,
    DEFAULT_HISTORICAL_FORECASTS_PATH,
    DEFAULT_RAW_FORECASTS_DIR,
    DEFAULT_SINGLE_RUNS_BASE_URL,
    fetch_historical_forecast_batch,
    fetch_raw_forecast_runs,
    plan_historical_forecast_requests,
    read_historical_forecasts_jsonl,
    resolve_raw_fetch_run_times,
)
from polytempo.weather.observations import DEFAULT_OBSERVATIONS_PATH, read_observations_jsonl
from polytempo.weather.open_meteo import (
    DEFAULT_MODELS,
    daily_to_forecast_values,
    fetch_open_meteo_live_bundle,
)
from polytempo.weather.stations import STATIONS, Station, get_station
from polytempo.weather.wu_live_forecast import append_wunderground_forecast

app = typer.Typer()
paper_app = typer.Typer(help="Paper trading ledger (demo account, no live orders).")
app.add_typer(paper_app, name="paper")


class ModelStrategy(str, Enum):
    """How the live command builds the forecast distribution.

    - ``ensemble_spread``: mean + spread across all live models (legacy).
    - ``best_historical``: pick the calibrated model with the lowest empirical
      sigma at the current lead time; bias-correct its prediction.
    """

    ENSEMBLE_SPREAD = MODEL_STRATEGY_ENSEMBLE_SPREAD
    BEST_HISTORICAL = MODEL_STRATEGY_BEST_HISTORICAL
    BEST_HISTORICAL_UPDATED = MODEL_STRATEGY_BEST_HISTORICAL_UPDATED
    WEIGHTED_HISTORICAL_UPDATED = MODEL_STRATEGY_WEIGHTED_HISTORICAL_UPDATED


class LiveMode(str, Enum):
    PREVIEW = "preview"
    TRADE = "trade"


class DayChoice(str, Enum):
    TODAY = "today"
    TOMORROW = "tomorrow"
    BOTH = "both"


@app.callback()
def _root() -> None:
    """PolyTempo command-line interface."""


# ---------------------------------------------------------------------------
# demo (Phase 5)
# ---------------------------------------------------------------------------


def _demo_input() -> AnalysisInput:
    return AnalysisInput(
        forecast_values_c=[23.5, 24.0, 24.5, 24.0, 23.8],
        bucket_labels=["22°C or below", "23°C", "24°C", "25°C", "26°C or higher"],
        market_prices=[
            MarketPrice(label="22°C or below", yes_bid=0.02, yes_ask=0.04, liquidity_usd=300.0),
            MarketPrice(label="23°C", yes_bid=0.18, yes_ask=0.22, liquidity_usd=250.0),
            MarketPrice(label="24°C", yes_bid=0.40, yes_ask=0.45, liquidity_usd=500.0, spread=0.05),
            MarketPrice(label="25°C", yes_bid=0.15, yes_ask=0.20, liquidity_usd=200.0),
            MarketPrice(label="26°C or higher", yes_bid=0.03, yes_ask=0.06, liquidity_usd=150.0),
        ],
    )


def _format_optional(value: float | None, fmt: str) -> str:
    return format(value, fmt) if value is not None else "-"


def _render(result: AnalysisResult) -> str:
    lines: list[str] = []
    strategy_bits = [f"strategy={result.model_strategy}"]
    if result.selected_model is not None:
        strategy_bits.append(f"selected_model={result.selected_model}")
    if result.calibration_sigma_source is not None:
        strategy_bits.append(f"sigma_source={result.calibration_sigma_source}")
    if result.fallback_reason is not None:
        strategy_bits.append(f"fallback={result.fallback_reason}")
    lines.append(" ".join(strategy_bits))
    lines.append(
        f"distribution: mean={result.distribution_mean_c:.2f}°C "
        f"sigma={result.distribution_sigma_c:.2f}°C"
    )
    lines.append("")
    header = f"{'bucket':<18} {'prob':>6} {'ask':>6} {'edge_pp':>8} {'action':<8} {'reason'}"
    lines.append(header)
    lines.append("-" * len(header))
    for row in result.rows:
        lines.append(
            f"{row.label:<18} "
            f"{row.probability:>6.3f} "
            f"{_format_optional(row.yes_ask, '>6.2f')} "
            f"{_format_optional(row.edge_yes_pp, '>8.2f')} "
            f"{row.action:<8} "
            f"{row.reason}"
        )
        for warning in row.warnings:
            lines.append(f"  ! {warning}")
    return "\n".join(lines)


@app.command()
def demo() -> None:
    """Run the local analysis on fake inputs and print the result."""
    result = analyze(_demo_input())
    typer.echo(_render(result))


# ---------------------------------------------------------------------------
# live (real APIs + markdown report)
# ---------------------------------------------------------------------------


@app.command()
def live(
    event_id: str | None = typer.Option(
        None,
        "--event-id",
        help="Gamma event id. If omitted, scans popular weather events for this --city.",
    ),
    city: str = typer.Option(
        "london",
        "--city",
        help="Registry city (contract station for Open-Meteo; filters Polymarket list by title/slug).",
    ),
    limit: int = typer.Option(
        20,
        "--limit",
        min=1,
        help="How many weather events to scan when --event-id is not set.",
    ),
    mode: LiveMode | None = typer.Option(
        None,
        "--mode",
        case_sensitive=False,
        help="`preview` runs the model only; `trade` also opens paper trades. Prompted on TTY when omitted.",
    ),
    day: DayChoice | None = typer.Option(
        None,
        "--day",
        case_sensitive=False,
        help="Which day(s) to run: `today` (T+0), `tomorrow` (T+1), or `both`. Prompted on TTY when omitted.",
    ),
    days_ahead: int | None = typer.Option(
        None,
        "--days-ahead",
        min=0,
        help="Target a single day = today + N (e.g. 0=today, 3=T+3). Overrides --day and its prompt when set.",
    ),
    model_strategy: ModelStrategy = typer.Option(
        ModelStrategy.BEST_HISTORICAL,
        "--model-strategy",
        case_sensitive=False,
        help=(
            "How to build the forecast distribution. "
            "'best_historical' (default) picks the calibrated model with lowest sigma at the "
            "current lead time and falls back to 'ensemble_spread' when calibration "
            "stats are missing. 'ensemble_spread' averages live models with spread sigma."
        ),
    ),
    strategy: str | None = typer.Option(
        None,
        "--strategy",
        help=(
            "Optional trade strategy name (e.g. dist_arb). When set, runs one decision "
            "at the current lead time (no entry gate). Omit to skip strategy output."
        ),
    ),
) -> None:
    """Fetch Polymarket + Open-Meteo data, write a concise markdown report.

    Interactive on a TTY: prompts for mode (preview/trade) and day (today/tomorrow/both)
    when those flags are omitted. Non-TTY defaults: ``--mode preview --day tomorrow``.

    Pass ``--strategy`` to include one trade-strategy decision in the report (and open
    a paper trade when ``--mode trade``). Without it, the report covers forecast,
    metadata, lead hours, and calibration only.
    """
    if strategy is not None:
        known = set(known_trade_strategies())
        if strategy not in known:
            typer.echo(
                f"Unknown --strategy {strategy!r}. Known: {', '.join(sorted(known))}",
                err=True,
            )
            raise typer.Exit(code=1)
    try:
        station = get_station(city)
    except KeyError:
        typer.echo(
            f"Unknown --city {city!r}. Registry keys: {', '.join(sorted(STATIONS))}",
            err=True,
        )
        raise typer.Exit(code=1) from None

    is_tty = sys.stdin.isatty()
    if mode is None:
        if is_tty:
            mode = LiveMode.TRADE if typer.confirm("Open paper trades?", default=False) else LiveMode.PREVIEW
        else:
            mode = LiveMode.PREVIEW

    if mode is LiveMode.TRADE and strategy is None:
        typer.echo(
            "note: --mode trade requires --strategy; staying in preview (no paper trades).",
            err=True,
        )
        mode = LiveMode.PREVIEW

    today = date.today()

    if days_ahead is not None:
        if day is not None:
            typer.echo(
                "note: --days-ahead overrides --day.",
                err=True,
            )
        target_dates = [today + timedelta(days=days_ahead)]
    else:
        if day is None:
            if is_tty:
                day_idx = typer.prompt(
                    "Day? 1=today (T+0)  2=tomorrow (T+1)  3=both",
                    default=2,
                    type=int,
                )
                day = {1: DayChoice.TODAY, 2: DayChoice.TOMORROW, 3: DayChoice.BOTH}.get(
                    day_idx, DayChoice.TOMORROW
                )
            else:
                day = DayChoice.TOMORROW

        if day is DayChoice.TODAY:
            target_dates = [today]
        elif day is DayChoice.BOTH:
            target_dates = [today, today + timedelta(days=1)]
        else:
            target_dates = [today + timedelta(days=1)]

    if event_id and len(target_dates) > 1:
        typer.echo(
            "--event-id with --day both is ambiguous; pick a single day.",
            err=True,
        )
        raise typer.Exit(code=1)

    for target_date in target_dates:
        _run_live_one_day(
            target_date=target_date,
            city=city,
            station=station,
            limit=limit,
            event_id=event_id,
            mode=mode,
            model_strategy=model_strategy,
            trade_strategy=strategy,
        )


def _run_live_one_day(
    *,
    target_date: date,
    city: str,
    station: Station,
    limit: int,
    event_id: str | None,
    mode: LiveMode,
    model_strategy: ModelStrategy,
    trade_strategy: str | None,
) -> None:
    reporter = RunReporter()
    days_ahead = (target_date - date.today()).days
    run_at = datetime.now(timezone.utc)
    try:
        with reporter:
            reporter.section(
                "Inputs",
                "\n".join(
                    [
                        f"- city: `{city}` (station {station.icao})",
                        f"- target_date: `{target_date.isoformat()}` (days_ahead={days_ahead})",
                        f"- mode: `{mode.value}`",
                        f"- event mode: {'explicit (--event-id)' if event_id else f'scan (limit={limit})'}",
                        f"- model_strategy: `{model_strategy.value}`",
                        f"- trade_strategy: `{trade_strategy}`" if trade_strategy else "- trade_strategy: _(none)_",
                    ]
                ),
            )

            if event_id:
                event = fetch_event(event_id.strip())
                if event.settlement_date is not None and event.settlement_date != target_date:
                    typer.echo(
                        f"Warning: event end date {event.settlement_date} != target day {target_date}.",
                        err=True,
                    )
            else:
                events = fetch_weather_events(
                    limit=limit,
                    end_on_date=target_date,
                    city=city,
                )
                event = first_parseable_weather_event(
                    events,
                    city=city,
                    settlement_date=target_date,
                )
                if event is None:
                    typer.echo(
                        f"No weather event matched city={city!r}, end date {target_date.isoformat()}, "
                        "and parseable Celsius buckets. Try a larger --limit or pass --event-id.",
                        err=True,
                    )
                    return

            event = hydrate_prices(event)
            reporter.section("Event", _md_event(event))

            bundle = fetch_open_meteo_live_bundle(
                latitude=station.latitude,
                longitude=station.longitude,
                timezone=station.timezone,
                models=DEFAULT_MODELS,
                target_dates=[target_date],
                fetched_at_utc=run_at,
            )
            if bundle.meta_staleness_detected:
                typer.echo(
                    "warning: Open-Meteo rolling meta changed during forecast fetch.",
                    err=True,
                )
            daily = bundle.daily_by_date[target_date]
            forecast = daily_to_forecast_values(bundle, target_date)
            forecast = append_wunderground_forecast(
                forecast,
                station,
                as_of_utc=run_at,
            )

            reporter.section(
                "Open-Meteo",
                format_open_meteo_md(
                    station=station,
                    target_date=target_date,
                    bundle=bundle,
                    forecast=forecast,
                ),
            )

            lead_hours = lead_hours_to_end_of_target_day(target_date, now=run_at)
            reporter.section(
                "Lead hours",
                format_lead_hours_md(wall_lead_hours=lead_hours, run_at=run_at),
            )
            if lead_hours < 6.0:
                typer.echo(
                    f"warning: lead_hours={lead_hours:.1f} < 6 — near settle, forecast value drops and edges sharpen.",
                    err=True,
                )

            static_selection = resolve_calibration_selection(
                csv_path=DEFAULT_CALIBRATION_STATS_CSV_PATH,
                label="best_historical (calibration_stats.csv)",
                station_id=station.icao,
                forecast=forecast,
                wall_lead_hours=lead_hours,
            )
            updated_selection = resolve_calibration_selection(
                csv_path=DEFAULT_UPDATED_CALIBRATION_STATS_CSV_PATH,
                label="best_historical_updated (calibration_stats_updated.csv)",
                station_id=station.icao,
                forecast=forecast,
                wall_lead_hours=lead_hours,
            )
            cal_path = DEFAULT_CALIBRATION_STATS_CSV_PATH
            if model_strategy in (
                ModelStrategy.BEST_HISTORICAL_UPDATED,
                ModelStrategy.WEIGHTED_HISTORICAL_UPDATED,
            ):
                cal_path = DEFAULT_UPDATED_CALIBRATION_STATS_CSV_PATH

            weighted_selection = resolve_weighted_calibration_selection(
                csv_path=DEFAULT_UPDATED_CALIBRATION_STATS_CSV_PATH,
                label="weighted_historical_updated (calibration_stats_updated.csv)",
                station_id=station.icao,
                forecast=forecast,
                wall_lead_hours=lead_hours,
            )

            calibration_sections = [
                format_calibration_selection_md(static_selection),
                format_calibration_selection_md(updated_selection),
                format_weighted_calibration_md(weighted_selection),
                "#### Per-model ceiling rows (static csv)",
                format_calibration_per_model_md(
                    csv_path=DEFAULT_CALIBRATION_STATS_CSV_PATH,
                    station_id=station.icao,
                    forecast=forecast,
                    wall_lead_hours=lead_hours,
                ),
                "#### Per-model ceiling rows (updated csv)",
                format_calibration_per_model_md(
                    csv_path=DEFAULT_UPDATED_CALIBRATION_STATS_CSV_PATH,
                    station_id=station.icao,
                    forecast=forecast,
                    wall_lead_hours=lead_hours,
                ),
            ]
            calibration_body = "\n\n".join(calibration_sections)
            reporter.section("Calibration", calibration_body)

            preview_result = analyze_event(
                forecast,
                event,
                lead_hours=lead_hours,
                model_strategy=model_strategy.value,
                station_id=station.icao,
                calibration_stats_path=cal_path,
            )
            if (
                model_strategy
                in (ModelStrategy.BEST_HISTORICAL, ModelStrategy.BEST_HISTORICAL_UPDATED)
                and preview_result.fallback_reason is not None
            ):
                typer.echo(
                    f"warning: --model-strategy {model_strategy.value} fell back to "
                    f"ensemble_spread ({preview_result.fallback_reason}).",
                    err=True,
                )
            if (
                model_strategy is ModelStrategy.WEIGHTED_HISTORICAL_UPDATED
                and preview_result.fallback_reason is not None
            ):
                typer.echo(
                    f"error: --model-strategy {model_strategy.value} could not build "
                    f"distribution ({preview_result.fallback_reason}).",
                    err=True,
                )
            reporter.section(
                "Distribution",
                format_distribution_md(
                    preview_result.distribution_build,
                    model_strategy=model_strategy.value,
                    result=preview_result,
                    forecast=forecast,
                ),
            )

            profile_result: ProfileRunResult | None = None
            traded_profile_id: str | None = None
            if trade_strategy is not None:
                if mode is LiveMode.TRADE:
                    profiles = _load_profiles()
                    profile = _profile_for_strategy(
                        profiles,
                        model_strategy=model_strategy.value,
                        trade_strategy=trade_strategy,
                    )
                    if profile is None:
                        typer.echo(
                            f"No paper profile for model_strategy={model_strategy.value!r} "
                            f"trade_strategy={trade_strategy!r}.",
                            err=True,
                        )
                    else:
                        store = default_ledger_store()
                        traded_profile_id = profile.id
                        profile_result = run_profile(
                            store,
                            profile,
                            forecast,
                            event,
                            lead_hours=lead_hours,
                            dedupe=True,
                            enforce_gate=False,
                        )
                else:
                    analysis = analyze_event(
                        forecast,
                        event,
                        strategy=trade_strategy_for_name(trade_strategy),
                        lead_hours=lead_hours,
                        model_strategy=model_strategy.value,
                        station_id=station.icao,
                        calibration_stats_path=cal_path,
                    )
                    profile_result = ProfileRunResult(
                        profile_id=f"live_{trade_strategy}",
                        action="PREVIEW",
                        lead_hours=lead_hours,
                        analysis=analysis,
                    )

                if profile_result.action in (
                    "SETTLED",
                    "NOTHING_TO_SETTLE",
                    "RESOLVED_NO_WINNER",
                ):
                    strategy_body = f"- action: `{profile_result.action}`"
                elif profile_result.analysis is not None:
                    strategy_body = format_strategy_analysis_md(
                        profile_result.analysis,
                        trade_strategy=trade_strategy,
                    )
                else:
                    strategy_body = format_strategy_analysis_md(
                        preview_result,
                        trade_strategy=trade_strategy,
                    )
                reporter.section(f"Strategy ({trade_strategy})", strategy_body)
                if profile_result.opened or profile_result.settled:
                    trade_lines = []
                    for t in profile_result.opened:
                        trade_lines.append(
                            f"- OPEN {t.bucket_label} side={t.side} entry={t.entry_price:.2f} "
                            f"stake=${t.stake_usd:.2f} edge={t.edge_pp:.2f}pp"
                        )
                    for t in profile_result.settled:
                        trade_lines.append(
                            f"- SETTLE {t.bucket_label} side={t.side} stake=${t.stake_usd:.2f}"
                        )
                    reporter.section("Trades", "\n".join(trade_lines))

            typer.echo(f"event: {event.title} ({event.event_id})")
            end_s = event.settlement_date.isoformat() if event.settlement_date else "unknown"
            typer.echo(f"event Gamma end date (UTC day): {end_s}")
            typer.echo(
                f"forecast: {station.city} ({station.icao}) "
                f"date={target_date.isoformat()} wall_lead={lead_hours:.1f}h -> {forecast.values_c}"
            )
            if static_selection.row is not None:
                typer.echo(
                    f"calibration (static): {static_selection.row.model} "
                    f"@ {static_selection.row.lead_hours:g}h"
                )
            if updated_selection.row is not None:
                typer.echo(
                    f"calibration (updated): {updated_selection.row.model} "
                    f"@ {updated_selection.row.lead_hours:g}h"
                )
            if model_strategy in (
                ModelStrategy.BEST_HISTORICAL,
                ModelStrategy.BEST_HISTORICAL_UPDATED,
                ModelStrategy.WEIGHTED_HISTORICAL_UPDATED,
            ):
                typer.echo(f"calibration per-model ceiling ({cal_path.name}):")
                typer.echo(
                    format_calibration_per_model_compact(
                        csv_path=cal_path,
                        station_id=station.icao,
                        forecast=forecast,
                        wall_lead_hours=lead_hours,
                    )
                )
            if (
                not math.isnan(preview_result.distribution_mean_c)
                and not math.isnan(preview_result.distribution_sigma_c)
            ):
                typer.echo(
                    f"distribution ({preview_result.model_strategy}): "
                    f"mean={preview_result.distribution_mean_c:.2f}°C "
                    f"sigma={preview_result.distribution_sigma_c:.2f}°C"
                )
            else:
                typer.echo(
                    f"distribution ({preview_result.model_strategy}): unavailable "
                    f"({preview_result.fallback_reason})"
                )
            if profile_result is not None and profile_result.analysis is not None:
                buys = [
                    row
                    for row in profile_result.analysis.rows
                    if row.action.startswith("BUY")
                ]
                if buys:
                    pick = max(buys, key=lambda r: r.stake_usd or 0.0)
                    stake = f"${pick.stake_usd:.2f}" if pick.stake_usd is not None else "—"
                    typer.echo(
                        f"strategy {trade_strategy}: {pick.action} {pick.label} stake={stake} "
                        f"({profile_result.action})"
                    )
                else:
                    typer.echo(f"strategy {trade_strategy}: SKIP ({profile_result.action})")
            if mode is LiveMode.TRADE and traded_profile_id is not None:
                store = default_ledger_store()
                state = store.read_state(traded_profile_id)
                typer.echo(
                    f"  {traded_profile_id}: balance=${state.balance_usd:.2f} "
                    f"open={len(state.open_trades)}"
                )
    finally:
        path = reporter.write()
        typer.echo(f"report written: {path}")


def _profile_for_strategy(
    profiles: list,
    *,
    model_strategy: str,
    trade_strategy: str,
):
    for profile in profiles:
        if profile.model_strategy == model_strategy and profile.trade_strategy == trade_strategy:
            return profile
    return None


def _md_event(event: PolymarketEvent) -> str:
    end_iso = event.settlement_date.isoformat() if event.settlement_date else "unknown"
    lines = [
        f"- title: {event.title}",
        f"- event_id: `{event.event_id}`",
        f"- slug: `{event.slug}`",
        f"- settlement_date: `{end_iso}`",
        f"- buckets ({len(event.buckets)}):",
        "",
        "| label | yes_bid | yes_ask | liquidity_usd | spread |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for bucket in event.buckets:
        lines.append(
            f"| {bucket.label} | "
            f"{_format_optional(bucket.yes_bid, '.4f')} | "
            f"{_format_optional(bucket.yes_ask, '.4f')} | "
            f"{_format_optional(bucket.liquidity_usd, '.2f')} | "
            f"{_format_optional(bucket.spread, '.4f')} |"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# offline calibration
# ---------------------------------------------------------------------------


@app.command("fetch-historical-forecasts")
def fetch_historical_forecasts(
    station_id: str = typer.Option(..., "--station-id", help="Contract station id, e.g. EGLC."),
    latitude: float = typer.Option(..., "--latitude", help="Station latitude."),
    longitude: float = typer.Option(..., "--longitude", help="Station longitude."),
    model: str = typer.Option(..., "--model", help="Open-Meteo model id."),
    start_date: str | None = typer.Option(
        None,
        "--start-date",
        help="First target day YYYY-MM-DD (required without --run-time).",
    ),
    end_date: str | None = typer.Option(
        None,
        "--end-date",
        help="Last target day YYYY-MM-DD (required without --run-time).",
    ),
    run_time: list[str] = typer.Option(
        [],
        "--run-time",
        help="UTC model init (repeatable). Raw JSON only; omit start/end dates.",
    ),
    run_time_start: str | None = typer.Option(
        None,
        "--run-time-start",
        help="First UTC init for a run range (with --run-time-end and --run-interval-hours).",
    ),
    run_time_end: str | None = typer.Option(
        None,
        "--run-time-end",
        help="Last UTC init for a run range (defaults to --run-time-start).",
    ),
    run_interval_hours: float | None = typer.Option(
        None,
        "--run-interval-hours",
        min=0.0,
        help="Hours between run inits when using --run-time-start (e.g. 6 for synoptic).",
    ),
    forecast_days: int = typer.Option(
        DEFAULT_FORECAST_DAYS,
        "--forecast-days",
        min=1,
        help="Single-Runs forward horizon length (forecast_days query param).",
    ),
    max_lead_hours: float = typer.Option(
        72.0,
        "--max-lead-hours",
        min=0.0,
        help="Maximum lead time before target day.",
    ),
    lead_step_hours: float = typer.Option(
        6.0,
        "--lead-step-hours",
        min=0.0,
        help="Lead-time step in hours.",
    ),
    timezone: str = typer.Option(
        "UTC",
        "--timezone",
        help="IANA timezone for target-day midnight anchor.",
    ),
    out: Path = typer.Option(
        DEFAULT_HISTORICAL_FORECASTS_PATH,
        "--out",
        help="Output JSONL path for parsed forecast records.",
    ),
    raw_dir: Path = typer.Option(
        DEFAULT_RAW_FORECASTS_DIR,
        "--raw-dir",
        help="Directory for full Single-Runs JSON responses.",
    ),
    base_url: str = typer.Option(
        DEFAULT_SINGLE_RUNS_BASE_URL,
        "--base-url",
        help="Open-Meteo Single Runs API base URL.",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Print planned request count without calling the API.",
    ),
) -> None:
    """Fetch Single Runs forecasts; save raw JSON and optionally append parsed JSONL."""
    station = station_id.strip()
    model_id = model.strip()
    tz = timezone.strip()

    raw_run_mode = bool(run_time or run_time_start)
    if raw_run_mode:
        if start_date or end_date:
            typer.echo(
                "note: --start-date/--end-date ignored in raw run mode",
                err=True,
            )
        try:
            parsed_runs = resolve_raw_fetch_run_times(
                run_time,
                run_time_start=run_time_start,
                run_time_end=run_time_end,
                run_interval_hours=run_interval_hours,
            )
        except ValueError as exc:
            raise typer.BadParameter(str(exc)) from exc

        if not parsed_runs:
            raise typer.BadParameter(
                "set --run-time and/or --run-time-start with --run-interval-hours"
            )

        typer.echo(
            f"planned_raw_requests={len(parsed_runs)} forecast_days={forecast_days}"
        )
        if dry_run:
            return

        fetched, skipped, failed = fetch_raw_forecast_runs(
            station,
            latitude,
            longitude,
            model_id,
            parsed_runs,
            timezone=tz,
            raw_dir=raw_dir,
            forecast_days=forecast_days,
            base_url=base_url,
        )
        typer.echo(
            f"raw_fetched={fetched} raw_skipped={skipped} raw_failed={failed} "
            f"raw_dir={raw_dir} forecast_days={forecast_days}"
        )
        return

    if not start_date or not end_date:
        raise typer.BadParameter(
            "--start-date and --end-date are required unless raw run mode is used "
            "(--run-time and/or --run-time-start)"
        )

    parsed_start = date.fromisoformat(start_date)
    parsed_end = date.fromisoformat(end_date)

    jobs = plan_historical_forecast_requests(
        station_id=station,
        latitude=latitude,
        longitude=longitude,
        model=model_id,
        start_date=parsed_start,
        end_date=parsed_end,
        max_lead_hours=max_lead_hours,
        lead_step_hours=lead_step_hours,
        timezone=tz,
    )

    target_days = (parsed_end - parsed_start).days + 1
    typer.echo(
        f"planned_requests={len(jobs)} "
        f"(target_days={target_days}, leads_per_day={len(jobs) // target_days if target_days else 0})"
    )

    if dry_run:
        return

    fetched, skipped, failed = fetch_historical_forecast_batch(
        jobs,
        out_path=out,
        base_url=base_url,
        raw_dir=raw_dir,
        forecast_days=forecast_days,
    )
    typer.echo(
        f"fetched={fetched} skipped={skipped} failed={failed} out={out} "
        f"raw_dir={raw_dir} forecast_days={forecast_days}"
    )


@app.command("compute-calibration-stats")
def compute_calibration_stats_cmd(
    forecasts: Path = typer.Option(
        DEFAULT_HISTORICAL_FORECASTS_PATH,
        "--forecasts",
        help="Historical forecast JSONL input.",
    ),
    observations: Path = typer.Option(
        DEFAULT_OBSERVATIONS_PATH,
        "--observations",
        help="Observed Tmax JSONL input.",
    ),
    out: Path = typer.Option(
        DEFAULT_CALIBRATION_STATS_PATH,
        "--out",
        help="Output calibration stats JSON path.",
    ),
) -> None:
    """Join forecasts with observations and write RMSE/MAE/bias stats."""
    forecast_rows = read_historical_forecasts_jsonl(forecasts)
    observation_rows = read_observations_jsonl(observations)

    errors = join_forecasts_with_observations(forecast_rows, observation_rows)
    stats = compute_calibration_stats(errors)
    write_calibration_stats_json(stats, out)

    typer.echo(
        f"joined_samples={len(errors)} stat_groups={len(stats)} out={out}"
    )


# ---------------------------------------------------------------------------
# paper (Phase 9)
# ---------------------------------------------------------------------------


def _load_profiles(config: Path = DEFAULT_PROFILES_PATH):
    return load_paper_profiles(config)


@paper_app.command("status")
def paper_status(
    config: Path = typer.Option(
        DEFAULT_PROFILES_PATH,
        "--config",
        help="Path to paper_profiles.yaml",
    ),
    no_open: bool = typer.Option(
        False,
        "--no-open",
        help="Only print profile balances; skip open position details.",
    ),
) -> None:
    """Print balances for all active trading profiles."""
    store = default_ledger_store()
    profiles = _load_profiles(config)
    header = f"{'profile':<24} {'balance':>10} {'open':>5} {'settled':>8} {'realized':>11}"
    typer.echo(header)
    typer.echo("-" * len(header))
    for profile in profiles:
        state = store.read_state(profile.id)
        typer.echo(
            f"{profile.id:<24} ${state.balance_usd:>9.2f} "
            f"{len(state.open_trades):>5} {state.settled_count:>8} "
            f"${state.realized_pnl_usd:>+10.2f}"
        )

    if not no_open:
        for profile in profiles:
            state = store.read_state(profile.id)
            if not state.open_trades:
                continue
            typer.echo("")
            typer.echo(f"[{profile.id}] open positions:")
            sub = f"  {'trade_id':<14} {'event_id':<14} {'bucket':<18} {'side':<4} {'entry':>6} {'stake':>8} {'shares':>8}"
            typer.echo(sub)
            typer.echo("  " + "-" * (len(sub) - 2))
            for trade in state.open_trades:
                entry = trade.entry_price if trade.entry_price is not None else trade.yes_ask
                typer.echo(
                    f"  {trade.trade_id:<14} {trade.event_id:<14} {trade.bucket_label:<18} "
                    f"{trade.side:<4} {entry:>6.2f} ${trade.stake_usd:>7.2f} {trade.shares:>8.2f}"
                )


@paper_app.command("active-monitor")
def paper_active_monitor(
    config: Path = typer.Option(
        DEFAULT_PROFILES_PATH,
        "--config",
        help="Path to paper_profiles.yaml",
    ),
) -> None:
    """Dry-run one active-management sweep (no DB writes).

    Prints, per active wallet, the adds/flattens/opens the edge controller
    would make right now against the live forecast + order book.
    """
    from polytempo.paper.active_controller import manage_active_wallets

    store = default_ledger_store()
    profiles = _load_profiles(config)
    active = [p for p in profiles if p.active_params is not None]
    if not active:
        typer.echo("no active wallets configured (add an active_wallets block)")
        return

    result = manage_active_wallets(store, active, dry_run=True)
    typer.echo(result.summary())
    for d in result.decisions:
        if d.action not in ("OPEN", "ADD", "FLATTEN"):
            continue
        size = (
            f"${d.stake_usd:.2f}"
            if d.stake_usd is not None
            else (f"@{d.exit_price:.3f}" if d.exit_price is not None else "")
        )
        edge = f"{d.edge_pp:+.1f}pp" if d.edge_pp is not None else "  n/a"
        typer.echo(
            f"  {d.profile_id:<26} {d.action:<8} {d.bucket_label:<16} "
            f"{d.side:<3} edge={edge:<8} {size} {d.reason}"
        )


@paper_app.command("scenarios")
def paper_scenarios(
    event_id: str | None = typer.Option(
        None,
        "--event-id",
        help="Restrict to one event. Defaults to every event with open trades.",
    ),
    min_prob: float = typer.Option(
        0.05,
        "--min-prob",
        min=0.0,
        max=1.0,
        help="Buckets with current yes_ask below this are rolled into tail rows.",
    ),
) -> None:
    """Per-event scenario PnL: net outcome per possible winning bucket.

    Pulls open trades from every per-strategy ledger, groups by event, then
    fetches each event from Polymarket for current yes_ask (used to label
    likely vs unlikely buckets). Low-prob tails are folded into one row each
    ("X°C or lower" / "Y°C or higher"); the row's PnL is the worst-case net
    across the rolled-up buckets.
    """
    store = default_ledger_store()
    profiles = _load_profiles()
    profile_ids = [p.id for p in profiles]
    open_by_event: dict[str, dict[str, list]] = {}
    for profile in profiles:
        state = store.read_state(profile.id)
        for trade in state.open_trades:
            if event_id and trade.event_id != event_id:
                continue
            open_by_event.setdefault(trade.event_id, {}).setdefault(
                profile.id, []
            ).append(trade)

    if not open_by_event:
        typer.echo("no open positions on any event")
        return

    strategy_names = profile_ids

    for evt_id, by_strat in open_by_event.items():
        try:
            event = fetch_event(evt_id)
        except Exception as exc:
            typer.echo(f"\nevent {evt_id}: fetch failed ({exc})", err=True)
            continue

        bucket_labels = [b.label for b in event.buckets]
        parsed = {b.label: parse_temperature_bucket(b.label) for b in event.buckets}
        ordered = sorted(
            bucket_labels,
            key=lambda lbl: (
                parsed[lbl].lower_c if parsed[lbl].lower_c is not None else float("-inf")
            ),
        )
        ask_by_label = {b.label: b.yes_ask for b in event.buckets}

        def _scenario_pnl(winner: str) -> dict[str, float]:
            out: dict[str, float] = {}
            for strat_name in strategy_names:
                trades = by_strat.get(strat_name, [])
                pnl = 0.0
                for t in trades:
                    payout = t.shares if (
                        (t.side == "YES" and t.bucket_label == winner)
                        or (t.side == "NO" and t.bucket_label != winner)
                    ) else 0.0
                    pnl += payout - t.stake_usd
                out[strat_name] = pnl
            return out

        yes_bet_labels = {
            t.bucket_label
            for trades in by_strat.values()
            for t in trades
            if t.side == "YES"
        }

        def _rollable(lbl: str) -> bool:
            return (
                (ask_by_label.get(lbl) or 0.0) < min_prob
                and lbl not in yes_bet_labels
            )

        low_tail: list[str] = []
        keep: list[str] = []
        high_tail: list[str] = []
        i = 0
        while i < len(ordered) and _rollable(ordered[i]):
            low_tail.append(ordered[i])
            i += 1
        j = len(ordered) - 1
        while j >= i and _rollable(ordered[j]):
            high_tail.insert(0, ordered[j])
            j -= 1
        keep = ordered[i : j + 1]

        rows: list[tuple[str, dict[str, float], float | None]] = []
        if low_tail:
            pnls = [_scenario_pnl(w) for w in low_tail]
            worst = {
                name: min(p[name] for p in pnls) for name in strategy_names
            }
            edge_lbl = low_tail[-1]
            edge_upper = parsed[edge_lbl].upper_c
            if parsed[edge_lbl].kind == "or_below":
                rollup = edge_lbl
            elif edge_upper is not None:
                rollup = f"{int(edge_upper - 0.5)}°C or lower"
            else:
                rollup = f"{edge_lbl} or lower"
            rows.append((rollup, worst, None))
        for label in keep:
            rows.append((label, _scenario_pnl(label), ask_by_label.get(label)))
        if high_tail:
            pnls = [_scenario_pnl(w) for w in high_tail]
            worst = {
                name: min(p[name] for p in pnls) for name in strategy_names
            }
            edge_lbl = high_tail[0]
            edge_lower = parsed[edge_lbl].lower_c
            if parsed[edge_lbl].kind == "or_higher":
                rollup = edge_lbl
            elif edge_lower is not None:
                rollup = f"{int(edge_lower + 0.5)}°C or higher"
            else:
                rollup = f"{edge_lbl} or higher"
            rows.append((rollup, worst, None))

        typer.echo("")
        typer.echo(f"event {evt_id}: {event.title}")
        end_iso = event.settlement_date.isoformat() if event.settlement_date else "unknown"
        typer.echo(f"  settlement_date={end_iso}  rollup_threshold yes_ask<{min_prob}")
        header = (
            f"  {'winner':<18} {'mkt':>6} "
            + " ".join(f"{n:>11}" for n in strategy_names)
            + f" {'total':>11}"
        )
        typer.echo(header)
        typer.echo("  " + "-" * (len(header) - 2))
        for label, pnls, ask in rows:
            mkt = f"{ask:.3f}" if ask is not None else "  -  "
            cells = " ".join(f"{pnls[n]:>+11.2f}" for n in strategy_names)
            total = sum(pnls[n] for n in strategy_names)
            typer.echo(
                f"  {label:<18} {mkt:>6} {cells} {total:>+11.2f}"
            )


@paper_app.command("list-london")
def paper_list_london(
    limit: int = typer.Option(20, help="Max events to scan."),
    target_date: str | None = typer.Option(
        None,
        "--date",
        help="Filter by settlement date YYYY-MM-DD (recommended).",
    ),
) -> None:
    """List active London weather events from Polymarket."""
    end_on = date.fromisoformat(target_date) if target_date else None
    events = fetch_weather_events(limit=limit, end_on_date=end_on, city="london")
    matches = [e for e in events if "london" in e.title.lower()]
    if not matches:
        typer.echo("no active London events found")
        raise typer.Exit(code=1)
    for event in matches:
        typer.echo(f"{event.event_id}\t{event.slug}\t{event.title}")


def _render_run_summary(summary: RunSummary) -> str:
    return format_run_summary(summary)


@paper_app.command("open")
def paper_open(
    event_id: str = typer.Option(..., "--event-id", help="Polymarket event id."),
    target_date: str = typer.Option(..., "--date", help="Settlement date YYYY-MM-DD."),
    city: str = typer.Option("london", "--city", help="Contract station for Open-Meteo."),
    config: Path = typer.Option(DEFAULT_PROFILES_PATH, "--config"),
) -> None:
    """Run all active profiles against one event. Settles automatically if resolved."""
    parsed_date = date.fromisoformat(target_date)
    ctx = fetch_market_context(city, parsed_date, event_id=event_id)
    if ctx.date_mismatch:
        typer.echo(
            f"Warning: event settlement {ctx.event.settlement_date} != target {parsed_date}.",
            err=True,
        )

    store = default_ledger_store()
    profiles = _load_profiles(config)
    summary = run_profiles(
        store,
        profiles,
        ctx.forecast,
        ctx.event,
        lead_hours=ctx.lead_hours,
        dedupe=True,
        enforce_gate=False,
    )
    typer.echo(_render_run_summary(summary))
    typer.echo("")
    for p in summary.profiles:
        state = store.read_state(p.profile_id)
        typer.echo(
            f"  {p.profile_id:<24} balance=${state.balance_usd:>8.2f} "
            f"open={len(state.open_trades):>2} settled={state.settled_count:>3} "
            f"realized=${state.realized_pnl_usd:>+8.2f}"
        )


@paper_app.command("settle")
def paper_settle(
    event_id: str | None = typer.Option(
        None, "--event-id", help="Polymarket event id. Omit and pass --all to sweep."
    ),
    all_open: bool = typer.Option(
        False,
        "--all",
        help="Settle every open event that Gamma reports resolved. Ignores winner overrides.",
    ),
    winner: str | None = typer.Option(
        None,
        "--winner",
        help='Manual override: winning bucket label, e.g. "29°C". Skips Wunderground.',
    ),
    observed_tmax: float | None = typer.Option(
        None,
        "--observed-tmax",
        help="Manual override: observed Tmax in °C. Mapped to a bucket via event labels.",
    ),
    city: str = typer.Option(
        "london",
        "--city",
        help="Contract station for Wunderground lookup (default london/EGLC).",
    ),
) -> None:
    """Settle every open trade on this event across all per-strategy ledgers.

    Winner resolution order: --winner > --observed-tmax > Wunderground fetch
    (station from --city, date from the event's settlement_date).

    With --all, sweep every event that still has open trades in any ledger and
    settle the ones Gamma reports resolved, using the market's own winning bucket.
    """
    if all_open:
        if event_id is not None:
            typer.echo("--all and --event-id are mutually exclusive.", err=True)
            raise typer.Exit(code=1)
        _settle_all_open()
        return

    if event_id is None:
        typer.echo("pass --event-id or --all.", err=True)
        raise typer.Exit(code=1)

    event = fetch_event(event_id.strip())

    if winner is not None:
        winning_label = winner
        source = "manual --winner"
    else:
        if observed_tmax is None:
            try:
                station = get_station(city)
            except KeyError:
                typer.echo(
                    f"Unknown --city {city!r}. Registry keys: {', '.join(sorted(STATIONS))}",
                    err=True,
                )
                raise typer.Exit(code=1) from None
            if event.settlement_date is None:
                typer.echo(
                    "event has no settlement_date; pass --observed-tmax or --winner",
                    err=True,
                )
                raise typer.Exit(code=1)
            try:
                obs = fetch_wunderground_observed_tmax(station.icao, event.settlement_date)
            except Exception as exc:
                typer.echo(f"wunderground fetch failed: {exc}", err=True)
                raise typer.Exit(code=1) from None
            observed_tmax = obs.observed_tmax_c
            source = f"wunderground {station.icao}@{event.settlement_date.isoformat()}"
        else:
            source = "manual --observed-tmax"

        parsed_buckets = [parse_temperature_bucket(b.label) for b in event.buckets]
        try:
            winning_label = bucket_label_for_value(observed_tmax, parsed_buckets)
        except ValueError as exc:
            typer.echo(f"bucket mapping failed: {exc}", err=True)
            raise typer.Exit(code=1) from None

    obs_note = f" observed={observed_tmax:.2f}°C" if observed_tmax is not None else ""
    typer.echo(f"winner={winning_label!r} source={source}{obs_note}")

    store = default_ledger_store()
    profiles = _load_profiles()
    total_settled = 0
    for profile in profiles:
        if not store.has_open_on_event(profile.id, event_id):
            continue
        settled = store.settle_event(profile.id, event_id, winning_label)
        state = store.read_state(profile.id)
        total_settled += len(settled)
        typer.echo("")
        if not settled:
            typer.echo(f"[{profile.id}] no open trades for this event")
        else:
            typer.echo(f"[{profile.id}] settled {len(settled)} trade(s):")
            for trade in settled:
                outcome = "YES" if trade.bucket_label == winning_label else "NO"
                typer.echo(f"  {trade.trade_id} {trade.bucket_label}  -> {outcome}")
        typer.echo(
            f"  balance=${state.balance_usd:.2f} "
            f"open={len(state.open_trades)} "
            f"settled={state.settled_count} "
            f"realized=${state.realized_pnl_usd:+.2f}"
        )

    if total_settled == 0:
        typer.echo("\nno open trades matched this event across any profile")


def _settle_all_open() -> None:
    """Settle every open event Gamma reports resolved, across all profiles."""
    store = default_ledger_store()
    profiles = _load_profiles()
    event_ids = open_event_ids(store)
    if not event_ids:
        typer.echo("no open trades in any profile")
        return

    grand_total = 0
    for eid in event_ids:
        event = fetch_event(eid)
        if not is_event_resolved(event):
            typer.echo(f"[{eid}] {event.title} — not resolved on Gamma yet, skipping")
            continue
        winning_label = winning_label_from_event(event)
        if winning_label is None:
            typer.echo(f"[{eid}] {event.title} — resolved but no single winner, skipping")
            continue

        typer.echo(f"[{eid}] {event.title} — winner={winning_label!r}")
        for profile in profiles:
            if not store.has_open_on_event(profile.id, eid):
                continue
            settled = store.settle_event(profile.id, eid, winning_label)
            grand_total += len(settled)
            if settled:
                typer.echo(f"  [{profile.id}] settled {len(settled)} trade(s)")

    typer.echo(f"\ntotal settled across all open events: {grand_total}")
