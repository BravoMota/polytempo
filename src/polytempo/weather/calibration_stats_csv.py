"""Reader and selectors for the offline per-lead calibration stats CSV.

The CSV is produced by ``scripts/6_compute_calibration_errors.py`` and lives at
``data/weather/statistical/calibration_stats.csv``. Each row holds aggregated
forecast-error metrics for one ``(station_id, model, lead_hours)`` group.

This module is intentionally read-only and side-effect free. It exposes:

- :class:`CalibrationStatRow` — one parsed row.
- :func:`read_calibration_stats_csv` — load + validate.
- :func:`select_ceiling_row` — pick the smallest ``lead_hours >= current`` per model.
- :func:`select_best_model` — pick the model with the lowest valid uncertainty
  among a set of available live models.

See [docs/calibration-data.md](docs/calibration-data.md) for the upstream
pipeline.
"""

from __future__ import annotations

import csv
import math
import shutil
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone as dt_timezone
from pathlib import Path

from polytempo.weather.data_dir import WEATHER_DATA_DIR
from polytempo.weather.schema import ForecastValues

LEAD_HOURS_ANCHOR_RUN_INIT = "run_init"
LEAD_HOURS_ANCHOR_SCRAPED_AT = "scraped_at"

DEFAULT_CALIBRATION_STATS_CSV_PATH = (
    WEATHER_DATA_DIR / "statistical" / "calibration_stats.csv"
)
DEFAULT_UPDATED_CALIBRATION_STATS_CSV_PATH = (
    WEATHER_DATA_DIR / "statistical" / "calibration_stats_updated.csv"
)
DEFAULT_WU_CALIBRATION_STATS_CSV_PATH = (
    WEATHER_DATA_DIR / "statistical" / "calibration_stats_wu.csv"
)
DEFAULT_COMBINED_CALIBRATION_STATS_CSV_PATH = (
    WEATHER_DATA_DIR / "statistical" / "calibration_stats_combined.csv"
)

CALIBRATION_STAT_COLUMNS = (
    "station_id",
    "model",
    "lead_hours",
    "lead_hours_anchor",
    "n_samples",
    "bias_c",
    "mae_c",
    "rmse_c",
    "error_std_c",
)


@dataclass(frozen=True)
class CalibrationStatRow:
    """One ``(station_id, model, lead_hours, lead_hours_anchor)`` aggregated error stat."""

    station_id: str
    model: str
    lead_hours: float
    n_samples: int
    bias_c: float
    mae_c: float
    rmse_c: float
    error_std_c: float
    lead_hours_anchor: str | None = None


def calibration_stat_row_to_dict(row: CalibrationStatRow) -> dict[str, object]:
    """Serialize one calibration CSV row for JSON audit metadata."""
    out: dict[str, object] = {
        "station_id": row.station_id,
        "model": row.model,
        "lead_hours": row.lead_hours,
        "n_samples": row.n_samples,
        "bias_c": row.bias_c,
        "mae_c": row.mae_c,
        "rmse_c": row.rmse_c,
        "error_std_c": row.error_std_c,
    }
    if row.lead_hours_anchor is not None:
        out["lead_hours_anchor"] = row.lead_hours_anchor
    return out


def _parse_float(value: object) -> float | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = float(text)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed):
        return None
    return parsed


def _parse_int(value: object) -> int | None:
    text = str(value).strip() if value is not None else ""
    if not text:
        return None
    try:
        return int(text)
    except (TypeError, ValueError):
        return None


