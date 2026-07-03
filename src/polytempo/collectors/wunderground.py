"""Wunderground HTML page collector (observation + hourly forecast).

Scrapes wunderground.com pages and parses the embedded Angular
``app-root-state`` JSON cache into observation and forecast snapshot rows.
Imperial (°F) comes from the HTML embed; metric (°C) from a separate
Weather.com API call (``units=m``).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from polytempo.collectors.config import CollectorConfig, StationConfig, WeatherCollectorsConfig
from polytempo.collectors.util import (
    forecast_dates_for_station,
    lead_hours_to_day_end,
    local_today,
)
from polytempo.storage.postgres import (
    insert_forecast_snapshot,
    insert_observation_snapshot,
    mark_collector_started,
    upsert_collector_state_error,
    upsert_collector_state_success,
    utc_now_iso,
)
from polytempo.weather.wunderground import (
    fetch_current_observation_payload,
    fetch_hourly_forecast_payload,
    resolve_station_geocode,
)

logger = logging.getLogger(__name__)

WUNDERGROUND_BASE = "https://www.wunderground.com"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36 PolyTempo/1.0"
)
REQUEST_TIMEOUT_S = 30.0

COLLECTOR_NAME = "wunderground"


def location_slug(station: StationConfig) -> str:
    """Slug used in hourly forecast URLs."""
    if station.station_type == "pws":
        if not station.pws_id:
            raise ValueError(f"PWS station {station.station_id!r} missing pws_id")
        return station.pws_id
    return station.station_id


def build_observation_url(station: StationConfig) -> str:
    """Build the live observation page URL for ICAO or PWS stations."""
    if station.station_type == "pws":
        if not station.pws_id:
            raise ValueError(f"PWS station {station.station_id!r} missing pws_id")
        return f"{WUNDERGROUND_BASE}/dashboard/pws/{station.pws_id}"
    return (
        f"{WUNDERGROUND_BASE}/weather/"
        f"{station.country}/{station.city_slug}/{station.station_id}"
    )


def build_hourly_forecast_url(station: StationConfig, target_date_local: date) -> str:
    """Build the hourly forecast page URL for one local target date."""
    slug = location_slug(station)
    return (
        f"{WUNDERGROUND_BASE}/hourly/"
        f"{station.country}/{station.city_slug}/{slug}/date/{target_date_local.isoformat()}"
    )


def compute_content_hash(body: bytes) -> str:
    """Return SHA-256 hex digest of raw response bytes."""
    return hashlib.sha256(body).hexdigest()


def fetch_raw_page(url: str, *, client: httpx.Client | None = None) -> bytes:
    """Fetch one page and return raw response body."""
    headers = {"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"}
    if client is not None:
        response = client.get(url, headers=headers, timeout=REQUEST_TIMEOUT_S, follow_redirects=True)
    else:
        response = httpx.get(
            url,
            headers=headers,
            timeout=REQUEST_TIMEOUT_S,
            follow_redirects=True,
        )
    response.raise_for_status()
    return response.content


@dataclass(frozen=True)
class ParsedObservation:
    """One observation snapshot extracted from a Wunderground page."""

    target_date_local: date
    temp_f: float
    temp_c: float
    raw_temp_text: str
    observed_at_utc: str | None = None
    observed_at_local: str | None = None


@dataclass(frozen=True)
class ParsedForecastHour:
    """One hourly forecast row extracted from a Wunderground page."""

    target_time_local: str
    target_time_utc: str
    temp_f: float
    temp_c: float
    raw_temp_text: str


_STATE_RE = re.compile(
    r'<script id="app-root-state" type="application/json">(.*?)</script>',
    re.S,
)


def _extract_app_root_state(html: bytes | str) -> dict[str, Any]:
    """Return the parsed Angular ``app-root-state`` JSON cache."""
    text = html.decode("utf-8", "replace") if isinstance(html, bytes) else html
    match = _STATE_RE.search(text)
    if not match:
        raise ValueError("Wunderground app-root-state JSON not found")
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise ValueError(f"failed to parse app-root-state JSON: {exc}") from exc


def _epoch_to_iso_z(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _state_bodies(state: dict[str, Any], url_fragment: str) -> Any:
    for entry in state.values():
        if isinstance(entry, dict) and url_fragment in entry.get("u", ""):
            yield entry.get("b")


def _station_geocode(station: StationConfig, *, client: httpx.Client) -> str:
    if station.lat is not None and station.lon is not None:
        return f"{station.lat},{station.lon}"
    return resolve_station_geocode(station.station_id, client=client)


def _temp_c_from_metric_observation(metric_body: dict[str, Any], station: StationConfig) -> float:
    if station.station_type == "pws":
        observations = metric_body.get("observations")
        if not isinstance(observations, list) or not observations:
            raise ValueError(f"PWS metric observation not found for {station.station_id}")
        latest = observations[-1]
        temp_c = (latest.get("metric") or {}).get("tempAvg")
        if temp_c is None:
            raise ValueError(f"PWS metric tempAvg missing for {station.station_id}")
        return float(temp_c)

    temp_c = metric_body.get("temperature")
    if temp_c is None:
        raise ValueError(f"ICAO metric temperature missing for {station.station_id}")
    return float(temp_c)


def _metric_hourly_temp_map(metric_body: dict[str, Any]) -> dict[int, float]:
    temps = metric_body.get("temperature")
    times_utc = metric_body.get("validTimeUtc")
    if not isinstance(temps, list) or not isinstance(times_utc, list):
        raise ValueError("metric hourly forecast arrays missing")
    out: dict[int, float] = {}
    for temp_c, utc_epoch in zip(temps, times_utc, strict=False):
        if temp_c is None or utc_epoch is None:
            continue
        out[int(utc_epoch)] = float(temp_c)
    return out


def parse_observation_page(
    html: bytes | str,
    station: StationConfig,
    scraped_at_utc: datetime,
    *,
    metric_body: dict[str, Any],
) -> ParsedObservation:
    """Parse the current observation from a live observation page."""
    state = _extract_app_root_state(html)
    target_date_local = local_today(station.timezone, scraped_at_utc)
    temp_c = _temp_c_from_metric_observation(metric_body, station)

    if station.station_type == "pws":
        for body in _state_bodies(state, "/v2/pws/observations/all/1day"):
            observations = body.get("observations") if isinstance(body, dict) else None
            if not observations:
                continue
            latest = observations[-1]
            temp_f = (latest.get("imperial") or {}).get("tempAvg")
            if temp_f is None:
                continue
            return ParsedObservation(
                target_date_local=target_date_local,
                temp_f=float(temp_f),
                temp_c=temp_c,
                raw_temp_text=str(temp_f),
                observed_at_utc=latest.get("obsTimeUtc"),
                observed_at_local=latest.get("obsTimeLocal"),
            )
        raise ValueError(f"PWS observation not found for {station.station_id}")

    for body in _state_bodies(state, "/v3/wx/observations/current"):
        if not isinstance(body, dict) or body.get("temperature") is None:
            continue
        epoch = body.get("validTimeUtc")
        temp_f_val = body["temperature"]
        temp_f = float(temp_f_val)
        return ParsedObservation(
            target_date_local=target_date_local,
            temp_f=temp_f,
            temp_c=temp_c,
            raw_temp_text=str(temp_f_val),
            observed_at_utc=_epoch_to_iso_z(epoch) if epoch else None,
            observed_at_local=body.get("validTimeLocal"),
        )
    raise ValueError(f"current observation not found for {station.station_id}")


def parse_hourly_forecast_page(
    html: bytes | str,
    station: StationConfig,
    target_date_local: date,
    scraped_at_utc: datetime,
    *,
    metric_body: dict[str, Any],
) -> list[ParsedForecastHour]:
    """Parse hourly forecast rows for ``target_date_local`` from a forecast page."""
    state = _extract_app_root_state(html)
    body = next(
        (b for b in _state_bodies(state, "/v3/wx/forecast/hourly/") if isinstance(b, dict)),
        None,
    )
    if body is None:
        raise ValueError(f"hourly forecast not found for {station.station_id}")

    temps = body.get("temperature")
    times_local = body.get("validTimeLocal")
    times_utc = body.get("validTimeUtc")
    if not temps or not times_local or not times_utc:
        raise ValueError(f"hourly forecast arrays missing for {station.station_id}")

    metric_by_epoch = _metric_hourly_temp_map(metric_body)
    prefix = target_date_local.isoformat()
    hours: list[ParsedForecastHour] = []
    for temp_f_raw, local_iso, utc_epoch in zip(temps, times_local, times_utc, strict=False):
        if temp_f_raw is None or not str(local_iso).startswith(prefix):
            continue
        temp_c = metric_by_epoch.get(int(utc_epoch))
        if temp_c is None:
            logger.warning(
                "hourly forecast missing metric temp station=%s epoch=%s local=%s",
                station.station_id,
                utc_epoch,
                local_iso,
            )
            continue
        hours.append(
            ParsedForecastHour(
                target_time_local=local_iso,
                target_time_utc=_epoch_to_iso_z(utc_epoch),
                temp_f=float(temp_f_raw),
                temp_c=temp_c,
                raw_temp_text=str(temp_f_raw),
            )
        )
    if not hours:
        raise ValueError(f"no hourly forecast rows for {target_date_local}")
    return hours


def _fetch_page(url: str, *, client: httpx.Client) -> tuple[bytes, str]:
    body = fetch_raw_page(url, client=client)
    content_hash = compute_content_hash(body)
    logger.debug("fetched %s (%s)", url, content_hash[:12])
    return body, content_hash


def run_station_observations(
    conn: Any,
    collector: CollectorConfig,
    station: StationConfig,
    raw_base_dir: Path,
    *,
    client: httpx.Client,
    now_utc: datetime | None = None,
) -> None:
    """Fetch and store the live observation page for one station."""
    now = now_utc or datetime.now(timezone.utc)
    scraped_at = now.astimezone(timezone.utc)
    scraped_iso = scraped_at.strftime("%Y-%m-%dT%H:%M:%SZ")
    source = collector.source

    mark_collector_started(conn, COLLECTOR_NAME, station.station_id, source, now_utc=utc_now_iso())

    try:
        geocode = _station_geocode(station, client=client)
        metric_body = fetch_current_observation_payload(
            station_type=station.station_type,
            station_id=station.station_id,
            geocode=geocode,
            pws_id=station.pws_id,
            units="m",
            client=client,
        )
        obs_url = build_observation_url(station)
        body, content_hash = _fetch_page(obs_url, client=client)
        obs = parse_observation_page(body, station, scraped_at, metric_body=metric_body)
        insert_observation_snapshot(
            conn,
            station_id=station.station_id,
            source=source,
            scraped_at_utc=scraped_iso,
            target_date_local=obs.target_date_local.isoformat(),
            station_timezone=station.timezone,
            observed_at_utc=obs.observed_at_utc,
            observed_at_local=obs.observed_at_local,
            temp_f=obs.temp_f,
            temp_c=obs.temp_c,
            raw_temp_text=obs.raw_temp_text,
            content_hash=content_hash,
        )
    except Exception as exc:
        logger.error("observation failed station=%s: %s", station.station_id, exc)
        conn.rollback()
        upsert_collector_state_error(
            conn,
            COLLECTOR_NAME,
            station.station_id,
            source,
            f"observation: {exc}",
        )
        conn.commit()
        return

    upsert_collector_state_success(
        conn,
        COLLECTOR_NAME,
        station.station_id,
        source,
    )
    conn.commit()


def run_station_forecasts(
    conn: Any,
    collector: CollectorConfig,
    station: StationConfig,
    raw_base_dir: Path,
    *,
    client: httpx.Client,
    now_utc: datetime | None = None,
) -> None:
    """Fetch and store today/tomorrow hourly forecast pages for one station."""
    now = now_utc or datetime.now(timezone.utc)
    scraped_at = now.astimezone(timezone.utc)
    scraped_iso = scraped_at.strftime("%Y-%m-%dT%H:%M:%SZ")
    source = collector.source

    mark_collector_started(conn, COLLECTOR_NAME, station.station_id, source, now_utc=utc_now_iso())

    errors: list[str] = []
    today_local, tomorrow_local = forecast_dates_for_station(station.timezone, now)
    try:
        geocode = _station_geocode(station, client=client)
        metric_body = fetch_hourly_forecast_payload(geocode, units="m", client=client)
    except Exception as exc:
        conn.rollback()
        upsert_collector_state_error(
            conn,
            COLLECTOR_NAME,
            station.station_id,
            source,
            f"hourly_forecast metric API: {exc}",
        )
        conn.commit()
        return

    for target_day in (today_local, tomorrow_local):
        try:
            fc_url = build_hourly_forecast_url(station, target_day)
            body, content_hash = _fetch_page(fc_url, client=client)
            for hour in parse_hourly_forecast_page(
                body, station, target_day, scraped_at, metric_body=metric_body
            ):
                insert_forecast_snapshot(
                    conn,
                    station_id=station.station_id,
                    source=source,
                    scraped_at_utc=scraped_iso,
                    target_date_local=target_day.isoformat(),
                    station_timezone=station.timezone,
                    target_time_utc=hour.target_time_utc,
                    target_time_local=hour.target_time_local,
                    lead_hours_to_day_end=lead_hours_to_day_end(
                        scraped_at, target_day, station.timezone
                    ),
                    temp_f=hour.temp_f,
                    temp_c=hour.temp_c,
                    raw_temp_text=hour.raw_temp_text,
                    requested_lat=station.lat,
                    requested_lon=station.lon,
                    content_hash=content_hash,
                )
        except Exception as exc:
            conn.rollback()
            errors.append(f"hourly_forecast {target_day}: {exc}")
            logger.error(
                "hourly forecast failed station=%s date=%s: %s",
                station.station_id,
                target_day,
                exc,
            )

    if errors:
        upsert_collector_state_error(
            conn,
            COLLECTOR_NAME,
            station.station_id,
            source,
            "; ".join(errors),
        )
        conn.commit()
        return

    upsert_collector_state_success(
        conn,
        COLLECTOR_NAME,
        station.station_id,
        source,
    )
    conn.commit()


def run_station_cycle(
    conn: Any,
    collector: CollectorConfig,
    station: StationConfig,
    raw_base_dir: Path,
    *,
    client: httpx.Client | None = None,
    now_utc: datetime | None = None,
    fetch_observations: bool = True,
    fetch_forecasts: bool = True,
) -> None:
    """Fetch observation and/or forecast pages for one station."""
    own_client = client is None
    http = client or httpx.Client()
    try:
        if fetch_observations:
            run_station_observations(
                conn,
                collector,
                station,
                raw_base_dir,
                client=http,
                now_utc=now_utc,
            )
        if fetch_forecasts:
            run_station_forecasts(
                conn,
                collector,
                station,
                raw_base_dir,
                client=http,
                now_utc=now_utc,
            )
    finally:
        if own_client:
            http.close()


def run_cycle(
    conn: Any,
    config: WeatherCollectorsConfig,
    collector: CollectorConfig,
    *,
    fetch_observations: bool = True,
    fetch_forecasts: bool = True,
) -> None:
    """Run one collector cycle for all configured stations."""
    if not collector.enabled:
        return
    if not fetch_observations and not fetch_forecasts:
        return

    with httpx.Client() as client:
        for station in collector.stations:
            try:
                run_station_cycle(
                    conn,
                    collector,
                    station,
                    config.raw_base_dir,
                    client=client,
                    fetch_observations=fetch_observations,
                    fetch_forecasts=fetch_forecasts,
                )
            except Exception as exc:
                logger.exception(
                    "unexpected error station=%s collector=%s: %s",
                    station.station_id,
                    collector.name,
                    exc,
                )
                upsert_collector_state_error(
                    conn,
                    COLLECTOR_NAME,
                    station.station_id,
                    collector.source,
                    str(exc),
                )
                conn.commit()
