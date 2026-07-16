"""Tests for probability distribution building."""

import math
import statistics

import pytest

from polytempo.markets.buckets import parse_temperature_bucket
from polytempo.model.distribution import (
    ForecastDistribution,
    build_calibrated_distribution,
    build_distribution,
    condition_probabilities_on_observed_floor,
    format_distribution_build_markdown,
    lead_time_sigma_floor,
    probabilities_for_buckets,
    probability_for_bucket,
    BucketProbability,
)


def test_build_distribution_single_value_uses_default_sigma() -> None:
    d, info = build_distribution([24.2], default_sigma_c=1.5)
    assert info.method == "legacy_single_default_sigma"
    assert d.mean_c == 24.2
    assert d.sigma_c == 1.5
    assert d.source_values_c == [24.2]


def test_build_distribution_multiple_values_mean_and_sigma() -> None:
    values = [23.8, 24.1, 24.3, 24.7, 25.0]
    d, info = build_distribution(values, default_sigma_c=1.0)
    assert info.method == "legacy_multi_ensemble_stdev"
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
    d, _ = build_distribution([24.0], default_sigma_c=1.0)
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
    d, _ = build_distribution([24.0], default_sigma_c=1.0)
    buckets = [
        parse_temperature_bucket("23°C"),
        parse_temperature_bucket("24°C"),
        parse_temperature_bucket("25°C"),
    ]
    out = probabilities_for_buckets(d, buckets, normalize=True)
    total = sum(bp.probability for bp in out)
    assert math.isclose(total, 1.0)


def test_probabilities_for_buckets_preserves_order() -> None:
    d, _ = build_distribution([24.0], default_sigma_c=5.0)
    buckets = [
        parse_temperature_bucket("22°C"),
        parse_temperature_bucket("25°C"),
        parse_temperature_bucket("28°C"),
    ]
    out = probabilities_for_buckets(d, buckets, normalize=False)
    assert [bp.label for bp in out] == [b.label for b in buckets]


def test_zero_spread_uses_default_sigma() -> None:
    d, info = build_distribution([24.0, 24.0, 24.0], default_sigma_c=2.5)
    assert info.method == "legacy_multi_zero_stdev_uses_default_sigma"
    assert d.mean_c == 24.0
    assert d.sigma_c == 2.5


@pytest.mark.parametrize(
    ("lead_hours", "expected_floor"),
    [
        (0.0, 0.5),
        (11.9, 0.5),
        (12.0, 0.8),
        (23.9, 0.8),
        (24.0, 1.2),
        (47.9, 1.2),
        (48.0, 1.6),
        (71.9, 1.6),
        (72.0, 2.0),
        (120.0, 2.0),
    ],
)
def test_lead_time_sigma_floor_bands(lead_hours: float, expected_floor: float) -> None:
    assert lead_time_sigma_floor(lead_hours) == expected_floor


def test_lead_time_sigma_floor_negative_raises() -> None:
    with pytest.raises(ValueError, match="lead_hours"):
        lead_time_sigma_floor(-1.0)


def test_build_distribution_single_value_with_lead_hours_uses_floor() -> None:
    d, info = build_distribution([24.0], lead_hours=24.0)
    assert info.method == "lead_time_single_floor"
    assert d.mean_c == 24.0
    assert d.sigma_c == pytest.approx(1.2)


def test_build_distribution_multiple_close_at_72h_sigma_at_least_floor() -> None:
    values = [24.0, 24.1, 24.0]
    d, info = build_distribution(values, lead_hours=72.0)
    assert info.method == "lead_time_multi_quadrature"
    base = 2.0
    disagreement = statistics.stdev(values)
    assert d.sigma_c >= base
    assert d.sigma_c == pytest.approx(math.sqrt(base**2 + disagreement**2))


def test_build_distribution_multiple_uses_quadrature() -> None:
    values = [20.0, 21.0, 22.0]
    lead_hours = 50.0
    base = lead_time_sigma_floor(lead_hours)
    disagreement = statistics.stdev(values)
    d, info = build_distribution(values, lead_hours=lead_hours)
    assert info.lead_hours_sigma_floor_c == pytest.approx(base)
    assert info.ensemble_stdev_c == pytest.approx(disagreement)
    assert d.sigma_c == pytest.approx(math.sqrt(base**2 + disagreement**2))


def test_build_distribution_without_lead_hours_unchanged() -> None:
    d, _ = build_distribution([24.2], default_sigma_c=1.5)
    assert d.sigma_c == 1.5
    values = [23.8, 24.1, 24.3]
    d_multi, _ = build_distribution(values, default_sigma_c=1.0)
    assert d_multi.sigma_c == pytest.approx(statistics.stdev(values))


