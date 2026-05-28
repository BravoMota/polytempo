"""Tests for temperature bucket parsing."""

import pytest

from polytempo.markets.buckets import (
    TemperatureBucket,
    bucket_label_for_value,
    parse_temperature_bucket,
)


def test_single_bucket() -> None:
    b = parse_temperature_bucket("23°C")
    assert b == TemperatureBucket(
        label="23°C",
        lower_c=22.5,
        upper_c=23.5,
        kind="single",
    )


def test_range_bucket() -> None:
    b = parse_temperature_bucket("21°C - 22°C")
    assert b == TemperatureBucket(
        label="21°C - 22°C",
        lower_c=20.5,
        upper_c=22.5,
        kind="range",
    )


def test_or_below_bucket() -> None:
    b = parse_temperature_bucket("11°C or below")
    assert b == TemperatureBucket(
        label="11°C or below",
        lower_c=None,
        upper_c=11.5,
        kind="or_below",
    )


def test_or_higher_bucket() -> None:
    b = parse_temperature_bucket("25°C or higher")
    assert b == TemperatureBucket(
        label="25°C or higher",
        lower_c=24.5,
        upper_c=None,
        kind="or_higher",
    )


def test_above_bucket() -> None:
    b = parse_temperature_bucket(">29°C")
    assert b == TemperatureBucket(
        label=">29°C",
        lower_c=29.5,
        upper_c=None,
        kind="above",
    )


def test_whitespace_stripped() -> None:
    b = parse_temperature_bucket("  23°C  ")
    assert b.label == "23°C"
    assert b.lower_c == 22.5
    assert b.upper_c == 23.5
    assert b.kind == "single"


def test_degrees_optional_space_and_plain_c() -> None:
    b = parse_temperature_bucket("23 C")
    assert b.kind == "single"
    assert b.lower_c == 22.5
    assert b.upper_c == 23.5


def test_invalid_label_raises() -> None:
    with pytest.raises(ValueError):
        parse_temperature_bucket("not a bucket")


def test_invalid_range_raises() -> None:
    with pytest.raises(ValueError):
        parse_temperature_bucket("22°C - 21°C")


def _london_event_buckets() -> list[TemperatureBucket]:
    labels = [
        "22°C or below",
        "23°C",
        "24°C",
        "25°C",
        "26°C",
        "27°C",
        "28°C",
        "29°C",
        "30°C or higher",
    ]
    return [parse_temperature_bucket(s) for s in labels]


def test_bucket_label_for_value_single() -> None:
    assert bucket_label_for_value(28.7, _london_event_buckets()) == "29°C"


def test_bucket_label_for_value_boundary_inclusive_lower() -> None:
    assert bucket_label_for_value(28.5, _london_event_buckets()) == "29°C"


def test_bucket_label_for_value_boundary_exclusive_upper() -> None:
    assert bucket_label_for_value(28.4999, _london_event_buckets()) == "28°C"


def test_bucket_label_for_value_or_below() -> None:
    assert bucket_label_for_value(18.0, _london_event_buckets()) == "22°C or below"


def test_bucket_label_for_value_or_higher() -> None:
    assert bucket_label_for_value(35.0, _london_event_buckets()) == "30°C or higher"


def test_bucket_label_for_value_range() -> None:
    buckets = [
        parse_temperature_bucket("20°C or below"),
        parse_temperature_bucket("21°C - 22°C"),
        parse_temperature_bucket("23°C or higher"),
    ]
    assert bucket_label_for_value(21.7, buckets) == "21°C - 22°C"


def test_bucket_label_for_value_no_match_raises() -> None:
    buckets = [parse_temperature_bucket("23°C")]
    with pytest.raises(ValueError):
        bucket_label_for_value(10.0, buckets)
