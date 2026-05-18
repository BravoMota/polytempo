"""CLI entry point.

Exposes the `polytempo` command:

* `demo`            - run the analysis on hardcoded fake inputs (no APIs).
* `paper open`      - fetch a London event + forecast, run analysis, lock paper trades.
* `paper settle`    - settle open paper trades for an event against a winning bucket.
* `paper status`    - print the paper account balance and open positions.
* `paper list-london` - list active London weather events from Polymarket.
"""

from __future__ import annotations

from datetime import date

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
from polytempo.markets.polymarket import fetch_event, fetch_weather_events
from polytempo.paper.ledger import (
    DEFAULT_LEDGER_PATH,
    open_trades_from_analysis,
    read_state,
    settle_event,
)
from polytempo.strategy.edge import MarketPrice
from polytempo.weather.open_meteo import fetch_for_station
from polytempo.weather.stations import get_station

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
