"""Concise markdown sections for ``polytempo live`` reports."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from polytempo.analysis import (
    MODEL_STRATEGY_WEIGHTED_HISTORICAL_UPDATED,
    AnalysisResult,
)
from polytempo.model.distribution import DistributionBuildInfo
from polytempo.weather.calibration_stats_csv import (
    CalibrationStatRow,
    WeightedModelContribution,
    build_weighted_distribution_params,
    lookup_lead_hours_for_calibration,
    read_calibration_stats_csv,
    resolve_lead_hours_anchor,
    select_best_model,
    select_ceiling_row,
    select_weighted_models,
    verified_init_lead_hours_by_model,
)
from polytempo.weather.open_meteo import (
    OpenMeteoLiveBundle,
    availability_lag_hours,
    format_run_time_utc,
)
from polytempo.weather.schema import ForecastValues
from polytempo.weather.stations import Station


@dataclass(frozen=True)
class CalibrationSelection:
    """Best-historical winner for one calibration CSV."""

    csv_path: Path
    label: str
    row: CalibrationStatRow | None
    sigma_source: str | None
    lookup_lead_hours: float | None
    predicted_tmax_c: float | None = None
    fallback_reason: str | None = None


@dataclass(frozen=True)
class WeightedCalibrationSelection:
    """WHU weighted mixture for one calibration CSV."""

    csv_path: Path
    label: str
    contributions: tuple[WeightedModelContribution, ...] | None
    mean_c: float | None
    sigma_c: float | None
    distribution_params: dict[str, object] | None
    fallback_reason: str | None = None
    excluded_models: tuple[str, ...] = ()


def _predicted_tmax_by_model(forecast: ForecastValues) -> dict[str, float]:
    if not forecast.models:
        return {}
    return {
        model: forecast.values_c[index]
        for index, model in enumerate(forecast.models)
        if index < len(forecast.values_c)
    }


def _init_lead_by_model(forecast: ForecastValues) -> dict[str, float] | None:
    return verified_init_lead_hours_by_model(forecast)


def resolve_calibration_selection(
    *,
    csv_path: Path,
    label: str,
    station_id: str,
    forecast: ForecastValues,
    wall_lead_hours: float,
) -> CalibrationSelection:
    """Pick the best-historical row from one calibration stats CSV."""
    rows = read_calibration_stats_csv(csv_path)
    if not rows:
        return CalibrationSelection(
            csv_path=csv_path,
            label=label,
            row=None,
            sigma_source=None,
            lookup_lead_hours=None,
            fallback_reason="no_calibration_csv",
        )
    if not forecast.models:
        return CalibrationSelection(
            csv_path=csv_path,
            label=label,
            row=None,
            sigma_source=None,
            lookup_lead_hours=None,
            fallback_reason="forecast_missing_model_identity",
        )

    init_leads = _init_lead_by_model(forecast)
    selection = select_best_model(
        rows,
        station_id=station_id,
        available_models=list(forecast.models),
        current_lead_hours=wall_lead_hours,
        init_lead_hours_by_model=init_leads,
    )
    if selection is None:
        return CalibrationSelection(
            csv_path=csv_path,
            label=label,
            row=None,
            sigma_source=None,
            lookup_lead_hours=None,
            fallback_reason="no_ceiling_row_for_any_live_model",
        )

    row, sigma_source = selection
    anchor = resolve_lead_hours_anchor(rows, station_id=station_id, model=row.model)
    lookup_lead = lookup_lead_hours_for_calibration(
        model=row.model,
        lead_hours_anchor=anchor,
        wall_lead_hours=wall_lead_hours,
        init_lead_hours_by_model=init_leads,
    )
    tmax_by_model = _predicted_tmax_by_model(forecast)
    return CalibrationSelection(
        csv_path=csv_path,
        label=label,
        row=row,
        sigma_source=sigma_source,
        lookup_lead_hours=lookup_lead,
        predicted_tmax_c=tmax_by_model.get(row.model),
    )


def resolve_weighted_calibration_selection(
    *,
    csv_path: Path,
    label: str,
    station_id: str,
    forecast: ForecastValues,
    wall_lead_hours: float,
) -> WeightedCalibrationSelection:
    """Build the WHU weighted mixture from one calibration stats CSV."""
    rows = read_calibration_stats_csv(csv_path)
    if not rows:
        return WeightedCalibrationSelection(
            csv_path=csv_path,
            label=label,
            contributions=None,
            mean_c=None,
            sigma_c=None,
            distribution_params={"error": "no_calibration_csv"},
            fallback_reason="no_calibration_csv",
        )
    if not forecast.models:
        return WeightedCalibrationSelection(
            csv_path=csv_path,
            label=label,
            contributions=None,
            mean_c=None,
            sigma_c=None,
            distribution_params={"error": "forecast_missing_model_identity"},
            fallback_reason="forecast_missing_model_identity",
        )

    init_leads = _init_lead_by_model(forecast)
    attempt = select_weighted_models(
        rows,
        station_id=station_id,
        available_models=list(forecast.models),
        current_lead_hours=wall_lead_hours,
        predicted_tmax_by_model=_predicted_tmax_by_model(forecast),
        init_lead_hours_by_model=init_leads,
    )
    if attempt.result is None:
        return WeightedCalibrationSelection(
            csv_path=csv_path,
            label=label,
            contributions=None,
            mean_c=None,
            sigma_c=None,
            distribution_params={
                "error": "no_eligible_models",
                "excluded_models": list(attempt.excluded_models),
            },
            fallback_reason="no_eligible_models",
            excluded_models=attempt.excluded_models,
        )

    result = attempt.result
    return WeightedCalibrationSelection(
        csv_path=csv_path,
        label=label,
        contributions=result.contributions,
        mean_c=result.mean_c,
        sigma_c=result.sigma_c,
        distribution_params=build_weighted_distribution_params(
            result,
            excluded_models=attempt.excluded_models,
        ),
    )


def format_weighted_calibration_md(selection: WeightedCalibrationSelection) -> str:
    lines = [
        f"### {selection.label}",
        f"- csv: `{selection.csv_path}`",
    ]
    if selection.contributions is None:
        reason = selection.fallback_reason or "unknown"
        lines.append(f"- fit: _failed_ (`{reason}`)")
        if selection.excluded_models:
            lines.append(f"- excluded_models: `{list(selection.excluded_models)}`")
        return "\n".join(lines)

    lines.extend(
        [
            f"- mean_c: **{selection.mean_c:.2f}**",
            f"- sigma_c: **{selection.sigma_c:.2f}**",
            "",
            "| model | weight | predicted_tmax_c | corrected_mu_c | error_std_c | lookup_lead_h |",
            "| --- | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for entry in selection.contributions:
        lines.append(
            f"| `{entry.model}` | {entry.weight:.3f} | {entry.predicted_tmax_c:.2f} | "
            f"{entry.corrected_mu_c:.2f} | {entry.error_std_c:.3f} | "
            f"{entry.lookup_lead_hours:g} |"
        )
    if selection.excluded_models:
        lines.append(f"- excluded_models: `{list(selection.excluded_models)}`")
    return "\n".join(lines)


def _format_optional(value: float | None, fmt: str) -> str:
    return format(value, fmt) if value is not None else "—"


def format_calibration_row(row: CalibrationStatRow) -> str:
    return (
        f"`{row.station_id}` / `{row.model}` / lead={row.lead_hours:g}h "
        f"n={row.n_samples} bias={row.bias_c:.3f}°C mae={row.mae_c:.3f}°C "
        f"rmse={row.rmse_c:.3f}°C std={row.error_std_c:.3f}°C"
    )


def format_calibration_selection_md(selection: CalibrationSelection) -> str:
    lines = [
        f"### {selection.label}",
        f"- csv: `{selection.csv_path}`",
    ]
    if selection.row is None:
        reason = selection.fallback_reason or "unknown"
        lines.append(f"- selected: _none_ (`{reason}`)")
        return "\n".join(lines)

    lines.extend(
        [
            f"- selected_model: `{selection.row.model}`",
            f"- open_meteo_predicted_tmax_c: **{_format_optional(selection.predicted_tmax_c, '.2f')}**",
            f"- lookup_lead_hours: {selection.lookup_lead_hours:g}h",
            f"- sigma_source: `{selection.sigma_source}`",
            f"- row: {format_calibration_row(selection.row)}",
        ]
    )
    return "\n".join(lines)


@dataclass(frozen=True)
class ModelCalibrationCeiling:
    """Ceiling calibration stat row used for one live model."""

    model: str
    anchor: str
    lookup_lead_hours: float | None
    row: CalibrationStatRow | None


def calibration_ceilings_by_model(
    *,
    csv_path: Path,
    station_id: str,
    forecast: ForecastValues,
    wall_lead_hours: float,
) -> list[ModelCalibrationCeiling]:
    """Per live model: anchor, lookup lead, and ceiling CSV row (if any)."""
    rows = read_calibration_stats_csv(csv_path)
    if not rows or not forecast.models:
        return []

    init_leads = _init_lead_by_model(forecast)
    out: list[ModelCalibrationCeiling] = []
    for model in forecast.models:
        anchor = resolve_lead_hours_anchor(rows, station_id=station_id, model=model)
        lookup = lookup_lead_hours_for_calibration(
            model=model,
            lead_hours_anchor=anchor,
            wall_lead_hours=wall_lead_hours,
            init_lead_hours_by_model=init_leads,
        )
        if lookup is None:
            out.append(
                ModelCalibrationCeiling(
                    model=model,
                    anchor=anchor,
                    lookup_lead_hours=None,
                    row=None,
                )
            )
            continue
        ceiling = select_ceiling_row(
            rows,
            station_id,
            model,
            lookup,
            lead_hours_anchor=anchor,
        )
        out.append(
            ModelCalibrationCeiling(
                model=model,
                anchor=anchor,
                lookup_lead_hours=lookup,
                row=ceiling,
            )
        )
    return out


def format_calibration_per_model_compact(
    *,
    csv_path: Path,
    station_id: str,
    forecast: ForecastValues,
    wall_lead_hours: float,
) -> str:
    """One line per model: ceiling ``lead_hours`` and ``error_std_c``."""
    ceilings = calibration_ceilings_by_model(
        csv_path=csv_path,
        station_id=station_id,
        forecast=forecast,
        wall_lead_hours=wall_lead_hours,
    )
    if not ceilings:
        return "(no per-model calibration rows)"

    lines: list[str] = []
    for entry in ceilings:
        if entry.row is None:
            lines.append(f"  {entry.model}: —")
        else:
            lines.append(
                f"  {entry.model}: lead_hours={entry.row.lead_hours:g} "
                f"error_std_c={entry.row.error_std_c:.3f}"
            )
    return "\n".join(lines)


def format_calibration_per_model_md(
    *,
    csv_path: Path,
    station_id: str,
    forecast: ForecastValues,
    wall_lead_hours: float,
) -> str:
    """Compact ceiling-row lookup per live model (debug aid)."""
    ceilings = calibration_ceilings_by_model(
        csv_path=csv_path,
        station_id=station_id,
        forecast=forecast,
        wall_lead_hours=wall_lead_hours,
    )
    if not ceilings:
        return "_no per-model rows (missing csv or model list)_"

    tmax_by_model = _predicted_tmax_by_model(forecast)
    lines = [
        "| model | anchor | predicted_tmax_c | lookup_lead_h | ceiling_lead_h | error_std_c |",
        "| --- | --- | ---: | ---: | ---: | ---: |",
    ]
    for entry in ceilings:
        tmax_s = _format_optional(tmax_by_model.get(entry.model), ".2f")
        if entry.lookup_lead_hours is None:
            lines.append(f"| `{entry.model}` | `{entry.anchor}` | {tmax_s} | excluded | — | — |")
            continue
        if entry.row is None:
            lines.append(
                f"| `{entry.model}` | `{entry.anchor}` | {tmax_s} | "
                f"{entry.lookup_lead_hours:g} | — | — |"
            )
        else:
            lines.append(
                f"| `{entry.model}` | `{entry.anchor}` | {tmax_s} | "
                f"{entry.lookup_lead_hours:g} | {entry.row.lead_hours:g} | "
                f"{entry.row.error_std_c:.3f} |"
            )
    return "\n".join(lines)


def format_open_meteo_md(
    *,
    station: Station,
    target_date: date,
    bundle: OpenMeteoLiveBundle,
    forecast: ForecastValues,
) -> str:
    fetched = format_run_time_utc(bundle.fetched_at_utc)
    lines = [
        f"- fetched_at_utc: `{fetched}`",
        f"- station: {station.city} (`{station.icao}`)",
        f"- coordinates: lat={station.latitude} lon={station.longitude} tz={station.timezone}",
        f"- target_date: `{target_date.isoformat()}`",
        f"- meta_staleness_detected: `{bundle.meta_staleness_detected}`",
        "- predicted daily max (`temperature_2m_max`) from Open-Meteo Forecast API for target date:",
        "",
        "| model | predicted_tmax_c | run_init_utc | init_lead_h | wall_lead_h | meta |",
        "| --- | ---: | --- | ---: | ---: | --- |",
    ]

    models = forecast.models or []
    values = forecast.values_c
    for index, model in enumerate(models):
        tmax = values[index] if index < len(values) else None
        key = (model, target_date)
        init_lead = bundle.init_lead_hours.get(key)
        wall_lead = bundle.wall_clock_lead_hours.get(key)
        meta = bundle.meta_by_model.get(model)
        if meta is None:
            run_init = "—"
            meta_note = "missing"
        else:
            run_init = format_run_time_utc(meta.run_init_utc)
            lag = availability_lag_hours(meta)
            meta_note = f"ok (avail_lag={lag:.1f}h)"
        lines.append(
            f"| `{model}` | {_format_optional(tmax, '.2f')} | {run_init} | "
            f"{_format_optional(init_lead, '.1f')} | {_format_optional(wall_lead, '.1f')} | {meta_note} |"
        )

    missing_meta = [m for m in models if m not in bundle.meta_by_model]
    if missing_meta:
        lines.extend(
            [
                "",
                f"- models without rolling meta (excluded from best_historical): "
                f"{', '.join(f'`{m}`' for m in missing_meta)}",
            ]
        )

    lines.extend(["", "### Rolling metadata", ""])
    if not bundle.meta_by_model:
        lines.append("_no meta.json fetched_")
    else:
        lines.extend(
            [
                "| model | run_init | run_available | run_modified | update_interval_s | data_end |",
                "| --- | --- | --- | --- | ---: | --- |",
            ]
        )
        for model, meta in sorted(bundle.meta_by_model.items()):
            data_end = (
                format_run_time_utc(meta.data_end_utc)
                if meta.data_end_utc is not None
                else "—"
            )
            lines.append(
                f"| `{model}` | {format_run_time_utc(meta.run_init_utc)} | "
                f"{format_run_time_utc(meta.run_available_utc)} | "
                f"{format_run_time_utc(meta.run_modified_utc)} | "
                f"{meta.update_interval_seconds} | {data_end} |"
            )

    return "\n".join(lines)


def format_lead_hours_md(*, wall_lead_hours: float, run_at: datetime) -> str:
    return "\n".join(
        [
            f"- run_at_utc: `{format_run_time_utc(run_at)}`",
            f"- wall_clock_lead_hours (to end of target day): **{wall_lead_hours:.1f}h**",
            "- init_lead_hours: per-model from rolling `run_init_utc` (see Open-Meteo table)",
        ]
    )


def format_distribution_md(
    info: DistributionBuildInfo,
    *,
    model_strategy: str,
    result: AnalysisResult,
    forecast: ForecastValues | None = None,
) -> str:
    lines = [
        f"- model_strategy: `{model_strategy}`",
    ]
    if (
        model_strategy == MODEL_STRATEGY_WEIGHTED_HISTORICAL_UPDATED
        and result.fallback_reason is not None
    ):
        lines.append(f"- error: `{result.fallback_reason}`")
        if result.distribution_params is not None:
            lines.append(f"- distribution_params: `{result.distribution_params}`")
        return "\n".join(lines)

    if result.selected_model is not None:
        lines.append(f"- selected_model: `{result.selected_model}`")
        if forecast is not None:
            tmax = _predicted_tmax_by_model(forecast).get(result.selected_model)
            if tmax is not None:
                lines.append(
                    f"- open_meteo_predicted_tmax_c: **{tmax:.2f}** "
                    f"(raw `{result.selected_model}` before bias correction)"
                )
    if result.calibration_sigma_source is not None:
        lines.append(f"- sigma_source: `{result.calibration_sigma_source}`")
    if result.fallback_reason is not None:
        lines.append(f"- fallback: `{result.fallback_reason}`")
    if result.distribution_params is not None:
        params = result.distribution_params
        lines.extend(
            [
                f"- precision_exponent: `{params.get('precision_exponent')}`",
                f"- within_variance: `{params.get('within_variance')}`",
                f"- between_variance: `{params.get('between_variance')}`",
            ]
        )
    if result.weighted_contributions:
        lines.extend(
            [
                "",
                "| model | weight | corrected_mu_c | error_std_c |",
                "| --- | ---: | ---: | ---: |",
            ]
        )
        for entry in result.weighted_contributions:
            lines.append(
                f"| `{entry.model}` | {entry.weight:.3f} | "
                f"{entry.corrected_mu_c:.2f} | {entry.error_std_c:.3f} |"
            )
    lines.extend(
        [
            f"- method: `{info.method}`",
            f"- mean_c: **{info.mean_c:.2f}**",
            f"- sigma_c: **{info.sigma_c:.2f}**",
        ]
    )
    if info.method not in ("calibrated_single_model", "weighted_calibrated_mixture_p2.0"):
        lines.append(f"- values_used_c: `{info.values_used_c}`")
    lines.extend(["", _format_bucket_edges_table(result)])
    return "\n".join(lines)


def _yes_edge(probability: float, yes_ask: float | None) -> float | None:
    if yes_ask is None:
        return None
    return probability - yes_ask


def _format_bucket_edges_table(result: AnalysisResult) -> str:
    lines = [
        "| bucket | yes_ask | dist_prob | edge |",
        "| --- | ---: | ---: | ---: |",
    ]
    for row in result.rows:
        edge = _yes_edge(row.probability, row.yes_ask)
        lines.append(
            f"| {row.label} | {_format_optional(row.yes_ask, '.4f')} | "
            f"{row.probability:.4f} | {_format_optional(edge, '+.4f')} |"
        )
    return "\n".join(lines)


def format_strategy_analysis_md(result: AnalysisResult, *, trade_strategy: str) -> str:
    lines = [
        f"- trade_strategy: `{trade_strategy}`",
        f"- model_strategy: `{result.model_strategy}`",
        f"- distribution: mean={result.distribution_mean_c:.2f}°C "
        f"sigma={result.distribution_sigma_c:.2f}°C",
        "",
        "| bucket | prob | ask | edge_pp | action | reason |",
        "| --- | ---: | ---: | ---: | --- | --- |",
    ]
    for row in result.rows:
        lines.append(
            f"| {row.label} | {row.probability:.3f} | "
            f"{_format_optional(row.yes_ask, '.4f')} | "
            f"{_format_optional(row.edge_yes_pp, '.2f')} | "
            f"{row.action} | {row.reason} |"
        )
    buys = [row for row in result.rows if row.action.startswith("BUY")]
    if buys:
        pick = max(buys, key=lambda r: r.stake_usd or 0.0)
        stake = f"${pick.stake_usd:.2f}" if pick.stake_usd is not None else "—"
        lines.extend(["", f"**Decision:** {pick.action} `{pick.label}` stake={stake}"])
    else:
        lines.extend(["", "**Decision:** SKIP"])
    return "\n".join(lines)
