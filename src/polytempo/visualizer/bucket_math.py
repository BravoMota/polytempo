"""Pure bucket geometry and market-implied moments (no pandas/plotly).

Extracted from ``chart.py`` so non-plotting callers (e.g. the distribution data
layer) can reuse them without importing the visualization stack. ``chart.py``
re-exports these names for backward compatibility.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from polytempo.markets.buckets import TemperatureBucket, parse_temperature_bucket


@dataclass(frozen=True)
class MarketImpliedSummary:
    """Discrete market-implied location/spread from normalized yes_ask weights."""

    mean_c: float
    discrete_std_c: float


def bucket_center_c(bucket: TemperatureBucket) -> float:
    if bucket.lower_c is not None and bucket.upper_c is not None:
        return (bucket.lower_c + bucket.upper_c) / 2.0
    if bucket.lower_c is not None:
        return bucket.lower_c + 0.5
    if bucket.upper_c is not None:
        return bucket.upper_c - 0.5
    raise ValueError(f"bucket has no finite bounds: {bucket.label!r}")


def compute_market_implied_summary(
    labels: list[str],
    yes_asks: list[float | None],
) -> MarketImpliedSummary | None:
    """Weighted mean/std of bucket centers using normalized yes_ask masses."""
    centers: list[float] = []
    weights: list[float] = []
    for label, ask in zip(labels, yes_asks, strict=True):
        if ask is None or ask <= 0:
            continue
        bucket = parse_temperature_bucket(label)
        centers.append(bucket_center_c(bucket))
        weights.append(float(ask))
    if not weights:
        return None
    total = sum(weights)
    norm = [w / total for w in weights]
    mean = sum(w * c for w, c in zip(norm, centers, strict=True))
    var = sum(w * (c - mean) ** 2 for w, c in zip(norm, centers, strict=True))
    return MarketImpliedSummary(mean_c=mean, discrete_std_c=math.sqrt(var))


DEFAULT_MARKET_SIGMA_MAX_MEAN_DELTA_C = 1.0
DEFAULT_MARKET_SIGMA_SCALE = 1.0
MIN_MARKET_CAPPED_SIGMA_C = 0.1


def cap_sigma_to_market(
    mean_c: float,
    sigma_c: float,
    *,
    labels: list[str],
    yes_asks: list[float | None],
    max_mean_delta_c: float = DEFAULT_MARKET_SIGMA_MAX_MEAN_DELTA_C,
    market_sigma_scale: float = DEFAULT_MARKET_SIGMA_SCALE,
) -> tuple[float, dict[str, object]]:
    """Cap model sigma so the market is not more confident when means agree.

    When ``|mean_c - market_mean| <= max_mean_delta_c`` and the market's
    discrete std (from normalized ``yes_ask`` weights on bucket centers) implies
    a tighter distribution, returns ``min(sigma_c, market_sigma_scale *
    market_std)``. Otherwise leaves ``sigma_c`` unchanged.
    """
    summary = compute_market_implied_summary(labels, yes_asks)
    audit: dict[str, object] = {
        "sigma_model_c": sigma_c,
        "mean_model_c": mean_c,
        "max_mean_delta_c": max_mean_delta_c,
        "market_sigma_scale": market_sigma_scale,
    }
    if summary is None:
        audit["applied"] = False
        audit["reason"] = "no_market_prices"
        return sigma_c, audit

    market_sigma = summary.discrete_std_c * market_sigma_scale
    mean_delta = abs(mean_c - summary.mean_c)
    audit["sigma_market_c"] = market_sigma
    audit["mean_market_c"] = summary.mean_c
    audit["mean_delta_c"] = mean_delta

    if mean_delta > max_mean_delta_c:
        audit["applied"] = False
        audit["reason"] = "means_diverge"
        return sigma_c, audit

    if market_sigma >= sigma_c:
        audit["applied"] = False
        audit["reason"] = "market_not_tighter"
        audit["sigma_final_c"] = sigma_c
        return sigma_c, audit

    capped = max(market_sigma, MIN_MARKET_CAPPED_SIGMA_C)
    audit["applied"] = True
    audit["reason"] = "capped_to_market"
    audit["sigma_final_c"] = capped
    return capped, audit
