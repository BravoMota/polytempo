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
class BucketProbability:
    """Probability mass for one temperature bucket under the forecast model."""

    label: str
    probability: float
    lower_c: float | None
    upper_c: float | None


def build_distribution(values_c: list[float], default_sigma_c: float = 1.0) -> ForecastDistribution:
    """Build N(mean, sigma) parameters from one or more Celsius forecast samples."""
    if default_sigma_c <= 0:
        raise ValueError("default_sigma_c must be positive")
    if not values_c:
        raise ValueError("values_c must not be empty")

    source = list(values_c)

    if len(source) == 1:
        return ForecastDistribution(
            mean_c=source[0],
            sigma_c=default_sigma_c,
            source_values_c=source,
        )

    mean_c = statistics.mean(source)
    sigma_c = statistics.stdev(source)
    if sigma_c == 0:
        sigma_c = default_sigma_c

    return ForecastDistribution(mean_c=mean_c, sigma_c=sigma_c, source_values_c=source)


def _normal_cdf(x: float, mu: float, sigma: float) -> float:
    """CDF of Normal(mu, sigma) at x."""
    if sigma <= 0:
        raise ValueError("sigma must be positive")
    z = (x - mu) / (sigma * math.sqrt(2.0))
    return 0.5 * (1.0 + math.erf(z))


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
