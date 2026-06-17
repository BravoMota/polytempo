"""Tests for calibration_runner pure helpers."""

from __future__ import annotations

from datetime import date

from polytempo.weather.calibration_runner import _snap_observations_to_integer_c


def test_snap_recovers_integer_c_from_fahrenheit_artifact():
    # The °F round-trip sawtooth from the diagnostics: stored °C -> true °C.
    artifact = {
        ("EGLC", date(2026, 6, 6)): 17.22,  # 63°F -> 17°C
        ("EGLC", date(2026, 6, 7)): 18.89,  # 66°F -> 19°C
        ("EGLC", date(2026, 6, 8)): 16.11,  # 61°F -> 16°C
        ("LEMD", date(2026, 6, 8)): 22.78,  # 73°F -> 23°C
    }
    snapped = _snap_observations_to_integer_c(artifact)
    assert snapped == {
        ("EGLC", date(2026, 6, 6)): 17.0,
        ("EGLC", date(2026, 6, 7)): 19.0,
        ("EGLC", date(2026, 6, 8)): 16.0,
        ("LEMD", date(2026, 6, 8)): 23.0,
    }


def test_snap_is_idempotent_for_clean_integer_values():
    clean = {("EGLC", date(2026, 6, 8)): 16.0}
    assert _snap_observations_to_integer_c(clean) == clean
