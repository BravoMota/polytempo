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
    MODEL_STRATEGY_ENSEMBLE_SPREAD,
    AnalysisInput,
    AnalysisResult,
    analyze,
    analyze_event,
)
from polytempo.model.distribution import format_distribution_build_markdown
from polytempo.markets.buckets import bucket_label_for_value, parse_temperature_bucket
from polytempo.markets.polymarket import (
    PolymarketEvent,
    fetch_event,
    fetch_weather_events,
    first_parseable_weather_event,
)
from polytempo.weather.wunderground import fetch_wunderground_observed_tmax
from polytempo.reports.writer import RunReporter
from polytempo.paper.ledger import (
    DEFAULT_LEDGER_DIR,
    DEFAULT_LEDGER_PATH,
    ledger_path_for,
    open_trades_from_analysis,
    read_state,
    settle_event,
)
from polytempo.paper.run import RunSummary, preview_pipeline, run_pipeline
from polytempo.strategy import ArgmaxYesStrategy, DistArbStrategy, MidBandStrategy
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
from polytempo.weather.open_meteo import DailyMaxForecast, fetch_for_station
from polytempo.weather.schema import ForecastValues
from polytempo.weather.stations import STATIONS, Station, get_station

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
) -> None:
    """Fetch Polymarket + Open-Meteo data, run all strategies, optionally open paper trades.

    Interactive on a TTY: prompts for mode (preview/trade) and day (today/tomorrow/both)
    when those flags are omitted. Non-TTY defaults: ``--mode preview --day tomorrow``.
    """
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

    if day is None:
        if is_tty and mode is LiveMode.TRADE:
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

    today = date.today()
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
) -> None:
    reporter = RunReporter()
    days_ahead = (target_date - date.today()).days
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
                events = fetch_weather_events(limit=limit, end_on_date=target_date)
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

            reporter.section("Event", _md_event(event))

            daily = fetch_for_station(station, target_date)
            forecast = daily.to_forecast_values()
            reporter.section(
                "Forecast",
                _md_forecast(station, target_date, daily, forecast),
            )

            lead_hours = _lead_hours_until_target_day(target_date)
            if lead_hours < 6.0:
                typer.echo(
                    f"warning: lead_hours={lead_hours:.1f} < 6 — near settle, forecast value drops and edges sharpen.",
                    err=True,
                )

            preview_result = analyze_event(
                forecast,
                event,
                lead_hours=lead_hours,
                model_strategy=model_strategy.value,
                station_id=station.icao,
            )
            if (
                model_strategy is ModelStrategy.BEST_HISTORICAL
                and preview_result.fallback_reason is not None
            ):
                typer.echo(
                    f"warning: --model-strategy best_historical fell back to "
                    f"ensemble_spread ({preview_result.fallback_reason}).",
                    err=True,
                )
            reporter.section(
                "Distribution",
                format_distribution_build_markdown(preview_result.distribution_build),
            )

            strategies = _default_strategies()
            if mode is LiveMode.TRADE:
                summary = run_pipeline(
                    forecast,
                    event,
                    strategies,
                    dedupe=True,
                    lead_hours=lead_hours,
                    model_strategy=model_strategy.value,
                    station_id=station.icao,
                )
            else:
                summary = preview_pipeline(
                    forecast,
                    event,
                    strategies,
                    lead_hours=lead_hours,
                    model_strategy=model_strategy.value,
                    station_id=station.icao,
                )

            reporter.section(
                "Run outcome",
                _md_run_summary(summary),
            )

            typer.echo(f"event: {event.title} ({event.event_id})")
            end_s = event.settlement_date.isoformat() if event.settlement_date else "unknown"
            typer.echo(f"event Gamma end date (UTC day): {end_s}")
            typer.echo(
                f"forecast: {station.city} ({station.icao}) "
                f"date={target_date.isoformat()} lead_hours={lead_hours:.1f} -> {forecast.values_c}"
            )
            typer.echo("")
            typer.echo(_render_run_summary(summary))
            if mode is LiveMode.TRADE:
                typer.echo("")
                for s in summary.strategies:
                    state = read_state(ledger_path_for(s.name))
                    typer.echo(
                        f"  {s.name:<12} balance=${state.balance_usd:>8.2f} "
                        f"open={len(state.open_trades):>2} settled={state.settled_count:>3} "
                        f"realized=${state.realized_pnl_usd:>+8.2f}"
                    )
    finally:
        path = reporter.write()
        typer.echo(f"report written: {path}")


