"""CLI entry point.

Exposes the `polytempo` command:

* `demo`            - run the analysis on hardcoded fake inputs (no APIs).
* `live`            - fetch Polymarket + Open-Meteo, run analysis, write a markdown report.
* `paper open`      - fetch a London event + forecast, run analysis, lock paper trades.
* `paper settle`    - settle open paper trades for an event against a winning bucket.
* `paper status`    - print the paper account balance and open positions.
* `paper list-london` - list active London weather events from Polymarket.
"""

from __future__ import annotations

from datetime import date, timedelta

import typer

try:
    import truststore

    truststore.inject_into_ssl()
except ImportError:
    pass

from polytempo.analysis import (
    AnalysisInput,
    AnalysisResult,
    analyze,
    analyze_event,
)
from polytempo.markets.polymarket import (
    PolymarketEvent,
    fetch_event,
    fetch_weather_events,
    first_parseable_weather_event,
)
from polytempo.reports.writer import RunReporter
from polytempo.paper.ledger import (
    DEFAULT_LEDGER_PATH,
    open_trades_from_analysis,
    read_state,
    settle_event,
)
from polytempo.strategy.edge import MarketPrice
from polytempo.weather.open_meteo import DailyMaxForecast, fetch_for_station
from polytempo.weather.schema import ForecastValues
from polytempo.weather.stations import STATIONS, Station, get_station

