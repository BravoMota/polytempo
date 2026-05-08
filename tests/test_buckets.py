"""Tests for temperature bucket parsing."""

import pytest

from polytempo.markets.buckets import TemperatureBucket, parse_temperature_bucket


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