def read_calibration_stats_csv(path: Path) -> list[CalibrationStatRow]:
    """Read the calibration stats CSV.

    Rows with missing/non-finite numerics or ``n_samples <= 0`` are skipped so
    callers can assume every returned row is usable.
    """
    if not path.exists():
        return []

    rows: list[CalibrationStatRow] = []
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        for raw in reader:
            station_id = (raw.get("station_id") or "").strip()
            model = (raw.get("model") or "").strip()
            lead_hours = _parse_float(raw.get("lead_hours"))
            n_samples = _parse_int(raw.get("n_samples"))
            bias_c = _parse_float(raw.get("bias_c"))
            mae_c = _parse_float(raw.get("mae_c"))
            rmse_c = _parse_float(raw.get("rmse_c"))
            error_std_c = _parse_float(raw.get("error_std_c"))
            anchor_raw = (raw.get("lead_hours_anchor") or "").strip()
            lead_hours_anchor = anchor_raw or None

            if (
                not station_id
                or not model
                or lead_hours is None
                or n_samples is None
                or n_samples <= 0
                or bias_c is None
                or mae_c is None
                or rmse_c is None
                or error_std_c is None
            ):
                continue

            rows.append(
                CalibrationStatRow(
                    station_id=station_id,
                    model=model,
                    lead_hours=lead_hours,
                    n_samples=n_samples,
                    bias_c=bias_c,
                    mae_c=mae_c,
                    rmse_c=rmse_c,
                    error_std_c=error_std_c,
                    lead_hours_anchor=lead_hours_anchor,
                )
            )
    return rows


def _format_lead_hours(value: float) -> str:
    return f"{value:g}"


