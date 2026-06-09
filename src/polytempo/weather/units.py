"""Temperature unit conversions for weather data."""


def fahrenheit_to_celsius(f: float) -> float:
    """Convert Fahrenheit to Celsius, rounded to 2 decimal places."""
    return round((f - 32.0) * 5.0 / 9.0, 2)
