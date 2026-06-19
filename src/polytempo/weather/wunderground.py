"""Wunderground daily Tmax fetcher.

Reads the daily high (°C) from Weather.com station hourly observations
(``v1/location/{ICAO}/observations/historical.json``, ``units=m``);
daily high = ``max(observations[*].temp)``. Stored as integer °C (market
resolution). Fallback uses geocode ``dailysummary/30day`` (``units=m``),
which may differ from the history-page Summary table.
"""

from __future__ import annotations

import json
import re
import sys
from datetime import date, timedelta
from typing import Any

import httpx

from polytempo.weather.observations import CalibrationObservedTmax, ObservedTmax
from polytempo.weather.units import fahrenheit_to_celsius

WUNDERGROUND_BASE = "https://www.wunderground.com"
WUNDERGROUND_API_KEY = "e1f10a1e78da46f5b10a1e78da96f525"

WUNDERGROUND_LOCATION_POINT_URL_TEMPLATE = (
    "https://api.weather.com/v3/location/point"
    "?apiKey={api_key}&language=en-US&icaoCode={station_id}&format=json"
)

DAILYSUMMARY_30DAY_URL_TEMPLATE = (
    "https://api.weather.com/v3/wx/conditions/historical/dailysummary/30day"
    "?geocode={geocode}&format=json&language=en-US&units=m&apiKey={api_key}"
)

V1_HISTORICAL_URL_TEMPLATE = (
    "https://api.weather.com/v1/location/{station_id}:9:{country_code}"
    "/observations/historical.json"
    "?apiKey={api_key}&units=m&startDate={start_date}"
)

DEFAULT_COUNTRY_CODE = "GB"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36 PolyTempo/1.0"
)
REQUEST_TIMEOUT_S = 30.0

_STATE_RE = re.compile(
    r'<script id="app-root-state" type="application/json">(.*?)</script>',
    re.S,
)
_SUMMARY_HIGH_TEMP_RE = re.compile(
    r"<th[^>]*>\s*High Temp\s*</th>\s*<td[^>]*>\s*(-?\d+(?:\.\d+)?)\s*</td>",
    re.I,
)


def build_wunderground_history_page_url(
    station_id: str,
    target_date: date,
    *,
    country: str,
    city_slug: str,
) -> str:
    """Build the WU history daily page URL for one station/day."""
    return (
        f"{WUNDERGROUND_BASE}/history/daily/"
        f"{country}/{city_slug}/{station_id}/date/{target_date.isoformat()}"
    )


def _build_location_point_url(station_id: str, api_key: str = WUNDERGROUND_API_KEY) -> str:
    return WUNDERGROUND_LOCATION_POINT_URL_TEMPLATE.format(
        station_id=station_id,
        api_key=api_key,
    )


def _build_dailysummary_30day_url(geocode: str, api_key: str = WUNDERGROUND_API_KEY) -> str:
    return DAILYSUMMARY_30DAY_URL_TEMPLATE.format(geocode=geocode, api_key=api_key)


def _build_v1_historical_url(
    station_id: str,
    target_date: date,
    *,
    country_code: str,
    api_key: str = WUNDERGROUND_API_KEY,
) -> str:
    return V1_HISTORICAL_URL_TEMPLATE.format(
        station_id=station_id,
        country_code=country_code.upper(),
        api_key=api_key,
        start_date=target_date.strftime("%Y%m%d"),
    )


def _api_headers(*, referer: str | None = None) -> dict[str, str]:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
        "Origin": WUNDERGROUND_BASE,
        "Referer": referer or f"{WUNDERGROUND_BASE}/",
    }
    return headers


