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
import logging
import math
import shutil
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone as dt_timezone
from pathlib import Path

from polytempo.weather.data_dir import WEATHER_DATA_DIR
from polytempo.weather.schema import ForecastValues

logger = logging.getLogger(__name__)

WEIGHTED_PRECISION_EXPONENT = 2.0
WEIGHTED_DISAGREEMENT_WEIGHT = 0.2
WEIGHTED_CALIBRATION_METHOD = "weighted_calibrated_mixture_p2.0"

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


@dataclass(frozen=True)
class WeightedModelContribution:
    """One model's inputs to the WHU precision-weighted mixture."""

    model: str
    row: CalibrationStatRow
    error_std_c: float
    corrected_mu_c: float
    weight: float
    predicted_tmax_c: float
    lookup_lead_hours: float


@dataclass(frozen=True)
class WeightedSelectionResult:
    """Blended WHU distribution parameters and per-model audit trail."""

    contributions: tuple[WeightedModelContribution, ...]
    mean_c: float
    sigma_c: float
    within_variance: float
    between_variance: float
    sigma_squared: float


@dataclass(frozen=True)
class WeightedSelectionAttempt:
    """Outcome of ``select_weighted_models`` including exclusions."""

    result: WeightedSelectionResult | None
    excluded_models: tuple[str, ...]


def weighted_contribution_to_dict(
    contribution: WeightedModelContribution,
) -> dict[str, object]:
    """Serialize one WHU contributor for JSON audit metadata."""
    return {
        "model": contribution.model,
        "weight": contribution.weight,
        "predicted_tmax_c": contribution.predicted_tmax_c,
        "bias_c": contribution.row.bias_c,
        "corrected_mu_c": contribution.corrected_mu_c,
        "error_std_c": contribution.error_std_c,
        "lookup_lead_hours": contribution.lookup_lead_hours,
        "lead_hours_anchor": effective_lead_hours_anchor(contribution.row),
        "calibration_row": calibration_stat_row_to_dict(contribution.row),
    }


def build_weighted_distribution_params(
    result: WeightedSelectionResult,
    *,
    excluded_models: tuple[str, ...] = (),
) -> dict[str, object]:
    """Audit payload for a successful WHU fit."""
    return {
        "method": WEIGHTED_CALIBRATION_METHOD,
        "precision_exponent": WEIGHTED_PRECISION_EXPONENT,
        "disagreement_weight": WEIGHTED_DISAGREEMENT_WEIGHT,
        "mean_c": result.mean_c,
        "sigma_c": result.sigma_c,
        "within_variance": result.within_variance,
        "between_variance": result.between_variance,
        "sigma_squared": result.sigma_squared,
        "contributions": [
            weighted_contribution_to_dict(c) for c in result.contributions
        ],
        "excluded_models": list(excluded_models),
    }


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


def _error_std_for_weighted(row: CalibrationStatRow) -> float | None:
    """WHU sigma source: ``error_std_c`` only (no ``rmse_c`` fallback)."""
    if math.isfinite(row.error_std_c) and row.error_std_c > 0:
        return row.error_std_c
    return None


def select_weighted_models(
    rows: list[CalibrationStatRow],
    station_id: str,
    available_models: list[str],
    current_lead_hours: float,
    predicted_tmax_by_model: dict[str, float],
    *,
    init_lead_hours_by_model: dict[str, float] | None = None,
) -> WeightedSelectionAttempt:
    """Build a precision-weighted mixture over eligible calibrated models.

    Weights use ``(1 / error_std_c²) ** WEIGHTED_PRECISION_EXPONENT``. Sigma uses
    mixture variance: within (historical error) + between (model disagreement).
    """
    excluded: list[str] = []
    pending: list[tuple[str, CalibrationStatRow, float, float, float]] = []

    for model in available_models:
        if model not in predicted_tmax_by_model:
            excluded.append(f"{model}:missing_live_prediction")
            continue
        anchor = resolve_lead_hours_anchor(rows, station_id=station_id, model=model)
        lead = lookup_lead_hours_for_calibration(
            model=model,
            lead_hours_anchor=anchor,
            wall_lead_hours=current_lead_hours,
            init_lead_hours_by_model=init_lead_hours_by_model,
        )
        if lead is None:
            excluded.append(f"{model}:no_lookup_lead_hours")
            continue
        row = select_ceiling_row(
            rows,
            station_id,
            model,
            lead,
            lead_hours_anchor=anchor,
        )
        if row is None:
            excluded.append(f"{model}:no_ceiling_row")
            continue
        error_std = _error_std_for_weighted(row)
        if error_std is None:
            logger.warning(
                "whu: excluding %s: invalid error_std_c (station=%s lead=%s)",
                model,
                station_id,
                current_lead_hours,
            )
            excluded.append(f"{model}:invalid_error_std_c")
            continue
        predicted = predicted_tmax_by_model[model]
        corrected_mu = predicted - row.bias_c
        pending.append((model, row, error_std, corrected_mu, lead))

    if not pending:
        logger.error(
            "whu: no eligible models for station=%s lead=%s (excluded=%s)",
            station_id,
            current_lead_hours,
            excluded,
        )
        return WeightedSelectionAttempt(result=None, excluded_models=tuple(excluded))

    raw_weights = [
        (1.0 / (error_std**2)) ** WEIGHTED_PRECISION_EXPONENT
        for _, _, error_std, _, _ in pending
    ]
    weight_sum = sum(raw_weights)
    normalized = [raw / weight_sum for raw in raw_weights]

    mean_c = sum(w * corrected for w, (_, _, _, corrected, _) in zip(normalized, pending, strict=True))
    within = sum(
        w * (error_std**2)
        for w, (_, _, error_std, _, _) in zip(normalized, pending, strict=True)
    )
    between = sum(
        w * (corrected - mean_c) ** 2
        for w, (_, _, _, corrected, _) in zip(normalized, pending, strict=True)
    )
    sigma_squared = within + WEIGHTED_DISAGREEMENT_WEIGHT * between
    sigma_c = math.sqrt(sigma_squared)

    contributions = tuple(
        WeightedModelContribution(
            model=model,
            row=row,
            error_std_c=error_std,
            corrected_mu_c=corrected,
            weight=weight,
            predicted_tmax_c=predicted_tmax_by_model[model],
            lookup_lead_hours=lookup_lead,
        )
        for (model, row, error_std, corrected, lookup_lead), weight in zip(
            pending,
            normalized,
            strict=True,
        )
    )
    result = WeightedSelectionResult(
        contributions=contributions,
        mean_c=mean_c,
        sigma_c=sigma_c,
        within_variance=within,
        between_variance=between,
        sigma_squared=sigma_squared,
    )
    return WeightedSelectionAttempt(result=result, excluded_models=tuple(excluded))
