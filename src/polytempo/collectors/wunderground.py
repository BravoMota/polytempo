"""Wunderground HTML page collector (observation + hourly forecast).

Scrapes wunderground.com pages, saves raw HTML under
``data/weather/raw/wunderground/``, and parses the embedded Angular
``app-root-state`` JSON cache into observation and forecast snapshot rows.
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
from polytempo.storage.sqlite import (
    insert_forecast_snapshot,
    insert_observation_snapshot,
    mark_collector_started,
    upsert_collector_state_error,
    upsert_collector_state_success,
    utc_now_iso,
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


def _scraped_at_compact(scraped_at_utc: datetime) -> str:
    return scraped_at_utc.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def save_raw_response(
    raw_dir: Path,
    station_id: str,
    page_kind: str,
    scraped_at: datetime,
    body: bytes,
    url: str,
    *,
    target_date_local: date | None = None,
) -> tuple[Path, str]:
    """Write raw HTML and a JSON meta sidecar; return (html_path, content_hash)."""
    raw_dir.mkdir(parents=True, exist_ok=True)
    content_hash = compute_content_hash(body)
    ts = _scraped_at_compact(scraped_at)
    date_part = f"_{target_date_local.isoformat()}" if target_date_local else ""
    stem = f"{station_id}_{page_kind}{date_part}_{ts}_{content_hash[:12]}"
    html_path = raw_dir / f"{stem}.html"
    html_path.write_bytes(body)

    scraped_iso = scraped_at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    meta: dict[str, Any] = {
        "station_id": station_id,
        "page_kind": page_kind,
        "scraped_at_utc": scraped_iso,
        "url": url,
        "content_hash": content_hash,
        "raw_file_path": str(html_path),
    }
    if target_date_local is not None:
        meta["target_date_local"] = target_date_local.isoformat()

    meta_path = raw_dir / f"{stem}.meta.json"
    meta_path.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    logger.info(
        "saved raw %s station=%s hash=%s path=%s",
        page_kind,
        station_id,
        content_hash[:12],
        html_path.name,
    )
    return html_path, content_hash


@dataclass(frozen=True)
class ParsedObservation:
    """One observation snapshot extracted from a Wunderground page."""

    target_date_local: date
    temp_c: float
    raw_temp_text: str
    observed_at_utc: str | None = None
    observed_at_local: str | None = None


@dataclass(frozen=True)
class ParsedForecastHour:
    """One hourly forecast row extracted from a Wunderground page."""

    target_time_local: str
    target_time_utc: str
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


def _f_to_c(value: float) -> float:
    """Convert Fahrenheit to Celsius (pages always serve imperial units)."""
    return round((value - 32.0) * 5.0 / 9.0, 2)


def _epoch_to_iso_z(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _state_bodies(state: dict[str, Any], url_fragment: str) -> Any:
    for entry in state.values():
        if isinstance(entry, dict) and url_fragment in entry.get("u", ""):
            yield entry.get("b")


def parse_observation_page(
    html: bytes | str,
    station: StationConfig,
    scraped_at_utc: datetime,
) -> ParsedObservation:
    """Parse the current observation from a live observation page."""
    state = _extract_app_root_state(html)
    target_date_local = local_today(station.timezone, scraped_at_utc)

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
                temp_c=_f_to_c(temp_f),
                raw_temp_text=str(temp_f),
                observed_at_utc=latest.get("obsTimeUtc"),
                observed_at_local=latest.get("obsTimeLocal"),
            )
        raise ValueError(f"PWS observation not found for {station.station_id}")

    for body in _state_bodies(state, "/v3/wx/observations/current"):
        if not isinstance(body, dict) or body.get("temperature") is None:
            continue
        epoch = body.get("validTimeUtc")
        return ParsedObservation(
            target_date_local=target_date_local,
            temp_c=_f_to_c(body["temperature"]),
            raw_temp_text=str(body["temperature"]),
            observed_at_utc=_epoch_to_iso_z(epoch) if epoch else None,
            observed_at_local=body.get("validTimeLocal"),
        )
    raise ValueError(f"current observation not found for {station.station_id}")


def parse_hourly_forecast_page(
    html: bytes | str,
    station: StationConfig,
    target_date_local: date,
    scraped_at_utc: datetime,
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

    prefix = target_date_local.isoformat()
    hours: list[ParsedForecastHour] = []
    for temp_f, local_iso, utc_epoch in zip(temps, times_local, times_utc):
        if temp_f is None or not str(local_iso).startswith(prefix):
            continue
        hours.append(
            ParsedForecastHour(
                target_time_local=local_iso,
                target_time_utc=_epoch_to_iso_z(utc_epoch),
                temp_c=_f_to_c(temp_f),
                raw_temp_text=str(temp_f),
            )
        )
    if not hours:
        raise ValueError(f"no hourly forecast rows for {target_date_local}")
    return hours


def _fetch_and_save(
    *,
    raw_dir: Path,
    station: StationConfig,
    page_kind: str,
    url: str,
    scraped_at: datetime,
    client: httpx.Client,
    target_date_local: date | None = None,
) -> tuple[bytes, Path, str]:
    body = fetch_raw_page(url, client=client)
    path, content_hash = save_raw_response(
        raw_dir,
        station.station_id,
        page_kind,
        scraped_at,
        body,
        url,
        target_date_local=target_date_local,
    )
    logger.debug("fetched %s -> %s (%s)", url, path.name, content_hash[:12])
    return body, path, content_hash


def run_station_cycle(
    conn: Any,
    collector: CollectorConfig,
    station: StationConfig,
    raw_base_dir: Path,
    *,
    client: httpx.Client | None = None,
    now_utc: datetime | None = None,
) -> None:
    """Fetch observation + today/tomorrow hourly pages for one station."""
    now = now_utc or datetime.now(timezone.utc)
    scraped_at = now.astimezone(timezone.utc)
    scraped_iso = scraped_at.strftime("%Y-%m-%dT%H:%M:%SZ")
    source = collector.source
    raw_dir = raw_base_dir / source

    mark_collector_started(conn, COLLECTOR_NAME, station.station_id, source, now_utc=utc_now_iso())

    errors: list[str] = []
    own_client = client is None
    http = client or httpx.Client()

    try:
        try:
            obs_url = build_observation_url(station)
            body, path, content_hash = _fetch_and_save(
                raw_dir=raw_dir,
                station=station,
                page_kind="observation",
                url=obs_url,
                scraped_at=scraped_at,
                client=http,
            )
            obs = parse_observation_page(body, station, scraped_at)
            insert_observation_snapshot(
                conn,
                station_id=station.station_id,
                source=source,
                scraped_at_utc=scraped_iso,
                target_date_local=obs.target_date_local.isoformat(),
                station_timezone=station.timezone,
                observed_at_utc=obs.observed_at_utc,
                observed_at_local=obs.observed_at_local,
                temp_c=obs.temp_c,
                raw_temp_text=obs.raw_temp_text,
                raw_file_path=str(path),
                content_hash=content_hash,
            )
        except Exception as exc:
            errors.append(f"observation: {exc}")
            logger.error("observation failed station=%s: %s", station.station_id, exc)

        today_local, tomorrow_local = forecast_dates_for_station(station.timezone, now)
        for target_day in (today_local, tomorrow_local):
            try:
                fc_url = build_hourly_forecast_url(station, target_day)
                body, path, content_hash = _fetch_and_save(
                    raw_dir=raw_dir,
                    station=station,
                    page_kind="hourly_forecast",
                    url=fc_url,
                    scraped_at=scraped_at,
                    client=http,
                    target_date_local=target_day,
                )
                for hour in parse_hourly_forecast_page(body, station, target_day, scraped_at):
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
                        temp_c=hour.temp_c,
                        requested_lat=station.lat,
                        requested_lon=station.lon,
                        raw_file_path=str(path),
                        content_hash=content_hash,
                    )
            except Exception as exc:
                errors.append(f"hourly_forecast {target_day}: {exc}")
                logger.error(
                    "hourly forecast failed station=%s date=%s: %s",
                    station.station_id,
                    target_day,
                    exc,
                )
    finally:
        if own_client:
            http.close()

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


def run_cycle(
    conn: Any,
    config: WeatherCollectorsConfig,
    collector: CollectorConfig,
) -> None:
    """Run one collector cycle for all configured stations."""
    if not collector.enabled:
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
