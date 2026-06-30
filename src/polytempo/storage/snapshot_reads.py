"""Read historical Open-Meteo and CLOB snapshots for performance replay."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime

from psycopg import Connection

from polytempo.markets.polymarket import (
    PolymarketBucket,
    PolymarketEvent,
    is_untradeable_market_price,
)
from polytempo.storage.postgres import get_connection, resolve_database_url
from polytempo.weather.calibration_storage import (
    load_wu_history_observations_for_day,
)
from polytempo.weather.calibration_wu_forecasts import (
    ForecastHourRow,
    adjusted_predicted_tmax_c,
    is_oclock_scrape,
    normalize_scrape_time,
)
from polytempo.weather.schema import ForecastValues
from polytempo.weather.stations import Station


def _parse_ts(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


DEFAULT_CLOB_MAX_AGE_SECONDS = 30 * 60


def _pick_nearest_iso_timestamp(
    rows: list[dict],
    *,
    ts_column: str,
    at_utc: datetime,
) -> dict | None:
    if not rows:
        return None

    def _distance(row: dict) -> tuple[int, float]:
        ts = _parse_ts(str(row[ts_column]))
        if ts <= at_utc:
            return (0, (at_utc - ts).total_seconds())
        return (1, (ts - at_utc).total_seconds())

    return min(rows, key=_distance)


def _pick_nearest_at_or_before(
    rows: list[dict],
    *,
    ts_column: str,
    at_utc: datetime,
    max_age_seconds: float | None = None,
) -> dict | None:
    """Return the newest row with ``ts_column <= at_utc``, optionally within max age."""
    best: tuple[float, dict] | None = None
    for row in rows:
        ts = _parse_ts(str(row[ts_column]))
        if ts > at_utc:
            continue
        age = (at_utc - ts).total_seconds()
        if max_age_seconds is not None and age > max_age_seconds:
            continue
        if best is None or age < best[0]:
            best = (age, row)
    return None if best is None else best[1]


@dataclass(frozen=True)
class WundergroundForecastSnapshot:
    """Adjusted daily Tmax from nearest o'clock WU ``forecast_snapshots`` scrape."""

    scraped_at_utc: str
    predicted_tmax_c: float


@dataclass(frozen=True)
class OpenMeteoSnapshotBundle:
    fetch_cycle_id: int
    fetched_at_utc: str
    forecast: ForecastValues


def fetch_nearest_open_meteo_forecast(
    station: Station,
    target_date: date,
    at_utc: datetime,
    *,
    database_url: str | None = None,
    conn: Connection | None = None,
) -> OpenMeteoSnapshotBundle | None:
    """Load the Open-Meteo forecast from the fetch cycle nearest ``at_utc``.

    Matches what the paper bot would see: nearest collector cycle by time, then
    rows for ``target_date``. Returns ``None`` when that cycle has no rows for
    the settlement date.
    """
    target_s = target_date.isoformat()

    def _query(connection: Connection) -> OpenMeteoSnapshotBundle | None:
        cycles = connection.execute(
            """
            SELECT id, fetched_at_utc
            FROM open_meteo_fetch_cycles
            WHERE station_id = %(station_id)s
            ORDER BY fetched_at_utc ASC
            """,
            {"station_id": station.icao},
        ).fetchall()
        cycle = _pick_nearest_iso_timestamp(cycles, ts_column="fetched_at_utc", at_utc=at_utc)
        if cycle is None:
            return None

        snapshots = connection.execute(
            """
            SELECT model, predicted_tmax_c, init_lead_hours, wall_clock_lead_hours,
                   run_init_utc, fetched_at_utc
            FROM open_meteo_forecast_snapshots
            WHERE fetch_cycle_id = %(cycle_id)s
              AND station_id = %(station_id)s
              AND target_date_local = %(target_date)s
            ORDER BY model ASC
            """,
            {
                "cycle_id": cycle["id"],
                "station_id": station.icao,
                "target_date": target_s,
            },
        ).fetchall()
        if not snapshots:
            return None

        models = [str(row["model"]) for row in snapshots]
        values = [float(row["predicted_tmax_c"]) for row in snapshots]
        init_leads = [
            float(row["init_lead_hours"]) if row["init_lead_hours"] is not None else 0.0
            for row in snapshots
        ]
        forecast = ForecastValues(
            source="open_meteo_snapshot",
            latitude=station.latitude,
            longitude=station.longitude,
            target_date=target_date,
            values_c=values,
            models=models,
            init_lead_hours=init_leads,
        )
        return OpenMeteoSnapshotBundle(
            fetch_cycle_id=int(cycle["id"]),
            fetched_at_utc=str(cycle["fetched_at_utc"]),
            forecast=forecast,
        )

    if conn is not None:
        return _query(conn)

    url = resolve_database_url(override=database_url)
    with get_connection(url) as connection:
        return _query(connection)