def test_format_distribution_build_markdown_includes_inputs_and_final() -> None:
    _, info = build_distribution([24.0, 25.0], lead_hours=48.0)
    md = format_distribution_build_markdown(info)
    assert "values_used_c" in md
    assert "lead_hours" in md
    assert "mean_c" in md
    assert "sigma_c" in md
    assert info.method in md


def test_build_calibrated_distribution_passes_mu_and_sigma_through() -> None:
    d, info = build_calibrated_distribution(mu=17.5, sigma=1.4, source_values_c=[16.0, 18.0])

    assert d.mean_c == pytest.approx(17.5)
    assert d.sigma_c == pytest.approx(1.4)
    assert info.method == "calibrated_single_model"
    assert info.mean_c == pytest.approx(17.5)
    assert info.sigma_c == pytest.approx(1.4)
    assert info.lead_hours is None
    assert info.lead_hours_sigma_floor_c is None
    assert info.ensemble_stdev_c is None
    assert info.values_used_c == [16.0, 18.0]
    assert d.source_values_c == [16.0, 18.0]


def test_build_calibrated_distribution_defaults_source_values_to_mu() -> None:
    d, info = build_calibrated_distribution(mu=20.0, sigma=0.7)

    assert d.source_values_c == [20.0]
    assert info.values_used_c == [20.0]


@pytest.mark.parametrize("bad_sigma", [0.0, -0.1, float("nan"), float("inf")])
def test_build_calibrated_distribution_rejects_bad_sigma(bad_sigma: float) -> None:
    with pytest.raises(ValueError, match="sigma"):
        build_calibrated_distribution(mu=20.0, sigma=bad_sigma)


def test_build_calibrated_distribution_rejects_non_finite_mu() -> None:
    with pytest.raises(ValueError, match="mu"):
        build_calibrated_distribution(mu=float("nan"), sigma=1.0)


def test_format_distribution_build_markdown_describes_calibrated_method() -> None:
    _, info = build_calibrated_distribution(mu=20.0, sigma=1.1, source_values_c=[19.0, 21.0])
    md = format_distribution_build_markdown(info)
    assert "calibrated_single_model" in md
    assert "Best historical model" in md


def test_normalize_raises_when_total_probability_is_zero() -> None:
    # Mass concentrated away from buckets so raw probabilities underflow to zero.
    d = ForecastDistribution(mean_c=0.0, sigma_c=1e-300, source_values_c=[0.0])
    buckets = [
        parse_temperature_bucket("50°C"),
        parse_temperature_bucket("51°C"),
    ]
    with pytest.raises(ValueError, match="zero"):
        probabilities_for_buckets(d, buckets, normalize=True)


def test_condition_probabilities_on_observed_floor_renormalizes() -> None:
    probs = [
        BucketProbability(label="19°C", probability=0.05, lower_c=18.5, upper_c=19.5),
        BucketProbability(label="20°C", probability=0.10, lower_c=19.5, upper_c=20.5),
        BucketProbability(label="21°C", probability=0.30, lower_c=20.5, upper_c=21.5),
        BucketProbability(label="22°C", probability=0.35, lower_c=21.5, upper_c=22.5),
        BucketProbability(label="23°C", probability=0.20, lower_c=22.5, upper_c=23.5),
    ]
    out, audit = condition_probabilities_on_observed_floor(probs, 21.0)
    assert audit["applied"] is True
    assert audit["policy"] == "renormalize"
    assert audit["dead_mass"] == pytest.approx(0.15)
    assert audit["zeroed_labels"] == ["19°C", "20°C"]
    by_label = {bp.label: bp.probability for bp in out}
    assert by_label["19°C"] == 0.0
    assert by_label["20°C"] == 0.0
    assert by_label["21°C"] == pytest.approx(0.30 / 0.85)
    assert by_label["22°C"] == pytest.approx(0.35 / 0.85)
    assert by_label["23°C"] == pytest.approx(0.20 / 0.85)
    assert sum(by_label.values()) == pytest.approx(1.0)


def test_condition_probabilities_on_observed_floor_noop_when_floor_below_all() -> None:
    probs = [
        BucketProbability(label="21°C", probability=0.4, lower_c=20.5, upper_c=21.5),
        BucketProbability(label="22°C", probability=0.6, lower_c=21.5, upper_c=22.5),
    ]
    out, audit = condition_probabilities_on_observed_floor(probs, 10.0)
    assert audit["applied"] is False
    assert audit["reason"] == "no_invalid_buckets"
    assert out is probs


def test_condition_probabilities_on_observed_floor_noop_when_no_remaining_mass() -> None:
    probs = [
        BucketProbability(label="19°C", probability=0.5, lower_c=18.5, upper_c=19.5),
        BucketProbability(label="20°C", probability=0.5, lower_c=19.5, upper_c=20.5),
    ]
    out, audit = condition_probabilities_on_observed_floor(probs, 25.0)
    assert audit["applied"] is False
    assert audit["reason"] == "no_remaining_mass"
    assert out is probs
