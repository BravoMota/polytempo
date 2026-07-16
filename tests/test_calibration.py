"""Tests for forecast calibration."""

from datetime import date

import pytest

from polytempo.model.calibration import (
    CalibrationRule,
    apply_bias_to_values,
    calibrate_forecast,
)
from polytempo.weather.schema import ForecastValues


def _forecast(values_c: list[float]) -> ForecastValues:
    return ForecastValues(
        source="open_meteo",
        latitude=40.4168,
        longitude=-3.7038,
        target_date=date(2026, 5, 14),
        values_c=values_c,
    )


def test_apply_bias_to_values_positive_bias() -> None:
    assert apply_bias_to_values([10.0, 12.5], bias_c=0.5) == [9.5, 12.0]


def test_apply_bias_to_values_negative_bias() -> None:
    assert apply_bias_to_values([10.0, 12.5], bias_c=-0.5) == [10.5, 13.0]


def test_apply_bias_to_values_zero_bias_returns_same_values() -> None:
    assert apply_bias_to_values([10.25, 12.75]) == [10.25, 12.75]


def test_apply_bias_to_values_does_not_mutate_original_list() -> None:
    values = [10.0, 12.0]

    corrected = apply_bias_to_values(values, bias_c=1.0)

    assert values == [10.0, 12.0]
    assert corrected == [9.0, 11.0]
    assert corrected is not values


def test_apply_bias_to_values_empty_values_returns_empty_list() -> None:
    corrected = apply_bias_to_values([], bias_c=1.0)

    assert corrected == []


@pytest.mark.parametrize("bias_c", [float("nan"), float("inf"), float("-inf")])
def test_apply_bias_to_values_non_finite_bias_raises(bias_c: float) -> None:
    with pytest.raises(ValueError, match="bias_c"):
        apply_bias_to_values([10.0], bias_c=bias_c)


def test_calibrate_forecast_without_rule_uses_zero_bias() -> None:
    forecast = _forecast([24.7])

    calibrated = calibrate_forecast(forecast)

    assert calibrated.values_c == [24.7]


def test_calibrate_forecast_with_rule_applies_correction() -> None:
    forecast = _forecast([24.7, 25.1])
    rule = CalibrationRule(source="open_meteo", station_id=None, bias_c=0.4)

    calibrated = calibrate_forecast(forecast, rule)

    assert calibrated.values_c == pytest.approx([24.3, 24.7])


def test_calibrate_forecast_preserves_metadata() -> None:
    forecast = ForecastValues(
        source="open_meteo",
        latitude=40.4168,
        longitude=-3.7038,
        target_date=date(2026, 5, 14),
        values_c=[24.7],
        observed_running_max_c=22.0,
    )
    rule = CalibrationRule(source="open_meteo", station_id="station-1", bias_c=0.4)

    calibrated = calibrate_forecast(forecast, rule)

    assert calibrated.source == forecast.source
    assert calibrated.latitude == forecast.latitude
    assert calibrated.longitude == forecast.longitude
    assert calibrated.target_date == forecast.target_date
    assert calibrated.observed_running_max_c == pytest.approx(22.0)


def test_calibrate_forecast_does_not_mutate_original_forecast() -> None:
    forecast = _forecast([24.7, 25.1])
    original_values = forecast.values_c
    rule = CalibrationRule(source="open_meteo", station_id=None, bias_c=0.4)

    calibrated = calibrate_forecast(forecast, rule)

    assert forecast.values_c == [24.7, 25.1]
    assert forecast.values_c is original_values
    assert calibrated.values_c is not forecast.values_c