def fetch_nearest_wunderground_adjusted_tmax(
    station: Station,
    target_date: date,
    at_utc: datetime,
    *,
    database_url: str | None = None,
    conn: Connection | None = None,
) -> WundergroundForecastSnapshot | None:
    """Load WU hourly scrapes from ``forecast_snapshots`` and compute adjusted Tmax.

    Uses the newest o'clock scrape at or before ``at_utc``, then applies the same
    obs+forecast merge as the calibration pipeline with ``as_of_utc=at_utc``.
    """
    target_s = target_date.isoformat()

    def _query(connection: Connection) -> WundergroundForecastSnapshot | None:
        scrape_rows = connection.execute(
            """
            SELECT DISTINCT scraped_at_utc
            FROM forecast_snapshots
            WHERE station_id = %(station_id)s
              AND source = 'wunderground'
              AND target_date_local = %(target_date)s
            ORDER BY scraped_at_utc ASC
            """,
            {"station_id": station.icao, "target_date": target_s},
        ).fetchall()
        oclock_scrapes = [
            row
            for row in scrape_rows
            if is_oclock_scrape(_parse_ts(str(row["scraped_at_utc"])))
        ]
        picked = _pick_nearest_at_or_before(
            oclock_scrapes,
            ts_column="scraped_at_utc",
            at_utc=at_utc,
        )
        if picked is None:
            return None

        scraped_at = normalize_scrape_time(_parse_ts(str(picked["scraped_at_utc"])))
        scraped_text = scraped_at.strftime("%Y-%m-%dT%H:%M:%SZ")

        hourly_rows = connection.execute(
            """
            SELECT target_time_utc, temp_c
            FROM forecast_snapshots
            WHERE station_id = %(station_id)s
              AND source = 'wunderground'
              AND target_date_local = %(target_date)s
              AND scraped_at_utc = %(scraped_at)s
              AND target_time_utc IS NOT NULL
              AND temp_c IS NOT NULL
            ORDER BY target_time_utc ASC
            """,
            {
                "station_id": station.icao,
                "target_date": target_s,
                "scraped_at": str(picked["scraped_at_utc"]),
            },
        ).fetchall()
        if not hourly_rows:
            return None

        forecast_hours = [
            ForecastHourRow(
                target_time_utc=_parse_ts(str(row["target_time_utc"])),
                temp_c=float(row["temp_c"]),
            )
            for row in hourly_rows
        ]
        history_obs = load_wu_history_observations_for_day(
            connection,
            station_id=station.icao,
            target_date=target_date,
        )
        predicted = adjusted_predicted_tmax_c(
            target_date=target_date,
            as_of_utc=at_utc,
            hourly_forecast_rows=forecast_hours,
            history_observations=history_obs,
        )
        if predicted is None:
            return None
        return WundergroundForecastSnapshot(
            scraped_at_utc=scraped_text,
            predicted_tmax_c=predicted,
        )

    if conn is not None:
        return _query(conn)

    url = resolve_database_url(override=database_url)
    with get_connection(url) as connection:
        return _query(connection)


def missing_open_meteo_forecast_reason(
    station: Station,
    target_date: date,
    at_utc: datetime,
    *,
    database_url: str | None = None,
    conn: Connection | None = None,
) -> str:
    """Explain why ``fetch_nearest_open_meteo_forecast`` returned None."""
    target_s = target_date.isoformat()

    def _query(connection: Connection) -> str:
        nearest_any = _pick_nearest_iso_timestamp(
            connection.execute(
                """
                SELECT id, fetched_at_utc
                FROM open_meteo_fetch_cycles
                WHERE station_id = %(station_id)s
                ORDER BY fetched_at_utc ASC
                """,
                {"station_id": station.icao},
            ).fetchall(),
            ts_column="fetched_at_utc",
            at_utc=at_utc,
        )
        if nearest_any is None:
            return "no Open-Meteo fetch cycle near OPEN time"

        has_target = connection.execute(
            """
            SELECT 1
            FROM open_meteo_forecast_snapshots
            WHERE fetch_cycle_id = %(cycle_id)s
              AND station_id = %(station_id)s
              AND target_date_local = %(target_date)s
            LIMIT 1
            """,
            {
                "cycle_id": nearest_any["id"],
                "station_id": station.icao,
                "target_date": target_s,
            },
        ).fetchone()
        if has_target is not None:
            return "no Open-Meteo forecast snapshot near OPEN time"

        cycle_at = str(nearest_any["fetched_at_utc"])
        return (
            f"nearest Open-Meteo cycle is {cycle_at} but it has no forecast for "
            f"settlement date {target_s}; the collector likely did not persist that "
            f"target date at OPEN time (check open_meteo target_horizon_days)"
        )

    if conn is not None:
        return _query(conn)

    url = resolve_database_url(override=database_url)
    with get_connection(url) as connection:
        return _query(connection)