app = typer.Typer()
paper_app = typer.Typer(help="Paper trading ledger (demo account, no live orders).")
app.add_typer(paper_app, name="paper")


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
    days_ahead: int = typer.Option(
        1,
        "--days-ahead",
        min=0,
        help="Target calendar day = today + N (Open-Meteo max temp + Gamma end-date filter). Default 1 = tomorrow.",
    ),
) -> None:
    """Fetch Polymarket + Open-Meteo data, print analysis, and write a markdown report."""
    try:
        station = get_station(city)
    except KeyError:
        typer.echo(
            f"Unknown --city {city!r}. Registry keys: {', '.join(sorted(STATIONS))}",
            err=True,
        )
        raise typer.Exit(code=1) from None

    target_date = date.today() + timedelta(days=days_ahead)
    reporter = RunReporter()

    try:
        with reporter:
            reporter.section(
                "Inputs",
                "\n".join(
                    [
                        f"- city: `{city}` (station {station.icao})",
                        f"- target_date: `{target_date.isoformat()}` (days_ahead={days_ahead})",
                        f"- event mode: {'explicit (--event-id)' if event_id else f'scan (limit={limit})'}",
                    ]
                ),
            )

            if event_id:
                event = fetch_event(event_id.strip())
                if event.settlement_date is not None and event.settlement_date != target_date:
                    typer.echo(
                        f"Warning: event end date {event.settlement_date} != target day {target_date} "
                        f"(from --days-ahead). Forecast still uses {target_date}.",
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
                    raise typer.Exit(code=1)

            reporter.section("Event", _md_event(event))

            daily = fetch_for_station(station, target_date)
            forecast = daily.to_forecast_values()
            reporter.section(
                "Forecast",
                _md_forecast(station, target_date, daily, forecast),
            )

            result = analyze_event(forecast, event)
            reporter.section("Analysis result", _md_result(result))

            typer.echo(f"event: {event.title} ({event.event_id})")
            end_s = event.settlement_date.isoformat() if event.settlement_date else "unknown"
            typer.echo(f"event Gamma end date (UTC day): {end_s}")
            typer.echo(
                f"forecast: {station.city} ({station.icao}) lat={station.latitude} lon={station.longitude} "
                f"tz={station.timezone} date={target_date.isoformat()} -> {forecast.values_c}"
            )
            if event_id is None:
                typer.echo(
                    f"(Event: Gamma list filtered by --city, end date {target_date.isoformat()}, "
                    "parseable buckets; Open-Meteo uses the contract station for that same day.)\n"
                )
            else:
                typer.echo(
                    f"(Forecast uses contract station for --city on {target_date.isoformat()}; "
                    "verify --event-id matches that settlement day.)\n"
                )
            typer.echo(_render(result))
    finally:
        path = reporter.write()
        typer.echo(f"report written: {path}")


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
    lines = [
        f"- distribution: mean={result.distribution_mean_c:.2f}°C "
        f"sigma={result.distribution_sigma_c:.2f}°C",
        "",
        "| bucket | prob | ask | edge_pp | action | confidence | reason | warnings |",
        "| --- | ---: | ---: | ---: | --- | --- | --- | --- |",
    ]
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
# paper (Phase 9)
# ---------------------------------------------------------------------------


@paper_app.command("status")
def paper_status() -> None:
    """Print paper account balance and open positions."""
    state = read_state(DEFAULT_LEDGER_PATH)
    typer.echo(f"balance:   ${state.balance_usd:.2f}")
    typer.echo(f"open:      {len(state.open_trades)}")
    typer.echo(f"settled:   {state.settled_count}")
    typer.echo(f"realized:  ${state.realized_pnl_usd:.2f}")
    if not state.open_trades:
        return
    typer.echo("")
    header = f"{'trade_id':<14} {'event_id':<14} {'bucket':<18} {'ask':>5} {'edge_pp':>8} {'stake':>8} {'shares':>8}"
    typer.echo(header)
    typer.echo("-" * len(header))
    for trade in state.open_trades:
        typer.echo(
            f"{trade.trade_id:<14} {trade.event_id:<14} {trade.bucket_label:<18} "
            f"{trade.yes_ask:>5.2f} {trade.edge_pp:>8.2f} "
            f"${trade.stake_usd:>7.2f} {trade.shares:>8.2f}"
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


@paper_app.command("open")
def paper_open(
    event_id: str = typer.Option(..., "--event-id", help="Polymarket event id."),
    target_date: str = typer.Option(..., "--date", help="Settlement date YYYY-MM-DD."),
) -> None:
    """Fetch London event + forecast, run analysis, lock BUY_YES trades."""
    parsed_date = date.fromisoformat(target_date)
    event = fetch_event(event_id)
    station = get_station("london")
    forecast = fetch_for_station(station, parsed_date).to_forecast_values()
    result = analyze_event(forecast, event)
    typer.echo(_render(result))
    typer.echo("")

    opened = open_trades_from_analysis(result, event_id=event.event_id)
    if not opened:
        typer.echo("no BUY_YES trades opened")
        return
    typer.echo(f"opened {len(opened)} trade(s):")
    for trade in opened:
        typer.echo(
            f"  {trade.trade_id} {trade.bucket_label} "
            f"ask={trade.yes_ask:.2f} edge={trade.edge_pp:.2f}pp "
            f"stake=${trade.stake_usd:.2f} shares={trade.shares:.2f}"
        )
    state = read_state(DEFAULT_LEDGER_PATH)
    typer.echo(f"balance: ${state.balance_usd:.2f}")


@paper_app.command("settle")
def paper_settle(
    event_id: str = typer.Option(..., "--event-id", help="Polymarket event id."),
    winner: str = typer.Option(..., "--winner", help="Winning bucket label, e.g. \"23°C\"."),
) -> None:
    """Settle every open trade on this event against the winning bucket."""
    settled = settle_event(event_id, winner)
    if not settled:
        typer.echo("no open trades for this event")
        return
    typer.echo(f"settled {len(settled)} trade(s) against winner={winner!r}")
    for trade in settled:
        outcome = "YES" if trade.bucket_label == winner else "NO"
        typer.echo(f"  {trade.trade_id} {trade.bucket_label}  -> {outcome}")
    state = read_state(DEFAULT_LEDGER_PATH)
    typer.echo(f"balance:  ${state.balance_usd:.2f}")
    typer.echo(f"realized: ${state.realized_pnl_usd:.2f}")
