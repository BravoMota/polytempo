"""Polymarket temperature bucket parsing.

Will parse labels like "23°C", "21°C - 22°C", "11°C or below", and "25°C or higher".
Purpose: convert bucket text into numeric temperature intervals.

Intervals are half-open [lower, upper): lower inclusive, upper exclusive.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Matches °C, ° C, or C after the integer (optional spaces before C).
_TEMP_SUFFIX = r"(?:°\s*C|°C|\s*C)"


@dataclass(frozen=True)
class TemperatureBucket:
    """Parsed Polymarket-style max-temperature bucket label."""

    label: str
    lower_c: float | None
    upper_c: float | None
    kind: str


def parse_temperature_bucket(label: str) -> TemperatureBucket:
    """Parse a Polymarket-style temperature bucket label into numeric bounds."""
    s = label.strip()
    if not s:
        raise ValueError("empty temperature bucket label")

    ts = _TEMP_SUFFIX

    m = re.match(rf"^\s*>\s*(\d+)\s*{ts}\s*$", s, re.IGNORECASE)
    if m:
        n = int(m.group(1))
        return TemperatureBucket(label=s, lower_c=n + 0.5, upper_c=None, kind="above")

    m = re.match(rf"^(\d+)\s*{ts}\s+or\s+below\s*$", s, re.IGNORECASE)
    if m:
        n = int(m.group(1))
        return TemperatureBucket(label=s, lower_c=None, upper_c=n + 0.5, kind="or_below")

    m = re.match(rf"^(\d+)\s*{ts}\s+or\s+higher\s*$", s, re.IGNORECASE)
    if m:
        n = int(m.group(1))
        return TemperatureBucket(label=s, lower_c=n - 0.5, upper_c=None, kind="or_higher")

    m = re.match(rf"^(\d+)\s*{ts}\s*-\s*(\d+)\s*{ts}\s*$", s, re.IGNORECASE)
    if m:
        lo = int(m.group(1))
        hi = int(m.group(2))
        if lo > hi:
            raise ValueError(f"invalid temperature range in label: {label!r}")
        return TemperatureBucket(
            label=s,
            lower_c=lo - 0.5,
            upper_c=hi + 0.5,
            kind="range",
        )

    m = re.match(rf"^(\d+)\s*{ts}\s*$", s, re.IGNORECASE)
    if m:
        n = int(m.group(1))
        return TemperatureBucket(
            label=s,
            lower_c=n - 0.5,
            upper_c=n + 0.5,
            kind="single",
        )

    raise ValueError(f"unknown temperature bucket label: {label!r}")


def bucket_label_for_value(
    value_c: float,
    buckets: list[TemperatureBucket],
) -> str:
    """Return the label of the bucket whose half-open interval contains ``value_c``.

    Open-ended kinds: ``or_below`` matches ``value_c < upper_c``; ``or_higher``
    and ``above`` match ``value_c >= lower_c``. Raises ``ValueError`` if no
    bucket matches (event labels do not cover this value).
    """
    for bucket in buckets:
        if bucket.kind in ("or_higher", "above"):
            if bucket.lower_c is not None and value_c >= bucket.lower_c:
                return bucket.label
        elif bucket.kind == "or_below":
            if bucket.upper_c is not None and value_c < bucket.upper_c:
                return bucket.label
        else:
            if (
                bucket.lower_c is not None
                and bucket.upper_c is not None
                and bucket.lower_c <= value_c < bucket.upper_c
            ):
                return bucket.label
    raise ValueError(f"no bucket covers value {value_c}")
