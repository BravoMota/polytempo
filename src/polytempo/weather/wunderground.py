"""Wunderground daily Tmax fetcher.

Calls the public ``api.weather.com/v1/.../observations/historical.json``
endpoint (same JSON the WU history page hydrates client-side) and returns
the calendar-day maximum of ``observations[*].temp`` in Celsius.
"""

from __future__ import annotations

import json
import sys
from datetime import date, timedelta

import httpx

from polytempo.weather.observations import ObservedTmax

WUNDERGROUND_API_KEY = "e1f10a1e78da46f5b10a1e78da96f525"

WUNDERGROUND_HISTORICAL_URL_TEMPLATE = (
    "https://api.weather.com/v1/location/{station_id}:9:{country_code}"
    "/observations/historical.json"
    "?apiKey={api_key}&units=m&startDate={date_compact}&endDate={date_compact}"
)

WUNDERGROUND_LOCATION_POINT_URL_TEMPLATE = (
    "https://api.weather.com/v3/location/point"
    "?apiKey={api_key}&language=en-US&icaoCode={station_id}&format=json"
)

DEFAULT_COUNTRY_CODE = "GB"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36 PolyTempo/1.0"
)
REQUEST_TIMEOUT_S = 30.0


def build_wunderground_history_url(
    station_id: str,
    target_date: date,
    country_code: str = DEFAULT_COUNTRY_CODE,
    api_key: str = WUNDERGROUND_API_KEY,
) -> str:
    """Build the historical-observations JSON URL for one station/day."""
    return WUNDERGROUND_HISTORICAL_URL_TEMPLATE.format(
        station_id=station_id,
        country_code=country_code,
        api_key=api_key,
        date_compact=target_date.strftime("%Y%m%d"),
    )


def _build_location_point_url(station_id: str, api_key: str = WUNDERGROUND_API_KEY) -> str:
    return WUNDERGROUND_LOCATION_POINT_URL_TEMPLATE.format(
        station_id=station_id,
        api_key=api_key,
    )


def fetch_station_country_code(
    station_id: str,
    *,
    client: httpx.Client | None = None,
    api_key: str = WUNDERGROUND_API_KEY,
) -> str:
    """Resolve the 2-letter country code WU expects in the historical URL.

    Falls back to ``DEFAULT_COUNTRY_CODE`` on any error so callers don't fail
    hard for transient location-service issues.
    """
    url = _build_location_point_url(station_id, api_key=api_key)
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
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
            return country_code
    except Exception:
        pass
    return DEFAULT_COUNTRY_CODE


def parse_wunderground_daily_high(
    html_or_json: str,
    station_id: str,
    target_date: date,
) -> ObservedTmax:
    """Parse observed daily high °C from the WU v1 historical observations JSON."""
    try:
        payload = json.loads(html_or_json)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"daily high not found for {station_id} on {target_date.isoformat()}: "
            f"response is not JSON ({exc.msg})"
        ) from exc

    if not isinstance(payload, dict):
        raise ValueError(
            f"daily high not found for {station_id} on {target_date.isoformat()}: "
            "unexpected payload root"
        )

    observations = payload.get("observations")
    if not isinstance(observations, list) or not observations:
        raise ValueError(
            f"daily high not found for {station_id} on {target_date.isoformat()}: "
            "no observations array"
        )

    temps: list[float] = []
    for obs in observations:
        if not isinstance(obs, dict):
            continue
        value = obs.get("temp")
        if isinstance(value, (int, float)):
            temps.append(float(value))

    if not temps:
        raise ValueError(
            f"daily high not found for {station_id} on {target_date.isoformat()}: "
            "all temp readings are null"
        )

    return ObservedTmax(
        station_id=station_id,
        target_date=target_date,
        observed_tmax_c=float(max(temps)),
        source="wunderground",
    )


def fetch_wunderground_observed_tmax(
    station_id: str,
    target_date: date,
    base_url: str | None = None,
    *,
    client: httpx.Client | None = None,
    country_code: str | None = None,
) -> ObservedTmax:
    """Fetch and parse one station/day Wunderground observed Tmax."""
    if country_code is None:
        country_code = fetch_station_country_code(station_id, client=client)

    if base_url is None:
        url = build_wunderground_history_url(station_id, target_date, country_code)
    else:
        url = base_url.format(
            station_id=station_id,
            country_code=country_code,
            api_key=WUNDERGROUND_API_KEY,
            date_compact=target_date.strftime("%Y%m%d"),
            target_date=target_date.isoformat(),
        )

    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
    }

    if client is not None:
        response = client.get(url, headers=headers, timeout=REQUEST_TIMEOUT_S)
    else:
        response = httpx.get(url, headers=headers, timeout=REQUEST_TIMEOUT_S)
    response.raise_for_status()

    return parse_wunderground_daily_high(response.text, station_id, target_date)


def fetch_wunderground_observations_range(
    station_id: str,
    start_date: date,
    end_date: date,
    base_url: str | None = None,
    *,
    client: httpx.Client | None = None,
    country_code: str | None = None,
) -> list[ObservedTmax]:
    """Fetch observations over date range, skipping failed dates and reporting them."""
    if end_date < start_date:
        raise ValueError("end_date must be on or after start_date")

    if country_code is None:
        country_code = fetch_station_country_code(station_id, client=client)

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
                country_code=country_code,
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
