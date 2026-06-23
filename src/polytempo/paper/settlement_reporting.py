"""Settlement-date bucketing for paper performance reporting."""

from __future__ import annotations

from datetime import date, datetime
from functools import lru_cache


@lru_cache(maxsize=4096)
def _gamma_settlement_date(event_id: str) -> date | None:
    from polytempo.markets.polymarket import fetch_event

    try:
        event = fetch_event(event_id)
    except Exception:
        return None
    return event.settlement_date


def build_event_settlement_dates(event_ids: set[str]) -> dict[str, date]:
    """Map Polymarket event id → weather settlement date (Gamma endDate)."""
    out: dict[str, date] = {}
    for eid in sorted(event_ids):
        settlement = _gamma_settlement_date(eid)
        if settlement is not None:
            out[eid] = settlement
    return out


def realization_day(
    ts: datetime,
    *,
    polymarket_event_id: str | None,
    event_settlement_dates: dict[str, date],
) -> date:
    """Reporting day for a SETTLE/CLOSE row."""
    if polymarket_event_id:
        settlement = event_settlement_dates.get(polymarket_event_id)
        if settlement is not None:
            return settlement
    return ts.date()
