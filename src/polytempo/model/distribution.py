"""Probability distribution builder.

Turns forecast temperature values into normal-model probabilities for parsed buckets.
Intervals match Phase 1 half-open semantics: [lower, upper) where bounds exist.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass

from polytempo.markets.buckets import TemperatureBucket


@dataclass(frozen=True)
class ForecastDistribution:
    """Normal summary of raw forecast values in Celsius."""

    mean_c: float
    sigma_c: float
    source_values_c: list[float]


@dataclass(frozen=True)
class DistributionBuildInfo:
    """Inputs and outputs recorded when building a forecast distribution."""

    values_used_c: list[float]
    default_sigma_c: float
    lead_hours: float | None
    lead_hours_sigma_floor_c: float | None
    ensemble_stdev_c: float | None
    mean_c: float
    sigma_c: float
    method: str


@dataclass(frozen=True)
class BucketProbability:
    """Probability mass for one temperature bucket under the forecast model."""

    label: str
    probability: float
    lower_c: float | None
    upper_c: float | None


def lead_time_sigma_floor(lead_hours: float) -> float:
    """Minimum sigma (°C) from forecast lead time before model disagreement is added."""
    if lead_hours < 0:
        raise ValueError("lead_hours must be non-negative")
    if lead_hours < 12:
        return 0.5
    if lead_hours < 24:
        return 0.8
    if lead_hours < 48:
        return 1.2
    if lead_hours < 72:
        return 1.6
    return 2.0


def _combine_sigma(base_sigma_c: float, model_disagreement: float) -> float:
    """Quadrature combination of lead-time floor and ensemble spread."""
    return math.sqrt(base_sigma_c**2 + model_disagreement**2)


def build_distribution(
    values_c: list[float],
    default_sigma_c: float = 1.0,
    lead_hours: float | None = None,
) -> tuple[ForecastDistribution, DistributionBuildInfo]:
    """Build N(mean, sigma) parameters from one or more Celsius forecast samples."""
    if default_sigma_c <= 0:
        raise ValueError("default_sigma_c must be positive")
    if not values_c:
        raise ValueError("values_c must not be empty")

    source = list(values_c)

    if lead_hours is not None:
        base_sigma = lead_time_sigma_floor(lead_hours)
        if len(source) == 1:
            dist = ForecastDistribution(
                mean_c=source[0],
                sigma_c=base_sigma,
                source_values_c=source,
            )
            info = DistributionBuildInfo(
                values_used_c=source,
                default_sigma_c=default_sigma_c,
                lead_hours=lead_hours,
                lead_hours_sigma_floor_c=base_sigma,
                ensemble_stdev_c=None,
                mean_c=dist.mean_c,
                sigma_c=dist.sigma_c,
                method="lead_time_single_floor",
            )
            return dist, info
        mean_c = statistics.mean(source)
        disagreement = statistics.stdev(source)
        sigma_c = _combine_sigma(base_sigma, disagreement)
        dist = ForecastDistribution(mean_c=mean_c, sigma_c=sigma_c, source_values_c=source)
        info = DistributionBuildInfo(
            values_used_c=source,
            default_sigma_c=default_sigma_c,
            lead_hours=lead_hours,
            lead_hours_sigma_floor_c=base_sigma,
            ensemble_stdev_c=disagreement,
            mean_c=dist.mean_c,
            sigma_c=dist.sigma_c,
            method="lead_time_multi_quadrature",
        )
        return dist, info

    if len(source) == 1:
        dist = ForecastDistribution(
            mean_c=source[0],
            sigma_c=default_sigma_c,
            source_values_c=source,
        )
        info = DistributionBuildInfo(
            values_used_c=source,
            default_sigma_c=default_sigma_c,
            lead_hours=None,
            lead_hours_sigma_floor_c=None,
            ensemble_stdev_c=None,
            mean_c=dist.mean_c,
            sigma_c=dist.sigma_c,
            method="legacy_single_default_sigma",
        )
        return dist, info

    mean_c = statistics.mean(source)
    sigma_c = statistics.stdev(source)
    if sigma_c == 0:
        dist = ForecastDistribution(
            mean_c=mean_c,
            sigma_c=default_sigma_c,
            source_values_c=source,
        )
        info = DistributionBuildInfo(
            values_used_c=source,
            default_sigma_c=default_sigma_c,
            lead_hours=None,
            lead_hours_sigma_floor_c=None,
            ensemble_stdev_c=0.0,
            mean_c=dist.mean_c,
            sigma_c=dist.sigma_c,
            method="legacy_multi_zero_stdev_uses_default_sigma",
        )
        return dist, info

    dist = ForecastDistribution(mean_c=mean_c, sigma_c=sigma_c, source_values_c=source)
    info = DistributionBuildInfo(
        values_used_c=source,
        default_sigma_c=default_sigma_c,
        lead_hours=None,
        lead_hours_sigma_floor_c=None,
        ensemble_stdev_c=sigma_c,
        mean_c=dist.mean_c,
        sigma_c=dist.sigma_c,
        method="legacy_multi_ensemble_stdev",
    )
    return dist, info


def build_calibrated_distribution(
    mu: float,
    sigma: float,
    *,
    source_values_c: list[float] | None = None,
    method: str = "calibrated_single_model",
) -> tuple[ForecastDistribution, DistributionBuildInfo]:
    """Build a normal distribution from a pre-chosen ``(mu, sigma)`` pair.

    Used by the ``best_historical`` live strategy: the selected model's
    bias-corrected prediction is the mean, and its empirical error sigma
    (``error_std_c`` or ``rmse_c``) is the spread. Lead-time floors and
    ensemble stdev are intentionally bypassed since ``sigma`` already reflects
    empirical forecast error.
    """
    if not math.isfinite(mu):
        raise ValueError("mu must be finite")
    if not math.isfinite(sigma) or sigma <= 0:
        raise ValueError("sigma must be positive and finite")

    values = list(source_values_c) if source_values_c is not None else [mu]
    dist = ForecastDistribution(mean_c=mu, sigma_c=sigma, source_values_c=values)
    info = DistributionBuildInfo(
        values_used_c=values,
        default_sigma_c=sigma,
        lead_hours=None,
        lead_hours_sigma_floor_c=None,
        ensemble_stdev_c=None,
        mean_c=mu,
        sigma_c=sigma,
        method=method,
    )
    return dist, info


_METHOD_DESCRIPTIONS: dict[str, str] = {
    "legacy_single_default_sigma": (
        "Single forecast value; sigma = default_sigma_c (lead time not used)."
    ),
    "legacy_multi_ensemble_stdev": (
        "Multiple values; sigma = ensemble stdev (lead time not used)."
    ),
    "legacy_multi_zero_stdev_uses_default_sigma": (
        "Multiple identical values (stdev=0); sigma = default_sigma_c (lead time not used)."
    ),
    "lead_time_single_floor": (
        "Single value with lead time; sigma = lead_hours_sigma_floor."
    ),
    "lead_time_multi_quadrature": (
        "Multiple values with lead time; "
        "sigma = sqrt(lead_hours_sigma_floor² + ensemble_stdev²)."
    ),
    "calibrated_single_model": (
        "Best historical model with bias-corrected mean and empirical sigma "
        "(error_std_c or rmse_c) from offline calibration stats."
    ),
}


def format_distribution_build_markdown(info: DistributionBuildInfo) -> str:
    """Render distribution build inputs and final parameters for reports."""
    method_desc = _METHOD_DESCRIPTIONS.get(info.method, info.method)
    lead_s = (
        f"{info.lead_hours:.2f} h"
        if info.lead_hours is not None
        else "not used (legacy mode)"
    )
    floor_s = (
        f"{info.lead_hours_sigma_floor_c:.2f} °C"
        if info.lead_hours_sigma_floor_c is not None
        else "—"
    )
    stdev_s = (
        f"{info.ensemble_stdev_c:.4f} °C"
        if info.ensemble_stdev_c is not None
        else "—"
    )
    return "\n".join(
        [
            "### Parameters chosen",
            f"- values_used_c: `{info.values_used_c}`",
            f"- default_sigma_c: `{info.default_sigma_c}` °C",
            f"- lead_hours: {lead_s}",
            f"- lead_hours_sigma_floor: {floor_s}",
            f"- ensemble_stdev (model disagreement): {stdev_s}",
            "",
            "### Method",
            f"- `{info.method}`: {method_desc}",
            "",
            "### Final distribution",
            f"- mean_c: **{info.mean_c:.4f}** °C",
            f"- sigma_c: **{info.sigma_c:.4f}** °C",
        ]
    )


def _normal_cdf(x: float, mu: float, sigma: float) -> float:
    """CDF of Normal(mu, sigma) at x."""
    if sigma <= 0:
        raise ValueError("sigma must be positive")
    z = (x - mu) / (sigma * math.sqrt(2.0))
    return 0.5 * (1.0 + math.erf(z))


def normal_pdf(x: float, mu: float, sigma: float) -> float:
    """PDF of Normal(mu, sigma) at x."""
    if sigma <= 0:
        raise ValueError("sigma must be positive")
    z = (x - mu) / sigma
    return math.exp(-0.5 * z * z) / (sigma * math.sqrt(2.0 * math.pi))


def probability_for_bucket(distribution: ForecastDistribution, bucket: TemperatureBucket) -> float:
    """Probability mass for bucket under Normal(mean_c, sigma_c)."""
    mu = distribution.mean_c
    sigma = distribution.sigma_c

    lo = bucket.lower_c
    hi = bucket.upper_c

    if lo is None and hi is None:
        raise ValueError("bucket must have at least one finite bound")

    if lo is None:
        return _normal_cdf(hi, mu, sigma)

    if hi is None:
        return 1.0 - _normal_cdf(lo, mu, sigma)

    return _normal_cdf(hi, mu, sigma) - _normal_cdf(lo, mu, sigma)


def probabilities_for_buckets(
    distribution: ForecastDistribution,
    buckets: list[TemperatureBucket],
    normalize: bool = True,
) -> list[BucketProbability]:
    """Compute per-bucket probabilities; optionally renormalize over the given set."""
    raw = [probability_for_bucket(distribution, b) for b in buckets]
    out: list[BucketProbability] = [
        BucketProbability(label=b.label, probability=p, lower_c=b.lower_c, upper_c=b.upper_c)
        for b, p in zip(buckets, raw, strict=True)
    ]

    if not normalize:
        return out

    total = sum(bp.probability for bp in out)
    if total == 0:
        raise ValueError("total probability over buckets is zero")

    inv = 1.0 / total
    return [
        BucketProbability(
            label=bp.label,
            probability=bp.probability * inv,
            lower_c=bp.lower_c,
            upper_c=bp.upper_c,
        )
        for bp in out
    ]