def archive_calibration_stats_csv_before_write(path: Path) -> Path | None:
    """If path exists, copy to parent/historic/<stem>_<UTC-timestamp>.csv."""
    if not path.is_file():
        return None
    historic_dir = path.parent / "historic"
    historic_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(dt_timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    archive_path = historic_dir / f"{path.stem}_{timestamp}{path.suffix}"
    shutil.copy2(path, archive_path)
    return archive_path


def write_calibration_stats_csv(rows: list[CalibrationStatRow], path: Path) -> None:
    """Write per-(station, model, lead_hours) aggregated metrics."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CALIBRATION_STAT_COLUMNS)
        writer.writeheader()
        for row in rows:
            anchor = row.lead_hours_anchor or ""
            writer.writerow(
                {
                    "station_id": row.station_id,
                    "model": row.model,
                    "lead_hours": _format_lead_hours(row.lead_hours),
                    "lead_hours_anchor": anchor,
                    "n_samples": row.n_samples,
                    "bias_c": row.bias_c,
                    "mae_c": row.mae_c,
                    "rmse_c": row.rmse_c,
                    "error_std_c": row.error_std_c,
                }
            )


def effective_lead_hours_anchor(row: CalibrationStatRow) -> str:
    """Resolve anchor for one CSV row (explicit column or legacy default)."""
    if row.lead_hours_anchor:
        return row.lead_hours_anchor
    if row.model == "wunderground":
        return LEAD_HOURS_ANCHOR_SCRAPED_AT
    return LEAD_HOURS_ANCHOR_RUN_INIT


def resolve_lead_hours_anchor(
    rows: list[CalibrationStatRow],
    *,
    station_id: str,
    model: str,
) -> str:
    """Anchor used for live ceiling lookup for one model at one station."""
    anchors = {
        effective_lead_hours_anchor(row)
        for row in rows
        if row.station_id == station_id and row.model == model
    }
    if len(anchors) == 1:
        return next(iter(anchors))
    if LEAD_HOURS_ANCHOR_SCRAPED_AT in anchors:
        return LEAD_HOURS_ANCHOR_SCRAPED_AT
    if LEAD_HOURS_ANCHOR_RUN_INIT in anchors:
        return LEAD_HOURS_ANCHOR_RUN_INIT
    if model == "wunderground":
        return LEAD_HOURS_ANCHOR_SCRAPED_AT
    return LEAD_HOURS_ANCHOR_RUN_INIT


def lookup_lead_hours_for_calibration(
    *,
    model: str,
    lead_hours_anchor: str,
    wall_lead_hours: float,
    init_lead_hours_by_model: dict[str, float] | None,
) -> float | None:
    """Map live context to the lead bucket used for ceiling-row lookup."""
    if lead_hours_anchor == LEAD_HOURS_ANCHOR_SCRAPED_AT:
        return wall_lead_hours
    if init_lead_hours_by_model is not None:
        return init_lead_hours_by_model.get(model)
    # Legacy CSV rows without init meta: run_init stats keyed on wall clock.
    return wall_lead_hours


def select_ceiling_row(
    rows: list[CalibrationStatRow],
    station_id: str,
    model: str,
    current_lead_hours: float,
    *,
    lead_hours_anchor: str | None = None,
) -> CalibrationStatRow | None:
    """Return the row with the smallest ``lead_hours >= current_lead_hours``.

    Matches station + model (+ anchor when given). Returns ``None`` when no row qualifies.
    """
    candidates = [
        row
        for row in rows
        if row.station_id == station_id
        and row.model == model
        and row.lead_hours >= current_lead_hours
        and (
            lead_hours_anchor is None
            or effective_lead_hours_anchor(row) == lead_hours_anchor
        )
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda row: row.lead_hours)


def _sigma_for_calibration(row: CalibrationStatRow) -> tuple[float, str] | None:
    """Return ``(sigma, source_label)`` preferring ``error_std_c``.

    Falls back to ``rmse_c`` when ``error_std_c`` is missing/zero/non-finite.
    Returns ``None`` if neither is usable.
    """
    if math.isfinite(row.error_std_c) and row.error_std_c > 0:
        return row.error_std_c, "error_std_c"
    if math.isfinite(row.rmse_c) and row.rmse_c > 0:
        return row.rmse_c, "rmse_c"
    return None


def verified_init_lead_hours_by_model(forecast: ForecastValues) -> dict[str, float] | None:
    """Init lead per model eligible for ``best_historical`` calibration lookup.

    Models without non-empty ``model_run_init_utc`` are omitted (no rolling meta).
    When ``model_run_init_utc`` is unset, all paired init leads are returned (legacy).
    """
    if forecast.models is None or forecast.init_lead_hours is None:
        return None
    if len(forecast.init_lead_hours) != len(forecast.models):
        return None
    if forecast.model_run_init_utc is not None:
        if len(forecast.model_run_init_utc) != len(forecast.models):
            return None
        return {
            model: lead
            for model, lead, run_init in zip(
                forecast.models,
                forecast.init_lead_hours,
                forecast.model_run_init_utc,
                strict=True,
            )
            if run_init.strip()
        }
    return dict(zip(forecast.models, forecast.init_lead_hours, strict=True))


def select_best_model(
    rows: list[CalibrationStatRow],
    station_id: str,
    available_models: list[str],
    current_lead_hours: float,
    *,
    init_lead_hours_by_model: dict[str, float] | None = None,
) -> tuple[CalibrationStatRow, str] | None:
    """Pick the model with the lowest valid sigma at its ceiling lead row.

    Each model's ceiling lookup lead depends on ``lead_hours_anchor`` in the CSV:
    ``scraped_at`` rows use wall-clock lead; ``run_init`` rows use init/run-time
    lead when ``init_lead_hours_by_model`` is available (legacy rows without init
    meta fall back to wall clock). Models without a ceiling row are dropped.
    """
    candidates: list[tuple[CalibrationStatRow, float, str]] = []
    for model in available_models:
        anchor = resolve_lead_hours_anchor(rows, station_id=station_id, model=model)
        lead = lookup_lead_hours_for_calibration(
            model=model,
            lead_hours_anchor=anchor,
            wall_lead_hours=current_lead_hours,
            init_lead_hours_by_model=init_lead_hours_by_model,
        )
        if lead is None:
            continue
        row = select_ceiling_row(
            rows,
            station_id,
            model,
            lead,
            lead_hours_anchor=anchor,
        )
        if row is None:
            continue
        sigma_info = _sigma_for_calibration(row)
        if sigma_info is None:
            continue
        sigma, source = sigma_info
        candidates.append((row, sigma, source))

    if not candidates:
        return None

    winner = min(candidates, key=lambda entry: entry[1])
    return winner[0], winner[2]
