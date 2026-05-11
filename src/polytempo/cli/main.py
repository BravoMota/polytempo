"""CLI entry point.

Exposes the `polytempo` command. The `demo` subcommand runs the local
analysis pipeline on hardcoded fake inputs and prints a small report.
No APIs are called.
"""

from __future__ import annotations

import typer

from polytempo.analysis import AnalysisInput, AnalysisResult, analyze
from polytempo.strategy.edge import MarketPrice

app = typer.Typer()


@app.callback()
def _root() -> None:
    """PolyTempo command-line interface."""


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
