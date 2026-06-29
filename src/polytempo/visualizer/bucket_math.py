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
