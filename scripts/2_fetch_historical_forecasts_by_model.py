#!/usr/bin/env python3
"""Bulk-fetch Single Runs raw JSON per model using the capabilities CSV.

Reads ``single_runs_model_capabilities.csv`` and, for each model with ``status=ok``:

- Steps run inits from ``RUN_TIME_START_UTC`` to ``RUN_TIME_END_UTC`` at the model's
  ``run_init_interval_hours``.
- Uses ``max(daily_non_null_at_12z, daily_non_null_at_18z)`` as ``forecast_days``.
- Saves each raw response under ``data/weather/raw/`` (same convention
  as the polytempo CLI; uses ``load_or_fetch_single_run_payload`` so existing files
  are skipped).

All settings are global vars at the top — no CLI flags. Not part of the polytempo CLI.
"""

from __future__ import annotations

import csv
import sys
from datetime import timezone as dt_timezone
from pathlib import Path

import httpx
from rich.console import Console
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from polytempo.weather.data_dir import WEATHER_DATA_DIR
from polytempo.weather.historical_forecasts import (  # noqa: E402
    DEFAULT_RAW_FORECASTS_DIR,
    DEFAULT_SINGLE_RUNS_BASE_URL,
    existing_raw_forecast_keys,
    generate_run_times_utc,
    load_or_fetch_single_run_payload,
    parse_forecast_run_time,
    raw_forecast_path,
    sanitize_filename_component,
)

# --- configuration (edit here) ---

STATION_LABEL = "EGLC"
LATITUDE = 51.5053
LONGITUDE = 0.0553
TIMEZONE = "Europe/London"

# Inclusive UTC range of model initialisation times to fetch.
RUN_TIME_START_UTC = "2026-02-25T00:00:00Z"
RUN_TIME_END_UTC = "2026-05-25T00:00:00Z"

MODELS: list[str] = [
    "ukmo_uk_deterministic_2km",
    "ukmo_global_deterministic_10km",
    "ecmwf_ifs025",
    "icon_eu",
    "gfs_seamless",
]

CAPABILITIES_CSV = WEATHER_DATA_DIR / "single_runs_model_capabilities.csv"
RAW_DIR = DEFAULT_RAW_FORECASTS_DIR
BASE_URL = DEFAULT_SINGLE_RUNS_BASE_URL

# --- implementation ---


def _load_capabilities(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        raise FileNotFoundError(f"capabilities CSV not found: {path}")
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _coerce_int(value: str) -> int | None:
    cleaned = (value or "").strip()
    if not cleaned:
        return None
    try:
        return int(float(cleaned))
    except ValueError:
        return None


def _coerce_float(value: str) -> float | None:
    cleaned = (value or "").strip()
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _model_plan(
    row: dict[str, str],
) -> tuple[float, int] | None:
    """Return ``(run_init_interval_hours, forecast_days)`` if the row is usable."""
    interval = _coerce_float(row.get("run_init_interval_hours", ""))
    if interval is None or interval <= 0:
        return None

    non_null_12 = _coerce_int(row.get("daily_non_null_at_12z", "")) or 0
    non_null_18 = _coerce_int(row.get("daily_non_null_at_18z", "")) or 0
    forecast_days = max(non_null_12, non_null_18)
    if forecast_days < 1:
        return None
    return interval, forecast_days


def _fetch_runs_for_model(
    *,
    client: httpx.Client,
    progress: Progress,
    model: str,
    run_times,
    forecast_days: int,
    console: Console,
) -> tuple[int, int, int]:
    station = sanitize_filename_component(STATION_LABEL)
    model_token = sanitize_filename_component(model)
    seen = existing_raw_forecast_keys(RAW_DIR)
    fetched = skipped = failed = 0

    task_id = progress.add_task(
        f"[cyan]{model}[/]", total=len(run_times), info="0/0/0"
    )

    for run_time in run_times:
        run_utc = run_time.astimezone(dt_timezone.utc)
        key = (station, model_token, run_utc.isoformat())
        cached_path = raw_forecast_path(RAW_DIR, STATION_LABEL, model, run_utc)

        if key in seen or cached_path.exists():
            skipped += 1
        else:
            try:
                load_or_fetch_single_run_payload(
                    LATITUDE,
                    LONGITUDE,
                    model,
                    run_utc,
                    station_id=STATION_LABEL,
                    timezone=TIMEZONE,
                    raw_dir=RAW_DIR,
                    forecast_days=forecast_days,
                    base_url=BASE_URL,
                    client=client,
                )
                fetched += 1
                seen.add(key)
            except Exception as exc:
                failed += 1
                console.log(
                    f"[red]fail[/] {model} run={run_utc.isoformat()}: {exc}"
                )

        progress.update(
            task_id,
            advance=1,
            info=f"{fetched}/{skipped}/{failed}",
        )

    return fetched, skipped, failed


def main() -> int:
    rows = _load_capabilities(CAPABILITIES_CSV)
    by_model = {row["model"]: row for row in rows if row.get("model")}

    selected = list(MODELS)
    if not selected:
        print("MODELS is empty — nothing to fetch", file=sys.stderr)
        return 1

    start = parse_forecast_run_time(RUN_TIME_START_UTC)
    end = parse_forecast_run_time(RUN_TIME_END_UTC)

    console = Console(stderr=True)
    console.rule(
        f"[bold]station={STATION_LABEL}[/]  "
        f"{start.isoformat()} → {end.isoformat()}",
    )

    total_fetched = total_skipped = total_failed = 0
    progress = Progress(
        TextColumn("[bold blue]{task.description}"),
        BarColumn(bar_width=None),
        MofNCompleteColumn(),
        TextColumn("[cyan]fetched/skipped/failed:[/] {task.fields[info]}"),
        TimeElapsedColumn(),
        TextColumn("ETA"),
        TimeRemainingColumn(),
        console=console,
        transient=False,
    )

    with httpx.Client() as client, progress:
        for index, model in enumerate(selected, start=1):
            prefix = f"[dim][{index}/{len(selected)}][/]"
            row = by_model.get(model)
            if row is None:
                console.log(f"{prefix} [yellow]SKIP[/] {model}: missing from CSV")
                continue
            if row.get("status") != "ok":
                console.log(
                    f"{prefix} [yellow]SKIP[/] {model}: status={row.get('status')}"
                )
                continue
            plan = _model_plan(row)
            if plan is None:
                console.log(
                    f"{prefix} [yellow]SKIP[/] {model}: "
                    "no run_init_interval_hours or forecast_days"
                )
                continue

            interval_hours, forecast_days = plan
            run_times = generate_run_times_utc(start, end, interval_hours)
            console.log(
                f"{prefix} [bold]{model}[/]  "
                f"runs={len(run_times)} interval={interval_hours}h "
                f"forecast_days={forecast_days}"
            )

            fetched, skipped, failed = _fetch_runs_for_model(
                client=client,
                progress=progress,
                model=model,
                run_times=run_times,
                forecast_days=forecast_days,
                console=console,
            )
            total_fetched += fetched
            total_skipped += skipped
            total_failed += failed

    console.rule(
        f"[bold]done[/]  fetched={total_fetched} "
        f"skipped={total_skipped} failed={total_failed}  raw_dir={RAW_DIR}"
    )
    return 0 if total_failed == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