def _extract_app_root_state(html: bytes | str) -> dict[str, Any]:
    text = html.decode("utf-8", "replace") if isinstance(html, bytes) else html
    match = _STATE_RE.search(text)
    if not match:
        raise ValueError("Wunderground app-root-state JSON not found")
    try:
        payload = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise ValueError(f"failed to parse app-root-state JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("app-root-state root must be an object")
    return payload


def _state_bodies(state: dict[str, Any], url_fragment: str) -> Any:
    for entry in state.values():
        if isinstance(entry, dict) and url_fragment in entry.get("u", ""):
            yield entry.get("b")


def _temperature_max_f_from_body(body: object, *, target_date: date) -> float | None:
    if not isinstance(body, dict):
        return None

    valid_times = body.get("validTimeLocal")
    values = body.get("temperatureMax")
    if isinstance(valid_times, list) and isinstance(values, list):
        for value_raw, valid_raw in zip(values, valid_times, strict=False):
            if not isinstance(valid_raw, str):
                continue
            if date.fromisoformat(valid_raw[:10]) != target_date:
                continue
            if isinstance(value_raw, (int, float)) and not isinstance(value_raw, bool):
                return float(value_raw)

    value = body.get("temperatureMax")
    if isinstance(value, list) and value:
        candidate = value[0]
    else:
        candidate = value
    if isinstance(candidate, (int, float)) and not isinstance(candidate, bool):
        return float(candidate)
    return None


def _parse_summary_table_high_temp_f(html: str) -> float | None:
    match = _SUMMARY_HIGH_TEMP_RE.search(html)
    if not match:
        return None
    return float(match.group(1))


def _observed_tmax_from_celsius(
    *,
    station_id: str,
    target_date: date,
    temp_c: float,
) -> ObservedTmax:
    return ObservedTmax(
        station_id=station_id,
        target_date=target_date,
        observed_tmax_c=float(round(temp_c)),
        observed_tmax_f=None,
        source="wunderground",
    )


def _observed_tmax_from_fahrenheit(
    *,
    station_id: str,
    target_date: date,
    temp_f: float,
) -> ObservedTmax:
    return ObservedTmax(
        station_id=station_id,
        target_date=target_date,
        observed_tmax_c=fahrenheit_to_celsius(temp_f),
        observed_tmax_f=temp_f,
        source="wunderground",
    )


def parse_wunderground_daily_high(
    html_or_json: str,
    station_id: str,
    target_date: date,
) -> ObservedTmax:
    """Parse reported daily high from embedded page JSON or Summary HTML."""
    temp_f = _parse_summary_table_high_temp_f(html_or_json)
    if temp_f is None:
        state = _extract_app_root_state(html_or_json)
        for fragment in ("dailysummary", "historical/dailysummary"):
            for body in _state_bodies(state, fragment):
                temp_f = _temperature_max_f_from_body(body, target_date=target_date)
                if temp_f is not None:
                    break
            if temp_f is not None:
                break

    if temp_f is None:
        raise ValueError(
            f"daily high not found for {station_id} on {target_date.isoformat()}: "
            "temperatureMax missing from history page"
        )

    return _observed_tmax_from_fahrenheit(
        station_id=station_id,
        target_date=target_date,
        temp_f=temp_f,
    )


def parse_dailysummary_payload(
    payload: dict[str, Any],
    *,
    station_id: str,
    target_date: date,
) -> ObservedTmax:
    """Parse one day from a dailysummary API payload."""
    temp_f = _temperature_max_f_from_body(payload, target_date=target_date)
    if temp_f is None:
        raise ValueError(
            f"daily high not found for {station_id} on {target_date.isoformat()}: "
            "temperatureMax missing from dailysummary payload"
        )
    return _observed_tmax_from_fahrenheit(
        station_id=station_id,
        target_date=target_date,
        temp_f=temp_f,
    )


def to_calibration_observed(row: ObservedTmax) -> CalibrationObservedTmax:
    """Convert a parsed observation to the DB store shape."""
    return CalibrationObservedTmax(
        station_id=row.station_id,
        target_date=row.target_date,
        observed_tmax_f=row.observed_tmax_f,
        observed_tmax_c=row.observed_tmax_c,
        source=row.source,
    )


def fetch_station_country_code(
    station_id: str,
    *,
    client: httpx.Client | None = None,
    api_key: str = WUNDERGROUND_API_KEY,
) -> str:
    """Resolve the 2-letter country code WU expects in URLs."""
    url = _build_location_point_url(station_id, api_key=api_key)
    headers = _api_headers()
    try:
        if client is not None:
            response = client.get(url, headers=headers, timeout=REQUEST_TIMEOUT_S)
        else:
            response = httpx.get(url, headers=headers, timeout=REQUEST_TIMEOUT_S)
        response.raise_for_status()
        payload = response.json()
        location = payload.get("location", {}) if isinstance(payload, dict) else {}
        country_code = location.get("countryCode")
        if isinstance(country_code, str) and country_code:
            return country_code.upper()
    except Exception:
        pass
    return DEFAULT_COUNTRY_CODE


def resolve_station_geocode(
    station_id: str,
    *,
    client: httpx.Client | None = None,
    lat: float | None = None,
    lon: float | None = None,
    api_key: str = WUNDERGROUND_API_KEY,
) -> str:
    """Return ``lat,lon`` geocode string for Weather.com API calls."""
    if lat is not None and lon is not None:
        return f"{lat},{lon}"

    url = _build_location_point_url(station_id, api_key=api_key)
    headers = _api_headers()
    if client is not None:
        response = client.get(url, headers=headers, timeout=REQUEST_TIMEOUT_S)
    else:
        response = httpx.get(url, headers=headers, timeout=REQUEST_TIMEOUT_S)
    response.raise_for_status()
    payload = response.json()
    location = payload.get("location", {}) if isinstance(payload, dict) else {}
    latitude = location.get("latitude")
    longitude = location.get("longitude")
    if not isinstance(latitude, (int, float)) or not isinstance(longitude, (int, float)):
        raise ValueError(f"could not resolve geocode for station {station_id!r}")
    return f"{latitude},{longitude}"


def fetch_dailysummary_30day_map(
    geocode: str,
    *,
    client: httpx.Client | None = None,
    api_key: str = WUNDERGROUND_API_KEY,
) -> dict[date, float]:
    """Fetch rolling 30-day reported highs keyed by calendar date."""
    url = _build_dailysummary_30day_url(geocode, api_key=api_key)
    headers = _api_headers()
    if client is not None:
        response = client.get(url, headers=headers, timeout=REQUEST_TIMEOUT_S)
    else:
        response = httpx.get(url, headers=headers, timeout=REQUEST_TIMEOUT_S)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("dailysummary/30day payload must be an object")

    valid_times = payload.get("validTimeLocal")
    values = payload.get("temperatureMax")
    if not isinstance(valid_times, list) or not isinstance(values, list):
        raise ValueError("dailysummary/30day missing validTimeLocal/temperatureMax")

    out: dict[date, float] = {}
    for value_raw, valid_raw in zip(values, valid_times, strict=False):
        if not isinstance(valid_raw, str):
            continue
        if not isinstance(value_raw, (int, float)) or isinstance(value_raw, bool):
            continue
        out[date.fromisoformat(valid_raw[:10])] = float(value_raw)
    return out


def _fetch_v1_historical_daily_high_c(
    station_id: str,
    target_date: date,
    *,
    country_code: str,
    client: httpx.Client | None = None,
    api_key: str = WUNDERGROUND_API_KEY,
) -> float:
    """Daily high in °C from station hourly observations (metric API)."""
    url = _build_v1_historical_url(
        station_id,
        target_date,
        country_code=country_code,
        api_key=api_key,
    )
    headers = _api_headers()
    if client is not None:
        response = client.get(url, headers=headers, timeout=REQUEST_TIMEOUT_S)
    else:
        response = httpx.get(url, headers=headers, timeout=REQUEST_TIMEOUT_S)
    response.raise_for_status()
    payload = response.json()
    observations = payload.get("observations") if isinstance(payload, dict) else None
    if not isinstance(observations, list) or not observations:
        raise ValueError("historical observations missing or empty")

    temps = [
        float(row["temp"])
        for row in observations
        if isinstance(row, dict)
        and isinstance(row.get("temp"), (int, float))
        and not isinstance(row.get("temp"), bool)
    ]
    if not temps:
        raise ValueError("historical observations contained no temperature values")
    return max(temps)


def _resolve_history_location(
    *,
    country: str | None,
    country_code: str | None,
    city_slug: str | None,
) -> tuple[str, str]:
    resolved_country = (country or country_code or DEFAULT_COUNTRY_CODE).lower()
    resolved_city = city_slug or "london"
    return resolved_country, resolved_city


def fetch_wunderground_observed_tmax(
    station_id: str,
    target_date: date,
    base_url: str | None = None,
    *,
    client: httpx.Client | None = None,
    country_code: str | None = None,
    country: str | None = None,
    city_slug: str | None = None,
    lat: float | None = None,
    lon: float | None = None,
    dailysummary_cache: dict[date, float] | None = None,
    geocode: str | None = None,
) -> ObservedTmax:
    """Fetch reported daily Tmax (°C) for one station/day."""
    if base_url is not None:
        raise ValueError("base_url override is not supported; use Weather.com APIs")

    resolved_country_code = (country_code or country or DEFAULT_COUNTRY_CODE).upper()
    _resolve_history_location(
        country=country,
        country_code=country_code,
        city_slug=city_slug,
    )

    try:
        temp_c = _fetch_v1_historical_daily_high_c(
            station_id,
            target_date,
            country_code=resolved_country_code,
            client=client,
        )
    except Exception as exc:
        dailysummary_c: float | None = None
        if dailysummary_cache is not None:
            dailysummary_c = dailysummary_cache.get(target_date)
        else:
            try:
                resolved_geocode = geocode or resolve_station_geocode(
                    station_id,
                    client=client,
                    lat=lat,
                    lon=lon,
                )
                dailysummary_c = fetch_dailysummary_30day_map(
                    resolved_geocode, client=client
                ).get(target_date)
            except Exception:
                dailysummary_c = None
        if dailysummary_c is not None:
            print(
                f"wunderground: v1 historical failed for {target_date.isoformat()}; "
                "falling back to dailysummary/30day grid (may differ from Summary table)",
                file=sys.stderr,
            )
            temp_c = dailysummary_c
        else:
            raise ValueError(
                f"station hourly observations failed: {exc}"
            ) from exc

    return _observed_tmax_from_celsius(
        station_id=station_id,
        target_date=target_date,
        temp_c=temp_c,
    )


def fetch_wunderground_observations_range(
    station_id: str,
    start_date: date,
    end_date: date,
    base_url: str | None = None,
    *,
    client: httpx.Client | None = None,
    country_code: str | None = None,
    country: str | None = None,
    city_slug: str | None = None,
    lat: float | None = None,
    lon: float | None = None,
) -> list[ObservedTmax]:
    """Fetch observations over date range, skipping failed dates and reporting them."""
    if end_date < start_date:
        raise ValueError("end_date must be on or after start_date")

    resolved_country, resolved_city = _resolve_history_location(
        country=country,
        country_code=country_code,
        city_slug=city_slug,
    )
    resolved_country_code = (country_code or country or DEFAULT_COUNTRY_CODE).upper()

    out: list[ObservedTmax] = []
    failures: list[str] = []
    day = start_date
    while day <= end_date:
        try:
            row = fetch_wunderground_observed_tmax(
                station_id,
                day,
                base_url=base_url,
                client=client,
                country=resolved_country,
                city_slug=resolved_city,
                country_code=resolved_country_code,
                lat=lat,
                lon=lon,
            )
            out.append(row)
        except Exception as exc:
            failures.append(f"{day.isoformat()}: {exc}")
        day += timedelta(days=1)

    if failures:
        print(
            "wunderground failures (skipped):\n- " + "\n- ".join(failures),
            file=sys.stderr,
        )
    return out
