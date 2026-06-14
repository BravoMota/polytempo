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
from dataclasses import dataclass
from pathlib import Path

from polytempo.weather.data_dir import WEATHER_DATA_DIR

DEFAULT_CALIBRATION_STATS_CSV_PATH = (
    WEATHER_DATA_DIR / "statistical" / "calibration_stats.csv"
)
DEFAULT_UPDATED_CALIBRATION_STATS_CSV_PATH = (
    WEATHER_DATA_DIR / "statistical" / "calibration_stats_updated.csv"
)


@dataclass(frozen=True)
class CalibrationStatRow:
    """One ``(station_id, model, lead_hours)`` aggregated error stat."""

    station_id: str
    model: str
    lead_hours: float
    n_samples: int
    bias_c: float
    mae_c: float
    rmse_c: float
    error_std_c: float


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
                )
            )
    return rows


def select_ceiling_row(
    rows: list[CalibrationStatRow],
    station_id: str,
    model: str,
    current_lead_hours: float,
) -> CalibrationStatRow | None:
    """Return the row with the smallest ``lead_hours >= current_lead_hours``.

    Matches station + model. Returns ``None`` when no row qualifies.
    """
    candidates = [
        row
        for row in rows
        if row.station_id == station_id
        and row.model == model
        and row.lead_hours >= current_lead_hours
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

    For each available model, the ceiling row (smallest ``lead_hours >= current``)
    is looked up.     When ``init_lead_hours_by_model`` is set, only models present in that
    mapping are considered; each uses its init-based lead for ceiling lookup.
    Otherwise all ``available_models`` share ``current_lead_hours``. Models
    without a ceiling row are dropped. Among the
    remaining models, the one with the lowest valid ``error_std_c`` wins; if every
    candidate's ``error_std_c`` is missing/zero/non-finite, ``rmse_c`` is used
    as the tie-break source. Returns ``(row, sigma_source)`` or ``None`` when
    no model qualifies.
    """
    candidates: list[tuple[CalibrationStatRow, float, str]] = []
    if init_lead_hours_by_model:
        models_to_try = [
            model for model in available_models if model in init_lead_hours_by_model
        ]
    else:
        models_to_try = available_models
    for model in models_to_try:
        lead = (
            init_lead_hours_by_model[model]
            if init_lead_hours_by_model
            else current_lead_hours
        )
        row = select_ceiling_row(rows, station_id, model, lead)
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
