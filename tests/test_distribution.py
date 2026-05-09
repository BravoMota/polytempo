"""Tests for probability distribution building."""

import math
import statistics

import pytest

from polytempo.markets.buckets import parse_temperature_bucket
from polytempo.model.distribution import (
    ForecastDistribution,
    build_distribution,
    probabilities_for_buckets,
    probability_for_bucket,
)


def test_build_distribution_single_value_uses_default_sigma() -> None:
    d = build_distribution([24.2], default_sigma_c=1.5)
    assert d.mean_c == 24.2
    assert d.sigma_c == 1.5
    assert d.source_values_c == [24.2]


def test_build_distribution_multiple_values_mean_and_sigma() -> None:
    values = [23.8, 24.1, 24.3, 24.7, 25.0]
    d = build_distribution(values, default_sigma_c=1.0)
    assert d.mean_c == pytest.approx(sum(values) / len(values))
    assert d.sigma_c == pytest.approx(statistics.stdev(values))
    assert d.sigma_c > 0
    assert d.source_values_c == values


def test_build_distribution_empty_raises() -> None:
    with pytest.raises(ValueError, match="empty"):
        build_distribution([])


def test_build_distribution_invalid_default_sigma_raises() -> None:
    with pytest.raises(ValueError, match="default_sigma"):
        build_distribution([24.0], default_sigma_c=0.0)
    with pytest.raises(ValueError, match="default_sigma"):
        build_distribution([24.0], default_sigma_c=-1.0)


def test_probability_for_bucket_finite_interval_in_unit_interval() -> None:
    d = build_distribution([24.0], default_sigma_c=1.0)
    bucket = parse_temperature_bucket("23°C")
    p = probability_for_bucket(d, bucket)
    assert 0.0 <= p <= 1.0


def test_probability_open_below_positive_and_below_one() -> None:
    d = ForecastDistribution(mean_c=15.0, sigma_c=2.0, source_values_c=[15.0])
    bucket = parse_temperature_bucket("11°C or below")
    p = probability_for_bucket(d, bucket)
    assert 0.0 < p < 1.0


def test_probability_open_above_positive_and_below_one() -> None:
    d = ForecastDistribution(mean_c=26.0, sigma_c=2.0, source_values_c=[26.0])
    bucket = parse_temperature_bucket("25°C or higher")
    p = probability_for_bucket(d, bucket)
    assert 0.0 < p < 1.0


def test_probabilities_for_buckets_normalized_sum_to_one() -> None:
    d = build_distribution([24.0], default_sigma_c=1.0)
    buckets = [
        parse_temperature_bucket("23°C"),
        parse_temperature_bucket("24°C"),
        parse_temperature_bucket("25°C"),
    ]
    out = probabilities_for_buckets(d, buckets, normalize=True)
    total = sum(bp.probability for bp in out)
    assert math.isclose(total, 1.0)


def test_probabilities_for_buckets_preserves_order() -> None:
    d = build_distribution([24.0], default_sigma_c=5.0)
    buckets = [
        parse_temperature_bucket("22°C"),
        parse_temperature_bucket("25°C"),
        parse_temperature_bucket("28°C"),
    ]
    out = probabilities_for_buckets(d, buckets, normalize=False)
    assert [bp.label for bp in out] == [b.label for b in buckets]


def test_zero_spread_uses_default_sigma() -> None:
    d = build_distribution([24.0, 24.0, 24.0], default_sigma_c=2.5)
    assert d.mean_c == 24.0
    assert d.sigma_c == 2.5


def test_normalize_raises_when_total_probability_is_zero() -> None:
    # Mass concentrated away from buckets so raw probabilities underflow to zero.
    d = ForecastDistribution(mean_c=0.0, sigma_c=1e-300, source_values_c=[0.0])
    buckets = [
        parse_temperature_bucket("50°C"),
        parse_temperature_bucket("51°C"),
    ]
    with pytest.raises(ValueError, match="zero"):
        probabilities_for_buckets(d, buckets, normalize=True)