@dataclass(frozen=True)
class ClobSnapshotBundle:
    poll_slot_utc: str
    rows: tuple[dict, ...]


def fetch_nearest_clob_snapshot(
    polymarket_event_id: str,
    at_utc: datetime,
    *,
    database_url: str | None = None,
    conn: Connection | None = None,
    max_age_seconds: float | None = DEFAULT_CLOB_MAX_AGE_SECONDS,
) -> ClobSnapshotBundle | None:
    """Load CLOB bucket rows for the newest poll slot at or before ``at_utc``.

    Returns ``None`` when no slot exists within ``max_age_seconds`` before
    ``at_utc`` (future-only snapshots are ignored).
    """

    def _query(connection: Connection) -> ClobSnapshotBundle | None:
        slots = connection.execute(
            """
            SELECT DISTINCT poll_slot_utc
            FROM clob_bucket_snapshots
            WHERE polymarket_event_id = %(event_id)s
            ORDER BY poll_slot_utc ASC
            """,
            {"event_id": polymarket_event_id},
        ).fetchall()
        slot_row = _pick_nearest_at_or_before(
            slots,
            ts_column="poll_slot_utc",
            at_utc=at_utc,
            max_age_seconds=max_age_seconds,
        )
        if slot_row is None:
            return None

        poll_slot = str(slot_row["poll_slot_utc"])
        rows = connection.execute(
            """
            SELECT bucket_label, yes_bid, yes_ask, spread, liquidity_usd,
                   market_id, yes_token_id, poll_slot_utc, fetched_at_utc
            FROM clob_bucket_snapshots
            WHERE polymarket_event_id = %(event_id)s
              AND poll_slot_utc = %(poll_slot)s
            ORDER BY bucket_label ASC
            """,
            {"event_id": polymarket_event_id, "poll_slot": poll_slot},
        ).fetchall()
        if not rows:
            return None
        return ClobSnapshotBundle(poll_slot_utc=poll_slot, rows=tuple(rows))

    if conn is not None:
        return _query(conn)

    url = resolve_database_url(override=database_url)
    with get_connection(url) as connection:
        return _query(connection)


def hydrate_event_from_clob_snapshot(
    event: PolymarketEvent,
    clob_rows: list[dict] | tuple[dict, ...],
) -> PolymarketEvent:
    """Overlay stored CLOB prices onto a Gamma event by bucket label."""
    by_label = {str(row["bucket_label"]): row for row in clob_rows}
    hydrated: list[PolymarketBucket] = []
    for bucket in event.buckets:
        row = by_label.get(bucket.label)
        if row is None:
            hydrated.append(bucket)
            continue
        yes_bid = row.get("yes_bid")
        yes_ask = row.get("yes_ask")
        if is_untradeable_market_price(yes_bid, yes_ask):
            hydrated.append(bucket)
            continue
        hydrated.append(
            replace(
                bucket,
                yes_bid=yes_bid,
                yes_ask=yes_ask,
                spread=row.get("spread"),
                liquidity_usd=row.get("liquidity_usd"),
            )
        )
    return replace(event, buckets=hydrated)


def overlay_open_trade_prices(
    event: PolymarketEvent,
    *,
    bucket_prices: dict[str, tuple[float | None, float | None]],
) -> PolymarketEvent:
    """Fill yes_bid/yes_ask from OPEN ledger rows for specific buckets."""
    hydrated: list[PolymarketBucket] = []
    for bucket in event.buckets:
        prices = bucket_prices.get(bucket.label)
        if prices is None:
            hydrated.append(bucket)
            continue
        yes_bid, yes_ask = prices
        hydrated.append(replace(bucket, yes_bid=yes_bid, yes_ask=yes_ask))
    return replace(event, buckets=hydrated)