def _md_run_summary(summary: RunSummary) -> str:
    lines = [
        f"- mode: `{summary.mode}`",
        f"- resolved: `{summary.resolved}`",
        f"- winning_label: `{summary.winning_label}`" if summary.winning_label else "- winning_label: `-`",
        "",
    ]
    for s in summary.strategies:
        lines.append(f"### {s.name} — {s.action}")
        if s.analysis is not None:
            lines.append("")
            lines.append(_md_result(s.analysis))
        if s.opened:
            lines.append("")
            lines.append("opened:")
            for t in s.opened:
                lines.append(
                    f"- {t.bucket_label} side={t.side} entry={t.entry_price:.2f} "
                    f"stake=${t.stake_usd:.2f} shares={t.shares:.2f} edge={t.edge_pp:.2f}pp"
                )
        if s.settled:
            lines.append("")
            lines.append("settled:")
            for t in s.settled:
                lines.append(
                    f"- {t.bucket_label} side={t.side} stake=${t.stake_usd:.2f} shares={t.shares:.2f}"
                )
        lines.append("")
    return "\n".join(lines)


def _lead_hours_until_target_day(target: date) -> float:
    """Hours from now (UTC) until the start of the target calendar day (UTC)."""
    target_start = datetime.combine(target, time.min, tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    return max(0.0, (target_start - now).total_seconds() / 3600.0)


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


def _md_forecast(
    station: Station,
    target_date: date,
    daily: DailyMaxForecast,
    forecast: ForecastValues,
) -> str:
    return "\n".join(
        [
            f"- station: {station.city} ({station.icao})",
            f"- coordinates: lat={station.latitude} lon={station.longitude} tz={station.timezone}",
            f"- target_date: `{target_date.isoformat()}`",
            f"- models: {', '.join(daily.models) if daily.models else '-'}",
            f"- raw values_c: {daily.values_c}",
            f"- normalized ForecastValues.values_c: {forecast.values_c}",
        ]
    )


def _md_result(result: AnalysisResult) -> str:
    lines: list[str] = []
    lines.append(f"- model_strategy: `{result.model_strategy}`")
    if result.selected_model is not None:
        lines.append(f"- selected_model: `{result.selected_model}`")
    if result.calibration_sigma_source is not None:
        lines.append(f"- sigma_source: `{result.calibration_sigma_source}`")
    if result.fallback_reason is not None:
        lines.append(f"- fallback_reason: `{result.fallback_reason}`")
    if result.calibration_row is not None:
        row = result.calibration_row
        lines.append(
            "- calibration_row: "
            f"lead_hours={row.lead_hours:g} n_samples={row.n_samples} "
            f"bias_c={row.bias_c:.4f} mae_c={row.mae_c:.4f} "
            f"rmse_c={row.rmse_c:.4f} error_std_c={row.error_std_c:.4f}"
        )
    lines.append(
        f"- distribution: mean={result.distribution_mean_c:.2f}°C "
        f"sigma={result.distribution_sigma_c:.2f}°C"
    )
    lines.append("")
    lines.append("| bucket | prob | ask | edge_pp | action | confidence | reason | warnings |")
    lines.append("| --- | ---: | ---: | ---: | --- | --- | --- | --- |")
    for row in result.rows:
        warns = ", ".join(row.warnings) if row.warnings else ""
        lines.append(
            f"| {row.label} | "
            f"{row.probability:.3f} | "
            f"{_format_optional(row.yes_ask, '.4f')} | "
            f"{_format_optional(row.edge_yes_pp, '.2f')} | "
            f"{row.action} | "
            f"{row.confidence} | "
            f"{row.reason} | "
            f"{warns} |"
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


@paper_app.command("status")
def paper_status() -> None:
    """Print balances side-by-side for all three Phase B strategies."""
    names = [s.name for s in _default_strategies()]
    header = f"{'strategy':<12} {'balance':>10} {'open':>5} {'settled':>8} {'realized':>11}"
    typer.echo(header)
    typer.echo("-" * len(header))
    for name in names:
        state = read_state(ledger_path_for(name))
        typer.echo(
            f"{name:<12} ${state.balance_usd:>9.2f} "
            f"{len(state.open_trades):>5} {state.settled_count:>8} "
            f"${state.realized_pnl_usd:>+10.2f}"
        )

    for name in names:
        state = read_state(ledger_path_for(name))
        if not state.open_trades:
            continue
        typer.echo("")
        typer.echo(f"[{name}] open positions:")
        sub = f"  {'trade_id':<14} {'event_id':<14} {'bucket':<18} {'side':<4} {'entry':>6} {'stake':>8} {'shares':>8}"
        typer.echo(sub)
        typer.echo("  " + "-" * (len(sub) - 2))
        for trade in state.open_trades:
            entry = trade.entry_price if trade.entry_price is not None else trade.yes_ask
            typer.echo(
                f"  {trade.trade_id:<14} {trade.event_id:<14} {trade.bucket_label:<18} "
                f"{trade.side:<4} {entry:>6.2f} ${trade.stake_usd:>7.2f} {trade.shares:>8.2f}"
            )


@paper_app.command("list-london")
def paper_list_london(limit: int = typer.Option(20, help="Max events to scan.")) -> None:
    """List active London weather events from Polymarket."""
    events = fetch_weather_events(limit=limit)
    matches = [e for e in events if "london" in e.title.lower()]
    if not matches:
        typer.echo("no active London events found")
        raise typer.Exit(code=1)
    for event in matches:
        typer.echo(f"{event.event_id}\t{event.slug}\t{event.title}")


def _default_strategies() -> list:
    return [ArgmaxYesStrategy(), DistArbStrategy(), MidBandStrategy()]


def _render_run_summary(summary: RunSummary, base_dir: date | str | None = None) -> str:
    """Side-by-side strategy summary for one pipeline run."""
    lines: list[str] = []
    header = f"event: {summary.event_title} ({summary.event_id})"
    lines.append(header)
    if summary.resolved:
        winner = summary.winning_label or "(no winner)"
        lines.append(f"status:   RESOLVED winner={winner}")
    else:
        lines.append("status:   OPEN (unresolved)")
    lines.append("")
    for s in summary.strategies:
        lines.append(f"[{s.name}] {s.action}")
        for t in s.opened:
            lines.append(
                f"  + OPEN  {t.bucket_label:<14} side={t.side} "
                f"entry={t.entry_price:.2f} stake=${t.stake_usd:.2f} "
                f"shares={t.shares:.2f} edge={t.edge_pp:.2f}pp"
            )
        for t in s.settled:
            lines.append(
                f"  - SETTLE {t.bucket_label:<14} side={t.side} "
                f"stake=${t.stake_usd:.2f} shares={t.shares:.2f}"
            )
    return "\n".join(lines)


@paper_app.command("open")
def paper_open(
    event_id: str = typer.Option(..., "--event-id", help="Polymarket event id."),
    target_date: str = typer.Option(..., "--date", help="Settlement date YYYY-MM-DD."),
    city: str = typer.Option("london", "--city", help="Contract station for Open-Meteo."),
) -> None:
    """Run all strategies against one event. Settles automatically if resolved."""
    parsed_date = date.fromisoformat(target_date)
    event = fetch_event(event_id)
    station = get_station(city)
    forecast = fetch_for_station(station, parsed_date).to_forecast_values()

    summary = run_pipeline(forecast, event, _default_strategies(), dedupe=True)
    typer.echo(_render_run_summary(summary))
    typer.echo("")
    for s in summary.strategies:
        state = read_state(ledger_path_for(s.name))
        typer.echo(
            f"  {s.name:<12} balance=${state.balance_usd:>8.2f} "
            f"open={len(state.open_trades):>2} settled={state.settled_count:>3} "
            f"realized=${state.realized_pnl_usd:>+8.2f}"
        )


@paper_app.command("settle")
def paper_settle(
    event_id: str = typer.Option(..., "--event-id", help="Polymarket event id."),
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
    """
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

    strategies = _default_strategies()
    total_settled = 0
    for strat in strategies:
        strategy = strat.name
        path = ledger_path_for(strategy)
        if not path.exists():
            continue
        settled = settle_event(event_id, winning_label, path=path)
        state = read_state(path)
        total_settled += len(settled)
        typer.echo("")
        if not settled:
            typer.echo(f"[{strategy}] no open trades for this event")
        else:
            typer.echo(f"[{strategy}] settled {len(settled)} trade(s):")
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
        typer.echo("\nno open trades matched this event across any strategy")
